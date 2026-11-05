# 관측성 2단계: Relay·SQS 메트릭 (2026-11-06, 로컬 구현·검증 완료, 배포 전)

비밀 값 없음. 1단계: `observability-stage1.md`. 앱 계측 코드는 capston 리포(`backend/app/metrics.py`, `cli.py`, `services/relay.py`, `adapters/queue.py`).

## 1. 지표 정의 (이름 · 종류 · 단위 · 라벨 · 출처)
라벨은 작고 고정된 집합만 쓴다. **회사·장비·작업 ID 라벨은 없다**(시험 `test_exposition_format_parses_and_labels_are_small_fixed_sets` 가 허용 라벨 값 집합을 고정).
| 지표 | 종류 | 단위 | 라벨 | 출처 / 의미 |
|---|---|---|---|---|
| `pg_queue_messages` | Gauge | 메시지 수 | `queue`=main\|dlq, `state`=visible\|not_visible\|delayed | SQS `GetQueueAttributes`(ApproximateNumberOfMessages / ...NotVisible / ...Delayed). 근사값. **조회 실패 시 시리즈를 지운다(0 으로 두지 않음)** |
| `pg_queue_depth_last_success_timestamp_seconds` | Gauge | Unix 초 | - | 마지막 큐 깊이 조회 성공 시각. 한 번도 성공하지 못하면 시리즈 없음 |
| `pg_sqs_api_errors_total` | Counter | 건 | `operation`=send\|receive_dead_letters\|get_queue_attributes | Relay 가 낸 SQS 호출 실패. 라벨 3개 모두 0 으로 미리 노출(정상 동작 중 rate=0 은 진짜 0) |
| `pg_jobs` | Gauge | 작업 수 | `status`=pending_publish\|queued\|processing\|dead_lettered | DB `detection_jobs` 상태별 건수, Relay 가 최대 15초마다 1회 집계. dead_lettered 는 redrive 전까지 남는다 = **지속되는 DLQ 신호** |
| `pg_oldest_open_job_age_seconds` | Gauge | 초 | - | DB 기준 가장 오래된 **미완료 작업** 나이(아래 2절). 없으면 0 |
| `pg_oldest_pending_publish_age_seconds` | Gauge | 초 | - | 아직 SQS 에 못 보낸(pending_publish) 가장 오래된 작업 나이(기존 `_report_old_pending` 값) |
| `pg_relay_publish_total` | Counter | 건 | `outcome`=success\|failure | Relay 가 SQS 로 보낸 시도. API 의 접수 직후 발행(fast path)은 세지 않는다. 라벨 2개 0 으로 미리 노출 |
| `pg_relay_publish_delay_seconds` | Histogram | 초 | - | Relay 가 재발행한 작업의 (발행 시각 − `created_at`). 버킷 0.1~600 s |
| `pg_relay_last_pass_timestamp_seconds` | Gauge | Unix 초 | - | Relay 가 마지막으로 한 회차를 끝낸 시각(루프 정지 감지) |
- pending_publish **작업 수** = `pg_jobs{status="pending_publish"}`. "SQS API 조회 실패 건수" = `pg_sqs_api_errors_total`.

## 2. 작업 정체 시간 정의 (SQS ApproximateAgeOfOldestMessage 와 다르다)
- **`pg_oldest_open_job_age_seconds`(DB 기준)**: 대상 상태 = `pending_publish`, `queued`, `processing`(= `OPEN_STATUSES`, 완료·stale·rejected·dead_lettered 제외). 기준 시각 = `detection_jobs.created_at`(서버 DB 시각, 접수 시각과 사실상 같음). 계산 = `now − min(created_at)`(Relay 의 시계). Relay 가 **이미 매 회차(2초) 실행하던 `_report_open_jobs` 의 값을 재사용**하므로 DB 부하가 늘지 않는다. 발행 대기와 판정 중인 작업도 포함한다.
- **SQS `ApproximateAgeOfOldestMessage`**: SQS 안에 있는 가장 오래된 메시지의 나이(CloudWatch, 1분 해상도·1~2분 지연·근사). 발행 전 작업과 판정 중(SQS 밖 처리)을 모르고 DB 와 다른 시계다. **이번 단계에서는 수집하지 않는다**(4단계 선택). 메트릭 이름·패널 설명·알림 설명에 "DB 기준"을 명시했다.

