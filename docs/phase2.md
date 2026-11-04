# Phase 2 배포 구성 (Worker · Relay · KEDA)

Phase 2 는 접수와 AI 판별을 큐로 떼어 낸다(앱 리포 ADR 0019). 이 문서는 그 배포 구성을 정리한다. 2026-11-03 기준으로 로컬 렌더링과 계약 검증까지 했고,
**2026-11-04 에 AWS(EKS)에 처음 배포해 정상 경로 E2E 1건을 확인했다** → [docs/e2e-aws-first-run-2026-11-04.md](e2e-aws-first-run-2026-11-04.md)
(단일 실행이라 일반적인 성능으로 읽지 않는다). 같은 날 차량 사진 2장으로 **위반 2회 확정·관리자 알림까지** 확인했다 → [e2e-aws-violation-confirm-2026-11-04.md](e2e-aws-violation-confirm-2026-11-04.md).
운영 절차: [RDS 비밀번호 회전 런북](runbook-rds-rotation.md), [테스트 장비 사용 절차](test-device-procedure.md).
후속 분석·계획: [남은 운영 위험 R3·R6·R7 + Fluent Bit 경고](ops-risks-2026-11-04.md), [DNS·관리자 UI 연결 계획](dns-admin-ui-plan.md), [Phase 1↔2 부하 테스트 계획](loadtest-plan-phase1-vs-phase2.md).
계정 ID·도메인 같은 식별자는 이 문서에 쓰지 않는다(`config.env` 에만 있다).

검증: `python scripts/check-phase2-render.py` (Windows 콘솔은 `PYTHONUTF8=1`). 배포 직전에는 `--strict` 로 아래 2절이 모두 끝났는지 본다.

## 1. Phase 1 과 달라지는 것

| 구성 요소 | Phase 1 | Phase 2 추가·변경 |
|---|---|---|
| 접수 | `POST /device/detections` (사진 → API → inference HTTP → 판정, 동기) | `POST /device/detection-jobs` (사진 → S3 inbox, DB 작업 행, SQS 발행, 202). **두 경로가 같은 API Pod 에서 공존한다** |
| AI | `inference` 서비스(HTTP, HPA 2~3) | `worker`: 큐를 소비하고 **프로세스 안에서** YOLO 를 돈다(`flask judge-worker`) |
| 복구 | 없음(동기라 호출자가 재시도) | `relay`: 재발행, DLQ, 부수 효과 재시도, 보존 정리, **멈춘 작업 복구**(`flask outbox-relay`) |
| 확장 | api HPA(CPU), inference HPA(CPU) | + worker 는 KEDA(SQS 대기 수). api·inference HPA 는 그대로 |
| 이미지 | backend(api), frontend, inference | + **worker (새 이미지, 아직 없음)**. relay 는 backend 이미지 재사용 |
| 신원(Pod Identity) | backend, inference | + worker, relay (Terraform `iam_phase2.tf`). KEDA operator 는 **아직 없음** |
| Secret | `backend-secrets` | + `pipeline-secrets` (DB 계정만. Worker·Relay 에 JWT·시드 비밀을 주지 않는다) |

기존 리소스는 그대로 두고 더했다. 바뀐 기존 파일은 둘뿐이다: `backend-deployment.yaml` 의 `envFrom` 에 `queue-config` 한 줄(QUEUE_BACKEND=sqs),
`kustomization.yaml` (리소스·생성기·치환 추가). Ingress·Service·HPA·PDB·마이그레이션 Job 은 손대지 않았다.

### 이름 대응 (Terraform ↔ 매니페스트) — 틀리면 AWS 자격 증명이 안 나온다

| 워크로드 | Namespace / ServiceAccount (매니페스트) | Terraform 변수 기본값 | IAM 역할 | 하는 일 |
|---|---|---|---|---|
| api | parking-guardian / `backend` | `backend_service_account` | `parking-guardian-backend` | SQS SendMessage, S3 inbox 쓰기·읽기, violations, ListBucket |
| worker | parking-guardian / `worker` | `worker_service_account` | `parking-guardian-worker` | SQS Receive·Delete·ChangeVisibility(본 큐), inbox 읽기, violations 쓰기, 모델 읽기 |
| relay | parking-guardian / `relay` | `relay_service_account` | `parking-guardian-relay` | SQS Send(본 큐), DLQ Receive·Delete·ChangeVisibility, inbox 읽기, violations 쓰기 |
| keda operator | keda / `keda-operator` | **없음** | **없음** | 본 큐 `sqs:GetQueueAttributes` |

앱 코드가 실제로 부르는 SQS·S3 API 와 위 권한이 일치함은 infra 작업 때 코드(`queue.py`, `storage.py`, `services/*`)로 확인했다. 검증 스크립트는
이름(ServiceAccount·Namespace)을 Terraform 변수 기본값과 대조한다.

## 2. 배포 전에 아직 없는 것 (이것들이 끝나기 전에는 배포하지 않는다)

