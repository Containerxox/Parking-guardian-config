# 관측성 1단계: Alertmanager + Slack + Phase 2 대시보드 (2026-11-06)

이 문서는 **비밀 값을 담지 않는다**(Slack Webhook URL 은 사용자가 터미널에서 직접 Secrets Manager 에 넣는다). 구현 계획과 근거는 로컬 문서(capston/docs/OBSERVABILITY_PHASE2_PLAN_LOCAL.md).

## 1. 무엇이 바뀌는가
| 구분 | 변경 | 파일 |
|---|---|---|
| Alertmanager 켜기 | `alertmanager.enabled: true`, Slack 수신자·라우팅·억제 규칙, 비밀 마운트 | `platform/monitoring/values.yaml` |
| 비밀 | ExternalSecret `slack-webhook` (Secrets Manager `parking-guardian/slack` 키 `url` → Secret `monitoring/slack-webhook`) | `platform/monitoring/config/slack-webhook-externalsecret.yaml` |
| 알림 규칙 | PrometheusRule `pg-platform-alerts`(Worker·5xx·재시작·Pending 5개) + `pg-alert-pipeline-test`(시험용) | `platform/monitoring/config/rules/` |
| 대시보드 | 신규 2개(Service Overview, Infrastructure — 폴더 `Parking Guardian (Phase 2)`). Phase 1 대시보드 3개는 **삭제하지 않고** 폴더만 `Parking Guardian (Phase 1 참고)` 로 구분 | `platform/monitoring/config/dashboards/`, `kustomization.yaml` |
| 단위 시험 | promtool 시험과 CR → 일반 규칙 변환기 | `platform/monitoring/config/rules/tests/` |
- 새 Pod: `alertmanager-monitoring-alertmanager-0` 1개(컨테이너 alertmanager + config-reloader). 자원: alertmanager 요청 CPU 10m·메모리 64Mi/상한 메모리 128Mi, config-reloader CPU 요청 5m(Operator 기본은 0 이라 `containers` 로 덮어씀). CPU limit 없음. 이유: 요청 0 이면 노드가 포화일 때 알림 전송이 가장 늦어질 수 있다. 노드 CPU 남은 요청 여유 190m·1275m·90m 모두에 합계 15m 가 들어간다. 새 PVC 없음(침묵 설정은 재시작에 사라져도 되는 emptyDir). 새 Service `monitoring-alertmanager`(9093), ServiceMonitor `monitoring-alertmanager`(Prometheus 가 Alertmanager 자체도 수집). Prometheus CR 이 Alertmanager 를 목적지로 자동 등록된다(Prometheus Pod 재시작 없음, 설정 리로드).
- 앱 코드·운영 Pod(backend·worker·relay) 변경 없음.

## 2. 알림 규칙(운영 정의)
| 알림 | 조건 | for | severity | 복구 |
|---|---|---|---|---|
| PGWorkerNotReady | worker Ready < 목표(spec) | 5m | warning | Ready >= 목표 |
| PGWorkerDown | worker Ready = 0 이고 목표 > 0 | 2m | critical | Ready >= 1 |
| PGHttp5xxRateHigh | 5xx 비율(5분 평균, 헬스체크 제외) > 5% **그리고** 요청률 > 0.2 req/s | 5m | critical | 비율 <= 5% |
| PGPodCrashLooping | 컨테이너 15분 재시작 증가량 > 2 | 1m | warning | 창이 지나 <= 2 |
| PGPodPending | Pending Pod 존재 | 10m | warning | Pending 해소 |
| PGAlertPipelineTest | 스위치 ConfigMap `pg-alert-test-switch` 존재 | 0m | info(test=true) | ConfigMap 삭제 |
- Slack 색상: Alertmanager 기본(Firing=빨강 danger, Resolved=초록 good)이라 warning 도 빨강으로 보인다. 심각도별 색은 필요하면 나중에 `color` 템플릿으로 조정한다(1단계 범위 밖).
- 라우팅: `group_by [alertname, severity]`, `group_wait 30s`, `group_interval 5m`, critical 은 1시간·warning 은 4시간마다 반복, `send_resolved`, 같은 alertname 의 critical 이 warning 을 억제. `test=true` 는 10초 안에 전송.