## 3. 설계상 중요한 두 가지 (구현 중 발견)
1. **DLQ 는 거의 항상 0 이다**: Relay 가 2초마다 DLQ 를 읽어 작업을 `dead_lettered` 로 DB 에 옮기고 메시지를 지운다. 따라서 "DLQ visible ≥ 1 이 1분"만 보면 알림이 사실상 울리지 않는다. `PGDLQHasMessages` 는 `SQS DLQ(대기+처리 중+지연) > 0` **또는** `DB dead_lettered > 0`(redrive 전까지 지속)일 때 울린다. (요청의 "DLQ visible" 대신 세 상태 합 + DB 신호.)
2. **Relay IAM 에 `sqs:GetQueueAttributes` 가 없다**(현재: 본 큐 SendMessage, DLQ Receive/Delete/ChangeVisibility). 큐 깊이 조회에는 두 큐에 대한 이 읽기 권한이 필요하다 → infra cluster 스택 변경(아래 5절, **별도 승인**). 권한이 없으면 앱은 정상 동작하고 큐 지표만 "없음"이 되며 `PGPipelineMetricsMissing` 이 울린다.

## 4. "값 없음"을 0 으로 보이지 않게 하는 설계
- 큐 깊이 조회 실패(권한·스로틀·타임아웃) → `pg_queue_messages` 시리즈를 **지운다**(Grafana No data, 알림은 데이터 없음). 오류는 `pg_sqs_api_errors_total{operation="get_queue_attributes"}` 로 센다(장애 구간당 1번만 경고 로그).
- 대시보드 패널에는 `or vector(0)` 를 쓰지 않는다. 지표 신선도 패널과 알림 `PGPipelineMetricsMissing`(Relay 스크레이프 끊김 / 마지막 회차 2분 초과 / 큐 조회 성공 기록 없음 또는 3분 초과, for 5m, warning)이 "알 수 없음" 상태를 알린다.
- 큐 조회는 **별도 스레드**(`QueueDepthPoller`, 15초 주기)에서 빠른 클라이언트(연결 2초·읽기 3초·1회 시도)로 한다. Relay 처리 루프와 공유하는 것이 없어 느리거나 실패해도 루프가 지연되지 않는다. DB 지표 갱신은 루프 안에서 `try/except` 로 감싸 실패해도 회차 결과가 바뀌지 않는다(시험으로 고정).

## 5. 변경 목록과 승인이 필요한 항목
| 구분 | 변경 | 승인 |
|---|---|---|
| 앱 코드(capston) | metrics.py(지표 9개), queue.py(`depths()`), relay.py(기록 + `QueueDepthPoller`), cli.py(루프 모드에서 `/metrics` 서버와 poller 시작, `--once` 는 시작 안 함) | 이미지 빌드·배포 승인 필요 |
| config 리포 | `relay-deployment.yaml`(포트 `metrics: 9102`), `relay-podmonitor.yaml`(신규), `kustomization.yaml`, `rules/pg-alerts.yaml`(`pipeline` 그룹 4개), `dashboards/pipeline-phase2.json` + 생성기 + kustomization | 커밋·push 는 사용자 |
| **AWS(Terraform, cluster 스택)** | `iam_phase2.tf` relay 정책에 `QueueDepthRead`(`sqs:GetQueueAttributes`, 본 큐·DLQ ARN 2개) 1개 statement 추가. plan: **0 add / 1 change / 0 destroy**(정책 in-place) | **별도 승인 필요(이번에 적용하지 않음)** |
- **권장 배포 순서**: ① Terraform cluster 스택 apply(IAM, 부작용 없음 — 권한만 추가) → ② 앱 이미지 빌드(CI) → ③ config 커밋·push(이미지 태그 + relay 포트·PodMonitor + 규칙 + 대시보드). IAM 보다 앱이 먼저 배포되면 `PGPipelineMetricsMissing` 이 5분 뒤 울린다(큐 깊이 조회 실패 — 의도된 신호).