| # | 무엇 | 어디서 | 안 하면 |
|---|---|---|---|
| P1 | ✅ **코드 완료, apply 전.** ECR 리포지토리 `parking-guardian/worker` | infra `data/storage_ecr.tf` 의 `ecr_repositories` 에 `"worker"` 추가(2026-11-03). plan: 추가 2(리포지토리 + Lifecycle 정책), 변경 1(CI 역할 정책에 worker ARN), 삭제 0. CI 역할의 push 권한은 모든 리포지토리를 대상으로 해서 따로 고칠 것이 없고, 노드 Pull 권한도 계정 전체다 | Worker 가 ImagePullBackOff (다른 워크로드는 영향 없음) |
| P2 | ✅ **코드 완료, push 전.** CI 가 worker 이미지를 빌드·푸시하고 태그를 쓴다 | 앱 리포 `build-push.yml`: matrix 에 `component: worker, context: ./backend, target: prototype, yolov5: true`, 태그 갱신 목록 3개 → 4개, 커밋 메시지. 테스트 17개(태그 갱신 코드를 실제 `kustomization.yaml` 로 실행해 4줄이 모두 바뀌는지 확인). **이 파일은 P1 apply 뒤에 push 한다**: ECR 리포지토리가 없으면 worker 잡이 실패하고 `update-config` 가 모든 빌드를 기다리므로 **네 이미지 모두의 태그 갱신이 멈춘다** | `images:` 의 worker 태그가 `"unset"` 로 남는다 |
| P3 | ✅ **코드 완료, apply 전.** KEDA operator 의 IAM 역할 + Pod Identity 연결 | infra cluster 스택 `iam_keda.tf` (본 큐 `sqs:GetQueueAttributes` 하나, 연결 `keda`/`keda-operator`). plan: 추가 43(기존 40 + KEDA 3), 변경·삭제 0 | ScaledObject 가 Ready 가 안 되고, min 0 이면 Worker 가 0 에 머물러 **큐가 쌓인다**(fallback 이 구하지 못한다) |
| P4 | ✅ **로컬 구현·검증 완료, 이미지 반영 전.** 앱: `judge-worker`·`outbox-relay` 의 SIGTERM 처리 | 앱 리포 `cli.py`·`worker.py`·`relay.py`(ADR 0019 "P4·P5"). 새 이미지가 CI 로 올라가야 클러스터에서 효과가 있다 | 종료마다 유예 시간을 전부 기다린 뒤 SIGKILL (데이터는 안전, 느릴 뿐) |
| P5 | ✅ **로컬 구현·검증 완료, 이미지 반영 전.** 앱: `judge-worker` 가 첫 receive 전에 모델을 읽고 한 번 돌려 본다 | 앱 리포 `YoloInference.preload()` + `JudgementWorker.serve()` | 첫 메시지가 모델 로딩 시간(13~34초)을 lease 55초 안에서 쓴다 |
| P6 | `config.env` 재생성 | `python scripts/export-config-env.py` — **이미 한 번 했다**(SQS_QUEUE_URL, SQS_DLQ_URL, ECR_WORKER 추가). ECR_WORKER 는 P1 전에는 이름 규칙으로 계산한 값이다 | — |

배포 직전 확인: `python scripts/check-phase2-render.py --strict` 가 통과해야 한다(P1~P5 가 끝난 뒤).

### P3 제안: infra Terraform (cluster 스택, 별도 승인 후 작성)

```hcl
data "aws_iam_policy_document" "keda_operator" {
  statement {
    sid       = "QueueDepth"
    effect    = "Allow"
    actions   = ["sqs:GetQueueAttributes"]   # 큐 URL 을 직접 주므로 GetQueueUrl 은 필요 없다
    resources = [var.judgement_queue_arn]
  }
}
resource "aws_iam_role" "keda_operator" {
  name               = "${var.project_name}-keda-operator"
  assume_role_policy = data.aws_iam_policy_document.pod_identity_trust.json   # 기존 공용 신뢰 정책
}
resource "aws_iam_role_policy" "keda_operator" {
  name   = "sqs-queue-depth"
  role   = aws_iam_role.keda_operator.name
  policy = data.aws_iam_policy_document.keda_operator.json
}
resource "aws_eks_pod_identity_association" "keda_operator" {
  cluster_name    = aws_eks_cluster.this.name
  namespace       = "keda"
  service_account = "keda-operator"
  role_arn        = aws_iam_role.keda_operator.arn
  depends_on      = [aws_eks_addon.pod_identity_agent]
}
```

연결은 **KEDA operator Pod 가 만들어지기 전에** 있어야 한다(Pod Identity 는 Pod 생성 때 자격 증명을 주입한다). cluster 스택 apply 가 Argo CD 의 keda 앱보다 먼저이므로
정상 재구축 순서에서는 자연히 지켜진다. 먼저 KEDA 를 띄웠다면 operator 를 재시작한다(`kubectl -n keda rollout restart deploy keda-operator`).

## 3. 리소스와 비용

| 워크로드 | Replica | CPU 요청 | 메모리 요청 / limit | 비고 |
|---|---|---|---|---|
| api (backend) | 2~3 (HPA) | 200m | 256Mi / 512Mi | Phase 1 그대로 |
| inference | 2~3 (HPA) | 1000m | 1.5Gi / 2Gi | Phase 1 그대로 |
| frontend | 1 | 50m | 64Mi | |
| **worker** | **0~1 (KEDA)** | **1000m** (+init 100m) | **1.5Gi / 2Gi** | 모델 2개를 프로세스 안에서. 값은 inference 와 같은 시작값 [추정], E0 로 조정 |
| **relay** | **1** | **50m** | **128Mi / 256Mi** | 설계 6.7 |
| **KEDA** (Pod 3개) | 1+1+1 | 50m+50m+25m = **125m** | 292Mi 합 | 차트 기본(300m/300Mi)에서 줄였다 |

노드 3대(`m7i-flex.large`), 노드당 앱 가용 **1280m / 6733Mi** [상수. 재구축 뒤 `kubectl describe nodes` 로 다시 잰다]:

| 구성 | CPU 요청 합 / 3840m | 결과 |
|---|---|---|
| Phase 1 (api 2 + inference 2 + frontend) | 2450m (64%) | 여유 있음 |
| **공존, Worker 0 (유휴)** | 2625m (68%) | 마이그레이션 Job·inference 롤링 가능 |
| **공존, Worker 1 (처리 중)** | 3625m (94%) | 들어가지만 여유 55m. **마이그레이션 Job 과 inference 롤링 surge 는 못 들어간다** |
| 공존, Worker 2 | 4625m | 불가능 (두 번째 Worker 는 Pending) |
| 비교 구성 (inference 없음: api 2 + Worker 2) | 2625m | 가능. Worker 3 도 가능(55m), 단 Worker 3 + 배포 Job 은 불가 |

계산은 `scripts/check-phase2-render.py` 의 "노드 배치 계산"(요청 기준 best-case, 12 시나리오). 실제 배치는 kube-scheduler 가 정하므로 배포 뒤 `kubectl get pods -o wide` 로 확인한다.

