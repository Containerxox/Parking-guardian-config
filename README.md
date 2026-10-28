# parking-guardian-config

Parking Guardian 의 Kubernetes 매니페스트(Kustomize). AWS 인프라는 `parking-guardian-infra`(Terraform), 앱 코드와 이미지 빌드(GitHub Actions)는 `Parking-guardian` 리포에 있다.

이 리포에는 **구조와 고정값**만 있다. 계정 ID·도메인·ARN·RDS 호스트 같은 Terraform 출력값은 `apps/parking-guardian/config.env`(gitignore)에만 두고, 비밀 값은 어디에도 없다(AWS Secrets Manager → External Secrets Operator).

## 구조

```text
platform/                        클러스터 1벌(앱과 무관한 공용 오브젝트)
└─ cluster-secret-store.yaml     ClusterSecretStore aws-secrets-manager (Pod Identity 인증, auth 블록 없음)
apps/parking-guardian/           앱 1벌. Kustomize 단일 구성(overlay 없음)
├─ kustomization.yaml            configMapGenerator 2개, images: 태그, replacements
├─ config.env.example            키 16개와 출처. 실제 config.env 는 gitignore
├─ nginx.conf                    frontend nginx 설정(ConfigMap frontend-nginx 의 원본)
├─ namespace.yaml, serviceaccount-backend.yaml
├─ externalsecret.yaml           backend-secrets ← Secrets Manager 비밀 3개(키 이름만)
├─ backend-migrate-job.yaml      배포 전 DB 마이그레이션 Job(sync-wave 1, 이미지 태그가 바뀌면 Replace 로 재실행)
├─ backend-deployment.yaml, backend-service.yaml   (Deployment 는 sync-wave 2, replicas 없음 → HPA 가 관리)
├─ backend-hpa.yaml              HPA 1~2 Pod, CPU 70%
├─ backend-pdb.yaml              PodDisruptionBudget maxUnavailable 1
├─ frontend-deployment.yaml, frontend-service.yaml
└─ ingress.yaml                  ALB(internet-facing, ip 타깃, 80→443, ACM, 이름 parking-guardian)
scripts/
└─ export-config-env.py          infra 리포의 terraform output → config.env
```

## 값은 세 곳에서 온다

| 출처 | 내용 | 공개 |
|---|---|---|
| 매니페스트 | 구조, 고정값(`STORAGE_BACKEND=s3`, `COOKIE_SECURE=true`, `DB_SSLMODE=require`, probe, resources, ALB 어노테이션) | 공개 |
| `config.env` | Terraform 출력값 16개(RDS 호스트, 버킷, ECR 주소, 도메인, ACM ARN, SG ID, 비밀 이름) | gitignore |
| `kustomization.yaml` `images:` | 이미지 태그 = 앱 리포 커밋 짧은 해시. 앱 리포 CI 가 갱신 | 공개 |

`config.env` 는 `configMapGenerator` 로 ConfigMap `backend-config` 가 된다. backend Pod 는 이것을 `envFrom` 으로 받는다. 매니페스트 안에 계정 값이 필요한 자리(이미지 레지스트리, Ingress host·인증서·SG, ExternalSecret 의 비밀 이름)는 **자리표시자**로 두고, `kustomization.yaml` 의 `replacements` 가 같은 ConfigMap 의 값으로 바꿔 넣는다. 그래서 공개 파일 어디에도 계정 ID 가 없다.

ConfigMap 2개(`backend-config`, `frontend-nginx`)는 이름에 내용 해시가 붙는다. 값이 바뀌면 이름이 바뀌고 참조하는 Deployment 가 롤링 재시작된다.

## 사전 조건

- infra 리포의 `network`, `data` 스택 apply 완료(출력값), `cluster`·`platform` 스택 apply 완료(EKS, ALB Controller, ESO)
- ECR 에 두 이미지가 `kustomization.yaml` 의 태그로 존재(앱 리포 Actions)
- `kubectl` 1.24 이상(`replacements` 는 kustomize v4.5+), `aws eks update-kubeconfig` 로 클러스터 접근
- Python 3.10 이상, Terraform(`export-config-env.py` 가 실행)

## 적용

```powershell
$env:AWS_PROFILE = "parking-guardian"
aws sts get-caller-identity
aws eks update-kubeconfig --name parking-guardian --region ap-northeast-2

# 1. ESO 준비 확인 (platform 스택이 설치)
kubectl get crd clustersecretstores.external-secrets.io
kubectl -n external-secrets get pods

# 2. Terraform 출력 → config.env  (infra 리포가 이 리포 옆에 있으면 인자 없음)
python scripts/export-config-env.py            # --infra <경로>, --model-version v1
kubectl kustomize apps/parking-guardian/ | Select-String placeholder,APP_HOST,ACM_CERTIFICATE_ARN   # 아무것도 안 나와야 한다

# 3. 적용. ClusterSecretStore 가 먼저(ExternalSecret 이 참조)
kubectl apply -f platform/
kubectl apply -k apps/parking-guardian/

# 4. 확인
kubectl -n parking-guardian get externalsecret          # READY True, STATUS SecretSynced
kubectl -n parking-guardian get pods -w                 # backend: Init → Running → 1/1
kubectl -n parking-guardian logs deploy/backend -c fetch-models   # 모델 다운로드 시작·끝 시각
kubectl -n parking-guardian get ingress                 # ADDRESS 에 ALB DNS 이름 (2~3분)
```

