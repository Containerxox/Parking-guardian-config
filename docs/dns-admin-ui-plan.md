# DNS 연결과 관리자 UI 검증 계획 (사전 점검 결과, 2026-11-04)

**DNS 레코드는 적용하지 않았다.** 이 문서는 읽기 전용 점검(AWS 조회, `terraform plan`, DNS 없이 하는 공개 GET)의 결과와 적용·검증 계획이다.
앱 도메인은 이 문서에 쓰지 않고 `APP_HOST`(= `config.env` 의 값)로 적는다. 계정·호스티드 존·ALB 이름도 적지 않는다. 표기: **[관찰]** 직접 확인, **[추정]** 해석, **[미확인]** 확인하지 못함.

## 1. 사전 점검 결과

| 항목 | 상태 | 구분 |
|---|---|---|
| Route 53 호스티드 존 | 공개 존, 레코드 3개(NS, SOA, ACM 검증용 CNAME). **`APP_HOST` 의 A 레코드는 아직 없음**(`Could not resolve host`) | [관찰] |
| ACM 인증서 | `APP_HOST` 용, **ISSUED**, 만료 2027-05-13, 검증 SUCCESS, **ALB 가 사용 중**. ALB 가 내놓는 인증서의 주체가 `APP_HOST` | [관찰] |
| ALB | internet-facing, active. 리스너 80(→ 443 으로 리다이렉트), 443(HTTPS, `ELBSecurityPolicy-TLS13-1-2-2021-06`, 기본 동작 = 고정 응답 404) | [관찰] |
| 호스트 기반 규칙 | 우선순위 1: Host=`APP_HOST` **그리고** 경로 `/api`·`/api/*` → backend 대상 그룹(5000, `/readyz`). 우선순위 2: Host=`APP_HOST` 그리고 `/*` → frontend 대상 그룹(8080, `/healthz`). 둘 다 대상 healthy | [관찰] |
| ALB 보안 그룹 | 80·443 이 **사용자 공인 IP `/32` 하나로만 제한**되어 있다(2026-11-04 network 스택 apply, `0.0.0.0/0` 규칙 제거). `alb_ingress_cidrs` 는 기본값 없는 필수 변수이고 `terraform.tfvars` 에 CIDR 을 명시해야 한다. 재 plan `No changes` — 자세한 내용은 7장 | [관찰] |
| `dns-record` Terraform 스택 | `terraform plan`: **1 add, 0 change, 0 destroy** — `APP_HOST` 의 **A 별칭 레코드** 하나(ALB 로, `evaluate_target_health = true`). 상태에는 아직 아무것도 없음 | [관찰] |

### DNS 없이 같은 경로를 읽기 전용으로 확인 (`curl --connect-to`: ALB 이름으로 접속하되 인증서는 `APP_HOST` 로 검증)
| 요청 | 결과 | 뜻 |
|---|---|---|
| `GET /` | 200 `text/html` | frontend 로 간다 |
| `GET /healthz` | 200 | frontend 의 상태 확인 |
| `GET /api/v1/healthz` | 200 `application/json` | `/api` 가 **backend 로 간다** |
| `GET /api/v1/auth/me` (비로그인) | 401 JSON | backend 의 인증 오류 — 라우팅과 앱이 정상 |
| `GET /static/nonexistent.js` | 404 | 정적 파일 처리 |
| `http://APP_HOST/` | 301 → HTTPS | 리다이렉트 |
| Host 가 다른 HTTPS 요청 | 404 | 기본 규칙(다른 Host 는 막힘) |
| TLS 검증 | 성공(`ssl_verify=0`), 만료 2027-05-12 23:59 GMT | 인증서가 `APP_HOST` 에 맞는다 |

## 2. 프런트엔드가 backend API 를 호출할 수 있는가

