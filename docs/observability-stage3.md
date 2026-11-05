# 관측성 3단계: Worker · AI 추론 · E2E 지표 (로컬 구현·검증 완료, 배포 전)

2026-11-06. 앱 코드(capston `backend/`)와 이 리포의 GitOps 변경을 **로컬에서만** 구현·검증했다. 이미지 빌드, Git commit/push, AWS·Terraform·Kubernetes 변경, SQS/DLQ 메시지 주입, 부하 테스트는 하지 않았다. 배포는 별도 승인 뒤.

## 1. 기존 Worker 흐름과 재사용한 지표

`flask judge-worker`(앱 `services/worker.py`)는 **프로세스 하나가 메시지를 한 개씩** 처리한다(Prometheus 기본 레지스트리로 충분, multiprocess 불필요).

```
receive(SQS, long poll) → claim(조건부 UPDATE, claim_attempt) → 사진 읽기 → 추론(트랜잭션 밖) →
한 트랜잭션(기기 잠금, 판정 반영, 결과 행, 작업 done/stale)  ── commit ──→ 부수 효과(증거 사진·알림) → 메시지 삭제
```

| 상황 | 동작 |
|---|---|
| 사진 없음·손상 | `rejected`(판정 없음), 메시지 삭제 |
| 저장소·추론·DB 오류 | lease 해제, 메시지는 큐에 남음 → 재전달(maxReceiveCount 3, 이후 DLQ → Relay 가 `dead_lettered` 로 기록) |
| 커밋 뒤 부수 효과 실패 | 판정은 저장됨, 재전달이 부수 효과만 다시 함 |
| 같은 작업 재전달 | 이미 끝났으면 `already`, 다른 Worker 가 잡고 있으면 `busy` |

**재사용(중복 계측하지 않음)**: `pg_inference_stage_seconds{stage}`(디코드·앞유리·스티커), `pg_inference_lock_wait_seconds`, `pg_inference_requests_total{verdict,outcome}`, `pg_inference_model_state`(cold/loading/ready/failed, 어댑터가 이미 설정), `pg_model_load_seconds`, `pg_model_first_inference_seconds`. Worker 프로세스는 같은 어댑터를 쓰므로 `/metrics` 만 열면 나온다.

**시각 정의**: `detection_jobs.received_at` = API 서버 시계로 사진을 받은 시각(DB). `detected_at` = 장비 시계(표시·감사용, 신뢰하지 않음 → E2E 에 쓰지 않음). SQS `SentTimestamp` = AWS 시계의 첫 발행 시각(재전달해도 유지). 최종 판정 시각 = 판정 트랜잭션 **커밋 직후** Worker 시계.

## 2. 신규 지표 (앱 `app/metrics.py`, Worker 프로세스 `:9102`)

| 이름 | 종류 · 라벨 | 정의 · 기록 시점 |
|---|---|---|
| `pg_worker_messages_total` | Counter `outcome` = done, stale, effects_retry, rejected, retry, already, busy, lost_claim, isolated | 메시지 처리가 **끝났을 때** 1회. done/stale 은 판정 트랜잭션 **커밋 뒤**에만. 커밋 뒤 부수 효과 실패는 `retry` 가 아니라 `effects_retry`. 시작 시 0 으로 만들어 rate()가 진짜 0 |
| `pg_worker_process_seconds` | Histogram `outcome` | claim 성공 시점 → 처리 끝(사진 읽기·추론·트랜잭션·부수 효과·삭제). claim 못 한 메시지(busy·already·isolated)는 제외 |
| `pg_job_e2e_seconds` | Histogram | **서버 기준 E2E** = `received_at` → 판정 커밋 직후. done·stale 만, **작업당 1회**. 증거 사진·알림·SQS 삭제는 제외 |
| `pg_queue_wait_seconds` | Histogram | SQS `SentTimestamp` → Worker 수신. **첫 전달(ApproximateReceiveCount=1)만**. 같은 장비의 앞선 메시지 뒤 대기(FIFO 그룹) 포함 |
| `pg_worker_timing_skipped_total` | Counter `metric`(e2e, queue_wait), `reason`(missing, negative) | 시각 누락·시계 차이로 음수라 표본에서 뺀 건수(0 으로 기록하지 않음) |