## 6. 알림 규칙 (평가 30초, 모든 식이 `max(...)` 라 relay 복제 수와 무관)
| 알림 | 조건 | for | severity | 복구 / 오탐 |
|---|---|---|---|---|
| PGDLQHasMessages | `sum(max by (queue,state)(pg_queue_messages{queue="dlq"})) > 0` **또는** `max(pg_jobs{status="dead_lettered"}) > 0` | 1m | critical | 둘 다 0(원인 확인 뒤 `flask jobs redrive`). DLQ 로 막 옮겨진 1~2초는 for 1m 로 흡수 |
| PGQueueBacklogSustained | `sum(max by (state)(pg_queue_messages{queue="main",state=~"visible\|not_visible"})) > 30` | 5m | warning | 30건 이하. 버스트 직후 일시 적체(실험 2 최대 33건)는 5분 지속 조건으로 흡수. delayed 는 세지 않음 |
| PGPipelineOldestJobAge | `max(pg_oldest_open_job_age_seconds) > 30` | 3m | warning | 30초 미만. 장비 한 대의 연속 사진이 앞 사진 뒤에서 잠깐 기다리는 정상 직렬은 짧아 for 3m 로 흡수 |
| PGPipelineMetricsMissing(안전장치) | Relay 스크레이프 없음 또는 마지막 회차 > 120 s 또는 큐 조회 성공 기록 없음/ > 180 s | 5m | warning | 지표가 모두 돌아오면 |
요청한 3개 외에 안전장치 1개를 추가했다(값 없음 != 0 요구).

## 7. 대시보드 `SQS & Async Pipeline (Phase 2)` (`pg2-pipeline`, 패널 12개)
큐 깊이(메인) · DLQ(SQS 메시지 vs DB dead_lettered) · **가장 오래된 미완료 작업 나이(DB 기준, SQS 나이와 다름을 설명에 명시)** · 작업 상태별 개수 · Relay 발행률 · 발행 지연 p95/p99 · SQS API 오류 · 지표 신선도 · Relay 스크레이프 상태 · Worker Ready 대 목표 · KEDA/HPA replica. 패널 설명에 정의와 "No data = 알 수 없음" 을 적었다.

## 8. 로컬 검증 결과 (2026-11-06)
- 앱: 신규 시험 18개(`tests/test_relay_metrics.py`) + 기존 전체 **570 passed, 30 skipped**(SQLite). PostgreSQL 16 컨테이너에서 relay·queue 관련 109개 통과(PG 전용 시험 포함). 변이 시험(조회 실패 시 시리즈 삭제 코드를 지움 → 해당 시험이 실패)으로 시험의 유효성 확인.
- Prometheus 형식: 로컬 relay 프로세스(`flask outbox-relay`, 가짜 큐·SQLite) `/metrics` 10 KB 를 공식 이미지 `promtool check metrics` 로 검사 → 문제 없음. 지표 값 확인(큐 6개 시리즈, `pg_jobs` 4개, 나이 0, 마지막 회차·조회 성공 시각).
- 규칙: `promtool check rules` 10 rules SUCCESS, `promtool test rules` SUCCESS(신규 케이스: DLQ 두 경로·이중 계산 방지·침묵·지표 없음, 적체 35/25, 나이 45/20, 지표 없음 4가지). 틀린 기대값을 넣은 음성 대조는 FAILED 로 잡힘.
- GitOps: Kustomize 렌더 — apps 변경은 **relay Deployment 포트 1개 + relay PodMonitor 신규**뿐(backend·worker·frontend 불변, relay 자원·이미지·명령 불변), `monitoring-config` 에 대시보드 ConfigMap 1개 추가, 서버 dry-run 통과(저장 없음), Phase 1/2 렌더 검사·config 테스트 36개 통과, Phase 1 대시보드 JSON 3개 불변.
- Terraform: `fmt`/`validate` 통과, cluster 스택 plan 0 add / 1 change / 0 destroy(적용 안 함).
- 실행하지 않은 것: AWS·Kubernetes 변경, 이미지 빌드·배포, 실제 SQS/DLQ 메시지, 부하 테스트, Git 작업.