- **API 주소**: 프런트엔드는 빌드 시점의 `REACT_APP_API_BASE` 를 쓰고 없으면 `http://localhost:5000/api/v1` 로 폴백한다 [관찰: `client.js`].
  **배포된 번들(`main.*.js`)을 직접 받아 보니 `"/api/v1"`(상대 경로)이고 `localhost:5000` 문자열은 없다** [관찰]. 즉 브라우저는 같은 호스트의 `/api/v1/...` 를 부르고, ALB 의 `/api` 규칙이 backend 로 보낸다.
  같은 출처(same-origin)라 브라우저 CORS 검사를 받지 않는다. 그래도 backend 의 `CORS_ORIGINS` 는 `APP_HOST` 로 설정돼 있다 [관찰].
- **자격 증명 전송**: 요청마다 `credentials: "include"` [관찰]. 같은 출처이므로 쿠키가 붙는다.
- **쿠키**: 새로고침 토큰 쿠키 `pg_refresh`, `HttpOnly`, `COOKIE_SECURE=true`, `SameSite=Lax`(환경변수가 없어 기본값 [관찰: `config.py`]), 경로 `/api/v1/auth`. ALB 가 HTTPS 만 받으므로 `Secure` 쿠키가 동작한다.
  Port-forward(HTTP) 로는 안 된다는 이전 판단과 일치한다: 브라우저 UI 는 `APP_HOST` HTTPS 로만 확인한다.
- **판단**: 연결 구조상 **코드·매니페스트 변경 없이 DNS 레코드만 있으면 브라우저에서 동작해야 한다** [추정: 라우팅·번들·쿠키 설정은 확인했으나 브라우저 로그인은 실행해 보지 않았다 — 미확인].

## 3. 관리자 로그인과 조회 가능 여부

| 기능 | API | 현재 데이터 | 구분 |
|---|---|---|---|
| 로그인 | `POST /api/v1/auth/login` | 시험 관리자 `e2e-test-admin@example.invalid` ACTIVE, 시험 주차장에 배정, 비밀번호는 사용자가 보관 | [관찰] 데이터, 로그인은 [미확인] |
| 알림 목록 | `GET /notifications` (본인 것) | 시험 관리자에게 IN_APP·EMAIL(`SENT`) 2건 | [관찰] DB |
| 위반 목록·상세 | `GET /violations`, `/violations/<id>` (본인 회사 범위) | 시험 회사의 위반 #5(`NOTIFIED`) | [관찰] DB |
| 증거 이미지 | `GET /violations/<id>/image-url` → `/images/<token>` | S3 `UPLOADED`, 원본 해시 일치. **S3 presigned URL 이 아니다**: 백엔드가 서명 토큰이 든 주소 `https://APP_HOST/api/v1/images/<token>` 을 주고 이미지는 백엔드가 S3 에서 읽어 서빙한다(코드 주석의 "S3 면 presigned" 는 구현되지 않았다). `ProxyFix(x_proto=1, x_host=1)` 때문에 ALB 뒤에서도 https 주소가 만들어진다 — 같은 설정의 재현으로 확인했고(헤더가 없으면 http), 실제 브라우저는 [미확인] | [관찰] 코드·S3 |
| 조치 | `acknowledge` / `resolve` / `reopen` | — | 시험하지 않았다. 위반 상태를 바꾸므로 별도 승인 |

**읽음 처리 주의**: 알림 목록을 여는 것(`GET /notifications`)과 종 숫자 갱신은 데이터를 바꾸지 않는다. **알림을 클릭하면 `POST /notifications/<id>/read` 로 읽음 처리**(데이터 변경)가 되고 위반 화면으로 이동한다. UI 점검에서 데이터를 보존하려면 목록만 보고 클릭하지 않는다 [관찰: `Notifications.js`].

회사 관리자는 자기 회사 범위만 본다(코드의 `scoped_company_id`). 시험 관리자 화면에는 위반 #5 만 보여야 하고 데모 회사의 위반 4건은 안 보여야 한다 [추정: 브라우저 확인 필요].

