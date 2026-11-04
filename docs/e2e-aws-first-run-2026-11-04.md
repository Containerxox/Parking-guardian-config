# Phase 2 첫 AWS 정상 경로 E2E (2026-11-04)

**이 문서의 수치는 사진 1장, 1회 실행의 관찰값이다.** 평균·분포·처리량이 아니며 다른 환경·부하에 그대로 쓰면 안 된다. 표기: **[실측]** 로그·API·DB 에서 읽은 값,
**[계산]** 실측값으로 계산한 값, **[추정]** 근거가 있는 짐작이지만 확인하지 않은 것.

> 후속: 차량 사진 2장으로 위반 2회 확정까지 확인한 [위반 확정 E2E](e2e-aws-violation-confirm-2026-11-04.md)가 있다. **두 문서의 시간을 직접 비교하지 않는다**:
> 이 실행(05:16)은 Worker·aws-cli 이미지가 노드에 없어 pull 이 들어갔고(15.8 s + 5.4 s), 후속 실행(07:06)은 두 이미지가 이미 캐시돼 pull 이 없었다. 입력 사진도 다르다.

## 범위와 환경
- 확인한 것: 접수 API(202) → DB 작업 → Relay 의 SQS FIFO 발행 → KEDA 0→1 → Worker 스케줄링·모델 준비 → YOLO 판정·결과 저장 → 메시지 삭제 → KEDA 1→0.
- 환경: EKS 1.35, 노드 3대(`m7i-flex.large`), **Phase 1 과 Phase 2 공존 구성**(backend 2 + inference 2 + frontend + relay 가 떠 있고 Worker 는 min 0 / max 1),
  KEDA 차트 2.21.0, 이미지 태그 `60c2e43`, CloudWatch Agent CPU 요청 100m(250m 에서 낮춤).
- 요청: backend Pod 안에서 `127.0.0.1:5000` 으로 보냈다(ALB·Ingress·DNS 를 거치지 않았다). 사진 `sample_f.jpg`(9,614,622 바이트, 차량 없는 빈 공간), `event=arrived`, `seq=1`.
- 확인하지 않은 것: 차량 탐지, 위반 확인 2회, 위반 생성, 알림, 증거 사진 복사, ALB 경유 접수, 동시 요청, 처리량, 오류 경로(DLQ·재시도).

## 단계별 시간 (UTC, 2026-11-04)

| 단계 | 시각 | 값 | 구분 |
|---|---|---|---|
| API 접수 → `202 queued` | 05:16:58 | 응답 0.73초 | [실측] (클라이언트 측) |
| DB 수신 → SQS 발행 완료 | 05:16:58.84 → 05:16:59.46 | 0.63초 (`received_at` → `published_at`, `publish_attempts` 1) | [실측] |
| KEDA 0→1 | 05:17:00 | 발행 약 1초 뒤 (KEDA 로그, 초 단위) | [실측] |
| Pod 생성·스케줄 | 05:17:00 | 노드 `7-63` 에 배치, 지연 없음 | [실측] |
| init 이미지(`aws-cli`) Pull | 05:17:01~07 | 5.4초 | [실측] (kubelet 이벤트) |
| 모델 다운로드(init `fetch-models`) | 05:17:07 → 05:17:11 | 4초 (S3 → emptyDir, 약 412 MB) | [실측] (init 로그) |
| Worker 이미지 Pull | 05:17:14~30 | 15.8초 (ECR, 압축 약 630 MB) | [실측] (kubelet 이벤트) |
| Worker 컨테이너 시작 | 05:17:31 | Pod 생성 후 31초 | [실측] |
| YOLO 두 모델 로딩 | ~05:17:40 | 6.1초 | [실측] (`YOLOv5 models loaded in 6.1s`) |
| preload 합계 | 05:17:42 | `load_seconds=7.253`, 워밍업 추론 1.102초·1.086초 | [실측] (Worker 로그) |
| 실제 판정 추론 | 05:17:43 | 408 ms (`detection_results.elapsed_ms`) | [실측] |
| 결과 저장·작업 `done` | 05:17:43.68 | 접수 후 **약 44.8초** | [계산] (콜드 스타트 포함) |
| 큐 비어 있음 확인 | 05:18:04 | 메인 0, 처리 중 0, DLQ 0 (5~15초 간격 폴링이라 이 시각 이전에 비었다) | [실측] |
| KEDA 1→0 | 05:22:30 | 큐가 빈 뒤 약 4.5분 | [실측] (KEDA 로그) |
| Worker 종료 | ~05:22:46 | `handled 1 message(s) (stopped by signal)` 로그, 오류·재시작 없이 `Succeeded` | [실측] |

