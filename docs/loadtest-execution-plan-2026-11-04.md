# Phase 1 ↔ Phase 2 부하 테스트 실행 계획 (2026-11-04, 아직 실행하지 않았다)

**계획이다. 부하 테스트·사진 접수·SQS 발행·클러스터 변경을 하지 않았고, 아래 숫자 중 시험 결과는 없다.** [loadtest-plan-phase1-vs-phase2.md](loadtest-plan-phase1-vs-phase2.md)(가설·지표·시나리오)를 실행 가능한 절차로 구체화한다.
표기: **[관찰]** 기록·코드·클러스터에서 읽은 값, **[계산]** 관찰값으로 계산, **[추정]** 근거 있는 해석(미확인), **[미확인]**. 시험 도구·결과는 **로컬 전용**(앱 리포 `loadtest/`, Git 에 올리지 않는 규칙 [사용자 결정 2026-11-01]).
이 문서는 공개 config 저장소에 있으므로 비밀·계정 ID·IP 를 적지 않는다. 전제 문서: [ops-risks-2026-11-04.md](ops-risks-2026-11-04.md)(R3·R6·R7), [cost-and-teardown-plan-2026-11-04.md](cost-and-teardown-plan-2026-11-04.md).

## 0. 비교할 대상과 결정해야 할 것
- Phase 1: `POST /device/detections` — 접수 요청 안에서 inference(HTTP)가 판정까지 끝낸다(과부하면 503). Phase 2: `POST /device/detection-jobs` — S3 저장 + DB 작업 행 → Relay → SQS FIFO → Worker 가 판정(접수는 202).
- **사전 등록(실행 전 고정)**: 가설 H1~H5, 통과 기준(일반 API p95 < 500 ms, 오류율 < 1 %), 무효 조건, 아래 4·5절의 비교 지표 정의를 시험 전에 커밋(로컬)하고, 결과를 본 뒤 바꾼 것은 변경 기록에 남긴다(Phase 1 방식).
- 사용자가 정할 것: ①1일 연속 vs 2회 분리(8절), ②노드 +1 여부(5절), ③시험 데이터 보존 방침(6절), ④장애 주입 시나리오 P2-F 수행 여부, ⑤시험 전 RDS 스냅샷 여부.

## 1. 공정한 비교 조건
| 항목 | 고정할 값 | 이유·주의 |
|---|---|---|
| 빌드 | 둘 다 현재 빌드 `60c2e43` 에서 측정 | 기존 C = 3.107 장/초는 빌드 `c14a7f0` 의 값 [관찰]. 그 뒤 코드·애드온이 바뀌었으니 **다시 잰다** |
| AI 자원 | Phase 1 inference **2 Pod**(각 CPU 요청 1000m) ↔ Phase 2 Worker **2 Pod**(1000m) | 같은 CPU 요청 4000m 의 비교 A. 공존 구성에서는 Worker 가 1개만 들어가 같은 자원이 안 된다(4절) |
| 클러스터 | 같은 EKS(노드 3 × `m7i-flex.large`), 같은 RDS(`db.t4g.micro`), 같은 날·같은 세션 | 환경 편차(노드, RDS 크레딧) 최소화. Phase 1 → Phase 2 → (선택) Phase 1 짧은 재확인으로 드리프트 확인 |
| 장비 | **시험 전용 장비 3대**(회사 3, 주차장 6) — Phase 1 재측정도 같은 장비 | 데모 장비는 건드리지 않는다. 장비 수 3 = FIFO 그룹 수 = Phase 2 병렬도의 상한 |
| 사진 | `sample_a`(허가 판정, 369,857 B) 100 %, 위반 0 % | Phase 1 사전 등록 조건. 위반·알림·증거 사진 쓰기를 만들지 않는다. 위반 경로는 별도 소량 V0 |
| 도착률 | C 의 배수 0.2·0.5·0.8·1.0·1.2·1.5 × 3 분(S1), 일반 API 5 req/s 동시 | Phase 1 프로필 그대로. 같은 절대 도착률을 양쪽에 적용 |
| 부하 발생 | 같은 PC(Wi-Fi) → ALB(`https://APP_HOST`) | 네트워크 요인을 같게. PC 의 공인 IP 가 ALB 허용 IP 이므로 통과(보안 그룹 변경 불필요) |
| 자동 조정 | Argo 자동 동기화 중단, HPA 고정, Worker 수 고정(KEDA 일시정지) — 고정 실험과 KEDA 자동 실험(P2-K)을 구분 | 측정 중 구성이 바뀌지 않게 |
| 캐시 | 이미지·모델을 모든 노드에 미리 올린 상태에서 정상 상태를 재고, 콜드 스타트는 따로(9절) | 첫 E2E 의 이미지 pull 이 약 21 s 를 더했다 [관찰] |
| 반복 | 시나리오당 3회, 유효 기준(k6 `dropped_iterations` 0, k6 CPU < 70 %, 단계 중 Pod 재시작·HPA 변화 없음) 위반은 무효로 기록 후 재실행 | Phase 1 규칙. 무효 회차도 폴더에 남긴다 |