## 4. 위험과 고려할 점

1. **공개 노출 [관찰]**: ALB 보안 그룹은 이미 사용자 공인 IP `/32` 로만 열려 있다(7장). DNS 레코드가 생겨도 이 IP 가 아닌 곳에서는 접속되지 않으므로, 도메인을 알아도 로그인 화면에 닿지 못한다. 다만 데모 계정(`kim@a.com` 등)의 비밀번호는 시드 시크릿 값이며 강도를 확인하지 않았고, 로그인 실패 잠금·속도 제한은 코드에서 찾지 못했다 [관찰: 코드 검색]. 따라서 이 제한이 풀리면(전체 공개) 위험이 그대로 되살아난다.
   → 남은 위험: (a) **공인 IP 변경** — 유동 IP 라 바뀌면 접속이 막힌다(복구: 7장). (b) **다른 환경에서의 설정 실수** — `terraform.tfvars` 에 `0.0.0.0/0` 을 적거나 다른 PC 에서 다른 값으로 apply 하면 제한이 풀리거나 바뀐다. 기본값이 없어 값을 빠뜨리면 plan 이 실패하지만, 명시한 값은 막지 못하므로 apply 전에 plan 의 규칙 변경을 확인한다. (c) **현장 장비(Raspberry Pi)** 는 이 IP 가 아니면 막힌다. (d) 운영 공개 시 제한을 풀 때는 위 로그인 보호 부족을 먼저 해결한다.
2. **보안 헤더 없음 [관찰]**: 응답 헤더에 HSTS·X-Frame-Options·X-Content-Type-Options·CSP 가 없고 `Server: nginx/1.30.6` 이 노출된다. 기능에는 영향이 없다. 개선은 frontend `nginx.conf` 변경(+ 이미지 재배포)이며 이번 범위 밖.
3. **DNS 캐시 [추정]**: 지금 이 이름은 존재하지 않아 PC 의 확인자가 음수 응답을 캐시했을 수 있다(존의 SOA 최소 TTL 만큼). 레코드를 만든 뒤 바로 안 풀리면 몇 분 기다리거나 캐시를 비운다.
4. **`evaluate_target_health = true` [관찰]**: ALB 대상이 모두 unhealthy 면 이 이름이 응답하지 않는다. 시험 중 Pod 롤링과 겹치지 않게 한다.
5. **정리 순서**: teardown 시 `dns-record` 를 ALB 보다 먼저 지운다(ALB 가 사라지면 `data.aws_lb` 조회가 실패한다). `dns-destroy.tfplan` 이 이미 있다.
6. **비용**: Route 53 레코드는 호스티드 존 요금(이미 있음) 외 추가 없음 [추정]. 질의 요금은 소액.

## 5. 적용·검증 계획 (모두 별도 승인 후)

| 단계 | 내용 | 확인 |
|---|---|---|
| 0 | 노출 방식 결정(위 위험 1) | 사용자 결정 |
| 1 | `terraform plan` 재확인(1 add) → 저장한 plan 으로 `apply` | `1 added` |
| 2 | 이름 풀림 확인(`nslookup`) → `https://APP_HOST/` 가 로그인 화면 | 인증서 경고 없음 |
| 3 | 시험 관리자 로그인(비밀번호는 사용자 입력) | 로그인 성공, 쿠키 설정 |
| 4 | 알림 목록에서 위반 알림 2건(IN_APP) 확인, 위반 목록에서 #5, 증거 이미지 표시 | 데모 회사 데이터는 안 보임 |
| 5 | 로그아웃 → 쿠키 삭제, 재접속 시 로그인 요구 | — |
| 6 | (선택) 데모 SUPER_ADMIN 화면에서 시험 회사 확인 | — |
| 7 | 끝나면 레코드 유지/삭제 결정 | 노출 정책에 따라 |

