# AWS 비용 추정과 정리·보존 계획 (2026-11-04)

**분석이다. 리소스를 바꾸거나 `terraform destroy` 를 실행하지 않았다.** 표기: **[관찰]** 이 계정·저장소에서 직접 읽은 값, **[계산]** 아래 단가로 계산한 값, **[추정]** 단가를 내 기억/공시 기준으로 적은 것(청구서·Cost Explorer 로 확인 필요).
단가는 서울 리전 온디맨드 기준이며 **실제 청구액과 다를 수 있다.** 이 계정은 Free plan 크레딧이 있을 수 있으나 예산 경보는 크레딧을 제외하고 센다 [관찰: `budget.tf`]. 시험 계획: [loadtest-execution-plan-2026-11-04.md](loadtest-execution-plan-2026-11-04.md).

## 1. 지금 시간당 비용
| 항목 | 단가 | 근거 |
|---|---|---|
| EKS 컨트롤 플레인 | $0.10/h | [관찰: infra README "비용 끄기"] |
| 노드 3 × `m7i-flex.large` | 3 × $0.11771 = $0.353/h | 단가 확인값 [관찰: ops-risks R7] |
| NAT Gateway(+EIP) | 약 $0.06/h(+데이터 처리 $0.045/GB [추정]) | [관찰: README] |
| ALB | 약 $0.0225/h + LCU [추정] | |
| RDS `db.t4g.micro`(실행 중) | 약 $0.026/h [추정] | [관찰: 인스턴스 클래스 확인] |
| 공인 IPv4(NAT·ALB 등 약 3개) | 약 $0.015/h [추정] | |
| EBS(노드 루트 등) | 약 $0.01/h [추정] | |
| CloudWatch(Container Insights), SQS·S3 요청, Route 53 | 소액 | |
| **합계 [계산]** | **약 $0.58~0.60/h** | 이전 보고의 약 $0.60/h 와 일치 |
- 하루 24 h 켜 두면 약 $14, 노드만 0 으로 줄이면(컨트롤 플레인 + NAT + ALB + RDS + IPv4) 약 **$0.21/h(하루 약 $5)** [계산]. 노드 0 은 Pod 가 모두 사라져 이미지 캐시도 사라진다.

## 2. 예상 시간과 비용 [계산 — 시간은 [loadtest-execution-plan-2026-11-04.md 8절](loadtest-execution-plan-2026-11-04.md) 의 회차 길이에서]
| 작업 | 시간 | 비용(× $0.60/h) | 비고 |
|---|---|---|---|
| 선행 작업(장비 생성, 도구 준비, 워밍, 기준선) | 1~1.5 h | $0.6~0.9 | 도구 개발은 로컬이라 클러스터를 켤 필요 없음 — **개발을 끝낸 뒤에 클러스터를 켜는 편이 싸다** |
| Phase 1 재측정(B0 ×3, S0 ×3, S1 ×3) | 2.5~3 h | $1.5~1.8 | 선택 항목(S3, S5, V2, V3)을 더하면 +1.5 h |
| 구성 전환 + 검증(Git 커밋, Argo 동기화) | 0.5 h | $0.3 | |
| Phase 2 전체(P2-S1, W, D, K, C, F) | 5~7 h | $3.0~4.2 | 최소 부분집합(S1 ×3, W 1회씩, K·C·D 1회): 약 3.5 h → $2.1 |
| 복원 + 확인 + 결과 저장 | 0.5~1 h | $0.3~0.6 | |
| **합계(1일 연속)** | **약 10~13 h** | **약 $6~8** | 노드 +1 이면 +$0.118/h × 시간 |
| 2회 분리, 사이에 정리·재구축 | 사이 재구축 1 h 추가 | +$0.6 | 사이 대기가 길면(>1.5 h) 켜 둔 채 기다리는 비용이 더 크다: 14 h 밤새 켜 두면 $8.4 |
- 최소 부분집합이면 총 $4 안팎, 선택 항목까지 하면 $10 안팎 [계산, 단가 [추정]]. 이미 지난 비용(오늘 켠 시간)은 위에 포함하지 않았다. 월 예산 경보 값은 `monthly_budget_usd`(Terraform data) — 현재 값은 확인하지 않았다 [미확인].
- NAT 데이터: 시험 중 PC→ALB 트래픽은 NAT 를 거치지 않는다. Pod 의 아웃바운드(ECR pull 은 노드 이미지 캐시, S3 는 Gateway Endpoint 무료)라 NAT 데이터 비용은 소액 [추정].

