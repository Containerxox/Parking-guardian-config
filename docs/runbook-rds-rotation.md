# 런북: RDS 시작·비밀번호 회전 뒤 DB 인증 복구 (R6)

RDS 관리형 마스터 비밀이 회전하면 클러스터 안의 Kubernetes Secret 과 Pod 가 옛 비밀번호를 쥔 채 남는다. 이 문서는 **현재 구조를 그대로 두고** 사람이 하는 절차만 적는다.
Reloader 설치, ExternalSecret 갱신 주기 변경, RDS 로테이션 설정 변경은 하지 않았다(8.4절 해결안 B·C 는 별도 승인). 배경·비교: [phase2.md 8.4](phase2.md).

**비밀번호와 Secret 값은 어떤 명령으로도 출력하지 않는다.** 아래 명령은 모두 메타데이터·상태만 본다. 금지: `kubectl get secret -o yaml|jsonpath`, `aws secretsmanager get-secret-value`,
`kubectl exec ... env`, 접속 문자열을 찍는 코드. (오류 메시지에는 DB 호스트·사용자 이름은 나오지만 비밀번호는 나오지 않는다 [관찰: 2026-11-04 Job 로그].)

## 언제 하나
- RDS 를 중지했다가 다시 시작한 직후(이 문서의 1~7 을 한 번 훑는다)
- 회전 이벤트가 있었거나 7일 주기가 지났을 때
- backend·relay·worker 로그에 `password authentication failed` 가 보이거나 마이그레이션 Job 이 DB 인증으로 실패할 때

## 한눈에 (순서)
1. RDS `available` 확인 → 2. 회전 시각 확인 → 3. ExternalSecret 이 회전 뒤에 동기화됐나 → 4. 아니면 `force-sync` →
5. 새 연결로 DB 확인(backend·relay) → 6. 옛 비밀번호를 쥔 Pod 만 롤링 재시작(relay → backend, Worker 는 0 일 때) → 7. 마이그레이션 Job·Argo CD 확인

환경: `export AWS_PROFILE=parking-guardian AWS_REGION=ap-northeast-2` (Git Bash 에서 `/aws/...` 같은 인자를 쓸 때는 `MSYS_NO_PATHCONV=1`).

## 1. RDS 상태
```
aws rds describe-db-instances --db-instance-identifier parking-guardian \
  --query "DBInstances[0].[DBInstanceStatus,PendingModifiedValues]" --output text
```
`available` 이 아니면 여기서 멈춘다. 시작 직후에는 재시작이 한 번 더 있을 수 있다(2026-11-04: started → shutdown → restarted → 백업 순) [관찰].

## 2. 회전했는가 (값은 보지 않는다)
```
aws rds describe-events --source-type db-instance --source-identifier parking-guardian --duration 1440 \
  --query "Events[?contains(Message,'Reset master credentials')].[Date,Message]" --output text

aws secretsmanager describe-secret --secret-id <config.env 의 RDS_SECRET_ARN> \
  --query "{LastRotated:LastRotatedDate,Next:NextRotationDate,Stages:VersionIdsToStages}" --output json
```
- `--duration` 은 분 단위(최대 20160). 위 이벤트나 `LastRotated` 가 마지막 ExternalSecret 동기화(3단계)보다 **나중이면** 클러스터의 Secret 은 옛 값이다.
- `describe-secret` 은 메타데이터만 돌려준다. `AWSCURRENT` 가 붙은 버전이 현재 비밀번호다.

## 3. ExternalSecret 이 회전 뒤에 동기화됐는가
```
kubectl get externalsecret -n parking-guardian \
  -o custom-columns=NAME:.metadata.name,READY:.status.conditions[0].status,REFRESHED:.status.refreshTime
```
`REFRESHED` 가 2단계의 회전 시각보다 이르면 오래된 Secret 이다. 갱신 주기는 1시간이라 최대 1시간 동안 어긋난다(`refreshInterval: 1h`).
DB 계정을 쓰는 것은 `backend-secrets`(API, 마이그레이션 Job)와 `pipeline-secrets`(Relay, Worker)다. `grafana-admin`(monitoring)은 DB 와 무관하다.