- **KEDA 0→1 이 접수 1초 뒤였던 것은 폴링 주기(15초)가 우연히 맞은 결과일 수 있다.** 일반적으로는 최대 15초 정도 지연될 것으로 본다 [추정].
- 축소까지 약 4.5분은 `cooldownPeriod: 300` 이 마지막 활성 시각부터 센다고 보면 맞는 값이다 [추정].
- Worker 이미지 Pull 이 15.8초였던 것은 이 노드에서 Worker 가 처음 뜬 경우(캐시 없음)로 보인다 [추정]. 다른 노드·다른 시점의 값은 모른다.
- 콜드 스타트 구간(Pod 생성 → 판정 준비 완료 05:17:00 → 05:17:42 약 42초)이 전체 지연의 대부분이다 [계산]. Worker 를 계속 1개로 두는 구성(`min 1`)에서는 이 구간이 사라진다 [추정, 미측정].

## 자원 [실측, 한 시점 표본]
- Worker Pod: 메모리 893Mi, CPU 1m — 판정이 끝난 뒤(05:19:59) 한 번 잰 값이다. **추론 중 최대치가 아니다.** 요청 1000m / 1.5Gi, 상한 메모리 2Gi.
- Worker 가 떠 있는 동안 노드 `7-63`: CPU 요청 1840m(할당 가능 1930m 의 95%), 사용 약 125m / 메모리 약 48%. 다른 Pod 의 Pending 은 없었다.
- CloudWatch Agent: 12~15m(한 Pod 는 메모리 182Mi). 로그·지표 수집은 계속 정상이었다.

## KEDA 와 Argo CD
- ScaledObject `worker`: Ready=True, 활성화 시 Active=True, Fallback=False. 0→1 직전 Pod 가 만들어지는 몇 초 동안 Active 가 `Unknown` 으로 보였다 [실측].
- KEDA operator 에 인증 오류 0건(SQS `GetQueueAttributes` 가 Pod Identity 로 동작) [실측].
- Argo CD: 앱 6개가 실행 내내 Synced/Healthy. 이 사이 수동 Sync 는 없었다.

## 만들어진 데이터 (삭제하지 않았다)
| 대상 | 내용 |
|---|---|
| `device_sessions` | 1건 `e2e00000-0000-4000-8000-000000001104` (active) |
| `detection_jobs` | 1건 `8ac1e58b-8cd1-460a-b7f6-4fa6ca1514c7` (`e2e00000-0000-4000-8000-000000011041`, done, claim 1회) |
| `detection_results` | 1건 (`no_vehicle`, applied, 모델 `yolo`, 408 ms) |
| `machines` (RPI-A-SUWON-01) | `current_session_id`, `last_applied_seq=1`, 마지막 신호 시각 갱신 |
| S3 inbox | 사진 1장 (수명주기 7일) |
| 위반·알림 | 새로 생기지 않음 (`violations` 4, `notifications` 8 그대로) |

**예상 밖의 부작용**: 이 장비에는 Phase 1 시험 때 열려 있던 **위반 #4 의 사건**이 있었다. 장비의 마지막 신호가 약 6,656분(약 4.6일) 전이라 첫 신호가 처리되면서 앱 규칙
(`EPISODE_GAP_MINUTES=10` 을 넘긴 공백 → 열린 사건을 `SIGNAL_LOST` 로 종료)에 따라 #4 의 `ended_at`·`end_reason`·`updated_at` 이 바뀌고 `violation_history` 가 13건에서 14건이 됐다.
위반의 상태(`NOTIFIED`)와 알림은 그대로다. 앱의 정상 동작이며 되돌리지 않았다. 원인은 시험 장비를 고를 때 열린 사건을 확인하지 않은 것이다 → [test-device-procedure.md](test-device-procedure.md).

## 이 실행에서 겪은 일 (배포 문제, 시험 결과가 아님)
- 첫 배포의 worker Deployment 가 기본 replicas 1 로 만들어져 Pending → ScaledObject 가 만들어지지 않는 교착: [phase2.md 8.8](phase2.md). 로컬 수정은 했고 클러스터에는 아직 반영하지 않았다.
- RDS 비밀번호 회전으로 마이그레이션 Job 이 인증 실패: [runbook-rds-rotation.md](runbook-rds-rotation.md).
- 접수 경로는 `/api/v1/device/detection-jobs` 다(`/device/...` 로 보내면 라우팅 404 이고 큰 본문 전송 중 연결이 끊겨 `Broken pipe` 로 보인다).
- Fluent Bit 이 `cloudwatch-agent.amazon-cloudwatch:4311` 을 찾지 못한다는 경고를 1분마다 낸다. 로그·지표 수집은 계속 됐고 원인은 확인하지 못했다.

## 이 결과로 말할 수 없는 것
- 처리량, 지연 분포, 동시 접수, 차량이 있는 사진의 추론 시간·정확도, 위반 생성과 알림, Worker 2개 이상의 확장, `min 1` 구성의 지연, 오류·재시도·DLQ 경로.
- 부하 테스트·비교 측정은 별도로 계획한다([phase2.md 4절](phase2.md)).
