# Phase 2 비교 구성 적용 (실험 1: Phase 1 inference 2개 ↔ Phase 2 Worker 2개) — 2026-11-05

이 문서는 `apps/parking-guardian/kustomization.yaml` 끝에 붙은 **비교 구성 패치**의 목적, 효과, 적용·확인·되돌리기 순서를 적는다. 계정 ID·호스트·키는 적지 않는다(공개 저장소).
관련: [phase2.md](phase2.md) 4절(비교 구성), [loadtest-execution-plan-2026-11-04.md](loadtest-execution-plan-2026-11-04.md), `comparison-mode.example.yaml`(패치 원본).

## 1. 무엇을 바꾸나 (렌더링 결과 비교: 패치 전 ↔ 후)
| 리소스 | 변화 |
|---|---|
| Deployment / Service / HPA / PDB / PodMonitor `inference` | **삭제** (inference Pod 0개) |
| ScaledObject `worker` | `minReplicaCount` 0 → **2**, `maxReplicaCount` 1 → **2**, `fallback.replicas` 1 → **2** (KEDA 가 개수를 바꿀 수 없음 = 자동 확장 없음) |
| 그 밖의 모든 리소스(backend, frontend, relay, worker Deployment, Ingress, ExternalSecret, ConfigMap …) | 변화 없음 |
- 남는 것: `inference` ServiceAccount·ConfigMap(해가 없다). worker Deployment 의 `replicas: 0` 은 그대로이고 KEDA(HPA `keda-hpa-worker`, min=max=2)가 2 로 올린다.
- `scripts/check-phase2-render.py` 가 이 결과를 확인한다(inference 5종 없음, Worker 2/2, fallback 2, 참조·오토스케일러 충돌 없음). `kubectl apply --dry-run=server` 로 ScaledObject 가 받아들여지는 것도 확인했다.
- backend 의 `INFERENCE_BACKEND=http` 는 그대로다. `/readyz` 는 inference 서비스에 의존하지 않으므로(코드 `api/health.py`) backend 는 계속 Ready 이고, **Phase 1 동기 경로(`POST /device/detections`)만 503** 이다. Phase 2 접수 API(`POST /device/detection-jobs`)는 영향이 없다.

## 2. 왜 이렇게 하나 (공정한 비교)
- 같은 EKS 클러스터, 같은 노드 3대, 같은 빌드(`worker` 와 `backend` 이미지 태그 같음).
- AI 용 CPU 요청이 같다: Phase 1 inference 2 × 1000m = Phase 2 Worker 2 × 1000m.
- 메모리도 같다: 요청 1.5Gi, 한도 2Gi, **CPU 한도 없음**, `TORCH_NUM_THREADS=1`, `INFERENCE_BACKEND=yolo`, 같은 모델 2개.
- 차이(결과에 함께 적는다): (1) Worker 는 HTTP 서버가 없고 DB·S3·SQS 에 직접 접속한다(inference 는 HTTP 로 판정만 돌려준다). (2) Worker Pod 는 앱 지표 포트가 없어 CPU·메모리는 cAdvisor(kubelet)로만 본다. (3) 노드 배치는 스케줄러 결정이다 — 실제 배치는 시험 때 `result.json` 의 `placement` 에 기록한다. (4) 사진 완료가 비동기라 성공 지표가 다르다.
- Worker 를 **min=max=2** 로 두는 이유: 개수가 변수가 되지 않게, 그리고 시험 직전 콜드 스타트(이미지 Pull 약 660 MB + 모델 다운로드)를 기다리지 않게. 배포(커밋)와 함께 Worker 2개가 미리 떠서 모델을 읽는다.
- KEDA 자동 확장(P2-K) 실험이 필요하면 패치의 ScaledObject 값을 1 / 3 으로 되돌린다(예전 안).

## 3. 사용자가 직접 할 Git 작업 (config 리포, 공개 저장소)
1. 변경 파일 확인: `git status` → 아래 5개만 있어야 한다.
   - `apps/parking-guardian/kustomization.yaml` — 끝에 `# --- Phase 2 비교 구성` 주석과 `patches:` 블록 추가
   - `apps/parking-guardian/comparison-mode.example.yaml` — ScaledObject 값 1/3 → 2/2, fallback 2, 설명
   - `scripts/check-phase1-render.py` — 비교 구성이 켜져 있어도 기본 구성(inference 있음)을 검사하도록 패치 블록을 떼고 렌더링
   - `scripts/check-phase2-render.py` — Worker 2/2 기대값, 이미 붙은 패치는 다시 붙이지 않음
   - (이 문서) `docs/phase2-comparison-run-2026-11-05.md`