## 3. 부하 테스트 전에 리소스를 정리하고 다시 만드는 경우
정리가 이득인 경우: 도구 개발·검토가 길어질 때(하루 이상). 이득이 없는 경우: 몇 시간 안에 시험을 시작할 때.

**정리 순서(스택별)** — 각 단계 별도 승인, 앞 단계가 확인되기 전에 다음으로 가지 않는다 [근거: infra README 삭제 절 + 11-01 해체 기록].
1. 결과·로그 저장(4절) — 클러스터 안 데이터는 정리와 함께 사라진다.
2. `dns-record` 스택 destroy(레코드 1개) — 가장 먼저. ALB 보다 먼저 지워야 이름이 허공을 가리키지 않는다.
3. Argo CD 앱 제거(root → 자식) 후 **ALB·Target Group·Prometheus PVC·EBS 가 사라진 것을 AWS 에서 확인**(남아 있으면 Subnet·SG 삭제가 막힘).
4. `platform` 스택 destroy(10개: Argo, ESO, ALB Controller, KEDA 등 Helm 릴리스와 Pod Identity).
5. `cluster` 스택 destroy(현재 43개: EKS, 노드, NAT, IAM, 애드온…).
6. 확인: EKS·EC2·NAT·EIP·ALB·EBS 모두 0, cluster·platform·dns-record 의 State 비어 있음, `bootstrap`·`network`·`data` plan `No changes`.
**남기는 스택**: `bootstrap`(State 버킷, `prevent_destroy`), `network`(VPC·서브넷·보안 그룹 — 무료, RDS 가 의존, `terraform.tfvars` 의 ALB 허용 IP 는 그대로), `data`(RDS, S3, ECR, Secrets, Route 53 존·ACM, OIDC, 예산). **`terraform destroy` 는 이 문서에서 실행하지 않았다.**

**재구축 과정**(이전 기록과 오늘의 경험을 합침, 약 45~70 분 [추정], 약 $0.4~0.6)
1. RDS 가 `stopped` 면 시작하고 `available` 까지 대기. (정지 7일이 지나면 AWS 가 자동 재시작한다.) 비밀번호 마지막 회전 시각 확인(R6): `describe-secret` 의 `LastRotatedDate`.
2. `cluster` plan → apply(약 15~20 분, 43개) → `aws eks update-kubeconfig`.
3. `platform` plan → apply(10개) → `kubectl apply -f argocd/root.yaml`.
4. Argo 가 앱 6개를 `Synced/Healthy` 로 만들 때까지 대기. 함정: ① ALB 로그 버킷은 data 스택 소속(있음) ② 첫 배포 교착은 `replicas: 0` + `ignoreDifferences` 로 해결됨 ③ 회전된 비밀번호로 마이그레이션 Job 이 실패하면 force-sync 후 재시도([runbook-rds-rotation.md](runbook-rds-rotation.md)) ④ CloudWatch Agent CPU 요청 100m 은 Terraform 에 반영되어 있다.
5. `dns-record` apply(레코드 1개) → `https://APP_HOST/api/v1/healthz` 200.
6. 이미지·모델 캐시가 비어 있다 → 시험 전에 워밍(계획 9절). 시험 장비와 시험 회사 데이터는 RDS 에 남아 있다.
재구축 후 `network` 를 새로 만든 것이 아니므로 ALB 허용 IP 규칙도 그대로다. 단 **공인 IP 가 바뀌었는지** 확인한다.