## 4. 필요하면 force-sync
```
TS=$(date +%s)
kubectl annotate externalsecret backend-secrets pipeline-secrets -n parking-guardian force-sync=$TS --overwrite
kubectl get externalsecret -n parking-guardian -o custom-columns=NAME:.metadata.name,READY:.status.conditions[0].status,REFRESHED:.status.refreshTime
```
- 몇 초 안에 `REFRESHED` 가 바뀌고 `READY=True`(`SecretSynced`)가 된다. `.status.syncedResourceVersion` 이 바뀌면 내용이 바뀐 것이다 [관찰: 2026-11-04].
- **Secret 을 지우거나 손으로 만들지 않는다.** ExternalSecret 이 소유자(`creationPolicy: Owner`)이고 Secret 이름과 키(`DB_USER`, `DB_PASSWORD` 등)는 그대로 유지된다.
- 이것은 ESO 가 지원하는 방식(`force-sync` 어노테이션)이며 AWS 와 DB 에는 아무 변경이 없다.

## 5. 새 연결로 DB 확인 (출력은 `1` 또는 오류 이름뿐)
`kubectl exec` 로 띄운 프로세스는 **컨테이너가 시작될 때의 환경변수**를 그대로 물려받으므로, 이 검사가 곧 "그 Pod 가 지금 쥔 비밀번호로 새로 접속되는가" 다.
```
# backend (API 가 쥔 비밀번호)
kubectl exec deploy/backend -n parking-guardian -- python -c "from app import create_app; from app.extensions import db; from sqlalchemy import text; a=create_app(); a.app_context().push(); print(db.session.execute(text('select 1')).scalar())"
kubectl exec deploy/backend -n parking-guardian -- python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:5000/readyz').read().decode())"

# relay (같은 코드. HTTP 서버가 없어 readyz 는 없다)
kubectl exec deploy/relay -n parking-guardian -- python -c "from app import create_app; from app.extensions import db; from sqlalchemy import text; a=create_app(); a.app_context().push(); print(db.session.execute(text('select 1')).scalar())"
```
- `1` 이면 정상이다. `password authentication failed` 가 나오면 그 Pod 의 환경변수가 옛 값이다 → 6단계.
- **주의 [추정]**: PostgreSQL 은 비밀번호가 바뀌어도 이미 열린 연결을 끊지 않는다. 그래서 회전 직후 Pod 가 `Running`/`readyz OK` 여도 **새 연결이 필요해지는 순간**(연결 재생성, 재시작) 실패할 수 있다.
  2026-11-04 에는 backend·relay 로그에 인증 실패가 없었던 것이 이와 맞는다. `readyz` 통과를 복구의 증거로 쓰지 말고 위의 새 프로세스 접속으로 판단한다.

## 6. 안전한 롤링 재시작 (필요한 Pod 만)
환경변수는 Pod 시작 때 고정되므로 4단계 뒤에도 **떠 있는 Pod 는 옛 값**이다. 재시작 전에 노드 CPU 여유를 본다:
`kubectl describe nodes | grep -E "^Name:|^  cpu  "` — relay 는 50m, backend 의 surge Pod 는 200m 이 들어갈 노드가 있어야 한다.
Worker 가 떠 있으면 여유가 부족해 롤링이 멈출 수 있다(R7, phase2.md 8.2).