**비교 가능하도록 정의를 맞추는 지표 (사전 등록 대상)**
- `photo_ok` / `photo_ms` 는 의미가 다르다(Phase 1 = 판정까지 끝난 응답, Phase 2 = 접수) → 단독 비교 금지 [관찰: 앱 리포 `loadtest/README.md` 2.3절].
- **접수→판정 지연(End-to-End)**: Phase 2 = `detection_jobs.received_at` → `detection_results.created_at`(DB 시각). Phase 1 = 요청 응답 시간(판정 포함)과 같은 의미.
- **제시간 판정률(on-time)**: 접수된 시도 중 접수 후 **10 s 안에 판정이 끝난 비율**. Phase 1 은 마감(10 s) 안에 200 이 온 비율(503 = 실패), Phase 2 는 지연 ≤ 10 s 인 결과 비율. 10 s 는 Phase 1 의 마감 값에서 가져온다 [관찰: 앱 리포 `loadtest/README.md` 3절 S5(b) "마감(10초) 안 503"]. 이것이 두 구조를 한 숫자로 비교하는 주 지표다. 이 값(10 s)도 실행 전에 고정한다.
- 장비별 순차 전송 차이: Phase 2 는 장비당 `seq` 순서가 필요해(`last_applied_seq` 순서 보장 [코드 확인]) k6 가 **장비당 1 VU 로 직렬 전송**한다. Phase 1 은 독립 요청이었다. 이 차이와 1.5C 에서 장비당 필요 도착률(약 1.55 장/초)이 직렬 전송 한계를 넘을 수 있다는 점을 결과에 적는다(7절).

## 2. 현재 클러스터에서 Phase 1 재측정 (B0, S0, S1)
**현재 상태 [관찰]**: 공존 구성(inference 2 + backend 2 + frontend + relay, Worker 0/0, KEDA min 0/max 1), 빌드 `60c2e43`. Phase 1 경로(`/device/detections`)는 inference 가 살아 있어 그대로 동작한다. ALB 는 내 IP 만 허용, DNS 연결됨.

선행 작업(각각 별도 승인):
1. 시험 전용 장비 3대 생성 — SUPER_ADMIN 공식 API 로 회사 3 / 주차장 6 에 장비 2대를 더하고(지금 `RPI-E2E-TEST-01` 1대), 키는 `rotate-key` 로 받아 사용자 터미널의 환경변수로만 쓴다(파일·출력 금지, [test-device-procedure.md](test-device-procedure.md)). 데모 장비 키는 주지 않는다.
2. `p3.py` 갱신 — 앱 리포 `loadtest/tools/p3.py` 의 복구 검사가 "Application **5개**" 를 기대한다 [관찰: 사용법 문서]. 지금은 6개(KEDA 추가)라서 그대로 쓰면 복구 검증이 실패한다. 로컬 도구 수정 + 로컬 시험.
3. 이미지 캐시 워밍(9절), 기준선 스냅샷(`snapshot_e2e.py` 방식 해시, 데모 회사 데이터 불변 확인), 모니터 관찰자 시작(3절).

절차(요약):
| 단계 | 작업 | 승인 |
|---|---|---|
| a | `p3.py snapshot` → `pause --apply`(Argo 자동 동기화 중단, backend·inference HPA min=max=2) | 필요 |
| b | **B0**: inference 단독 E3(Pod 2, VUS = Pod × 2 = 4, 클러스터 안 k6 Job) × 3 → C 재산정. E0(Pod 1)는 Worker 와의 **1 Pod 대 1 Worker** 비교가 필요할 때만(HPA 1 고정 절차 별도) | 필요 |
| c | **S0** × 3 (일반 API 5 req/s, 300 s) | 필요 |
| d | **S1** × 3 (S0 + 사진 0.2C…1.5C × 3 분, 18 분 + 워밍) | 필요 |
| e | (선택) V0: 위반 소량 `ALLOW_VIOLATIONS=1 VIOLATION_RATIO=…` — 시험 회사 안, 별도 승인 | 필요 |
| f | **Phase 2 로 넘어가기 전에** 결과·기준선 비교 저장 | — |
각 회차 사이에 3 분 이상 쉬고, 시작 때 inference `ready` 를 확인한다. 한 회차 15 분 이내(S1 은 19 분 + 사전 정리).

