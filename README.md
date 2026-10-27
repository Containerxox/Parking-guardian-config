# parking-guardian-config

Parking Guardian 의 Kubernetes 매니페스트(Kustomize). AWS 인프라는 `parking-guardian-infra`(Terraform), 앱 코드는 `parking-guardian` 리포에 있다.

이 리포에는 **구조와 고정값**만 있다. 계정 ID·도메인·ARN·RDS 호스트 같은 Terraform 출력값은 `apps/parking-guardian/config.env`(gitignore)에만 두고, 비밀 값은 어디에도 없다(Secrets Manager → External Secrets Operator).

## 구조

```text
platform/                        클러스터 1벌(앱과 무관한 공용 오브젝트)
└─ cluster-secret-store.yaml     ClusterSecretStore(AWS Secrets Manager, ESO 의 Pod Identity 로 인증)
apps/parking-guardian/           앱 1벌. Kustomize 단일 구성(overlay 없음)
├─ kustomization.yaml            configMapGenerator(config.env + 고정값), images: 태그, replacements
├─ config.env.example            키 이름과 출처. 실제 config.env 는 gitignore
├─ namespace.yaml, serviceaccount-backend.yaml
├─ externalsecret.yaml           backend-secrets ← Secrets Manager 비밀 3개(키 이름만)
├─ configmap-nginx.yaml          frontend nginx.conf(Kubernetes 용)
├─ backend-deployment.yaml, backend-service.yaml
├─ frontend-deployment.yaml, frontend-service.yaml
└─ ingress.yaml                  ALB(internet-facing, ip 타깃, 80→443, ACM)
scripts/                         export-config-env.py(Terraform 출력 → config.env), 측정 스크립트
```

## 적용 순서

infra 리포의 `cluster`·`platform` 스택이 끝난 뒤(ESO Pod Running, CRD 존재).

```powershell
python scripts/export-config-env.py          # infra 리포의 terraform output → apps/parking-guardian/config.env
kubectl apply -f platform/                   # ClusterSecretStore
kubectl apply -k apps/parking-guardian/      # 앱
kubectl -n parking-guardian get externalsecret,pods,ingress
```

삭제는 역순. **Ingress 를 먼저 지워 ALB 가 사라진 것을 확인한 뒤** infra 의 `platform` 스택을 지운다(컨트롤러가 먼저 사라지면 ALB 가 고아로 남는다).

(작성 중. 각 파일은 하나씩 추가하면서 이 문서를 채운다.)