## 3. 배포 순서 (각 단계는 사용자 승인 뒤 실행 — 지금까지는 로컬 구현·검증만 했다)
Alertmanager Pod 는 Secret `slack-webhook` 이 있어야 시작한다(마운트). **비밀 값을 먼저 넣고 Git 에 push 한다.**
1. (사용자) Slack 에서 Incoming Webhook 을 만들고 URL 을 복사한다. 채팅에 붙여넣지 않는다.
2. (Terraform, AWS 변경) infra 리포 — data 스택: 빈 비밀 `parking-guardian/slack` 1개 추가(+1). `terraform output secret_arns` 로 ARN 확인 → platform 스택 `terraform.tfvars` 에 `slack_secret_arn = "<ARN>"` 추가 → platform 스택 apply(ESO IAM 정책에 ARN 1개 추가, 역할 설명 변경: 2 in-place, 재생성 없음). 각 apply 전에 plan 을 보고 승인한다.
3. (사용자) URL 을 비밀 파일을 거쳐 한 번 넣는다(명령줄·셸 이력에 URL 이 남지 않게 마스킹 입력 + 소유자 전용 임시 파일):
   ```bash
   export AWS_PROFILE=parking-guardian
   read -rsp "Slack webhook URL: " SLACK_URL; echo
   umask 077; F=$(mktemp)
   printf '{"url":"%s"}' "$SLACK_URL" > "$F"; unset SLACK_URL
   aws secretsmanager put-secret-value --secret-id parking-guardian/slack --secret-string "file://$F" --query VersionId --output text
   rm -f "$F"
   ```
4. (사용자) config 리포 커밋·push → Argo 가 `monitoring`(Helm: Alertmanager)과 `monitoring-config`(ExternalSecret·규칙·대시보드)를 동기화한다.
5. 확인(읽기 전용):
   ```bash
   kubectl -n monitoring get externalsecret slack-webhook            # READY=True, STATUS=SecretSynced
   kubectl -n monitoring get pods | grep alertmanager                 # alertmanager-monitoring-alertmanager-0  2/2 Running
   kubectl -n monitoring get prometheusrule                           # pg-platform-alerts, pg-alert-pipeline-test
   ```
   Secret 이 아직 없으면(3 이전 push) `kubectl -n monitoring annotate externalsecret slack-webhook force-sync=$(date +%s) --overwrite` 로 다시 읽게 한다.

## 4. Slack 수신 시험 (운영 Pod·데이터·큐를 건드리지 않는다)
**하지 않는 것**: Worker·Backend 종료, DLQ 에 실패 메시지 주입, 큐 적체 유발.
- **A. Alertmanager 직접 주입 (전달 경로: 라우팅·템플릿·비밀 마운트·Slack)**
  ```bash
  kubectl -n monitoring port-forward svc/monitoring-alertmanager 19093:9093 &
  END=$(date -u -d "+2 minutes" +%Y-%m-%dT%H:%M:%SZ)
  curl -s -XPOST http://127.0.0.1:19093/api/v2/alerts -H 'Content-Type: application/json' -d "[{\"labels\":{\"alertname\":\"PGSlackTest\",\"severity\":\"info\",\"test\":\"true\"},\"annotations\":{\"summary\":\"직접 주입한 시험 알림\",\"description\":\"장애가 아니다\"},\"endsAt\":\"$END\"}]"
  ```
  기대: 10~40초 안에 Slack `[FIRING:1] PGSlackTest (info)`, 2분 뒤 `[RESOLVED] PGSlackTest`. (종료: `kill %1`)
