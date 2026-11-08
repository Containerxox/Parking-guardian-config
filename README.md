# parking-guardian-config

Parking Guardian 의 Kubernetes 매니페스트(Kustomize)와 Argo CD 설정을 담은 GitOps 레포지토리입니다. 이 레포의 `main` 브랜치가 클러스터의 원하는 상태이고, Argo CD 가 EKS 에 자동으로 맞춥니다. 비밀 값은 이 레포에 없고 AWS Secrets Manager 에서 External Secrets Operator 가 전달합니다.

## 배포 흐름

```text
App 레포 push -> GitHub Actions 가 이미지 빌드 후 ECR 푸시
  -> 이 레포의 이미지 태그 갱신 커밋
  -> Argo CD 가 변경을 감지해 클러스터에 자동 반영
```

## 현재 배포 구성

- Phase 2(비동기) 구조: backend, frontend, worker, relay
- Worker 는 KEDA 설정을 최소 2 / 최대 3 로 운영합니다.
- backend 는 HPA 로 2~3개입니다.
- Phase 1 의 `inference` 서비스는 배포하지 않습니다.

## 구조

```text
argocd/        Argo CD Application (root.yaml 을 한 번 적용하면 나머지는 Git 에서 생성)
apps/          애플리케이션 매니페스트 (Kustomize)
platform/      공용 설정 (SecretStore, KEDA, 모니터링)
scripts/       렌더링 검증, config.env 생성
docs/          배포 구성과 운영 기록
```

## 적용

Infra 레포로 EKS 와 Argo CD 를 먼저 구축한 뒤 한 번만 실행합니다.

```powershell
aws eks update-kubeconfig --name parking-guardian --region ap-northeast-2
kubectl apply -f argocd/root.yaml
kubectl -n argocd get applications     # Synced / Healthy 확인
```

이후 변경은 Git 에 커밋하면 Argo CD 가 반영합니다. `kubectl` 로 직접 고친 내용은 Git 상태로 되돌려집니다.

## 관련 레포지토리

| Repository | 설명 | GitHub |
|---|---|---|
| App | 애플리케이션 소스 코드 및 AI 추론 로직 | [Containerxox/Parking-guardian](https://github.com/Containerxox/Parking-guardian) |
| Infra | Terraform 기반 AWS 인프라 구축 및 관리 | [Containerxox/parking-guardian-infra](https://github.com/Containerxox/parking-guardian-infra) |
| Config | Kubernetes 매니페스트 및 Argo CD GitOps 배포 설정 (이 레포) | [Containerxox/Parking-guardian-config](https://github.com/Containerxox/Parking-guardian-config) |