**추가 비용 (단가는 확인하지 않은 추정):**

| 항목 | 예상 |
|---|---|
| 공존 구성(노드 3대 그대로) | 컴퓨트 추가 **0원**. EKS 는 시간당 과금이라 켜 둔 시간만큼은 Phase 1 과 같다 |
| 노드를 1대 늘리면(4대) | `m7i-flex.large` 1대분 약 +$0.10/h |
| SQS 요청 | 월 $1 미만. Worker 긴 폴링, KEDA 15초 폴링(월 약 17만 건), Relay 의 DLQ 짧은 폴링(2초마다, 월 약 130만 건) |
| ECR worker 이미지 | 압축 약 660 MB × 10개 보관 ≈ 월 $0.7 |
| 데이터 전송 | 모델(412 MB)은 S3 게이트웨이 엔드포인트라 NAT 를 거치지 않는다. SQS API 는 NAT 를 거치지만 용량이 작다 |

### 성능 비교(Phase 1 ↔ 2)에 미치는 영향

- Phase 2 에는 Relay(50m)와 KEDA(125m)가 더해진다. 합 175m 는 노드 CPU 의 약 4.5% 라 측정 오차 범위일 수 있으나 같은 조건이라고 하지 않는다.
- Worker 와 inference 는 같은 모델·같은 요청값이다. 차이는 "HTTP 한 번 + 큐 한 번" 의 구조다. 이것이 비교하려는 대상이다.
- 공존 구성에서는 Worker 1개만 들어가서 **동일 자원(비교 A: AI 2개)** 이 되지 않는다. 비교 A 는 4절의 비교 구성에서 한다.
- Worker 콜드 스타트(이미지 Pull + 모델 다운로드 + 로딩, 1~3분 [추정])는 min 0 일 때 첫 사진 지연에 들어간다. 비교 측정은 min 1 로 한다.
- KEDA 의 반응 시간 = 폴링 15초 + HPA 동기화 15초 + Worker 시작(1~3분). 큐 대기 수 기반 확장이 CPU 기반 HPA 보다 늦게 반응할 수 있다. 이것도 측정 항목이다.

## 4. 구성 모드와 KEDA 정책

| | 공존 구성 (기본, 커밋된 상태) | 비교 구성 (부하 테스트용) |
|---|---|---|
| inference | 2~3 (HPA) | **없음** (`comparison-mode.example.yaml` 이 지운다) |
| Worker (KEDA) | min **0** / max **1** | min **1** / max **3** |
| 목적 | Phase 2 기능 검증, Phase 1 과 공존 확인, 최소 비용 | 설계 비교 A/B, 처리량·확장·복구 측정 |
| 제약 | Worker 가 떠 있으면 배포(마이그레이션 Job, inference 롤링)가 막힌다 → **배포는 큐가 빈 뒤에** | Phase 1 동기 경로는 503. Worker 3 + 배포는 불가 → 배포 전 2 이하로 |

**전환**: `apps/parking-guardian/comparison-mode.example.yaml` 의 `patches:` 블록을 `kustomization.yaml` 끝에 붙이고 `check-phase2-render.py` 로 확인한 뒤 커밋한다
(손으로 `kubectl scale` 을 하면 Argo CD selfHeal 이 되돌린다). 되돌리려면 붙인 블록을 지운다. 이 패치는 검증 스크립트가 매번 임시 복사본에 붙여 렌더링한다.

**KEDA 설정값과 근거** (`worker-scaledobject.yaml`):

| 값 | 설정 | 이유 |
|---|---|---|
| 트리거 | `aws-sqs-queue`, 본 큐 URL(치환), `queueLength: "5"`, `scaleOnInFlight: true` | 판정 1건 중앙값 약 0.7초 → Worker 1개당 대기 5건 ≈ 4초 지연. 처리 중 메시지도 세어 처리 도중 0 으로 줄지 않는다. E0 처리량으로 다시 정한다 |
| `pollingInterval` | 15초 | SQS 요청 월 약 17만 건 |
| `cooldownPeriod` | 300초 | 마지막 활동 뒤 0 으로 줄이기까지(min 0 일 때). 모델 재다운로드 비용이 커서 |
| 확장 | 1개/60초, 안정화 0 | |
| 축소 | 1개/120초, 안정화 300초 | 천천히 줄인다 |
| `fallback` | 3번 실패 뒤 1개 | **이미 떠 있는** Worker 를 0 으로 줄이지 않는다. 0개일 때는 0→1 로 못 올린다(아래 "fallback 의 한계") |
| 인증 | `TriggerAuthentication` `podIdentity.provider: aws`, `roleArn` 없음 | KEDA operator 자신의 Pod Identity 를 쓴다(아래) |

**0개일 때 다시 확장되는가**: 된다. min 0 이면 큐에 메시지가 하나라도 있을 때(`activationQueueLength` 기본 0) KEDA 가 직접 0→1 로 올리고, 그 뒤 HPA 가 1→N 을 맡는다.
처리 중인 메시지(NotVisible)도 세므로 처리 도중에 0 으로 내려가지 않는다. 단 **로컬에서 실제로 돌려 보지는 못했다**(클러스터가 없다). 첫 배포 뒤 확인 항목이다.

**호환성 (2026-11-03 확인)**: EKS 1.35. KEDA 2.21 의 시험된 Kubernetes 는 v1.34~v1.36(2.20 은 v1.33~v1.35), 차트 `kubeVersion` 제약은 `>=1.23`. 그래서 차트 2.21.0 을 쓴다.
Pod Identity: KEDA 소스(`aws_config_cache.go`)에서 `podIdentity.provider: aws` 이고 `roleArn` 이 비면 기본 자격 증명 체인(SDK `LoadDefaultConfig`)을 유지한다. EKS Pod Identity 는
operator Pod 에 `AWS_CONTAINER_CREDENTIALS_FULL_URI` 를 주입하고 Go SDK 의 기본 체인이 읽는다. AWS 규범 가이드의 방식(operator 에 Pod Identity 연결)과 같다.
`aws-eks` 제공자는 deprecated(KEDA v3 에서 제거)라 쓰지 않았다. **실제 EKS 에서 동작은 확인하지 못했다.**