## 3. 관찰자와 중단 규칙 (모든 시험에 공통)
Worker·Relay 에는 **앱 지표가 없다**(`app/metrics.py` 는 HTTP·inference 지표만 [관찰: 코드]). 프로브도 없고 CloudWatch 경보·SNS 도 없다(예산 알림 이메일만 [관찰: Terraform 검색]). 그래서 읽기 전용 관찰자를 시험과 함께 돌린다.
- 15 초마다: Worker·Relay Pod 수·Ready·재시작, KEDA ScaledObject Ready/Active, `kubectl top`(Pod·노드), SQS 가시/처리 중/지연 메시지(`get-queue-attributes`), DLQ 건수, DB 의 "미완료 작업 중 가장 오래된 `received_at` 나이"와 "마지막 결과 `created_at` 나이".
- **중단 규칙**: Worker ≥ 1 인데 60 s 동안 새 결과 0 이고 가장 오래된 미완료 나이가 120 s 이상 → 정지로 보고 시험을 멈추고 기록(R3). DLQ 가 늘면 즉시 기록. 노드 CPU steal 지속, Pod 재시작, Argo `OutOfSync/Degraded` 도 무효 사유.
- 읽기 전용만 한다(SELECT, `kubectl get/top/logs`, CloudWatch 조회). 이 도구는 `ui_state.py`·`observe_e2e.sh` 와 같은 방식으로 작성한다(비밀 컬럼 미조회).

## 4. Phase 2 비교 구성을 적용하는 방법
[phase2.md 4절](phase2.md) 의 **비교 구성**: inference 제거, Worker min 1 / max 3. 적용 방법 두 가지:
| 방법 | 절차 | 장점 | 단점·위험 |
|---|---|---|---|
| ①(설계된 방식) | `comparison-mode.example.yaml` 의 `patches:` 블록을 `kustomization.yaml` 끝에 붙이고 `check-phase2-render.py` 통과 → **config 리포 커밋·푸시(공개 저장소, 사용자 직접)** → Argo 동기화 | Git 이 구성의 단일 기록, 되돌리기 쉬움(블록 삭제) | 커밋 2번(적용·복원). 동기화가 마이그레이션 Job 을 만든다 → **R6 확인 필요**(회전 뒤 갱신 안 된 Secret 이면 Job 실패) |
| ②(Git 변경 없음) | Argo 자동 동기화 중단 상태에서 `kubectl scale`/`delete hpa inference` 로 inference 제거 | 커밋 없음 | HPA 가 되살림(최소 1, 삭제 필요), 복원 때 누락 위험, 기록이 Git 에 없다 |
권고: ①. Worker 수 **고정**은 ScaledObject 의 `autoscaling.keda.sh/paused-replicas: "N"` 어노테이션으로 하고(`kubectl annotate`, Argo 자동 동기화 중단 상태에서만 유지됨), **자동(KEDA)** 실험(P2-K)은 어노테이션을 지운 min 1 / max 3 상태로 한다.
`ScaledObject` 는 `worker` Deployment 의 `replicas` 를 Application 이 무시하도록 이미 구성되어 있다(Argo 가 되돌리지 않음 [관찰: 11-04 E2E]).