버킷(초): 0.1 0.25 0.5 1 2 3 5 7.5 10 15 20 30 60 120 300 600 (10초 목표가 정확한 경계). 라벨은 모두 작은 고정 집합이며 회사·장비·작업·이벤트 ID 는 없다(테스트로 고정).

### 정확성 기준

- **성공은 커밋 뒤에만**: `_apply` 가 `commit` 을 마치고 `Applied` 를 돌려준 뒤에만 `done/stale` 과 E2E 를 기록. 커밋 실패(트랜잭션 예외)는 `retry`, E2E·done 불변 (테스트).
- **중복 방지**: 같은 사진은 claim 펜싱(`claim_attempt`) 덕분에 정확히 한 시도만 커밋한다. 재시도로 두 번째 시도가 커밋해도 첫 시도는 커밋 못 했으므로 E2E 는 1건. 이미 끝난 작업의 재전달은 `already` 이며 E2E·done 에 더하지 않는다. 큐 대기도 첫 전달만 센다 (테스트).
- **E2E 에 재시도 시간은 포함된다**: 접수부터 최종 커밋까지라서 실패→재전달(≥60초)을 거친 작업은 그 시간만큼 크다. 사용자가 체감하는 시간이므로 의도된 정의다.
- **음수·누락**: `elapsed < 0`(시계 차이) 또는 시각 없음 → 표본에서 제외하고 `pg_worker_timing_skipped_total` 증가. 0 으로 넣지 않는다.
- **메트릭 오류는 처리에 영향 없음**: 모든 기록이 try/except. 메트릭이 예외를 던져도 outcome 은 그대로 (테스트).
- **측정 한계**: AWS·Pod 시계 차이로 큐 대기는 근사. 히스토그램 p50/p95/p99 는 **버킷 보간 추정값**이며 대시보드 제목에 "추정값"으로 표시했다. 판정이 DLQ 로 간 작업은 E2E 표본이 없다(정체는 `PGPipelineOldestJobAge`·DLQ 알림이 본다). 여러 Worker 의 합산은 `sum by (le)(rate(..._bucket))` 로 버킷을 합친 뒤 분위수를 구한다.

## 3. 앱 변경 파일 (capston, 미커밋)

- `backend/app/metrics.py`: 위 지표 정의.
- `backend/app/adapters/queue.py`: `QueueMessage.sent_at`(선택 필드), SQS 수신 시 `SentTimestamp` 요청·파싱(`_sent_at`), Fake 큐는 `enqueued_at` 전달. 권한 추가 없음(수신 속성).
- `backend/app/services/worker.py`: `process()` 를 `_process()` 로 감싸 결과·시간을 기록. 처리 로직은 변경 없음(claim·추론·트랜잭션·재시도·DLQ·삭제 그대로).
- `backend/app/cli.py`: `flask judge-worker` 가 상시 모드(`--until-empty`/`--max-messages` 아님)일 때 모델 로딩 **전에** `/metrics`(METRICS_PORT 9102)를 연다.
- `backend/tests/test_worker_metrics.py`(신규 14개), `backend/tests/test_queue.py`(수신 요청 속성 목록에 `SentTimestamp` 기대값 1줄).

## 4. GitOps 변경 (이 리포, 미커밋)

- `apps/parking-guardian/worker-deployment.yaml`: `ports: metrics 9102`.
- `apps/parking-guardian/worker-podmonitor.yaml`(신규, job = `parking-guardian/worker`, 15초) + `kustomization.yaml` 리소스 한 줄.
- `platform/monitoring/config/dashboards/build_dashboards.py` + 신규 `ai-inference-phase2.json`, `kustomization.yaml`(ConfigMap `dashboard-ai-inference-phase2`). 기존 Phase 1·1단계·2단계 대시보드는 재생성해도 바이트 변화 없음(보존).
- `platform/monitoring/config/rules/pg-alerts.yaml`(worker 그룹에 2개 추가), `rules/tests/pg-alerts.test.yaml`.

### 대시보드 `Parking Guardian / AI Inference & E2E (Phase 2)` (uid `pg2-inference`, 패널 13개: 설명 1 + 데이터 12)