**fallback 의 한계 (KEDA 소스 확인, 2026-11-03)**: `fallback` 은 aws-sqs-queue 같은 AverageValue 트리거에서 지원된다. 그러나 `RequestScale`(scale_scaledobjects.go)은
오류 + 비활성 상태에서 fallback 이 있으면 **로그만 남기고 스케일하지 않는다**. 그래서 Worker 가 이미 떠 있으면 0 으로 줄지 않고 fallback 개수를 유지하지만,
Worker 가 0개일 때 KEDA 가 큐를 못 읽으면(IAM 누락, 네트워크) 0 에서 올라오지 못한다. 공식 문서는 fallback 과 scale-to-zero 의 상호작용을 설명하지 않는다.
min 0 구성의 **감지 방법**: 배포 뒤 `kubectl -n parking-guardian get scaledobject worker` 의 READY=True·ACTIVE 를 확인하고, SQS `ApproximateAgeOfOldestMessage`
경보(위험 R13)를 둔다. KEDA 인증이 확인되기 전에는 min 1 로 시작하는 것도 방법이다(공존 구성의 배포 제약은 R7).

**HPA 와의 충돌**: 없다. backend HPA 는 `backend`, inference HPA 는 `inference`, KEDA 는 `worker` 만 조절한다(KEDA 가 만드는 HPA 는 `keda-hpa-worker`). worker Deployment 의
`replicas` 는 **처음 만들 때의 0 하나뿐**이고(첫 배포 교착 방지, 8.8) Application 의 `ignoreDifferences`(`/spec/replicas`) + `RespectIgnoreDifferences=true` 가 있어
Argo CD selfHeal 이 KEDA 의 결정을 되돌리지 않는다 — 2026-11-04 Worker 1개가 5분 떠 있는 동안 `Synced` 유지로 확인했다([e2e-aws-violation-confirm-2026-11-04.md](e2e-aws-violation-confirm-2026-11-04.md)).
검증 스크립트가 대상 중복, `replicas` 값(0 만 허용)과 이 짝을 검사한다.

**FIFO 병렬성의 한계 (중요)**: 큐는 FIFO 이고 MessageGroupId 가 장비다. 같은 장비의 메시지는 앞 것이 끝나야 다음이 나온다. 그래서 Worker 를 늘려도 **한 장비의 처리는 한 번에 하나**다.
확장이 도움이 되는 경우는 여러 장비가 동시에 쌓일 때뿐이고, Worker 수의 실효 상한은 활성 장비 수다. KEDA 의 지표(전체 메시지 수)는 이 그룹 제약을 모른다:
한 장비에 메시지 20개가 쌓이면 Worker 4개를 요구하지만 일하는 것은 1개다. max 를 장비 수 이하로 둔다(지금 장비 3대).

**S5 "Worker 0개" 실험**: min 이 1 이상이면 손으로 0 으로 내려도 KEDA 가 되돌린다. Argo CD 자동 동기화를 멈춘 뒤
`kubectl -n parking-guardian annotate scaledobject worker autoscaling.keda.sh/paused-replicas="0"` (해제는 같은 키 뒤에 `-`).

## 5. Argo CD 구조와 순서

기존 root → platform / parking-guardian / monitoring / monitoring-config 를 그대로 두고 앱 **keda** 하나를 더했다(`argocd/apps/keda.yaml`, monitoring 과 같은 Helm 다중 소스).

| 순서 | 무엇 | 비고 |
|---|---|---|
| (infra) | cluster 스택: Pod Identity 연결(backend, inference, **worker, relay, keda-operator**) | KEDA 연결은 P3 |
| Argo | platform (ClusterSecretStore) · monitoring · **keda** (Namespace keda, CRD, operator) | keda 는 `CreateNamespace`, `ServerSideApply` (CRD 가 크다) |
| parking-guardian wave 0 | Namespace, ServiceAccount 4개, ExternalSecret 2개, ConfigMap | |
| wave 1 | `backend-migrate` Job (새 스키마) | |
| wave 2 | backend, inference, frontend | |
| wave 3 | HPA (backend, inference) | worker 보다 **앞**: worker 가 Healthy 가 아니어도 Phase 1 HPA 가 적용된다 |
| wave 4 | **worker, relay** | 마이그레이션(1) 뒤. worker 는 **`replicas: 0` 으로 만들어진다**(첫 배포 교착 방지, 8.8) |
| wave 5 | **ScaledObject, TriggerAuthentication** | CRD 가 아직 없으면 `SkipDryRunOnMissingResource` + Application `retry`(PodMonitor 와 같은 방식). **대상(worker)보다 앞 wave 로 옮기면 안 된다**(8.8) |

**삭제 순서**: root → **parking-guardian → keda** → monitoring … → platform. ScaledObject 에 KEDA 의 finalizer 가 있어 KEDA 를 먼저 지우면 ScaledObject 가 영원히 남는다.

## 6. 위험과 한계 (해결로 표시하지 않는다)