```
kubectl rollout restart deployment/relay   -n parking-guardian
kubectl rollout status  deployment/relay   -n parking-guardian --timeout=180s
#  → 5단계의 relay 검사로 확인. 실패하면 backend 로 넘어가지 않는다.
kubectl rollout restart deployment/backend -n parking-guardian
kubectl rollout status  deployment/backend -n parking-guardian --timeout=240s
#  → 5단계의 backend 검사(readyz 의 database: ok, select 1)로 확인.
```
- 순서: relay → backend. 둘 다 `maxSurge 1 / maxUnavailable 0` 이라 무중단이다(backend 는 새 Pod 가 Ready 가 된 뒤 옛 Pod 를 내린다) [관찰: 2026-11-04 약 40초].
- **Worker**: 0 이면 재시작할 것이 없다. 다음에 KEDA 가 올리는 Pod 는 새 Secret 을 받는다. 떠 있다면 큐가 빌 때까지 기다린 뒤 재시작한다
  (`maxSurge 0` 이라 먼저 내려가고, 처리 중이던 메시지는 60초 뒤 다시 온다).
- `rollout restart` 가 붙이는 `restartedAt` 어노테이션은 Argo CD 가 되돌리지 않았다(`Synced` 유지) [관찰]. 이미지 태그는 바뀌지 않는다.
- Pod 를 강제로 지우거나 replicas 를 손으로 바꾸지 않는다.

## 7. 마이그레이션 Job 과 Argo CD
`parking-guardian` 앱은 sync 때마다 `backend-migrate` Job(`Replace=true,Force=true`)을 새로 만든다. Job 이 DB 인증으로 실패하면 앱이 `OutOfSync/Degraded` 가 되고 Application `retry`
(최대 5회, 30초에서 시작해 두 배씩, 최대 5분)가 다시 시도한다.
```
kubectl get job backend-migrate -n parking-guardian
kubectl get application parking-guardian -n argocd \
  -o jsonpath='{.status.sync.status} {.status.health.status} phase={.status.operationState.phase} retry={.status.operationState.retryCount}{"\n"}{.status.operationState.message}{"\n"}'
kubectl logs job/backend-migrate -n parking-guardian --tail=15
```
- 4단계 뒤 **다음 재시도**가 성공하면 Job 이 `Complete` 가 되고 앱이 진행한다 [관찰: 2026-11-04 약 90초 뒤 성공].
- Job 로그 끝이 `Seed complete` / `migrate end` 이고 `Running upgrade` 줄이 없으면 이미 최신 스키마다(변경 없음). `flask seed` 는 이미 있는 것은 건너뛴다.
- **Job 을 손으로 지우거나 다시 만들지 않는다**(Argo CD 가 만든다). 재시도 5회가 모두 끝나 `phase` 가 `Failed` 이면 그때 수동 Sync 가 필요하다 — 승인 후에 한다.
- 마지막에 `kubectl get application -n argocd` 가 모두 `Synced Healthy`, `kubectl get deploy -n parking-guardian` 이 backend 2/2, relay 1/1, worker 0/0(또는 큐가 있으면 1)인지 본다.

## 2026-11-04 에 실제로 일어난 순서 [관찰]
| 시각(UTC) | 일 |
|---|---|
| 03:38 | RDS 시작 (11-01 12:48 UTC 부터 중지돼 있었다) |
| 04:17~04:18 | ExternalSecret 동기화(04:17:20, Secret 생성), 첫 마이그레이션 Job 성공(04:18) |
| 04:23:59 | RDS 이벤트 `Reset master credentials`, 시크릿 `LastRotatedDate` 같은 시각 |
| 04:34~04:41 | Argo 재시도 3·4회에서 새 Job 이 `password authentication failed` 로 실패(BackoffLimit) |
| 04:40:37 | `force-sync` 로 두 ExternalSecret 갱신 |
| 04:42 | 다음 재시도의 Job 성공 |
| 04:45~04:47 | relay → backend 롤링 재시작, 새 Pod 의 `select 1` 성공 |

## 아직 하지 않은 것 (별도 승인)
- Reloader 설치(Secret 변경 시 자동 롤링), `refreshInterval` 단축, 앱이 새 연결마다 비밀을 다시 읽는 구조, RDS 로테이션 설정 변경 — [phase2.md 8.4](phase2.md) 의 B·C.
- 회전이 일어난 정확한 원인(예정 시각 도래인지 중지 기간의 영향인지)은 확인하지 못했다.