E2E 지연 p50/p95/p99(추정, 10초 점선) · 10초 이내 비율(실측 버킷) · 처리량(done/stale) · 오류·재시도(retry/effects_retry/rejected/lost_claim) · Worker 처리 시간 p50/p95(추정) · 큐 대기 p50/p95(추정) · 추론 단계별 p95(추정) · 판정 결과 · 모델 준비 상태(state-timeline) · 모델 로딩/첫 추론 시간 · Worker 수집 상태(UP) · 시간 측정 건너뜀. 추정값·근사는 제목과 설명에 명시. No data 는 0 으로 대체하지 않는다.

### 알림 (worker 그룹에 추가)

| 알림 | 조건 | for · 심각도 |
|---|---|---|
| `PGE2EP95High` | 5분 창 E2E p95(버킷 보간) > 10초 **그리고** 5분간 판정 ≥ 5건 | 5m · warning |
| `PGWorkerMetricsMissing` | `up{job="parking-guardian/worker"}==1` 이 없음 **그리고** Worker 목표 replica > 0 | 5m · warning |

수집이 끊기면 E2E·오류 규칙이 침묵하므로(지표 없음 ≠ 0) `PGWorkerMetricsMissing` 이 필요하다고 판단해 추가했다. Worker 를 일부러 0 으로 줄이면(`PGWorkerDown` 담당) 침묵한다. 표본 5건 미만이면 `PGE2EP95High` 는 평가하지 않는다(판정이 아예 없는 정체는 `PGQueueBacklogSustained`·`PGPipelineOldestJobAge` 가 본다).

## 5. 로컬 검증 결과

- 앱: SQLite 584 통과(30 skip = PostgreSQL 전용), **PostgreSQL 16(일회용 컨테이너) 607 통과**(7 skip), device 97 통과. 신규 14개 = 정상 커밋·E2E 값, stale, 추론 실패 retry, retry→성공 시 E2E·큐 대기 1회, 커밋 전 DB 실패(done 불변), 커밋 후 부수 효과 실패(`effects_retry` + 재전달 `already`, E2E 중복 없음), rejected, 시계 음수 skip, SentTimestamp 누락 skip, 메트릭 예외 무영향, `SentTimestamp` 파싱·요청, 라벨 집합, 버킷. 변이 점검: 커밋 표시 줄을 빼면 해당 테스트가 실패함을 확인.
- `/metrics` 서버를 로컬로 띄워 응답을 받아 공식 이미지 `quay.io/prometheus/prometheus:v3.15.1` 의 `promtool check metrics` 통과.
- 대시보드 쿼리 19개를 `promtool check rules` 로 문법 검증(통과).
- `promtool check rules`(12개 규칙) · `promtool test rules` 전부 통과(신규: E2E 울림·침묵·표본 부족·지표 없음, 수집 누락 울림·정상·DOWN·목표 0).
- `kubectl kustomize apps/parking-guardian`·`platform/monitoring/config` 렌더 정상(PodMonitor 3개, Worker 포트 9102), 렌더 점검 스크립트 phase1 54 ok·phase2 110 ok(FAIL 0), 스크립트 테스트 50 통과. 모니터링 Helm 값은 변경하지 않아 Helm 렌더는 해당 없음.
- 한계: 실제 Worker 이미지·Pod·클러스터에서의 수집은 배포 후에야 확인 가능. 부하 중 E2E 값은 검증하지 않았다.

## 6. 배포 순서 (별도 승인 뒤)

1. 앱 리포: 변경 6개 파일만 스테이징(`backend/app/metrics.py`, `adapters/queue.py`, `services/worker.py`, `cli.py`, `tests/test_worker_metrics.py`, `tests/test_queue.py` — 6개) → commit/push → CI 성공, ECR 태그(7자리) 확인. `loadtest/` 변경은 섞지 않는다.
2. CI 가 config 리포 태그 커밋을 자동 push → Argo 가 Worker 를 롤링(maxSurge 0, maxUnavailable 1: 한 Pod 씩, 모델 로딩 1~2분, 그동안 처리 용량 절반).
3. **롤아웃이 끝난 뒤**(Worker 2/2 Ready, 새 태그) config 리포 `git pull --rebase` → 이 리포 변경(포트·PodMonitor·대시보드·규칙·문서) commit/push. 포트·PodMonitor 를 이미지보다 먼저 올리면 타깃이 DOWN 이라 5분 뒤 `PGWorkerMetricsMissing` 오탐.
4. 배포 후 확인: `up{job="parking-guardian/worker"}`=1(Pod 수만큼), `pg_worker_messages_total` 시리즈, 대시보드 13개 패널, 규칙 health=ok·Inactive, Backend 2/2·Worker 2/2·Relay 1/1, SQS/DLQ 0/0, Pod 재시작 불변.
5. 롤백: config 커밋 `git revert`(PodMonitor·규칙·대시보드는 이미지와 독립). 이미지는 `newTag` 를 직전 값으로(ECR 태그 immutable).

