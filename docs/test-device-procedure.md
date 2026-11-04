# 테스트 장비 사용 절차 (Phase 2 접수 테스트)

접수 API(`POST /api/v1/device/detection-jobs`)로 테스트 사진을 보내면 DB 에 **실제로 기록이 남고**, 같은 장비의 기존 상태(열린 위반 사건, 카운터, 세션)가 바뀔 수 있다.
이 문서는 보내기 **전에** 무엇을 확인하고, 무엇이 바뀔 수 있으며, 어떤 경우에 멈출지를 정한다. 결과를 지우거나 되돌리는 절차는 없다(테스트 데이터는 삭제하지 않는다).

## 왜 필요한가 (2026-11-04 사례)
`RPI-A-SUWON-01` 로 사진 1장을 보냈더니, Phase 1 시험 때 열려 있던 **위반 #4 의 사건**이 `SIGNAL_LOST` 로 종료되고 `violation_history` 가 1건 늘었다.
앱 규칙: 장비가 사진도 하트비트도 `EPISODE_GAP_MINUTES`(기본 10분)보다 오래 보내지 않다가 새 신호를 보내면, 열려 있던 사건을 `SIGNAL_LOST` 로 닫고 카운터를 초기화한다
(앱 리포 `capston/backend/app/services/detections.py` 의 규칙 4). 사전 점검에서 "열린 사건이 있는가" 를 보지 않았다. 이 일은 되돌리지 않았다.

## 1. 어떤 장비를 쓰나
- 현재 DB 에는 데모 장비 5대(`flask seed`)뿐이고 **시험 전용 장비는 없다.** 그래서 "기존 운영 데이터와 분리" 는 아래 둘 중 하나로 한다.
  - **(권장 임시) 열린 사건이 없는 데모 장비**를 쓴다. 2026-11-04 점검 기준: `RPI-A-PANGYO-01`, `RPI-B-HQ-01`, `RPI-B-LOGIS-01` 은 열린 사건 0건(판정 GO),
    `RPI-A-GANGNAM-01` 은 열린 사건 #3 이 있어 **주의**, `RPI-A-SUWON-01` 은 이미 시험 세션이 있다. 매번 아래 2절로 다시 점검한다(상태는 바뀐다).
  - **(근본 해결) 시험 전용 회사·주차장·장비를 하나 만든다.** DB 쓰기이므로 별도 승인이 필요하고, 이 문서에서는 만들지 않았다. 만들면 열린 사건·알림 수신자가 처음부터 없는 장비가 된다.
- 장비 키는 `dev_api_key(serial)`(시드가 저장한 해시와 같은 키를 다시 계산하는 앱 함수)로 backend Pod 안에서만 만든다. 새 장비·새 키를 만들지 않는다. **키는 출력하지도 파일에 저장하지도 않는다**
  (Phase 1 문서 `s1-run-plan.md` 는 로컬 파일에 저장했다 지우는 방식이었는데, 이번에는 메모리에서만 쓴다).
  AI 도구의 자동 권한 판정이 Pod 안에서 키를 계산하는 명령을 거절할 수 있다. 이때 우회하지 말고 사용자가 직접 실행한다(`!` 접두사 등).

## 2. 보내기 전 점검 (필수, 읽기 전용)
```
kubectl exec -i deploy/backend -n parking-guardian -- python - RPI-A-PANGYO-01 < scripts/test-device-preflight.py
kubectl exec -i deploy/backend -n parking-guardian -- python -                  < scripts/test-device-preflight.py   # 장비 5대 요약
```
`scripts/test-device-preflight.py` 는 SELECT 만 한다. 키·해시·비밀번호는 읽지 않는다. 구문 확인(`py_compile`)과 2026-11-04 라이브 DB 에서 5대 읽기 실행 1회로 검증했다(자동 테스트는 DB 가 필요해 없다).