위반 조치(`acknowledge`/`resolve`)와 읽음 처리는 데이터를 바꾸므로 UI 점검에서 제외하거나 따로 승인받는다.

## 6. 필요한 변경 사항

- **연결 자체**: 없음(DNS 레코드 1개만).
- **권장(선택)**: ① ALB 보안 그룹 IP 제한(7장, **적용 완료**) 유지와 시험 뒤 DNS 레코드 정리, ② frontend nginx 보안 헤더·`server_tokens off`(후속), ③ 데모 계정 비밀번호 강도 확인(시드 시크릿 메타데이터가 아니라 정책 문제라 사용자가 판단).

## 7. ALB 접근 제한(IP) — 적용 완료 (2026-11-04)

**목적**: DNS 를 연결하기 전에 ALB(80·443)를 사용자 공인 IP 의 /32 로만 열어 둔다. 적용 전에는 `0.0.0.0/0` 이었다([관찰]: 전체 보안 그룹 중 공개 인바운드는 이 두 규칙뿐, IPv6 공개 없음, ALB 는 IPv4 전용).

**현재 상태 [관찰]**: 같은 날 network 스택을 apply 해 ALB 80·443 은 **사용자 공인 IP `/32` 하나만** 허용한다(`0.0.0.0/0` 규칙 제거, 재 plan `No changes`). `alb_ingress_cidrs` 는 **기본값이 없는 필수 변수**다 — 값은 `terraform.tfvars`(gitignore)에 명시해야 하고, 지정하지 않으면 Terraform plan 이 실패한다. 전체 공개는 `0.0.0.0/0` 을 명시한 경우에만 가능하다.

**변경 파일 (infra 저장소; 이 문서 작성 시점에는 로컬 수정·미커밋)**
| 파일 | 변경 |
|---|---|
| `terraform/stacks/network/variables.tf` | 변수 `alb_ingress_cidrs`(IPv4 CIDR 목록, **기본값 없음(필수)**. 빈 목록·IPv6·접두어 없는 주소·호스트 비트가 있는 값은 plan 에서 거부) |
| `terraform/stacks/network/security_groups.tf` | 80·443 인바운드 규칙을 CIDR 별 `for_each` 로, `create_before_destroy`, 기존 규칙을 이어 주는 `moved` 블록 |
| `terraform/stacks/network/terraform.tfvars.example` | 예시(추적). 복사해 `terraform.tfvars`(gitignore)를 만들고 실제 IP 를 적는다. 안전한 설정 방법 설명 포함 |
| `README.md` | network 절에 "ALB 접근 제한" 절 추가(다른 PC 설정 절차, IP 변경 복구) |

**plan 결과 [관찰]**
- **변수 값 없음**: plan 이 `No value for required variable` 로 실패한다(비대화형). 처음 설계에서는 기본값이 `["0.0.0.0/0"]` 이라 `terraform.tfvars` 를 빠뜨린 PC 에서 apply 하면 ALB 가 전체 공개되는 위험이 있었고, 그래서 **기본값을 제거**했다. 잘못된 값(빈 목록·IPv6·접두어 없음·호스트 비트·문자열)은 모두 plan 단계에서 거부됨을 확인했다.
- **공인 IP 1개(`terraform.tfvars`), 적용 전 기준**: `2 to add, 0 to change, 2 to destroy` — 추가: 80·443 규칙(소스 = 사용자 공인 IP /32), 삭제: `0.0.0.0/0` 규칙 2개. **다른 리소스 변경 없음.** `create_before_destroy` 라서 새 규칙이 먼저 생긴 뒤 옛 규칙이 지워진다(접속이 끊기는 틈 없음). 적용 뒤에는 같은 값으로 `No changes`.
- **DNS 스택**: `1 to add, 0 to change, 0 to destroy` — `parking.<도메인>` 의 A 별칭 레코드 1개만.