## 7. 예상 비용·주의

- AWS 비용 증가 없음: 새 API 호출 없음(`SentTimestamp` 는 기존 receive 응답 속성), IAM 변경 없음.
- Prometheus: Worker Pod 당 시리즈 약 200개 미만(히스토그램 버킷 포함, `process_seconds` 는 결과별 최대 6×18), 2~4 Pod → 최대 약 800개, 15초 수집 → 하루 수 MB. 현재 PVC 에 영향 없음.
- backend(API) Pod 도 같은 모듈을 import 해 `pg_worker_*`·`pg_job_e2e_*` 를 **0 으로** 낸다(Relay 지표와 같은 현상). 대시보드·알림은 합산/`max` 라 영향 없고, Worker 만 보는 패널은 `job="parking-guardian/worker"` 로 거른다.
- 한계는 2절 "측정 한계" 참고: 큐 대기는 근사, 분위수는 추정, DLQ 로 간 작업은 E2E 표본 없음, E2E 는 부수 효과·삭제 제외.
- 4단계(CloudWatch exporter, SQS `ApproximateAgeOfOldestMessage`)는 필요할 때만.

## 8. 앱 배포 확인 (2026-11-06 04:2x KST, 읽기 전용)

- 앱 커밋 `b7ac1b5`(develop, origin 과 일치). ECR(profile `parking-guardian`) backend·inference·worker 에 태그 `b7ac1b5` 확인(04:10~04:13 KST), frontend 도 같은 태그를 Pod 가 사용. CI 실행 화면은 이 PC 에 `gh` 가 없어 직접 보지 못했고, ECR 태그와 config 태그 커밋(`e6e1296`, github-actions)으로 성공을 확인했다.
- Argo `parking-guardian` Synced/Healthy 리비전 `e6e1296`. Backend 2/2, Worker 2/2, Relay 1/1, Frontend 1/1 모두 `b7ac1b5`, Pod 재시작 0.
- Worker 두 Pod 로그: `YOLOv5 models loaded in 6.0s / 4.7s`, `judge-worker ... ready: attempts=1 load_seconds=7.1/5.8 first_inference_seconds=1.10/0.92`, `metrics server listening` 각 1회. Relay 로그 오류 0. SQS 메인·DLQ 0/0, `dead_lettered` 0, firing 알림 0.
- 아직 config 3단계 변경(PodMonitor·규칙·대시보드)은 올라가지 않았다(Worker 포트는 이미지만 열려 있고 Pod 선언에는 아직 없음 — PodMonitor 가 포트 이름 `metrics` 를 쓰므로 config 적용 후 Pod 가 한 번 더 교체된다).

## 9. 실제 배포·수집 검증 결과 (2026-11-06 04:2x~04:3x KST, 읽기 전용)

**GitOps·Worker**
- config 커밋 `9f0e78b`: 로컬 HEAD = origin/main = Argo 적용 리비전(`parking-guardian`, `monitoring-config`). 둘 다 Synced/Healthy, `platform`·`root`·`keda`·`monitoring` 도 Synced/Healthy.
- PodMonitor `worker` 생성. Worker 는 포트 선언 변경으로 한 번 더 롤링되어 새 Pod 2개(`worker-6586977c94-*`), 둘 다 Ready·`b7ac1b5` 이미지, 재시작 0. 두 Pod 로그: `metrics server listening on :9102`, `ready: attempts=1 load_seconds=6.31/6.13 first_inference_seconds=1.04/1.04`, 오류·경고 0. Backend 2/2, Relay 1/1, Frontend 1/1 (`b7ac1b5`), 재시작 0. Relay 로그 오류 0.
- SQS 메인·DLQ 0/0(전 상태 0), `dead_lettered` 0. 운영 데이터·실험 결과는 건드리지 않았다.