| # | 위험 | 영향 | 최소 해결안 (승인 필요) |
|---|---|---|---|
| R1 | ~~`judge-worker` 가 시작 때 모델을 읽지 않는다~~ **해결(로컬, 2026-11-03)** | Compose 실측: 로딩 13.4~20.2초 + 첫 순전파 1.2~2.4초를 첫 메시지 **앞**에서 치른다. 첫 순전파와 두 번째가 거의 같아(1.24 vs 1.59초) 이득은 로딩 시간을 lease 밖으로 옮긴 것이다. EKS 에서 값은 달라질 수 있다 | 이미지에 반영(P2 의 CI) 후 EKS 에서 `ready:` 로그의 값 확인 |
| R2 | ~~`flask judge-worker`/`outbox-relay` 가 SIGTERM 을 무시한다~~ **해결(로컬, 2026-11-03)** | Compose 실측: 처리 중 SIGTERM → 그 작업 완료·새 작업 없음·종료 코드 0(5.7초), 유휴 15.6초, Relay 0.8초, 처리 중 SIGKILL → 62.9초 뒤 다른 Worker 가 인계(claim 2, 결과 1건). 남은 한계: 핸들러 설치 전(시작 후 수 초)에 온 SIGTERM 은 여전히 무시된다 → SIGKILL | 이미지에 반영 후 `kubectl delete pod` 로 확인 |
| R3 | Worker·Relay 에 HTTP 서버가 없다 | probe·PodMonitor 를 달 수 없다. 멈춘(교착) 프로세스는 kubelet 이 못 잡는다. Prometheus 지표 없음 | 지표 서버(`start_http_server`)나 heartbeat 파일 + exec probe |
| R4 | Worker 이미지 / ECR / CI 가 없다 | Worker 배포 불가 | 2절 P1·P2 |
| R5 | KEDA operator 의 IAM·Pod Identity: 코드는 있고(`iam_keda.tf`) **apply 전** | apply 전에 KEDA 가 뜨면 ScaledObject 인증 실패 → min 0 이면 Worker 가 0 에 머문다(R13) | cluster 스택 apply 가 keda 앱보다 먼저(정상 재구축 순서) |
| R6 | RDS 관리형 비밀은 7일마다 회전한다. ESO 는 1시간마다 Secret 을 갱신하지만 **환경변수는 Pod 시작 때 고정**이다 | 장시간 도는 Worker·Relay(와 backend)가 회전 뒤 새 연결에 옛 비밀번호를 쓴다. 기존 backend 와 같은 문제이고 Relay·Worker 는 더 오래 산다 | 회전 후 rollout restart 절차, 또는 Reloader. 재구축이 잦은 실험 기간에는 드러나지 않을 수 있다 |
| R7 | 공존 구성에서 Worker 가 떠 있으면 노드에 자리가 없다: **마이그레이션 Job, inference 롤링 surge, api 롤링 surge, api HPA 3개째** | 배포가 멈추고 api 가 부하에 확장하지 못한다(8.2절 표). 그래서 min 0, **배포는 큐가 빈 뒤에**, 부하 테스트는 비교 구성 | 노드 1대 추가, 또는 비교 구성으로 전환, 또는 inference·api 를 `maxSurge 0`(Phase 1 변경이라 별도 승인) |
| R8 | KEDA 가 FIFO 그룹 제약을 모른다 | 한 장비에 몰리면 Worker 만 늘고 처리량은 그대로 | max 를 장비 수 이하로 |
| R9 | DB 연결 수: api 3 × (5+10) + Worker·Relay·Job | RDS `db.t4g.micro` 의 `max_connections` 에 근접할 수 있다 [미확인: 재구축 뒤 `SHOW max_connections`] | 풀 크기 조정 |
| R10 | `config.env` 가 바뀌면 `backend-config` 해시가 바뀌어 inference 가 한 번 롤링된다(inference 가 그 ConfigMap 을 키 단위로 읽는다) | 첫 재구축에서는 무관. 이미 떠 있는 클러스터에 반영하면 공존 구성의 CPU 여유로는 inference surge 가 막힐 수 있다 | 클러스터가 내려가 있을 때 push |
| R11 | `ECR_WORKER` 는 P1 전에는 이름 규칙으로 계산한 값 | 리포지토리 이름이 다르면 Worker 이미지 주소가 틀린다 | P1 후 `export-config-env.py` 재실행 |
| R13 | min 0 에서 KEDA 인증이 깨지면 Worker 가 0 에 머문다(`fallback` 이 구하지 못함) | 큐에 메시지가 쌓이고 판정이 멈춘다. API 는 202 를 계속 준다. 4일이 지나면 SQS 보존 만료로 메시지가 사라지고 Relay 복구가 되살린다 | 배포 뒤 ScaledObject READY 확인, `ApproximateAgeOfOldestMessage` 경보, 의심되면 min 1 |
| R12 | 로컬에 `helm` 이 없어 KEDA 차트를 렌더링하지 못했다 | 차트 기본값과 내 값을 병합해 키 존재만 확인했다. 첫 Argo CD sync 가 실제 렌더링이다 | 배포 전 `helm template` 가능하면 한 번 |

## 7. 로컬에서 한 검증과 못 한 것

한 것: `kubectl kustomize apps/parking-guardian` 렌더링, 렌더링된 객체의 참조·이름·환경변수·순서·배치 검사(`check-phase2-render.py`, 현재 `--strict` 를 뺀 실행에서 실패 0),
비교 구성 패치 렌더링, 기존 Phase 1 검사 54개 불변, 단위 테스트 34개(결함 주입 포함), KEDA 값과 차트 기본값 병합 확인, 핸들러 유무에 따른 SIGTERM 실측,
**2026-11-03 추가**: Terraform plan 2종(data: 추가 2·변경 1·삭제 0, cluster: 추가 43·변경 0·삭제 0), CI 워크플로 YAML 구문과 태그 갱신 코드 실행, 앱 SIGTERM·선로딩 구현의 Compose 검증(7개 시나리오).

못 한 것: 클러스터에 적용(서버 측 dry-run, 어드미션), Helm 렌더링(`helm` 없음), 스키마 검증 도구(`kubeconform`, `jsonschema`, `actionlint` 없음 — 시스템을 바꾸지 않았다),
KEDA 의 실제 스케일(0→1, 1→N)과 fallback 동작, Pod Identity 동작, Worker 의 실제 CPU·메모리 사용량, GitHub Actions 의 실제 실행.

## 8. 운영 위험 검토 (분석과 해결안. 구조 변경은 하지 않았다)

### 8.1 CPU 산술: 55m 와 215m 는 다른 숫자다