## 4. 테스트 결과·로그 보존 방법 (정리 전에 할 것)
시험 결과는 로컬 전용이다(Git 에 올리지 않음 [사용자 결정]). 아래는 정리하면 사라지거나 만료되는 것들이다.
| 대상 | 사라지는 시점 | 보존 방법 |
|---|---|---|
| k6 요약·포인트 JSON | PC 에 남음 | 회차 폴더 + `SHA256SUMS`(Phase 1 방식) |
| Worker·Relay·backend Pod 로그 | Pod 삭제 시 | `kubectl logs --since=…` 로 회차별 저장(비밀·토큰 출력 점검: 이미지 토큰이 접근 로그에 남는다 — 저장 전 마스킹) |
| 관찰자 타임라인·`kubectl top` 표본 | PC 에 남음 | 회차 폴더 |
| DB 지표(작업·결과·세션) | RDS 에 남음(삭제하지 않음) | 회차별 집계 CSV 를 PC 로 내보내고 같은 쿼리를 문서화. 선택: 시험 전/후 RDS 수동 스냅샷(실행 중 스냅샷 가능, 20 GB 기준 월 약 $2 [추정]) |
| SQS CloudWatch 지표 | 1 분 해상도는 15일 [추정: CloudWatch 보존 규칙] | 시험 직후 `get-metric-data` JSON 저장 |
| RDS·EKS CloudWatch 지표 | 위와 같음 | 시험 직후 저장 |
| Container Insights 로그 그룹 | 보존 7일(`retentionInDays` 7) [관찰] | 필요한 구간만 내보내거나 위 Pod 로그로 대체 |
| Prometheus·Grafana 데이터 | PVC 삭제 시(정리하면 사라짐) | 쿼리 결과 CSV·대시보드 PNG 를 정리 전에 저장 |
| ALB 접근 로그(S3) | 수명 주기 만료(약 7일 [관찰: 11-01 기록]) | 필요하면 시험 시간대 객체만 복사(요청 경로에 이미지 토큰이 있으니 로컬 보관만) |
| S3 `inbox/` 시험 사진 | **7일 뒤 자동 삭제** [관찰: Terraform] | 보존 불필요(해시/건수만 기록) |

## 5. 정리 후 남는 데이터와 잔여 비용 [추정 — 청구서로 확인]
cluster·platform·dns-record 를 지운 뒤(network·data·bootstrap 유지) 시간당 비용은 **0** 이고 월 고정비만 남는다.
| 항목 | 규모 [관찰] | 월 비용 [추정] |
|---|---|---|
| RDS `db.t4g.micro` | 정지 시 인스턴스 요금 0, 20 GB gp3 저장 + 자동 백업 1일 | 정지: 약 $2.5 / **실행 중이면 약 $19(+$2.5)** — 정지 7일 후 자동 재시작되므로 놓치면 비용 발생 |
| 수동 RDS 스냅샷(`…-manual-20261101`) | 20 GB | 약 $1.9 |
| ECR 4개 저장소 | 합 약 2.85 GB(backend 913 MB, inference 1.25 GB, worker 661 MB, frontend 26 MB) | 약 $0.3 |
| S3 | models 412 MB, photos 12 MB, ALB 로그 3 MB, tfstate 0.3 MB | 약 $0.01 |
| Secrets Manager | 시크릿 4~5개 | 약 $1.6~2 |
| Route 53 호스티드 존 | 1개 | $0.50 |
| CloudWatch Logs | 보존 7일, 저장 거의 0 | 소액 |
| NAT·ALB·EKS·노드·EBS | 0 | 0 |
| **합계** | | **약 $6~8/월**(RDS 정지 유지 시). 이전 기록의 "월 $5~7(추정)" 과 일치 |
- 위험: ① RDS 자동 재시작(정지 7일) — 정리 후에도 날짜를 달력에 둔다. ② ECR·S3 모델 이미지는 재구축에 꼭 필요하므로 지우지 않는다. ③ ACM 인증서·존은 DNS 재연결에 필요. ④ 이 모든 보존 자원은 "삭제하지 않는다" 규칙의 대상이다.
- 이 문서는 어떤 삭제도 승인하지 않는다. 삭제 후보(비Terraform 로그 그룹 2개, 낡은 `*.tfplan`, RDS 삭제 여부)는 이전 미결 목록 그대로이며 사용자 결정이 필요하다.

## 6. 권고 요약
1. **도구(k6 P2, 수집기, p3 갱신)를 먼저 로컬에서 끝낸다** — 클러스터는 지금 켜져 있으므로 장시간 개발 동안 $0.6/h 가 든다. 개발이 반나절 이상 걸리면 정리·재구축($0.6 + 1 h)이 싸다.
2. 시험은 **1일차 Phase 1 재측정 / 2일차 Phase 2** 로 나누고(각 5~7 h), 하루 끝에 결과를 저장하고 노드 0 또는 전체 정리를 선택한다. 사이가 12 h 를 넘으면 전체 정리가 싸다.
3. 노드 +1 은 공존 상태 동시 비교가 꼭 필요할 때만(+$0.118/h).
4. 어느 쪽이든 정리 직전에 4절을 마친 뒤, 정리는 3절 순서로 별도 승인.