**80 번 포트**: 앱은 80 을 리다이렉트(301)에만 쓴다. https:// 로 직접 접속하면 필요 없지만, 주소창에 http:// 로 쳐도 되게 같은 IP 로만 연다. 닫고 싶으면 `security_groups.tf` 의 `alb_http` 리소스를 지우면 된다.

**영향 범위 분석**
| 대상 | 영향 | 근거 |
|---|---|---|
| ALB → Pod 상태 확인·전달 | **없음** | ALB 의 아웃바운드(5000·8080 → 노드 SG)와 노드 SG 의 인바운드(ALB SG 에서)는 그대로다. 상태 확인은 ALB 가 VPC 안에서 보내므로 바깥 인바운드 규칙의 영향을 받지 않는다 |
| Pod ↔ Pod, EKS 컨트롤 플레인, RDS | 없음 | 다른 보안 그룹. 이번 변경은 ALB 인바운드 두 규칙만 |
| ACM 인증서 갱신 | 없음 | DNS 검증(CNAME) 방식 |
| ALB 접근 로그(S3) | 없음 | AWS 서비스가 버킷에 쓴다 |
| CI(GitHub Actions)·Config 저장소 | 없음 | 둘 다 ALB 를 호출하지 않는다 [관찰: 워크플로 검색] |
| **현장 장비(Raspberry Pi)** | **막힌다** | 장비는 공개 API(`PG_SERVER_URL`)로 접속한다. 지금 연결된 실장비는 없다 [관찰]. 운영 전에는 장비 IP 를 목록에 더하거나 제한을 푼다 |
| k6 부하 테스트(PC → ALB) | 사용자 IP 면 통과 | 다른 장소에서 하면 그 IP 를 더해야 한다 |
| EKS API 접근(kubectl) | 별개 | 클러스터 스택의 `eks_public_access_cidrs` 가 따로 있다(지금 같은 IP) |

**IP 가 바뀌었을 때 복구 절차**
1. 새 IP 확인: `curl https://checkip.amazonaws.com`. 2. `terraform.tfvars` 의 `alb_ingress_cidrs` 를 새 `/32` 로 고친다(두 곳에서 접속하면 목록에 더한다).
3. network 스택에서 `terraform plan` → 규칙 2 add / 2 destroy 만 나오는지 확인 → `terraform apply`. (Terraform 은 AWS API 로 동작하므로 ALB 가 막혀 있어도 PC 에서 적용할 수 있다.)
4. 같은 IP 를 쓰는 **클러스터 스택**(`eks_public_access_cidrs`)도 바뀌었다면 kubectl 이 막히므로 별도로 고친다(EKS 엔드포인트 갱신에 몇 분).
5. 비상(Terraform 을 못 쓸 때): `aws ec2 authorize-security-group-ingress --group-id <ALB SG> --protocol tcp --port 443 --cidr <새IP>/32` 로 임시 허용. 이 규칙은 Terraform 이 모르는 규칙이라 **다음 plan 에서 지워진다(드리프트)** — 그때 `terraform.tfvars` 를 맞춘다.
6. 전체 공개: `terraform.tfvars` 에 `alb_ingress_cidrs = ["0.0.0.0/0"]` 를 **명시**하고 plan → apply. 줄을 지우면 기본값이 없으므로 전체 공개가 되지 않고 plan 이 실패한다.

**다른 PC 에서 실행할 때**: `terraform.tfvars` 는 저장소에 없다. network 스택의 `terraform.tfvars.example` 을 복사해 `alb_ingress_cidrs` 를 그 PC 의 공인 IP `/32`(또는 허용할 IP 목록)로 적는다. 현재 ALB 에 적용된 값과 다르면 plan 이 규칙 교체(2 add / 2 destroy)를 보여 주므로 apply 전에 반드시 확인한다. 같은 값이면 `No changes`. 상세 절차는 infra 저장소 README 의 "ALB 접근 제한" 절.