노드 3대의 앱 가용 CPU 합 3840m − Worker 1개가 떠 있을 때 요청 합 3625m = **215m 가 클러스터 전체의 여유 합**이다. 55m 는 그 합이 아니라 **가장 빡빡한 노드의 여유**다.
노드별로 풀면 `N1: inference 1000 + api 200 → 80m`, `N2: 같은 구성 → 80m`, `N3: Worker 1000 + 묶이지 않는 Pod(frontend 50 + relay 50 + KEDA 125 = 225) → 55m`.
`80 + 80 + 55 = 215`. Pod 는 노드 하나에 들어가야 하므로 합 215m 는 "200m 짜리 api 가 하나 더"를 보장하지 않는다(어느 노드에도 200m 가 없다).

### 8.2 노드별 여유와 롤링 가능 여부 (요청 기준 best-case, `check-phase2-render.py`)

> **2026-11-04 갱신**: 아래 표는 노드당 앱 가용 1280m 를 가정한 설계 계산이다. 실제 클러스터에서는 CloudWatch Agent 요청을 250m → 100m 로 낮춘 뒤 요청 합이 4235m / 5790m(73%)이고,
> Worker 가 떠 있어도 마이그레이션 Job 과 backend surge 는 들어가며 **막히는 것은 1000m 짜리(inference surge·inference 3번째·Worker 2번째)뿐**이다. 실측 기반 계산은 [ops-risks-2026-11-04.md R7](ops-risks-2026-11-04.md).

| 구성 | 노드별 남는 CPU (N1 / N2 / N3) | 남는 메모리 (Mi) | 스케줄 가능한 변화 | 불가능한 변화 |
|---|---|---|---|---|
| 공존, Worker 0 | 655 / 280 / 280 | 4041 / 5197 / 5197 | 마이그레이션 Job, api·inference 롤링 surge, api HPA 3 | — |
| 공존, Worker 1 | 80 / 80 / 55 | 4941 / 4941 / 3017 | frontend·relay·KEDA 의 surge(+50m, 여유 5m 로 줄어듦) | **마이그레이션 Job(100m), inference surge(1000m), api surge·HPA 3개째(200m), Worker 2개째** |
| 비교 구성 (api 2 + Worker 2) | 655 / 280 / 280 | 4041 / 5197 / 5197 | 마이그레이션 Job(여유 555/280/280), api surge | — |
| 비교 구성 (api 2 + Worker 3) | 80 / 80 / 55 | 4941 / 4941 / 3017 | — (Worker 1 일 때의 공존 구성과 같은 여유) | **마이그레이션 Job, api surge** → 배포 전에 Worker 를 2 이하로 |

메모리는 모든 구성에서 충분하다(노드당 약 6.7 Gi 중 CPU 보다 훨씬 덜 쓴다). 병목은 CPU 요청이다.

### 8.3 `minReplicaCount: 0` 의 영향

| | min 0 (지금 기본) | min 1 |
|---|---|---|
| 초기 기능 검증 | 적합: 유휴 때 노드가 비어 배포·롤링이 막히지 않는다. 비용 추가 없음 | 배포가 Worker 때문에 막힌다(R7) |
| 부하 테스트·측정 | **부적합**: 첫 요청이 콜드 스타트(이미지 Pull 약 660 MB + 모델 다운로드 412 MB + 로딩 13~20초 = 1~3분 [추정])를 치르고, KEDA 폴링(15초) + 시작이 지연에 섞인다. 지연 분포가 오염된다 | 적합. 비교 구성과 함께 쓴다 |
| Worker Pod 장애 | Deployment 가 되살린다(현재 개수가 1일 때). 0 이었다면 다음 메시지에서 KEDA 가 올린다(복구 = 폴링 15초 + 시작) | 즉시 1개 유지 |
| 노드 장애 | Worker 노드가 죽으면 남은 2노드(2560m)에 inference 2 + api 2(2400m)가 있어 Worker 는 노드가 교체될 때까지 Pending (min 0/1 동일, 3~5분 [추정]) | 같음 |
| KEDA 인증 장애 | **Worker 가 0 에 머문다**(fallback 불가: R13). 큐가 쌓이고 판정 정지, API 는 202 유지. 4일 뒤 SQS 보존 만료 → Relay 복구가 되살림 | fallback 이 현재 개수를 유지 |

권장: 기능 검증 = min 0 + `kubectl get scaledobject worker` READY 확인. 부하·복구 측정 = 비교 구성(min 1, inference 없음).

### 8.4 RDS 비밀번호 회전과 장시간 Worker·Relay (R6)

현상: RDS 관리형 마스터 비밀은 7일마다 회전한다. ESO 는 1시간마다 Kubernetes Secret 을 갱신하지만 `envFrom` 으로 받은 **환경변수는 Pod 시작 때 고정**이라, 오래 사는 Pod(Worker·Relay·backend)의 새 DB 연결은 옛 비밀번호로 인증을 시도한다. (회전 직후 옛 비밀번호가 얼마나 유효한지는 [미확인]. 단일 사용자 회전은 옛 비밀번호가 곧 무효라고 알려져 있다.)

| 해결안 | 내용 | 비용 | 평가 |
|---|---|---|---|
| A. 절차 | 회전 뒤(또는 7일 주기로) `kubectl -n parking-guardian rollout restart deploy backend worker relay` | 변경 없음 | **최소 해결안.** 재구축이 잦은 실험 기간에는 충분. 잊으면 장애 |
| B. Reloader | Stakater Reloader 를 Argo 앱으로 설치하고 Deployment 에 `reloader.stakater.com/auto: "true"` | Pod 1개(요청 약 10m/50Mi) + 앱 1개 | **자동화 최소 해결안.** Secret 이 바뀌면 롤링한다. 운영 전에 권장. 별도 승인 |
| C. 앱 | DB 비밀번호를 Secret 볼륨 파일에서 새 연결마다 읽기 | 앱 코드 변경 + 테스트 | 가장 정확하지만 가장 큼 |