## 9. 리소스·비용·부하
- Relay 자원: 변경 없음(요청 50m/128Mi). 추가 작업 = 15초마다 SQS 호출 2번(스레드) + 15초마다 DB 집계 1번(`GROUP BY status`, 인덱스 `ix_detection_jobs_status_next_publish`) + 이미 하던 나이 조회 재사용.
- SQS API: `GetQueueAttributes` 2건 × 4회/분 = 하루 11,520건, 월 약 35만 건. KEDA 의 기존 조회(월 약 17만 건)를 더해도 SQS 무료 범위(월 100만 건) 안 → **추가 비용 0**. IAM 변경 무료.
- DB: 집계 1회 = 상태별 몇 개 행(열린 작업은 수십 건 이하, dead_lettered 는 0~소수) — 실험 2 규모(작업 1.5만 건)에서도 인덱스 범위 조회. 부하 무시 가능.
- Prometheus: 새 시리즈 약 40개(큐 6 + jobs 4 + 히스토그램 14 + 기타). 저장량 무시 가능. 노드·Pod 추가 없음.

## 10. 알림 운영 보충 (배포 전 점검, 2026-11-06)

**PGDLQHasMessages (critical)**
- 왜 SQS DLQ 와 DB `dead_lettered` 를 함께 보나: Relay 가 2초마다 DLQ 를 읽어 작업을 `dead_lettered` 로 옮기고 DLQ 메시지를 지운다. SQS DLQ 개수는 거의 항상 0 이라 단독으로는 놓친다. DB `dead_lettered` 는 redrive 전까지 남는 지속 신호다.
- Firing: (DLQ 메시지 합계 > 0) 또는 (`pg_jobs{status="dead_lettered"}` > 0) 이 1분 지속. dead_lettered 가 남아 있는 한 계속 Firing 상태다.
- Resolved: DLQ 가 비고 dead_lettered 작업이 0 이 되면.
- 데이터를 지우지 않고 해소하는 절차: (1) Worker 로그로 실패 원인 확인(이미지·모델 오류 등) (2) 원인 해결 (3) `flask jobs redrive` 로 dead_lettered 를 pending_publish 로 되돌려 재처리 (4) 처리 완료 후 알림 Resolved 확인. DB 행·SQS 메시지를 삭제하지 않는다. 원인 미해결 상태에서 redrive 하면 다시 dead_lettered 가 된다.

**PGQueueBacklogSustained (warning)**
- 대상은 main 큐 `visible + not_visible` 합계 > 30 이 5분. `delayed` 는 아직 처리 대상이 아니므로 제외.
- Relay 가 여러 개여도 같은 큐를 읽으므로 `max by (state)` 로 합쳐 이중 계산하지 않는다(합산 아님).
- 값은 SQS `ApproximateNumberOfMessages*` 근사값이다(수 초 오차). 정확한 건수가 아니라 추세 신호로 본다.

**PGPipelineMetricsMissing (warning)** — 다음 중 하나면 Firing(5분 지속)
- Relay 스크레이프 실패(`up{job="parking-guardian/relay"}` 없음/0)
- Relay 루프 정지(마지막 회차 시각이 120초 넘게 갱신 안 됨)
- 큐 깊이 조회(GetQueueAttributes) 한 번도 성공 못함 또는 마지막 성공이 180초 넘게 stale
- 지표 없음 ≠ 0: 조회 실패 시 `pg_queue_messages` 시리즈를 지워 "0 = 정상" 으로 오해하지 않게 한다. 이 상태에서는 적체·DLQ 알림을 판단할 수 없다.

## 11. 실제 배포·수집 검증 결과 (2026-11-06 03:45~03:55 KST, 읽기 전용)