**Prometheus**
- 타깃 `parking-guardian/worker` 2개 모두 UP(오류 없음, 스크레이프 3~6 ms).
- 존재(값 0): `pg_worker_messages_total` 9 outcome × 2 Pod = 18 시리즈, `pg_job_e2e_seconds_count`·`pg_queue_wait_seconds_count` 각 2, `pg_worker_timing_skipped_total` 8. 모델: `pg_inference_model_state{ready}`=1 (2 Pod), `pg_model_load_seconds` 5.1/5.0초, `pg_model_first_inference_seconds` 1.04초.
- **시리즈 없음 = 트래픽 없음(수집 실패 아님)**: `pg_worker_process_seconds_*`(outcome 라벨이 첫 관측에 생김)와 `pg_inference_stage_seconds_*`(stage 라벨이 첫 추론에 생김)는 이번 구간에 판정이 한 건도 없어 아직 시리즈가 없다. 같은 스크레이프에서 다른 시리즈가 나오고 `up`=1 이므로 수집은 정상이다. 수집 실패라면 `up` 이 0/없음이고 `pg_worker_messages_total` 같은 사전 노출 시리즈도 사라진다.
- 기존 이미지 롤링으로 Worker 카운터가 0 으로 초기화됐다(Pod 새로 생성).

**Grafana·알림**
- 대시보드 `AI Inference & E2E (Phase 2)`(uid `pg2-inference`) 등록, 패널 13개(설명 1 + 데이터 12), 모든 쿼리가 Prometheus 에서 success. 결과 없음(No data): 처리 시간, 추론 단계별, 판정 결과, 10초 이내 비율 — 모두 판정 표본이 아직 없어서(위 설명). 모델 상태·수집 상태·로딩 시간·건너뜀·큐 대기/E2E(시리즈 존재) 등은 데이터 있음.
- 기존 대시보드 6개 보존: Phase 2 폴더(Overview, Infrastructure, SQS & Async Pipeline) + Phase 1 참고 폴더(Phase 비교, 리소스, 추론 파이프라인).
- 규칙 `PGE2EP95High`, `PGWorkerMetricsMissing` health=ok·Inactive, firing/pending 알림 0. Alertmanager Slack receiver(`api_url_file`)·라우팅 유지, ExternalSecret `slack-webhook`·`grafana-admin` SecretSynced.

**자원 영향**: Worker 메모리 887~918 Mi / CPU 1m(이전 885~932 Mi, 변화 없음 — 모델이 대부분), Relay 97 Mi, Backend 약 90 Mi, Prometheus 429 Mi. 새 지표로 인한 눈에 띄는 증가 없음.

**남은 검증**: 실제 사진이 처리될 때의 `done`·E2E·큐 대기·처리 시간·추론 단계 값과 `PGE2EP95High` 평가는 트래픽이 있어야 확인된다(이번에는 부하·메시지 주입을 하지 않음). Slack 수신은 사용자가 직접 확인.

## 10. 정상 사진 4장 기능 검증 (2026-11-06 04:36~04:40 KST)

성능 시험이 아니라 새 지표가 실제 처리에서 정확히 기록되는지 보는 기능 검증이다. EXP2 장비 `EXP2-M-0001`(machines id 15) 1대, 허용 사진(`sample_a`) 4장(3초 간격), 사용자가 `exp2_smoke.py --photos 4 --yes-run` 으로 전송. 처리 전후 값은 `capston/docs/measurements/exp2/2026-11-06-obs3-smoke/` 의 `metrics-before.json`·`metrics-after.json`·`baseline.json`.

**처리 결과(DB 읽기 전용)**: 작업 4건 모두 `done`, 결과 4건 모두 `permitted`·`applied`, `claim_attempt` 최대 1(재시도 없음), 오류 기록 0, **Worker 두 Pod 모두 사용**(workers=2). S3 `inbox/m-15/` 에 4개(19:37:00~19:37:10 UTC), 위반·알림 불변. DB 기준 접수→결과 행 시각차 평균 1.258초(최대 1.395초).

**지표 증가량 vs 실제 4장**