권고: 지금은 A 를 운영 절차에 적고(롤링은 maxSurge 규칙을 따른다: Worker 는 먼저 내리고 띄운다), 운영 직전에 B 를 승인받는다.

**2026-11-04 실제로 발생했다 [관찰]**: RDS 를 시작한 약 45분 뒤(시작 03:38 UTC, 이벤트 `Reset master credentials` 04:23:59 UTC) 관리형 비밀이 회전했다.
회전이 이 시점에 일어난 이유(7일 주기의 예정 시각인지, 중지 기간의 영향인지)는 확인하지 못했다. 그 사이 ExternalSecret 은 04:17 에 동기화돼 있었고
(갱신 주기 1시간), Argo CD 의 재시도로 다시 만들어진 마이그레이션 Job 이 옛 비밀번호로 `password authentication failed` 가 되어 `parking-guardian` 앱이 멈췄다.
ExternalSecret 강제 갱신(`force-sync` 어노테이션) 뒤 같은 재시도가 성공했고, backend·relay 는 롤링 재시작으로 새 비밀번호를 받았다.
이 절차는 **[runbook-rds-rotation.md](runbook-rds-rotation.md)** 에 정리했다. 해결안 A(절차)만 문서화했고 B(Reloader)·갱신 주기 변경·로테이션 설정 변경은 하지 않았다.

### 8.5 프로세스는 멈췄는데 컨테이너는 살아 있는 경우 (R3)

원인 후보: DB 쿼리 무한 대기(statement timeout 없음), 데드락, 네트워크 소켓 무응답. 지금은 kubelet 이 못 잡는다(HTTP 서버·probe 없음). 메시지는 lease·visibility 로 다른 Worker 에게 넘어가지만 멈춘 Pod 가 남고, Worker 가 하나뿐이면 판정이 멈춘다.

| 방법 | 내용 | 앱 변경 | 평가 |
|---|---|---|---|
| 1. heartbeat 파일 + exec livenessProbe | 루프를 한 바퀴 돌 때마다 `/tmp/alive` 를 touch. probe: `test $(( $(date +%s) - $(stat -c %Y /tmp/alive) )) -lt 120`. Worker 한 바퀴 = 긴 폴링 20초 또는 처리 중 ≤ lease 55초, Relay 한 바퀴 = 2초 + 단계 시간이라 임계 120초로 오탐을 피한다. preload 동안(약 15~35초)은 `startupProbe` 또는 `initialDelaySeconds: 90` | 약 15줄 + 테스트 | **최소 해결안.** 슬림 이미지에 `sh`·`stat`(coreutils)가 있어 추가 설치가 없다 |
| 2. 지표 서버 | 9102 포트 `start_http_server` + 마지막 루프 시각 gauge + PodMonitor/알람, httpGet probe | 중간 | 관측까지 얻지만 크다 |
| 3. 큐 쪽 감지 | `ApproximateAgeOfOldestMessage` 경보(CloudWatch), KEDA ScaledObject READY, 멈춘 작업 복구(이미 구현) | 없음 | 멈춤을 알려 줄 뿐 Pod 를 되살리지는 못한다. **1 과 같이** |

권고: 1 + 3. DB `statement_timeout`(예: 30초)을 같이 두면 가장 흔한 원인(쿼리 무한 대기)이 예외로 바뀌어 루프가 계속 돈다. 모두 별도 승인.

### 8.6 EKS 버전: 1.34 인가 1.35 인가

**Terraform 의 실제 설정은 1.35 다.** `terraform/stacks/cluster/variables.tf` 의 `eks_version` 기본값이 `"1.35"` 이고, `terraform.tfvars` 에서 덮어쓰지 않으며(예시 파일도 주석으로 1.35), 마지막 apply 로그에 `cluster_version = "1.35"` 가 남아 있다. "1.34" 는 이 PC 의 **kubectl 클라이언트 버전**(1.34.1, 서버와 마이너 1 차이는 허용 범위)이고, variables.tf 설명에 "표준 지원 1.34~1.37" 이라는 범위 문구가 있어 혼동된 것으로 보인다.
KEDA 2.21 의 시험된 Kubernetes 범위 v1.34~v1.36 안에 1.35 가 들어간다.

### 8.7 KEDA scale-to-zero 와 fallback (현재 설정에서 실제로)

- scale-to-zero: 지원된다. `minReplicaCount: 0` 이고 `idleReplicaCount` 를 쓰지 않으면 KEDA 가 0↔1 을, HPA 가 1→N 을 맡는다. 0→1 은 폴링(15초)마다 활성 여부를 보고 즉시 올린다(`activationQueueLength` 기본 0). `cooldownPeriod: 300` 은 0 으로 줄일 때만 쓰인다. (문서 확인. 실제 클러스터 동작은 미확인)
- fallback: aws-sqs-queue(AverageValue)에서 지원된다. 그러나 **KEDA 소스상 Worker 가 0개일 때 오류가 나면 0→1 로 올리지 못한다**(RequestScale 이 로그만 남김). 이미 떠 있는 Worker 를 0 으로 줄이는 것만 막는다. 공식 문서는 이 상호작용을 설명하지 않는다 → R13, 4절.

### 8.8 첫 배포 교착: worker 가 기본 replicas 1 로 만들어지는 문제 (2026-11-04 실제 발생)

**현상 [관찰]**: 새 클러스터에 처음 배포하자 worker Deployment 가 `replicas` 없이(=기본 1) 만들어졌다. 노드에 1000m 자리가 없어 Pod 가 `Insufficient cpu` 로 Pending →
Argo CD 가 wave 4 를 Healthy 로 보지 않아 `parking-guardian` 앱이 `waiting for healthy state of Deployment/worker` 에서 멈췄고, wave 5 의 ScaledObject 가
만들어지지 않았다. ScaledObject 가 없으니 KEDA 가 worker 를 0 으로 내릴 수도 없었다(순환). 그날은 `kubectl scale deployment worker --replicas=0` 으로 한 번 풀었다.