**GitOps**
- 앱 커밋 `7f4456b`, config `e8024c6`(CI 태그 커밋) → `e84ff2b`(관측성 2단계). config HEAD = origin/main = `e84ff2b`.
- Argo: `parking-guardian`, `monitoring-config`, `platform`, `root` 모두 Synced/Healthy, 적용 리비전 `e84ff2b`. (`monitoring`·`keda` 는 멀티 소스 앱이라 리비전 필드가 비어 있고 Synced/Healthy.)
- ECR(profile `parking-guardian`): backend·inference·worker 에 태그 `7f4456b` 확인(2026-11-06 03:37~03:42 KST). frontend 도 `7f4456b` 이미지를 Pod 가 사용 중. 실행 중 이미지: backend·relay(backend 이미지)·frontend·worker 모두 `7f4456b`. CI 실행 목록 조회(`gh`)는 이 PC 에 없어 하지 못했고, 위 ECR 태그와 config 태그 커밋으로 성공을 확인했다.
- Deployment: Backend 2/2, Worker 2/2, Relay 1/1, Frontend 1/1. 모든 Pod 재시작 0, 마이그레이션 Job Completed. monitoring 네임스페이스 Pod 재시작 0.
- 큐: SQS 메인·DLQ 모두 0/0(visible·not_visible·delayed). `pg_jobs` 4개 상태 모두 0. 코드·Terraform·AWS·운영 데이터·로컬 실험 결과는 건드리지 않았다.

**Prometheus**
- PodMonitor `relay`(parking-guardian) 생성, 타깃 `parking-guardian/relay` UP, 스크레이프 2.6ms, 오류 없음.
- 시리즈 존재: `pg_queue_messages`(main·dlq × visible·not_visible·delayed = 6개, 값 0), `pg_jobs`(pending_publish·queued·processing·dead_lettered = 4개, 값 0), `pg_oldest_open_job_age_seconds`(0), `pg_relay_publish_total`(success·failure 0), `pg_relay_publish_delay_seconds_*`(count 0), `pg_sqs_api_errors_total`(send·receive_dead_letters·get_queue_attributes 0).
- 신선도: 마지막 회차 시각은 스크레이프 시점 기준 1.4초 이내, 큐 깊이 마지막 성공은 12초 이내(15초 주기). 30분 구간 SQS 오류 증가 0.
- **빈 큐(0)와 수집 실패 구분**: 빈 큐는 시리즈가 *존재하고 값이 0*이다. 수집 실패라면 시리즈가 사라지거나(No data) 신선도 시각이 멈춘다. 지금은 시리즈 6개가 존재하고 신선도가 최신이므로 정상적인 0이다. 발행 지연 분위수는 재발행이 없어 NaN(No data)이며 이는 "해당 사건 없음"이다.
- 참고: backend Pod 도 같은 `pg_*` 지표를 0 으로 미리 만들어 낸다(코드 공유). 알림 식이 `max` 라 영향 없고, relay 큐 조회가 멈추면 오히려 stale 조건으로 울리는 안전한 방향이다.

**Grafana·알림**
- 대시보드 `Parking Guardian / SQS & Async Pipeline (Phase 2)`(uid `pg2-pipeline`, 폴더 Parking Guardian (Phase 2)) 등록, 패널 12개(설명 텍스트 1 + 데이터 11). 모든 쿼리 Prometheus 에서 success 로 평가, 시리즈 수 1~4(발행 지연은 NaN).
- PrometheusRule `pipeline` 그룹 규칙 health=ok: PGDLQHasMessages, PGQueueBacklogSustained, PGPipelineOldestJobAge, PGPipelineMetricsMissing 모두 inactive(+ 1단계 테스트 규칙 `PGAlertPipelineTest` inactive). firing 알림 0건.
- Alertmanager 설정의 Slack receiver·`api_url_file` 유지, ExternalSecret `slack-webhook`·`grafana-admin` SecretSynced.

**운영 안정성**
- Relay 로그에 AccessDenied·오류·예외 0건(`metrics server listening on :9102`). `get_queue_attributes` 오류 0 → IAM 정상.
- Relay CPU 5m·메모리 97Mi, 처리 루프 지연 없음(회차 시각 1.4초 이내), 스크레이프 2.6ms.
- 한계: 배포 후 약 5~10분 관찰이다. 부하 중 값(적체·발행 지연·DLQ)은 이번에 검증하지 않았다(새 부하·장애 주입 금지). 이는 실제 트래픽이 생길 때 확인한다.