- **B. 시험 규칙 + 스위치 ConfigMap (Prometheus 규칙 평가까지 종단)**
  ```bash
  kubectl -n monitoring create configmap pg-alert-test-switch      # -> 약 1분 안에 [FIRING] PGAlertPipelineTest
  kubectl -n monitoring delete configmap pg-alert-test-switch      # -> 약 2분 안에 [RESOLVED] PGAlertPipelineTest
  ```
  확인: Prometheus UI `Alerts`(port-forward svc/monitoring-prometheus 9090) 또는 `kubectl -n monitoring port-forward svc/monitoring-alertmanager 19093:9093` 후 `curl -s localhost:19093/api/v2/alerts`.
- 합격 기준: Firing·Resolved 두 메시지를 Slack 에서 모두 받는다. 반복 전송이 `repeat_interval`(test 1시간) 전에 오지 않는다. 시험 뒤 ConfigMap 이 남아 있지 않다.
- 실제 알림 규칙(Worker·5xx·재시작·Pending)은 값 조건을 인위로 만들지 않는다. 대신 `promtool test rules` 단위 시험(아래)과 운영 중 실제 이벤트로 확인한다.

## 5. 커밋 전 로컬 검증 (규칙 문법 오류는 적용 때 잡히지 않는다 — 승인 웹훅이 꺼져 있다)
공식 컨테이너 이미지(클러스터가 쓰는 것과 같은 버전)와 읽기 전용 마운트로 실행한다. Docker 가 필요하고 비밀은 쓰지 않는다.
```bash
export MSYS_NO_PATHCONV=1                                          # Git Bash 경로 변환 방지
cd platform/monitoring/config/rules
# <절대경로> 폴더에 build/ 와 tests/ 를 만든다(rules/tests/pg-alerts.test.yaml 을 tests/ 로 복사):
python tests/extract_rules.py <절대경로>/build/pg-rules.yaml
docker run --rm --entrypoint /bin/promtool -v <절대경로>:/w:ro quay.io/prometheus/prometheus:v3.15.1 check rules /w/build/pg-rules.yaml
docker run --rm --entrypoint /bin/promtool -v <절대경로>:/w:ro -w /w/tests quay.io/prometheus/prometheus:v3.15.1 test rules pg-alerts.test.yaml
# Alertmanager: Helm 렌더 결과의 Secret alertmanager-monitoring-alertmanager 에서 alertmanager.yaml 을 꺼내
docker run --rm --entrypoint /bin/amtool -v <절대경로>/alertmanager.yml:/etc/alertmanager/alertmanager.yml:ro quay.io/prometheus/alertmanager:v0.34.1 check-config /etc/alertmanager/alertmanager.yml
```
2026-11-06 실행 결과: `promtool check rules` SUCCESS(6 rules), `promtool test rules` SUCCESS(틀린 기대값을 넣은 음성 대조는 FAILED 로 잡힘), `amtool check-config` SUCCESS.
추가로 **실제 Alertmanager v0.34.1 컨테이너 + 로컬 모의 수신 서버**(Slack·비밀 없음)로 확인했다: test=true 알림 10초 만에 Firing, 종료 뒤 약 1분에 Resolved(`send_resolved`), 같은 alertname 의 critical 이 warning 을 억제(warning 은 suppressed, 본문 미전송), 다른 alertname 의 warning 은 전달, 제목·본문 템플릿 정상 렌더.
`api_url_file` 동작: 파일이 **없으면** amtool check-config 는 통과하지만(경로 존재는 설정 로드 때 검사하지 않는다) 알림 전송 때 `open …/url: no such file or directory` 오류만 로그에 남고 Alertmanager 는 계속 동작한다. 파일이 **생기면** 재시작 없이 다음 재시도(group_interval)에 이전 실패 알림까지 전달된다(생성 후 43초 만에 확인). 실제 클러스터에서는 Secret 볼륨이 없으면 Pod 가 ContainerCreating 에서 기다린다 — 그래서 비밀 값을 먼저 넣고 push 한다(3절).