ALB 가 `active` 가 되면 infra 리포의 `dns-record` 스택을 apply 해 `parking.<도메인>` Alias 를 만든다. 그 뒤 `https://parking.<도메인>/api/v1/readyz` 가 200 이면 끝.

로그인: `flask seed` 가 만드는 데모 계정의 비밀번호는 Secrets Manager `parking-guardian/seed` 의 `SEED_ADMIN_PASSWORD`. 아이디는 앱 코드에 고정(`super@pg.local` 등).

## 바꾸는 법

| 바꿀 것 | 방법 |
|---|---|
| 이미지 | 앱 리포에 push → Actions 가 ECR 에 푸시하고 `images:` 태그를 갱신 → `kubectl apply -k` |
| 모델 | 버킷에 `v2/` 업로드 → `export-config-env.py --model-version v2` → `kubectl apply -k` (ConfigMap 해시가 바뀌어 backend 재시작) |
| nginx 설정 | `nginx.conf` 수정 → `kubectl apply -k` (frontend 재시작) |
| 비밀 값 | Secrets Manager 에서 `put-secret-value` → ESO 가 최대 1시간 안에 Secret 갱신. 즉시 반영은 `kubectl -n parking-guardian annotate externalsecret backend-secrets force-sync=$(date +%s) --overwrite` 뒤 backend 재시작 |
| Terraform 출력값 | infra apply → `export-config-env.py` 재실행 → `kubectl apply -k` |

## 확장(HPA)과 마이그레이션

backend Pod 수는 HPA 가 CPU 사용률(요청 1코어 대비 70%)로 1~2 사이에서 정한다. 그래서 `backend-deployment.yaml` 에 `replicas` 가 없다(있으면 Argo CD selfHeal 이 HPA 의 결정을 되돌린다). 상한 2 는 노드 2대 기준이며, Pod 가 2개인 동안은 롤링 업데이트의 surge Pod 자리가 없어 배포가 기다린다(노드 3대로 올리거나 부하가 빠진 뒤 배포).

Pod 가 2개가 되면 컨테이너 시작 시 마이그레이션은 서로 충돌하므로 backend 는 `MIGRATE_ON_START=0` 으로 건너뛰고, `backend-migrate` Job 이 배포마다 한 번 `flask db upgrade`(와 `flask seed`)를 한다. 순서는 Argo CD sync-wave 로 보장한다: ConfigMap·Secret(0) → Job(1, Complete 까지 대기) → Deployment(2). Job 이 실패하면 sync 가 실패하고 Deployment 는 바뀌지 않는다. 로그는 `kubectl -n parking-guardian logs job/backend-migrate`.

새 backend Pod 는 gunicorn 이 뜨자마자 모델을 읽기 시작하고(앱의 `gunicorn.conf.py`), 읽는 약 30초 동안 `/readyz` 가 503 이라 Ready 가 아니다. HPA 가 띄운 Pod 가 "Ready 인데 첫 사진에 30초" 가 되는 일을 막기 위한 것이다.

```powershell
kubectl -n parking-guardian get hpa backend          # TARGETS 가 "<사용률>%/70%", REPLICAS 1~2
kubectl -n parking-guardian describe hpa backend      # 올리고 내린 이유(Events)
```

## 삭제

순서가 중요하다. **Ingress 를 먼저 지워 ALB 가 사라진 것을 확인한 뒤** infra 의 `platform` 스택을 지운다. 컨트롤러가 먼저 사라지면 ALB·타깃 그룹·SG 규칙이 고아로 남는다.

```powershell
kubectl delete -k apps/parking-guardian/          # Ingress 포함. ALB 삭제 1~2분
aws elbv2 describe-load-balancers --names parking-guardian   # LoadBalancerNotFound 가 나와야 다음으로
kubectl delete -f platform/
# 이후 infra 리포: platform → cluster destroy
```

`dns-record` 스택은 ALB 가 사라지기 전에 먼저 destroy 한다(infra README).

## 측정 스크립트 (예정)

`scripts/pod-timeline.py`(Pod 시작·이미지 Pull·모델 다운로드 시간), `scripts/recovery-probe.py`(Pod·노드 장애 복구 시간). 설계 문서 2.1절. 아직 없다.