| 지표 | 처리 전 | 처리 후 | 증가 | 판정 |
|---|---|---|---|---|
| `pg_worker_messages_total{outcome="done"}` | 0 | 4 | **+4** | 일치 |
| stale·retry류·already | 0 | 0 | 0 | 일치(재시도·중복 없음) |
| `pg_worker_process_seconds_count` | 시리즈 없음 | 4 | +4 | 일치(첫 관측 때 시리즈 생성) |
| `pg_job_e2e_seconds_count` | 0 | 4 | **+4** | 일치, 합 5.056초(평균 1.26초) |
| `pg_queue_wait_seconds_count` | 0 | 4 | **+4** | 일치, 합 0.022초 |
| `pg_inference_stage_seconds_count` decode·window·sticker | 시리즈 없음 | 각 4 | +4 | 일치 |
| `pg_inference_requests_total{outcome="ok"}` | 시리즈 없음 | 4 | +4 | 일치 |
| `pg_worker_timing_skipped_total` | 0 | 0 | 0 | 시각 누락·음수 없음 |
| 모델 Ready Pod / Worker 타깃 UP | 2 / 2 | 2 / 2 | - | 변화 없음 |
| SQS 메인·DLQ·`dead_lettered` | 0 | 0 | - | 정상 소화 |

중복 집계·누락 없음: 모든 계수가 정확히 4. 지표 E2E 평균(1.264초)이 DB 시각차 평균(1.258초)과 거의 같고(지표는 커밋 직후 시계라 수 ms 더 큼) 정의대로 동작함을 보여 준다. 큐 대기 합이 매우 작은 것(건당 약 5 ms)은 Worker 가 비어 있어 장기 폴링으로 즉시 받았기 때문이며, SentTimestamp 와 Pod 시계가 같은 초 안에 있다는 뜻이다(근사값, 정밀 측정이 아님).

**Grafana(`pg2-inference`)**: 13개 패널의 쿼리를 전송 구간(19:35~)에 대해 범위 조회로 확인 — 데이터 있음: E2E p50/p95/p99(추정 1.5/1.95/1.99초, 4건의 버킷 보간 값), 처리량, 오류·재시도(0), Worker 처리 시간, 큐 대기, 추론 단계별(디코드 약 0.009초, 앞유리·스티커 버킷 보간 약 0.49초), 판정 결과, 모델 Ready, 모델 로딩/첫 추론, 수집 상태, 건너뜀(0). **4건 표본이므로 성능 결론은 내지 않는다**(분위수는 버킷 경계 영향이 크다).

**발견·수정한 결함**: 패널 "10초 이내 판정 비율"이 No data 였다. `prometheus_client` 의 `le` 라벨 값은 `"10.0"` 인데 쿼리가 `le="10"` 이었다(알림 `PGE2EP95High` 은 `histogram_quantile` 이라 영향 없음). 로컬 수정: `build_dashboards.py` 와 JSON 재생성(`le="10.0"`, 실제 값 1.0 확인), 규칙 시험의 합성 시리즈 라벨도 `10.0`/`15.0` 으로 맞춤(promtool check·test 통과). **이 수정은 아직 배포되지 않았다**(config 커밋·push 필요, 이미지 변경 없음).

**마무리 검증**: Worker 2/2·Backend 2/2·Relay 1/1·Frontend 1/1, Pod 재시작 0, Worker 타깃 2개 UP, 알림 규칙 12개 health=ok·전부 Inactive·firing 0, Worker·Relay 로그 오류 0, Slack receiver(`api_url_file`)·ExternalSecret(`slack-webhook`, `grafana-admin`) 유지, 메모리 Worker 894~926 Mi(사진 처리 후에도 증가 없음). 기존 데이터 보호: `p2_protect verify`(장비 15 제외) 전 테이블 PASS, 감사로그만 +1(키 회전 1회) = 의도한 증가. 테스트 데이터 증가는 EXP2 장비 1대의 작업·결과 4건, S3 inbox 4개, 감사로그 1건, 로그인 토큰 1건뿐. 공식 스냅샷·원본 실험 결과는 건드리지 않았다.

**한계**: ① 사진 4장·정상 경로만 확인(실패·재시도·`effects_retry`·음수 시각은 단위 테스트로만 검증, 클러스터에서 장애 주입은 하지 않음). ② 분위수는 버킷 보간 추정, 큐 대기는 AWS·Pod 시계 근사. ③ `PGE2EP95High` 의 실제 평가는 표본 5건 미만이라 발동하지 않는 것이 정상이며 실제 경보 발생은 확인하지 못했다. ④ Slack 수신은 사용자가 직접 확인.

**결론**: 관측성 3단계 계측·수집·시각화·알림 규칙은 실제 배포에서 정상 동작을 확인했다. 남은 것은 위 대시보드 수정 1건의 배포.