## 6. 되돌리기
- Slack 알림만 끄기: `values.yaml` 의 `alertmanager.enabled: false` 로 되돌려 push(규칙은 남아도 보낼 곳이 없다).
- 전부 되돌리기: 이 변경의 커밋을 revert. Terraform 은 비밀 1개(7일 복구 유예)와 ESO 정책 ARN 1개만 되돌리면 된다. 웹훅이 의심되면 Slack 에서 웹훅을 재발급하고 3단계를 다시 한다.

## 7. 비용
Secrets Manager 비밀 1개 $0.40/월(+ESO 1시간 갱신 호출은 무시 가능). Alertmanager Pod 는 기존 노드 안(메모리 64Mi 요청). Slack 무료. 노드 증설 없음.

## 8. Slack Firing/Resolved 시험 결과 (2026-11-06 03:05–03:10 KST, 배포 후)
시험은 Alertmanager 지표(`alertmanager_notifications_total{integration="slack"}`)와 Prometheus 규칙 상태 API 로 관찰했다. **Slack 메시지의 실제 수신은 사용자가 직접 확인**한다(아래 "사용자 확인" 칸). 웹훅 URL 은 읽거나 출력하지 않았다. 운영 Pod·큐·데이터 변경 없음.
| 항목 | 방법 A (Alertmanager API 직접 주입) | 방법 B (시험 규칙 + 스위치 ConfigMap) |
|---|---|---|
| 시작 | 03:05:35 POST `PGSlackTest`(test=true, severity=info, endsAt +2분) → HTTP 200 | 03:08:41 `kubectl create configmap pg-alert-test-switch` |
| Prometheus | (해당 없음) | `PGAlertPipelineTest` `for: 0m` 이라 Pending 없이 **+33초(03:09:14) Firing**, Alertmanager 에 즉시 active |
| **Firing 전송** | **+13초(03:05:47)** — Slack 전송 카운터 0 → 1 | **+43초(03:09:24)** (규칙 평가·수집 주기 33초 + 그룹 대기 10초) — 2 → 3 |
| 해소 | endsAt(03:07:35) 도달 | 03:09:34 `kubectl delete configmap`; Prometheus 규칙 `inactive`, Alertmanager 알림 0 |
| **Resolved 전송** | **+131초(03:07:45)** (endsAt 후 10초, group_interval 1분 안의 다음 flush) — 1 → 2 | **삭제 후 +52초(03:10:26)** — 3 → 4 |
| 전송 실패 | 0 | 0 |
| 중복 전송 | 없음(Firing 1 + Resolved 1) | 없음(Firing 1 + Resolved 1) |
| 사용자 확인(Slack 수신) | (사용자 확인 대기) | (사용자 확인 대기) |
- 종료 상태: 활성 알림 0개, 시험 ConfigMap 삭제 확인(`NotFound`), Prometheus 규칙 6개 모두 `inactive`/`ok`, Alertmanager 오류·경고 로그 0건. backend 2/2, worker 2/2, relay 1/1, frontend 1/1, 앱 Pod 재시작 0, SQS/DLQ 0/0.
- 전달 지연 정리: 규칙 평가 방식(B)은 Firing 약 43초, Resolved 약 52초(삭제 후). 직접 주입(A)은 Firing 13초. 실제 장애 알림(for 5m 등)은 여기에 각 규칙의 `for` 기간이 더해진다.
- 설정 확인: `test=true` 경로(그룹 대기 10초·간격 1분)가 의도대로 동작, `send_resolved` 동작, 템플릿 렌더링(제목 `[FIRING:1] …` / `[RESOLVED] …`)은 로컬 컨테이너 시험에서 확인(Slack 화면은 사용자 확인).