**"ScaledObject 를 wave 3 으로 옮기면 되는가" → 안 된다. 근거 (소스·문서로 확인, 클러스터에서 실험하지는 않았다)**

| 확인 | 결과 | 출처 |
|---|---|---|
| 대상이 없는 ScaledObject 를 KEDA 가 어떻게 두는가 | `Ready=False`(`ScaledObjectCheckFailed`)로 두고 오류를 반환한다 | KEDA `controllers/keda/scaledobject_controller.go` |
| 대상이 나중에 생기면 바로 다시 보는가 | 아니다. 컨트롤러는 ScaledObject·HPA 만 watch 하고 Deployment 는 watch 하지 않는다. 오류 반환에 따른 백오프 재시도에 맡긴다 | 같은 파일 `SetupWithManager` |
| Argo CD 가 `Ready=False` 를 어떻게 보는가 | **Degraded**(`Ready=True` 일 때만 Healthy, `Unknown` 이거나 조건이 없으면 Progressing) | Argo CD `resource_customizations/keda.sh/ScaledObject/health.lua` |
| Degraded·Progressing 이 다음 wave 를 막는가 | 막는다. Argo CD 는 한 wave 의 리소스가 모두 Healthy 가 돼야 다음 wave 로 간다. 문서가 "앱이 영원히 Healthy 가 못 될 수 있다" 고 경고한다 | Argo CD sync-waves 문서 |

즉 wave 3 으로 옮기면 worker(wave 4)가 만들어지기 전에 ScaledObject 가 Degraded 로 남아 **wave 4 를 막는다.** 교착이 사라지는 것이 아니라 방향만 바뀐다.
그래서 `check-phase2-render.py` 의 "ScaledObject 는 대상보다 뒤 wave" 규칙은 그대로 두고, 이유를 오류 메시지에 적었다.

**비교한 방법**

| 방법 | 교착이 사라지는가 | 비용·위험 | 판단 |
|---|---|---|---|
| A. ScaledObject·TriggerAuthentication 을 wave 3 으로 | **아니다**(위 표) | — | 채택하지 않음 |
| B. ScaledObject 를 worker 와 같은 wave 4 로 | 아마도(KEDA `initialCooldownPeriod` 기본 0 이라 만들어진 뒤 곧바로 0 으로 내린다). 같은 wave 에서는 Deployment 가 먼저 적용되고 사용자 정의 종류가 뒤에 적용된다고 알려져 있으나 확인하지 못했다. 그 사이 Pod 가 잠시 Pending | 순서가 어긋나면 일시 Degraded → KEDA 백오프 재시도에 의존. 클러스터에서 확인되지 않음. 기존 규칙(`wave(so) > wave(target)`)을 깬다 | 확실하지 않아 채택하지 않음 |
| C. Argo CD 헬스 검사 우회 | 문서에 방법이 없다(`ignore-healthcheck` 어노테이션은 확인한 문서에 없음) | 쓰면 worker 의 이상이 Application 상태에 안 보인다 | 채택하지 않음 |
| **D. worker `replicas: 0` + Application `ignoreDifferences`(`/spec/replicas`) + `RespectIgnoreDifferences=true`** | **예.** Argo CD 문서: 리소스가 아직 없으면 원하는 상태를 그대로 적용한다 → 새 클러스터에서 0 으로 태어나 곧바로 Healthy 이고 wave 5 가 진행된다. 이후 개수는 KEDA 가 정하고 Argo CD 는 이 필드를 diff·sync 에서 모두 무시한다 | 파일 2개(worker-deployment.yaml, argocd/apps/parking-guardian.yaml). **짝이 깨지면 selfHeal 이 KEDA 가 올린 개수를 0 으로 되돌려 처리 중인 Worker 를 죽인다** → 검사·테스트로 짝을 강제 | **채택(로컬 수정만 했고 클러스터에는 아직 반영하지 않았다)** |
| E. 손으로 `kubectl scale ... --replicas=0` | 그때만 | 재구축마다 사람이 해야 한다 | 임시 우회일 뿐 |

**검증한 것 [로컬]**: 렌더링 결과에서 worker 는 `wave 4 / replicas 0`, ScaledObject·TriggerAuthentication 은 `wave 5`. `check-phase2-render.py` 가 (1) worker 가 `replicas: 0` 이고
(2) Application 의 `ignoreDifferences` 가 `apps/Deployment/worker` 의 `/spec/replicas` 를 가리키고 (3) `RespectIgnoreDifferences=true` 인지를 검사하며, 단위 테스트가
"replicas 없음(옛 상태)", "ignoreDifferences 없음", "RespectIgnoreDifferences 없음", "다른 Deployment·다른 경로를 무시", "0 이 아닌 값", "wave 3" 을 각각 잡는다.
**못 한 것 [미확인]**: 실제 클러스터에서의 새 배포와 selfHeal 동작. Argo CD 문서의 동작을 근거로 했을 뿐 이번 클러스터에서 실험하지 않았다.

**이미 떠 있는 클러스터에 반영할 때의 주의**: Git 에 두 파일이 같은 커밋으로 들어가면, 루트 앱이 Application 설정을 갱신하기 전에 `parking-guardian` 앱이 새 Deployment(`replicas: 0`)를
옛 Application 설정(무시 없음)으로 동기화하면 selfHeal 이 한 번 worker 를 0 으로 내릴 수 있다. 처리 중인 메시지는 60초 뒤 다시 오지만, 안전하게 하려면
(1) 큐가 비고 worker 가 0 일 때 반영하거나 (2) `argocd/apps/parking-guardian.yaml` 을 먼저 커밋·동기화한 뒤 `worker-deployment.yaml` 을 커밋한다. 반영은 별도 승인을 받는다.

**비교 구성(min 1)에서**: ScaledObject 의 min 이 1 이어도 worker 는 `replicas: 0` 으로 태어나고 KEDA 가 곧바로 1 로 올린다(큐가 비어도 min 1). 같은 짝이 필요하다.