2. 커밋 전 확인: `python scripts/check-phase2-render.py` 와 `python scripts/check-phase1-render.py` 가 둘 다 "모두 통과".
3. **커밋·푸시 한 번.** 메시지 예: `feat: Phase 2 비교 구성 적용 (inference 제거, Worker 2개 고정)`.
4. 푸시하면 Argo CD 가 자동 동기화한다(지금 자동 동기화가 켜진 기본 상태에서). 동기화 중에: inference Pod 2개 종료 → Worker 2개 기동(이미지 Pull·모델 다운로드·로딩, 수 분) → `backend-migrate` Job 이 한 번 더 돌 수 있다.
5. 푸시한 뒤 Claude 에게 알려 주면, Claude 가 직접: 동기화·Worker 2/2 Ready 확인 → `p3.py status --expect p2-comparison` → `p3.py p2-pause --apply`(승인 필요) → 사전 점검 → 부하 실행기 안내.

## 4. 적용 직후 확인할 것 (Claude 가 읽기 전용으로 확인)
- Argo 6개 Synced/Healthy, `inference` Deployment/HPA 없음, `worker` 2/2 Ready, `keda-hpa-worker` min=max=2, ScaledObject Ready=True.
- backend 2/2(재시작 없음), relay 1/1, 큐·DLQ 0, RDS 정상, 기존 데이터 보호 기준선 통과.
- 노드별 요청 합과 실제 Pod 배치(Worker 가 서로 다른 노드에 있는지, backend 와 같은 노드에 몰리지 않았는지)를 기록한다.

## 5. 되돌리기 (Phase 1 기본 구성으로)
1. **Worker 를 0 으로 비우고 큐가 빈 뒤**에 한다(inference 롤아웃에 1000m 자리가 필요하다): 시험이 끝났고 큐·DLQ 가 0 인지 확인.
2. 이 커밋을 `git revert` 하거나, `kustomization.yaml` 의 `# --- Phase 2 비교 구성` 부터 끝까지 지우고 푸시한다. `comparison-mode.example.yaml` 의 2/2 설명은 남아도 된다(사용되지 않는 예시 파일).
3. Argo 동기화 뒤 `python loadtest/tools/p3.py status --expect p2-baseline` 이 통과해야 한다(inference 2, Worker 0~1, 자동 확장 값 0/1).

## 6. 위험과 한계
- 비교 구성이 켜져 있는 동안 **Phase 1 동기 경로는 503** 이다(실제 장비가 없으므로 영향 없음). 이 구성에서 Phase 1 시험을 하면 안 된다.
- Worker 3개 + 마이그레이션 Job 은 노드에 들어가지 않는다(배치 계산). 이 패치는 Worker 2개라 들어간다. 이후 배포(이미지 태그 변경)는 Worker 가 2개일 때 가능하다(롤링은 maxSurge 0 / maxUnavailable 1).
- KEDA fallback 은 Worker 가 이미 떠 있을 때만 효과가 있다. 큐 길이 읽기 오류가 나도 min=2 가 개수를 지킨다.
- 이 패치로 `kubectl` 직접 조작(scale 등)은 KEDA/Argo 가 되돌린다. 실험 중에는 `p3.py p2-pause` 로 Argo 자동 동기화를 멈추고 backend HPA 를 2/2 로 고정한다.

## 8. Worker 수 바꾸기 (2026-11-05 Worker 확장 검증: 2 → 3)
- 바꾸는 곳은 `kustomization.yaml` 끝의 patch 블록과 `comparison-mode.example.yaml` 의 ScaledObject 값 세 개뿐이다: `minReplicaCount`, `maxReplicaCount`, `fallback.replicas` — **셋은 항상 같은 N**(2~4). 현재 N = 3.
- 노드 3대에서 N = 3 은 들어간다(세 번째 Worker 는 node-23, 이 노드에는 Worker 이미지가 아직 없어 첫 기동에 Pull 이 든다). N = 4 는 노드를 하나 더 둔 뒤에만 가능하다(Terraform `node_max_size`, 별도 승인).
- 푸시 후 확인: `kubectl -n parking-guardian get deploy worker` 가 N/N, `kubectl -n parking-guardian get scaledobject worker -o jsonpath='{.spec.minReplicaCount}/{.spec.maxReplicaCount}'` 가 N/N, `python loadtest/tools/p3.py status --expect p2-comparison` 전부 통과.
- 되돌리기: 세 값을 2 로 되돌려 푸시(또는 이 변경 커밋을 revert) → Worker 3 → 2. 큐·DLQ 가 0 이고 시험이 끝난 뒤에 한다.