## 5. 공존에서의 CPU 제약과 추가 노드 없이 가능한 Worker 수
**CPU 요청 [관찰, 11-04, Worker 0, 노드당 할당 가능 1930m]**: node-19 190m 여유, node-23 275m, node-7 1090m(합 1555m). 1000m 자리는 node-7 한 곳뿐이다 [ops-risks R7].
| 구성 | 1000m 자리 | 들어가는 Worker 수 | 비고 |
|---|---|---|---|
| 공존(inference 2 유지) | 1 (node-7) | **1** | Worker 2번째·inference 롤링·3번째 inference 는 Pending [계산] |
| **비교 구성**(inference 제거) | 3 (여유 1190/1275/1090m) | **3** (노드당 1) | Worker 3 일 때 남는 여유 190/275/90m: backend 롤링 surge(200m)는 node-23 에만, 마이그레이션 Job(100m)은 node-19·23 에 들어간다. 배포(동기화)는 Worker ≤ 2 에서 한다 |
| 노드 +1(`max_size` 3 → 4) | +2 vCPU | 공존에서도 Worker 2~3, inference 롤링 가능 | 약 +$0.118/h. Terraform 변경(별도 승인). 새 노드는 이미지 캐시가 없어 첫 기동에 pull 이 든다 |
- **추가 노드 없이 가능한 최대 Worker = 3**, 그리고 **의미 있는 최대도 3** — 큐가 FIFO(그룹 = 장비)라 장비가 3 대면 동시 처리는 최대 3 이다(Worker 4 개는 이득 없음) [코드 확인 + 설계]. 비교 구성에서 `max` 를 3 으로 두는 이유다.
- 실제 사용 CPU 는 노드당 5~7 % 였고 Worker 컨테이너는 최대 710m(표본) [관찰: 11-04]. 같은 노드의 backend·DaemonSet 이 Worker 와 CPU 를 다툰다. Phase 1 은 inference 두 Pod 가 CPU 1.0 에 포화했다 [관찰]. **노드 CPU(steal 포함)를 모든 회차에 기록**하고, 값이 다르면 구조 차이가 아니라 자원 차이임을 결과에 적는다.
- inference·Worker 의 requests 는 낮추지 않는다(비교가 흐려진다) [ops-risks R7 ③].

## 6. 부하 테스트용 장비·데이터 준비
- 장비 3대: 일련번호 `RPI-LOAD-01..03`(안), 회사 3, 주차장 6, 키는 `rotate-key` 로 발급, 부하 PC 의 환경변수 `DEVICE_KEYS`(쉼표 구분)로만 전달. 파일·로그·메모리 문서에 적지 않는다.
- 세션: 장비의 첫 사진이 새 세션을 등록한다(`session_id` 는 클라이언트가 만든 UUID) [코드 확인: `ingest.py`]. `event_id` 는 사진마다 유일해야 하고 `seq` 는 세션 안에서 증가해야 한다.
- 사진: `sample_a`(허가). 위반 사진은 보내지 않는다.
- **시험 데이터가 남기는 것 [계산]**: S1 1회 = 사진 5.2 × 3.107 × 180 s ≈ **2,908 장**(약 1.08 GB). Phase 2 는 사진마다 S3 `inbox/` 에 저장(약 370 KB)하고 작업·결과 행 2개를 만든다. Phase 2 S1 × 3 이면 사진 약 8,700 장·행 약 17,000 개·S3 약 3.2 GB. `inbox/` 는 **7일 뒤 자동 삭제**(수명 주기 규칙 [관찰: Terraform]). DB 행은 자동 삭제되지 않는다 — 시험 데이터를 지우지 않는 규칙과 정리 방침을 정해야 한다(**사용자 결정**). RDS 20 GB 는 충분하다 [계산].
- 업로드 대역폭 [계산]: 1.0C ≈ 9 Mbps, 1.5C ≈ 14 Mbps. PC 의 Wi-Fi 업로드 한계가 다르면 k6 가 `dropped_iterations` 를 내므로 무효 판정 규칙이 작동한다. 시작 전 업로드 속도를 기록한다(Phase 1 P0b 방식).
- 데이터 보호: 매 회차 전후 기준선 해시 비교(데모 회사 1·2 와 회사 3 의 기존 행 불변), 시험 장비 행만 변경 허용.