| 점검 | 기대 | 어긋나면 |
|---|---|---|
| 장비·주차장·회사가 모두 ACTIVE | ACTIVE | **중단** (인증 단계에서 403) |
| 열린 사건(`violations.ended_at IS NULL`) | 0건 | **주의**: 마지막 신호 공백이 `EPISODE_GAP_MINUTES` 를 넘었으면 첫 신호가 사건을 `SIGNAL_LOST` 로 닫는다. 공백이 짧아도 사진 판정에 따라 카운터·종료가 바뀐다 |
| 기존 `device_sessions` | 활성 세션 없음 | **주의**: 새 `session_id` 는 기존 세션을 대체할 수 있다. 같은 세션을 이어 쓰려면 그 세션의 다음 `seq` 를 쓴다 |
| `last_applied_seq` 와 최근 작업(seq, 시각) | 새 세션이면 `seq=1` 부터 | 이어 쓰는 세션이면 `seq` 를 마지막보다 크게 한다(순서가 어긋나면 순서 가드가 작업을 `stale` 로 처리해 사건 상태를 바꾸지 않을 수 있다 — 동작은 코드 설명 기준이고 직접 확인하지 않았다) |
| `pending_violation_count` | 0 | **주의**: 0 이 아니면 위반 판정 사진 한 장으로 확인 횟수(`VIOLATION_CONFIRM_COUNT`, 기본 2)가 채워져 위반·알림이 생길 수 있다 |
| 처리 중·대기 중 작업(`pending_publish`/`queued`/`processing`), 대기 중 명령 | 없음 | **중단** (작업 상태별 건수가 보인다) |
| 마지막 신호 | 확인용 | 열린 사건이 있을 때만 공백이 중요하다 |

점검 밖에서도 본다: SQS 메인 큐·DLQ 0개, Worker 0/0(또는 의도한 값), 노드에 Worker 1000m 이 들어갈 여유([phase2.md 8.2](phase2.md)), RDS `available` 이고 Secret 이 최신
([runbook-rds-rotation.md](runbook-rds-rotation.md)), Argo CD 앱이 모두 Synced/Healthy, 진행 중인 롤링 배포 없음.

## 3. 무엇이 바뀌는가 (보내는 것별)
| 보내는 것 | 새로 생기는 것 | 기존 데이터에 대한 영향 |
|---|---|---|
| `arrived`, **차량 없는 사진**, 새 세션 `seq=1` (2026-11-04 에 한 것) | `device_sessions` 1, `detection_jobs` 1, `detection_results` 1, S3 inbox 사진 1 | 장비의 `current_session_id`·`last_applied_seq`·마지막 신호 시각 갱신. **열린 사건이 있고 공백이 크면 그 사건이 `SIGNAL_LOST` 로 닫히고 이력 1건 추가.** 위반·알림 없음 |
| `arrived`, 차량 있는 사진 | 위 + (위반 판정이면) `violation_pending` | `pending_violation_count` 가 올라간다. 확인 횟수가 모자라 위반은 아직 안 생긴다 |
| 두 번째 확인 사진(`confirm`/`refresh`) | **위반 1건 + 알림 + 증거 사진 복사**가 생길 수 있다 | 별도 승인 후에만. 이 문서의 범위가 아니다 |
| `left` | 사건 종료 | 열려 있는 사건이 닫힌다 |
| 같은 `event_id` 를 다시 | 새 작업 없음 (`200`, `duplicate: true`) | 서버가 `event_id` 로 중복을 막는다(코드 설명. 이번에 재전송으로 확인하지는 않았다) |

## 4. 보내기
- 요청은 **1건**. 실패해도 같은 요청을 반복하지 않는다(오류의 원인을 먼저 본다. 2026-11-04 에는 URL 경로 오류로 404 가 났고 서버에 기록은 없었다).
- 테스트임을 식별할 수 있게 `session_id`·`event_id` 는 `e2e00000-0000-4000-8000-0000000...` 처럼 접두사가 있는 UUID 로 고정한다.
- 경로는 `/api/v1/device/detection-jobs`, 헤더 `X-Device-Key`, 폼 필드 `event`, `event_id`, `session_id`, `seq`, `detected_at`, 파일 `file`.
- 2026-11-04 에 쓴 전송 스크립트(사진을 base64 로 내장하고 Pod 안에서 키를 계산)는 임시 파일이라 저장소에 두지 않았다. 다시 필요하면 새로 작성하고 검토·승인을 받는다.

## 5. 보낸 뒤
1. 2절의 점검을 같은 장비로 다시 실행해 무엇이 바뀌었는지 기록한다(작업 1건 `done`, 결과 1건, 위반·알림 건수 변화 없음, 열린 사건 변화).
2. SQS 메인 큐·DLQ 0개, Worker 가 쿨다운(300초) 뒤 0 으로 돌아왔는지, Argo CD Synced/Healthy 를 본다.
3. 바뀐 기존 데이터가 있으면 **되돌리지 말고** 무엇이 왜 바뀌었는지 보고한다.

## 6. 하지 않는 것
테스트 데이터 삭제, DB 초기화, 새 장비 계정·키 생성, SQS 에 임의 형식 메시지 직접 발행, 같은 이벤트 반복 전송, 위반 확인 2회·알림 시험(별도 승인).