## 7. k6 스크립트 구현 범위 (로컬 전용, 아직 작성하지 않았다)
`loadtest/k6/scenarios_p2.js`(안) — Phase 1 의 `scenarios.js`·`lib.js` 를 재사용한다.
- 포함: ①장비당 1 VU 직렬 전송 + 목표 간격 유지(간격을 못 지키면 `dropped` 로 기록해 무효), ②`POST /api/v1/device/detection-jobs` 멀티파트(`event`, `event_id`, `session_id`, `seq`, 이미지), `X-Device-Key` 헤더, ③응답 202 와 상태별 개수(`accept_2xx/_4xx/_5xx`), 접수 시간 `accept_ms`, ④Phase 1 과 같은 관리자 API 부하(`api_ms`·`api_ok`), ⑤단계 태그(`{step}`), 워밍업(`setup()`), ⑥`SCENARIO` 환경변수: S0, S1(Phase 2 경로), W(포화), D(1.5C 60 s 후 중단), C(유휴 후 일괄 도착), ⑦종료 시 `handleSummary` 로 JSON 저장, ⑧비밀은 `-e` 로만.
- 제외(별도 도구): 접수→판정 지연·처리량·큐 나이(DB·CloudWatch 로 수집하는 `p2_collect.py`), KEDA·Pod 타임라인(관찰자), RDS 지표(CloudWatch).
- 확인할 것 [미확인]: 같은 장비가 `arrived` 사진을 연속해서 보낼 때의 상태 전이(허가 판정이 세션/카운터에 미치는 영향). 먼저 **로컬 시험(Docker Compose 가 가능한지부터 확인)** 이나 AWS 에서 사진 10장 정도의 스모크로 확인한다(후자는 별도 승인).
- 한계 [계산]: 장비 1 대의 처리는 한 번에 1건(FIFO 그룹)이고 추론이 약 0.7 s [관찰: E2E 715·688 ms] 이므로 장비당 최대 약 1.4 장/초, 3 대면 약 4.3 장/초 안팎이다. 1.5C(4.66 장/초)는 Worker 를 늘려도 이 상한을 넘으므로 **큐가 쌓이는 것이 정상 결과**일 수 있다(H1 을 검증하는 단계). 결과에 "Worker 부족"과 "장비 직렬화 한계"를 구분해 적는다.

## 8. 시나리오·실행 순서·시간
| 번호 | 내용 | 경로 | 회차 | 한 회차 [계산] |
|---|---|---|---|---|
| B0 | E3(inference 2 Pod) C 재산정 | 1 | 3 | 3 분 + 휴식 |
| S0 | 일반 API 만 | 1·2 | 3 | 5 분 + 휴식 |
| S1 | 혼합 부하 6 단계 | 1 | 3 | 19 분 + 휴식 |
| P2-S1 | 같은 프로필, Worker 2 고정 | 2 | 3 | 19 분 + 휴식 |
| P2-W | Worker 1·2·3 고정에서 포화 처리량 | 2 | 각 3 | 6 분 + 휴식 |
| P2-K | KEDA 자동(min 1 / max 3) 상승·하강 | 2 | 3 | 약 20 분 |
| P2-C | 유휴(Worker 0, 쿨다운 후) 일괄 도착 — 콜드 스타트 | 2 | 3(캐시 있음/없음 각각 구분) | 약 15 분 |
| P2-D | 1.5C 60 s 후 중단, 큐 소화 시간 | 2 | 3 | 약 10 분 |
| P2-F | 부하 중 Worker 1개 종료(장애 주입) — **별도 승인** | 2 | 2 | 약 15 분 |
| V0 | 위반 경로 소량 | 1·2 | 1 | 약 10 분 |
**순서**: 선행 작업 → Phase 1(B0 → S0 → S1) → 구성 전환(4절) → Phase 2(S0 → P2-S1 → P2-W → P2-D → P2-K → P2-C → P2-F) → (선택) Phase 1 S1 한 번 더(드리프트 확인은 되돌린 뒤) → 복원(10절).
**소요 [계산]**: Phase 1 재측정 약 2.5~3 h, Phase 2 약 5~7 h, 합쳐서 한 세션이면 9~11 h. 한 사람이 감시하며 하기엔 길다. **2회 분리 권고**: 1일차 = 선행 작업 + Phase 1 재측정 + 구성 전환 시험, 2일차 = Phase 2 + Phase 1 S1 1회(같은 날 드리프트 대조). 분리하면 클러스터를 중간에 내리는 편이 싸지만 이미지 캐시가 사라져 워밍을 다시 해야 하고 "같은 날·같은 클러스터" 규칙이 약해진다 — 비용은 [cost-and-teardown-plan-2026-11-04.md](cost-and-teardown-plan-2026-11-04.md).

## 9. 측정 항목과 콜드/워밍 분리
| 지표 | 계산·출처 |
|---|---|
| 처리량 | 구간(단계)별 완료 결과 수 / 초 — `detection_results.created_at` 집계(DB). Phase 1 은 k6 의 200 응답 수 |
| 지연 p50 / p95 / p99 | 접수→판정 지연(DB `percentile_cont`), 접수 시간은 k6 `accept_ms`. **p99 는 표본이 충분한 단계에서만**: 단계별 사진 수 = 도착률 × 180 s ≈ 112(0.2C), 280, 447, 559(1.0C), 671, 839(1.5C) [계산]. n < 300 인 단계는 p95 까지만 보고하고 p99 는 "표본 부족"으로 표기 |
| 오류율·거절 | k6 상태별 개수, Phase 1 `busy`/`bulkhead_full`(서버 지표 `pg_ai_client_requests_total`), Phase 2 `rejected`·`dead_lettered`·`stale`(DB), DLQ 건수(SQS) |
| 큐 상태 | SQS CloudWatch(1 분): visible/not visible/age. **age 는 CloudWatch 지표만** 제공 |
| CPU·메모리 | `kubectl top` 10 s 표본(Pod·노드) + Container Insights(CloudWatch, 수집 중 [관찰]). Prometheus 가 cAdvisor 를 수집하는지는 **[미확인]**(Phase 1 에서는 사용 [관찰: README 5절], 지금 클러스터에서 확인할 것) |
| DB | RDS CloudWatch: CPU, CPU 크레딧, 연결 수, IOPS |
| 확장 | KEDA 활성화·Pod 생성·Ready 시각(관찰자 타임라인) |

**워밍/콜드 분리**
- **워밍 상태**: Worker Pod 가 Ready 이고 모델 preload 가 끝난 뒤 ≥ 5건 처리, 이미지는 모든 노드에 존재. Phase 1 inference 는 시작 시 워밍업 5건을 `phase:warmup` 으로 태그해 제외한다.
- **이미지 워밍**: 시험 전 Worker 를 3개로 올려 세 노드에 이미지를 받고(`paused-replicas`), 다시 내린다. 이후 pull 은 `already present` [관찰: 11-04 E2E 의 두 번째 실행].
- **콜드 스타트(P2-C)**: 0→1 지연을 따로 잰다. 기준값 [관찰, 각 1회]: 이미지 캐시 있음 24.7 s(접수→첫 판정), 없음 44.8 s. 구성: KEDA 반응(폴링 15 s 내 우연) + Pod·init(모델 약 412 MB 다운로드 4 s) + 모델 로딩·워밍업 약 9.5 s + 추론. 정상 상태 지연 통계에 콜드 구간의 사진을 **섞지 않는다**(단계 태그와 Worker Pod 나이로 분리).
- Phase 1 의 콜드 비교: inference 는 상시 떠 있으므로 콜드가 없다. "Phase 2 min 0 은 첫 사진이 콜드 스타트를 치른다"는 사실만 보고한다.

## 10. 시험 후 원래 구성으로 복원
1. 어노테이션 제거: `kubectl -n parking-guardian annotate scaledobject worker autoscaling.keda.sh/paused-replicas-`.
2. 비교 구성 되돌리기: 붙였던 `patches:` 블록을 지우고 커밋·푸시(사용자) → Argo 동기화. inference 롤링은 1000m 자리가 필요하므로 **Worker 0 이고 큐가 빈 뒤에** 한다 [R7].
3. `p3.py restore --snap <스냅샷> --apply`: Argo 자동 동기화 복구, HPA 2/3 복구, 관찰 단계 통과(Application 개수 기대값 갱신 후).
4. 확인: Argo 6개 Synced/Healthy, backend 2/2·inference 2/2·frontend 1/1·relay 1/1·Worker 0/0, KEDA Ready=True, SQS·DLQ 0, ALB 대상 healthy, RDS available, 데모 회사 데이터 해시 불변.
5. 시험 장비 처리: 유지(키는 다음 `rotate-key` 때까지 유효) 또는 키 회전/비활성화 — 사용자 결정. 시험 행은 지우지 않는다(규칙).
6. 결과 보존은 [cost-and-teardown-plan-2026-11-04.md 4절](cost-and-teardown-plan-2026-11-04.md).

## 11. 실행 전 승인이 필요한 작업 요약
장비 2대 추가, `p3.py` 로컬 수정, k6 P2 스크립트·수집 도구 작성(로컬), 사진 10장 스모크, Phase 1 재측정 구성 고정(`p3 pause`), 비교 구성 커밋(공개 config 저장소), 부하 실행, 장애 주입(P2-F), 노드 +1(선택), 복원, 클러스터 정리. 각각 별도 승인이며 이 문서는 어떤 것도 실행하지 않았다.
