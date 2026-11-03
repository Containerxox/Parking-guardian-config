#!/usr/bin/env python3
"""Phase 2 매니페스트(Worker·Relay·KEDA)를 렌더링(kubectl kustomize)해서 설계대로인지 확인한다. 클러스터·AWS 에는 아무것도 하지 않는다.

    python scripts/check-phase2-render.py              # 렌더링 + 계약·참조·배치 계산 + 배포 전 준비 사항 목록
    python scripts/check-phase2-render.py --strict     # 배포 전 준비 사항이 하나라도 안 끝났으면 종료 코드 1 (배포 직전에 쓴다)

Windows 콘솔(cp949)에서는 PYTHONUTF8=1 을 준다.

확인하는 것:
  1. Phase 1 이 그대로다: backend / inference / frontend / Ingress / HPA·PDB 가 있고 backend-secrets 의 사용자도 그대로다
  2. API·Worker·Relay 의 환경변수: 이름이 앱 코드(capston/backend/app)가 읽는 이름과 일치하고, 겹침 우선순위로 INFERENCE_BACKEND 가 workload 마다 맞다
  3. Worker·Relay 는 Service·Ingress 가 없고 Secret 은 pipeline-secrets(DB 계정 2개)뿐이다
  4. ServiceAccount·Namespace 이름이 infra Terraform 의 Pod Identity 변수 기본값과 같다
  5. 모든 ConfigMap / Secret / ServiceAccount 참조가 렌더링 결과 안에 있고 키가 있다
  6. KEDA: ScaledObject 대상·인증·큐 URL, backend HPA 와의 충돌 없음, sync-wave 순서, Argo CD 앱 의존성, 차트 버전의 Kubernetes 호환
  7. 노드 배치 계산(요청 기준): 공존 구성에서 Worker 가 몇 개까지 들어가는가
  8. 배포 전에 아직 없는 것(ECR worker 리포지토리, KEDA Pod Identity, CI 의 worker 빌드, worker 이미지 태그)
"""
from __future__ import annotations

import argparse
import importlib.util
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
INFRA = REPO.parent / "parking-guardian-infra"
APP_REPO = REPO.parent / "capston"
NAMESPACE = "parking-guardian"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


render_check = _load("render_check", "check-phase1-render.py")
placement = _load("placement", "check-placement.py")

failures: list[str] = []
warnings: list[str] = []


def check(ok: bool, message: str) -> None:
    print(("  ok    " if ok else "  FAIL  ") + message)
    if not ok:
        failures.append(message)


def todo(done: bool, message: str) -> None:
    """배포 전 준비 사항. 끝났으면 ok, 아니면 TODO(--strict 일 때만 실패)."""
    print(("  ok    " if done else "  TODO  ") + message)
    if not done:
        warnings.append(message)


# --- 순수 함수 (테스트 대상) -------------------------------------------------------------------------------------------

def index(objs: list[dict]) -> dict[tuple[str, str], dict]:
    return {(o["kind"], o["metadata"]["name"]): o for o in objs}


def containers_of(workload: dict) -> list[dict]:
    spec = workload["spec"]["template"]["spec"]
    return spec.get("initContainers", []) + spec["containers"]


def config_maps(objs: list[dict]) -> dict[str, dict]:
    return {o["metadata"]["name"]: o.get("data", {}) for o in objs if o["kind"] == "ConfigMap"}


def secrets_provided(objs: list[dict]) -> dict[str, set[str]]:
    """ExternalSecret 이 만들 Secret 의 {이름: 키 집합}."""
    return {o["spec"]["target"]["name"]: {d["secretKey"] for d in o["spec"]["data"]}
            for o in objs if o["kind"] == "ExternalSecret"}


def merged_env(container: dict, cms: dict[str, dict], secrets: dict[str, set[str]]) -> dict[str, str]:
    """envFrom 을 순서대로 합치고 env 로 덮는다. 같은 키는 **뒤가 이긴다**(Kubernetes 규칙). Secret 값은 '<secret>' 으로 표시."""
    env: dict[str, str] = {}
    for source in container.get("envFrom", []):
        if "configMapRef" in source:
            env.update(cms.get(source["configMapRef"]["name"], {}))
        elif "secretRef" in source:
            env.update({k: "<secret>" for k in secrets.get(source["secretRef"]["name"], set())})
    for item in container.get("env", []):
        env[item["name"]] = item.get("value", "<ref>")
    return env


def reference_problems(objs: list[dict]) -> list[str]:
    """Deployment·Job 이 가리키는 ConfigMap / Secret / ServiceAccount 가 렌더링 결과에 있고 키가 있는지."""
    cms, secrets = config_maps(objs), secrets_provided(objs)
    accounts = {o["metadata"]["name"] for o in objs if o["kind"] == "ServiceAccount"}
    problems = []
    for o in objs:
        if o["kind"] not in ("Deployment", "Job"):
            continue
        who = f"{o['kind']} {o['metadata']['name']}"
        spec = o["spec"]["template"]["spec"]
        account = spec.get("serviceAccountName")
        if account and account not in accounts:
            problems.append(f"{who}: ServiceAccount {account} 가 렌더링 결과에 없다")
        for c in containers_of(o):
            for source in c.get("envFrom", []):
                if "configMapRef" in source and source["configMapRef"]["name"] not in cms:
                    problems.append(f"{who}/{c['name']}: ConfigMap {source['configMapRef']['name']} 가 없다")
                if "secretRef" in source and source["secretRef"]["name"] not in secrets:
                    problems.append(f"{who}/{c['name']}: Secret {source['secretRef']['name']} 를 만드는 ExternalSecret 이 없다")
            for item in c.get("env", []):
                ref = item.get("valueFrom", {})
                if "configMapKeyRef" in ref:
                    r = ref["configMapKeyRef"]
                    if r["key"] not in cms.get(r["name"], {}):
                        problems.append(f"{who}/{c['name']}: ConfigMap {r['name']} 에 키 {r['key']} 가 없다")
                if "secretKeyRef" in ref:
                    r = ref["secretKeyRef"]
                    if r["key"] not in secrets.get(r["name"], set()):
                        problems.append(f"{who}/{c['name']}: Secret {r['name']} 에 키 {r['key']} 가 없다")
    return problems


def wave(obj: dict) -> int:
    return int(obj["metadata"].get("annotations", {}).get("argocd.argoproj.io/sync-wave", 0))


def wave_problems(objs: list[dict]) -> list[str]:
    """Phase 2 순서: 마이그레이션 Job(1) < worker·relay Deployment < HPA·ScaledObject 는 대상보다 뒤."""
    by = index(objs)
    problems = []
    job = by.get(("Job", "backend-migrate"))
    for name in ("worker", "relay"):
        dep = by.get(("Deployment", name))
        if dep is None:
            problems.append(f"Deployment {name} 이 없다")
        elif job is not None and wave(dep) <= wave(job):
            problems.append(f"{name} (wave {wave(dep)}) 은 마이그레이션 Job (wave {wave(job)}) 뒤여야 한다: 새 스키마 전에 새 코드가 뜨면 안 된다")
    for so in (o for o in objs if o["kind"] == "ScaledObject"):
        target = by.get(("Deployment", so["spec"]["scaleTargetRef"]["name"]))
        if target is None:
            problems.append(f"ScaledObject {so['metadata']['name']} 의 대상 Deployment 가 없다")
        elif wave(so) <= wave(target):
            problems.append(f"ScaledObject {so['metadata']['name']} (wave {wave(so)}) 은 대상 {target['metadata']['name']} (wave {wave(target)}) 보다 뒤여야 한다")
    # 기존 Phase 1 규칙 + Phase 2: HPA 가 worker/relay 의 wave 에 막히지 않아야 한다(worker 이미지가 없어도 HPA 가 적용된다)
    phase2_blocker = [n for n in ("worker", "relay") if (("Deployment", n) in by) and
                      any(wave(h) >= wave(by[("Deployment", n)]) for h in objs if h["kind"] == "HorizontalPodAutoscaler")]
    problems += [f"HPA 가 {n} 의 wave 이후다: {n} 이 Healthy 가 되지 않으면 Phase 1 의 HPA 적용이 막힌다" for n in phase2_blocker]
    return problems


def hpa_conflicts(objs: list[dict]) -> list[str]:
    """한 Deployment 를 두 오토스케일러가 건드리면 서로 되돌린다. KEDA 가 만든 HPA 는 렌더링 결과에 없다(런타임에 생긴다)."""
    targets: dict[str, list[str]] = {}
    for o in objs:
        if o["kind"] == "HorizontalPodAutoscaler":
            targets.setdefault(o["spec"]["scaleTargetRef"]["name"], []).append(f"HPA {o['metadata']['name']}")
        if o["kind"] == "ScaledObject":
            targets.setdefault(o["spec"]["scaleTargetRef"]["name"], []).append(f"ScaledObject {o['metadata']['name']}")
    problems = [f"{name} 를 {len(who)}개가 조절한다: {who}" for name, who in targets.items() if len(who) > 1]
    for o in objs:
        if o["kind"] == "Deployment" and o["metadata"]["name"] in targets and "replicas" in o["spec"]:
            problems.append(f"Deployment {o['metadata']['name']} 에 replicas 가 적혀 있다 — 오토스케일러의 결정을 selfHeal 이 되돌린다")
    return problems


def terraform_defaults(infra: Path) -> dict[str, str]:
    """infra cluster 스택 variables.tf 의 문자열 default. {변수: 값}"""
    text = (infra / "terraform" / "stacks" / "cluster" / "variables.tf").read_text(encoding="utf-8")
    found = {}
    for name, body in re.findall(r'variable "(\w+)" \{(.*?)\n\}', text, re.S):
        m = re.search(r'default\s*=\s*"([^"]*)"', body)
        if m:
            found[name] = m.group(1)
    return found


def app_env_names(app_repo: Path) -> set[str]:
    """앱 코드가 읽는 환경변수 이름 (os.environ.get / os.environ[] / os.getenv)."""
    names: set[str] = set()
    # os.environ.get("X") / os.getenv("X") / os.environ["X"] 와 config.py 의 헬퍼 _bool("X", ...) / _int("X", ...) 도 읽는 것으로 센다
    pattern = re.compile(r'(?:environ\.get|getenv|environ|\b_[a-z]+)\(\s*["\']([A-Z][A-Z0-9_]*)["\']|environ\[\s*["\']([A-Z][A-Z0-9_]*)["\']')
    roots = [app_repo / "backend" / "app", app_repo / "backend"]
    for root in roots:
        for path in (root.rglob("*.py") if root.name == "app" else root.glob("*.py")):
            for a, b in pattern.findall(path.read_text(encoding="utf-8", errors="replace")):
                names.add(a or b)
    return names


# 앱 코드는 읽지 않지만 매니페스트 안에서 쓰이는 키(자리표시자 치환, initContainer, Dockerfile CMD 등). 오타를 찾을 때 제외한다.
NOT_READ_BY_APP = {
    "ECR_BACKEND", "ECR_FRONTEND", "ECR_INFERENCE", "ECR_WORKER", "APP_HOST", "ACM_CERTIFICATE_ARN", "ALB_SECURITY_GROUP_ID",
    "SECRET_NAME_APP", "SECRET_NAME_SEED", "RDS_SECRET_ARN",          # kustomize replacements 의 원본
    "MODELS_BUCKET", "MODEL_VERSION",                                  # initContainer fetch-models
    "GUNICORN_THREADS", "MIGRATE_ON_START", "SEED_DEMO_DATA",          # Dockerfile CMD / 마이그레이션 Job 의 셸이 읽는다(앱 코드가 아니다)
    "GUNICORN_CMD_ARGS",                                               # gunicorn 자신이 읽는다 (--keep-alive 130, ALB 502 대응)
}

REQUIRED_BY_ROLE = {
    "worker": ("QUEUE_BACKEND", "SQS_QUEUE_URL", "SQS_DLQ_URL", "S3_BUCKET", "STORAGE_BACKEND", "INFERENCE_BACKEND",
               "DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD", "DB_SSLMODE", "AWS_REGION"),
    "relay": ("QUEUE_BACKEND", "SQS_QUEUE_URL", "SQS_DLQ_URL", "S3_BUCKET", "STORAGE_BACKEND", "INFERENCE_BACKEND", "INFERENCE_URL",
              "DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD", "DB_SSLMODE", "AWS_REGION"),
    "backend": ("QUEUE_BACKEND", "SQS_QUEUE_URL", "S3_BUCKET", "STORAGE_BACKEND", "INFERENCE_BACKEND", "INFERENCE_URL",
                "DB_HOST", "DB_USER", "DB_PASSWORD", "SECRET_KEY", "JWT_SECRET", "AWS_REGION"),
}


def env_problems(role: str, env: dict[str, str], known: set[str] | None) -> list[str]:
    problems = [f"{role}: 필요한 환경변수 {k} 가 없다" for k in REQUIRED_BY_ROLE[role] if k not in env]
    if known is not None:
        unknown = sorted(k for k in env if k not in known and k not in NOT_READ_BY_APP)
        problems += [f"{role}: 환경변수 {k} 는 앱 코드가 읽지 않는다(오타이거나 쓰이지 않는 값)" for k in unknown]
    return problems


# --- 렌더링과 확인 ---------------------------------------------------------------------------------------------------

def check_phase1_still_there(objs: list[dict]) -> None:
    by = index(objs)
    print("\n[Phase 1 유지]")
    for kind, name in [("Deployment", "backend"), ("Deployment", "inference"), ("Deployment", "frontend"), ("Service", "backend"),
                       ("Service", "inference"), ("Service", "frontend"), ("Ingress", "parking-guardian"), ("HorizontalPodAutoscaler", "backend"),
                       ("HorizontalPodAutoscaler", "inference"), ("PodDisruptionBudget", "backend"), ("PodDisruptionBudget", "inference"),
                       ("Job", "backend-migrate"), ("ExternalSecret", "backend-secrets"), ("ServiceAccount", "backend"), ("ServiceAccount", "inference")]:
        check((kind, name) in by, f"{kind} {name} 가 있다")
    users = sorted(o["metadata"]["name"] for o in objs if o["kind"] in ("Deployment", "Job")
                   for c in containers_of(o) for s in c.get("envFrom", []) if s.get("secretRef", {}).get("name") == "backend-secrets")
    check(users == ["backend", "backend-migrate"], f"backend-secrets 를 쓰는 것은 backend 와 마이그레이션 Job 뿐이다 (실제: {users})")
    backends = {p["backend"]["service"]["name"] for r in by[("Ingress", "parking-guardian")]["spec"]["rules"] for p in r["http"]["paths"]}
    check(backends == {"backend", "frontend"}, f"Ingress 는 backend·frontend 만 노출한다 (실제: {sorted(backends)})")
    inf = by[("Deployment", "inference")]["spec"]["template"]["spec"]
    check([c["name"] for c in containers_of(by[("Deployment", "inference")])] == ["fetch-models", "inference"] and inf["serviceAccountName"] == "inference",
          "inference 는 그대로다 (initContainer 1 + 컨테이너 1, ServiceAccount inference)")


def check_workloads(objs: list[dict]) -> None:
    by, cms, secrets = index(objs), config_maps(objs), secrets_provided(objs)
    known = app_env_names(APP_REPO) if (APP_REPO / "backend" / "app").exists() else None
    print("\n[API·Worker·Relay 환경변수와 설정]")
    if known is None:
        print(f"  skip  앱 코드 위치({APP_REPO})가 없어 환경변수 이름 대조를 건너뛴다")
    api = next(c for c in containers_of(by[("Deployment", "backend")]) if c["name"] == "backend")
    worker = next(c for c in containers_of(by[("Deployment", "worker")]) if c["name"] == "worker")
    relay = next(c for c in containers_of(by[("Deployment", "relay")]) if c["name"] == "relay")
    envs = {"backend": merged_env(api, cms, secrets), "worker": merged_env(worker, cms, secrets), "relay": merged_env(relay, cms, secrets)}
    for role, env in envs.items():
        problems = env_problems(role, env, known)
        check(not problems, f"{role}: 필요한 환경변수가 모두 있고 앱 코드가 읽는 이름과 일치한다" + (f" — {problems}" if problems else ""))
    check(all(envs[r].get("QUEUE_BACKEND") == "sqs" for r in envs), "API·Worker·Relay 모두 QUEUE_BACKEND=sqs")
    check(envs["worker"].get("INFERENCE_BACKEND") == "yolo", f"Worker 는 INFERENCE_BACKEND=yolo (envFrom 겹침에서 worker-config 가 backend-config 를 이긴다) — 실제 {envs['worker'].get('INFERENCE_BACKEND')}")
    check(envs["backend"].get("INFERENCE_BACKEND") == "http" and envs["relay"].get("INFERENCE_BACKEND") == "http",
          "API·Relay 는 INFERENCE_BACKEND=http (Phase 1 동기 경로가 inference 서비스를 부른다)")
    order = [list(s.values())[0]["name"].rsplit("-", 1)[0] if "configMapRef" in s else list(s.values())[0]["name"] for s in worker["envFrom"]]
    check(order.index("worker-config") > order.index("backend-config"), f"Worker envFrom 순서: worker-config 가 backend-config 뒤 ({order})")
    check("S3_PREFIX" not in envs["worker"] and "S3_PREFIX" not in envs["relay"] and "S3_PREFIX" not in envs["backend"],
          "S3_PREFIX 가 설정되어 있지 않다 (Terraform 의 inbox/ 수명 주기·IAM 경로가 접두어 없는 키를 전제한다)")
    check(envs["worker"].get("STORAGE_BACKEND") == "s3", "STORAGE_BACKEND=s3 (inbox 와 증거 사진은 S3)")
    for name in ("SQS_QUEUE_URL", "SQS_DLQ_URL"):
        check(envs["worker"].get(name, "").endswith(".fifo") or name == "SQS_DLQ_URL" and envs["worker"].get(name, "").endswith("-dlq.fifo"),
              f"{name} 는 FIFO 큐 URL 이다")

    print("\n[Worker]")
    wdep = by[("Deployment", "worker")]
    wspec = wdep["spec"]["template"]["spec"]
    check(wspec["serviceAccountName"] == "worker" and wspec.get("automountServiceAccountToken") is False, "ServiceAccount worker, 토큰 미마운트")
    check(worker["command"] == ["flask", "judge-worker"], f"실행 명령 flask judge-worker ({worker['command']})")
    secret_refs = sorted(s["secretRef"]["name"] for s in worker["envFrom"] if "secretRef" in s)
    check(secret_refs == ["pipeline-secrets"], f"Worker 의 Secret 은 pipeline-secrets 뿐이다 (JWT·시드 비밀 없음) — 실제 {secret_refs}")
    check(secrets["pipeline-secrets"] == {"DB_USER", "DB_PASSWORD"}, f"pipeline-secrets 의 키는 DB_USER, DB_PASSWORD 뿐이다 ({sorted(secrets['pipeline-secrets'])})")
    res = worker["resources"]
    check(render_check.cpu_m(res["requests"]["cpu"]) == 1000 and render_check.mem_mib(res["requests"]["memory"]) == 1536
          and render_check.mem_mib(res["limits"]["memory"]) == 2048 and "cpu" not in res["limits"],
          f"요청 1000m / 1.5Gi, 메모리 limit 2Gi, CPU limit 없음 (inference 와 같은 시작값, EKS 실측으로 조정) — 실제 {res}")
    check(wspec["terminationGracePeriodSeconds"] >= 60, f"종료 유예 ≥ 60초 (= SQS visibility 60초 ≥ lease 55초) — 실제 {wspec['terminationGracePeriodSeconds']}")
    check([c["name"] for c in wspec.get("initContainers", [])] == ["fetch-models"] and any(v["name"] == "models" for v in wspec["volumes"]),
          "initContainer fetch-models 와 모델 emptyDir 이 있다")
    check(not any(k in wdep["spec"] for k in ("replicas",)), "Worker Deployment 에 replicas 를 적지 않는다 (KEDA 가 정한다)")
    check(wdep["spec"]["strategy"]["rollingUpdate"] == {"maxSurge": 0, "maxUnavailable": 1}, "롤링은 먼저 내리고 띄운다 (maxSurge 0 — surge 자리가 없다)")
    check("livenessProbe" not in worker and "readinessProbe" not in worker, "probe 가 없다 (HTTP 서버가 없는 CLI 프로세스. 위험 R3)")
    check(("Service", "worker") not in by and ("Ingress", "worker") not in by, "Worker 에 Service·Ingress 가 없다")
    check(re.search(r"/parking-guardian/worker:\S+$", worker["image"]) is not None, f"Worker 이미지는 ECR worker 리포지토리 (레지스트리 채워짐) — {re.sub(r'[0-9]{12}', '<acct>', worker['image'])}")

    print("\n[Relay]")
    rdep = by[("Deployment", "relay")]
    rspec = rdep["spec"]["template"]["spec"]
    check(rspec["serviceAccountName"] == "relay" and rspec.get("automountServiceAccountToken") is False, "ServiceAccount relay, 토큰 미마운트")
    check(relay["command"] == ["flask", "outbox-relay"], f"실행 명령 flask outbox-relay ({relay['command']})")
    check(sorted(s["secretRef"]["name"] for s in relay["envFrom"] if "secretRef" in s) == ["pipeline-secrets"], "Relay 의 Secret 은 pipeline-secrets 뿐이다")
    check(rdep["spec"]["replicas"] == 1, "Relay 1개 (여러 개도 안전하지만 HA 는 필요 없다)")
    check(relay["image"] == api["image"], "Relay 이미지는 backend(api) 이미지와 같다 (새 이미지 불필요)")
    check(not any(c.get("volumeMounts") for c in rspec["containers"]) and not rspec.get("initContainers"), "Relay 에 모델 볼륨·initContainer 가 없다")
    check(("Service", "relay") not in by and ("Ingress", "relay") not in by, "Relay 에 Service·Ingress 가 없다")
    check(rspec["terminationGracePeriodSeconds"] <= 30, f"Relay 종료 유예가 짧다 (SIGTERM 이 무시되는 PID 1 이라 길 이유가 없다) — {rspec['terminationGracePeriodSeconds']}")


def check_keda(objs: list[dict]) -> None:
    by = index(objs)
    print("\n[KEDA ScaledObject]")
    so, auth = by[("ScaledObject", "worker")], by[("TriggerAuthentication", "worker-sqs-auth")]
    trig = so["spec"]["triggers"][0]
    cms = config_maps(objs)
    queue_url = next(v for k, d in cms.items() if k.startswith("backend-config") for kk, v in d.items() if kk == "SQS_QUEUE_URL")
    check(so["spec"]["scaleTargetRef"]["name"] == "worker" and ("Deployment", "worker") in by, "대상은 Deployment worker")
    check(trig["type"] == "aws-sqs-queue" and trig["metadata"]["queueURL"] == queue_url, "트리거 aws-sqs-queue, queueURL 이 config.env 의 SQS_QUEUE_URL 과 같다")
    check(trig["metadata"]["queueURL"].endswith(".fifo") and trig["metadata"]["awsRegion"] == "ap-northeast-2", "FIFO 큐, 리전 ap-northeast-2")
    check(trig["authenticationRef"]["name"] == "worker-sqs-auth" and auth["spec"]["podIdentity"] == {"provider": "aws"},
          "인증은 TriggerAuthentication podIdentity.provider=aws, roleArn 없음 (KEDA operator 의 Pod Identity 를 쓴다)")
    s = so["spec"]
    check((s["minReplicaCount"], s["maxReplicaCount"]) == (0, 1),
          f"최소 0 / 최대 1 (공존 구성의 초기 검증용: 유휴일 때 노드를 비워 배포가 막히지 않게 한다) — 실제 {s['minReplicaCount']} / {s['maxReplicaCount']}")
    check(trig["metadata"].get("activationQueueLength", "0") == "0", "0 → 1 활성화는 큐에 메시지가 하나라도 있으면 (activationQueueLength 기본 0)")
    check(s["pollingInterval"] == 15 and s["cooldownPeriod"] == 300 and trig["metadata"]["queueLength"] == "5",
          "pollingInterval 15 / cooldownPeriod 300 / queueLength 5")
    check(s["fallback"]["replicas"] >= 1 and trig["type"] == "aws-sqs-queue",
          "fallback 1 이상 (이미 떠 있는 Worker 를 0 으로 줄이지 않는다. 0 개일 때의 인증 오류는 못 구한다: READY 확인이 필요 — 문서)")
    check(s["advanced"]["horizontalPodAutoscalerConfig"]["behavior"]["scaleDown"]["stabilizationWindowSeconds"] == 300, "축소 안정화 300초 (모델 재다운로드 비용)")
    anns = so["metadata"]["annotations"]
    check("SkipDryRunOnMissingResource=true" in anns["argocd.argoproj.io/sync-options"], "CRD 가 아직 없어도 dry-run 을 건너뛴다 (Application retry 가 재시도)")
    problems = hpa_conflicts(objs)
    check(not problems, "backend HPA / inference HPA 와 KEDA 의 대상이 겹치지 않고 replicas 충돌이 없다" + (f" — {problems}" if problems else ""))
    problems = wave_problems(objs)
    check(not problems, "sync-wave 순서: 마이그레이션(1) < worker·relay(4) < ScaledObject(5), HPA(3) 는 worker 에 막히지 않는다" + (f" — {problems}" if problems else ""))
    problems = render_check.hpa_wave_problems(objs)
    check(not problems, "기존 규칙: 모든 HPA 가 대상 Deployment 보다 늦은 wave 다" + (f" — {problems}" if problems else ""))


def check_references(objs: list[dict]) -> None:
    print("\n[참조 관계]")
    problems = reference_problems(objs)
    check(not problems, "모든 ConfigMap / Secret / ServiceAccount 참조가 렌더링 결과 안에 있고 키가 있다" + (f" — {problems}" if problems else ""))
    check({o["metadata"]["namespace"] for o in objs if o["kind"] != "Namespace"} == {NAMESPACE}, f"모든 리소스가 {NAMESPACE} Namespace 다")
    leftovers = [o["metadata"]["name"] for o in objs if o["kind"] in ("Deployment", "Job") for c in containers_of(o) if c["image"].startswith("parking-guardian/")]
    check(not leftovers, f"레지스트리가 비어 있는 이미지가 없다 ({leftovers})")


def check_terraform_contract(objs: list[dict]) -> None:
    print("\n[Terraform Pod Identity 와의 계약]")
    if not (INFRA / "terraform").exists():
        print(f"  skip  infra 리포({INFRA})가 없어 건너뛴다")
        return
    tf = terraform_defaults(INFRA)
    by = index(objs)
    for var, name, deployment in [("backend_service_account", "backend", "backend"), ("inference_service_account", "inference", "inference"),
                                  ("worker_service_account", "worker", "worker"), ("relay_service_account", "relay", "relay")]:
        sa = by.get(("ServiceAccount", name))
        spec_name = by[("Deployment", deployment)]["spec"]["template"]["spec"]["serviceAccountName"]
        check(tf.get(var) == name == spec_name and sa is not None,
              f"ServiceAccount {name}: Terraform {var}={tf.get(var)!r}, 매니페스트 {name!r}, Deployment 가 쓰는 이름 {spec_name!r}")
    check(tf.get("app_namespace") == NAMESPACE, f"Namespace: Terraform app_namespace={tf.get('app_namespace')!r} == {NAMESPACE!r}")
    iam2 = (INFRA / "terraform" / "stacks" / "cluster" / "iam_phase2.tf")
    check(iam2.exists() and "aws_eks_pod_identity_association\" \"worker\"" in iam2.read_text(encoding="utf-8") and
          "aws_eks_pod_identity_association\" \"relay\"" in iam2.read_text(encoding="utf-8"),
          "Terraform 에 worker·relay Pod Identity 연결이 정의되어 있다 (cluster 스택 apply 는 재구축 때)")


def check_argocd() -> None:
    print("\n[Argo CD 앱 구조]")
    apps = {p.stem: yaml.safe_load(p.read_text(encoding="utf-8")) for p in (REPO / "argocd" / "apps").glob("*.yaml")}
    check({"platform", "parking-guardian", "monitoring", "monitoring-config", "keda"} <= set(apps), f"argocd/apps 에 기존 4개 + keda ({sorted(apps)})")
    keda = apps["keda"]
    chart = next(s for s in keda["spec"]["sources"] if "chart" in s)
    values_source = next(s for s in keda["spec"]["sources"] if s.get("ref") == "values")
    values_file = REPO / chart["helm"]["valueFiles"][0].replace("$values/", "")
    check(chart["chart"] == "keda" and chart["repoURL"] == "https://kedacore.github.io/charts", "KEDA 차트 저장소·이름")
    check(values_file.exists(), f"values 파일이 있다 ({values_file.relative_to(REPO)})")
    check(values_source["repoURL"] == apps["parking-guardian"]["spec"]["source"]["repoURL"], "values 소스가 이 config 리포다")
    sync = keda["spec"]["syncPolicy"]
    check("ServerSideApply=true" in sync["syncOptions"] and "CreateNamespace=true" in sync["syncOptions"], "KEDA 앱: ServerSideApply(CRD 가 큼)·CreateNamespace(keda)")
    check(keda["spec"]["destination"]["namespace"] == "keda", "KEDA 는 Namespace keda (Pod Identity 연결 대상 keda/keda-operator)")
    check(apps["parking-guardian"]["spec"]["syncPolicy"].get("retry", {}).get("limit", 0) >= 5, "parking-guardian 앱의 retry (CRD 가 생길 때까지 ScaledObject 재시도)")
    check(all(a["spec"]["source"]["path"] != "platform/keda" for a in apps.values() if "source" in a), "platform 앱은 platform/ 만 읽고 platform/keda/(values)를 매니페스트로 적용하지 않는다")
    values = yaml.safe_load(values_file.read_text(encoding="utf-8"))
    check(values["serviceAccount"]["operator"]["name"] == "keda-operator", "operator ServiceAccount 이름 keda-operator (Pod Identity 연결 계약)")
    version = chart["targetRevision"]
    tested = KEDA_TESTED_K8S.get(version)
    eks = terraform_defaults(INFRA).get("eks_version") if (INFRA / "terraform").exists() else None
    if tested and eks:
        check(tested[0] <= float(eks) <= tested[1], f"KEDA {version} 가 시험된 Kubernetes {tested[0]}~{tested[1]} 안에 EKS {eks} 가 있다")
    else:
        print(f"  skip  KEDA {version} 호환 표에 없거나 EKS 버전을 읽지 못했다")


KEDA_TESTED_K8S = {"2.21.0": (1.34, 1.36), "2.20.2": (1.33, 1.35)}  # keda.sh/docs/<버전>/operate/cluster/ (2026-11-03 확인)


def keda_requests() -> tuple[float, float]:
    values = yaml.safe_load((REPO / "platform" / "keda" / "values.yaml").read_text(encoding="utf-8"))
    cpu = sum(render_check.cpu_m(values["resources"][p]["requests"]["cpu"]) for p in ("operator", "metricServer", "webhooks"))
    mem = sum(render_check.mem_mib(values["resources"][p]["requests"]["memory"]) for p in ("operator", "metricServer", "webhooks"))
    return cpu, mem


def check_placement(objs: list[dict]) -> None:
    print("\n[노드 배치 계산 (요청 기준, 노드 3대)]")
    by = index(objs)
    req = {n: placement.effective_request(by[("Deployment", n)]["spec"]["template"]["spec"]) for n in ("backend", "inference", "worker", "relay", "frontend")}
    job = placement.effective_request(by[("Job", "backend-migrate")]["spec"]["template"]["spec"])
    keda_cpu, keda_mem = keda_requests()
    free_cpu, free_mem = placement.node_free(False, 0, 0)
    floating_cpu = req["frontend"][0] + req["relay"][0] + keda_cpu
    floating_mem = req["frontend"][1] + req["relay"][1] + keda_mem + placement.FLOATING_MEM_MI
    print(f"  요청: api {req['backend'][0]:.0f}m/{req['backend'][1]:.0f}Mi, inference {req['inference'][0]:.0f}m/{req['inference'][1]:.0f}Mi, "
          f"worker {req['worker'][0]:.0f}m/{req['worker'][1]:.0f}Mi, relay {req['relay'][0]:.0f}m/{req['relay'][1]:.0f}Mi, "
          f"frontend {req['frontend'][0]:.0f}m, KEDA 3개 {keda_cpu:.0f}m/{keda_mem:.0f}Mi, 마이그레이션 Job {job[0]:.0f}m")
    print(f"  노드당 앱 가용 {free_cpu}m / {free_mem}Mi (상수, 재구축 뒤 kubectl describe nodes 로 다시 잰다). 노드에 묶이지 않는 Pod 합 {floating_cpu:.0f}m / {floating_mem:.0f}Mi")
    scenarios = [
        # (제목, api, inference, worker, migrate job, 기대: best-case 가능 여부)
        ("공존, Worker 0 (유휴): api 2 + inference 2", 2, 2, 0, 0, True),
        ("공존, Worker 1 (처리 중): api 2 + inference 2 + worker 1", 2, 2, 1, 0, True),
        ("공존, Worker 2: api 2 + inference 2 + worker 2", 2, 2, 2, 0, False),
        ("공존, 배포 중 마이그레이션 Job, Worker 0", 2, 2, 0, 1, True),
        ("공존, 배포 중 마이그레이션 Job, Worker 1", 2, 2, 1, 1, False),
        ("공존, inference 롤링 surge, Worker 0", 2, 3, 0, 0, True),
        ("공존, inference 롤링 surge, Worker 1", 2, 3, 1, 0, False),
        ("공존, api 롤링 surge 또는 api HPA 3 (api 3), Worker 0", 3, 2, 0, 0, True),
        ("공존, api 롤링 surge 또는 api HPA 3 (api 3), Worker 1", 3, 2, 1, 0, False),
        # 묶이지 않는 작은 Pod 의 surge(+50m): frontend / relay / KEDA operator. 들어가지만 여유가 5m 로 줄어든다
        ("공존, Worker 1, frontend·relay·KEDA 중 하나의 롤링 surge(+50m)", 2, 2, 1, 0, True, 50),
        ("공존, HPA 최대(api 3 + inference 3), Worker 1", 3, 3, 1, 0, False),
        ("비교 구성(inference 0): api 2 + worker 2", 2, 0, 2, 0, True),
        ("비교 구성(inference 0): api 2 + worker 2 + 마이그레이션 Job", 2, 0, 2, 1, True),
        ("비교 구성(inference 0): api 2 + worker 3", 2, 0, 3, 0, True),
        ("비교 구성(inference 0): api 2 + worker 3 + 마이그레이션 Job", 2, 0, 3, 1, False),
    ]
    print("  | 시나리오 | best-case | 남는 CPU(최소 노드) |\n  |---|---|---|")
    for title, n_api, n_inf, n_w, n_job, want, *extra in scenarios:
        pods = ([("api", *req["backend"])] * n_api + [("inference", *req["inference"])] * n_inf + [("worker", *req["worker"])] * n_w +
                [("migrate", *job)] * n_job)
        surge = extra[0] if extra else 0
        best = placement.best_case(pods, free_cpu, free_mem, floating_cpu + surge, floating_mem + surge * 2)
        shown = f"가능 ({placement.describe([[p[0] for p, node in zip(pods, best[0]) if node == i] for i in range(placement.NODES)])})" if best else "**불가능**"
        print(f"  | {title} | {shown} | {best[2]:.0f}m |" if best else f"  | {title} | {shown} | - |")
        check((best is not None) == want, f"{title}: 기대 {'가능' if want else '불가능'}")


def render_with_comparison_patches() -> list[dict]:
    """apps/parking-guardian 을 임시 폴더로 복사하고 comparison-mode.example.yaml 의 patches 블록을 kustomization.yaml 끝에 붙여 렌더링한다."""
    app = REPO / "apps" / "parking-guardian"
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "app"
        shutil.copytree(app, work)
        example = (app / "comparison-mode.example.yaml").read_text(encoding="utf-8")
        block = example[example.index("\npatches:"):]
        with (work / "kustomization.yaml").open("a", encoding="utf-8", newline="\n") as f:
            f.write("\n" + block.lstrip("\n"))
        done = subprocess.run(["kubectl", "kustomize", str(work)], capture_output=True, text=True, encoding="utf-8")
        if done.returncode:
            check(False, f"비교 구성 패치를 붙인 렌더링 실패: {done.stderr.strip()[:300]}")
            return []
        return [o for o in yaml.safe_load_all(done.stdout) if o]


def check_comparison_mode() -> None:
    print("\n[비교 구성 패치 (comparison-mode.example.yaml)]")
    objs = render_with_comparison_patches()
    if not objs:
        return
    by = index(objs)
    removed_kinds = {"Deployment", "Service", "HorizontalPodAutoscaler", "PodDisruptionBudget", "PodMonitor"}
    left = sorted(f"{k} {n}" for (k, n) in by if n == "inference" and k in removed_kinds)   # ServiceAccount·ConfigMap 은 남아도 해가 없다
    check(left == [], f"inference 의 Deployment·Service·HPA·PDB·PodMonitor 가 없다 (남은 것: {left})")
    so = by[("ScaledObject", "worker")]["spec"]
    check((so["minReplicaCount"], so["maxReplicaCount"]) == (1, 3), f"Worker 최소 1 / 최대 3 — 실제 {so['minReplicaCount']} / {so['maxReplicaCount']}")
    check(all(k in by for k in (("Deployment", "backend"), ("Deployment", "worker"), ("Deployment", "relay"), ("Ingress", "parking-guardian"))),
          "backend·worker·relay·Ingress 는 그대로다")
    problems = reference_problems(objs) + hpa_conflicts(objs)
    check(not problems, "참조 관계와 오토스케일러 충돌에 문제가 없다" + (f" — {problems}" if problems else ""))


def prerequisites(objs: list[dict]) -> None:
    print("\n[배포 전에 필요한 준비 사항 — TODO 는 --strict 에서 실패다]")
    env = dict(line.split("=", 1) for line in (REPO / "apps" / "parking-guardian" / "config.env").read_text(encoding="utf-8").splitlines()
               if "=" in line and not line.startswith("#"))
    todo(all(env.get(k) for k in ("SQS_QUEUE_URL", "SQS_DLQ_URL", "ECR_WORKER")), "config.env 에 SQS_QUEUE_URL / SQS_DLQ_URL / ECR_WORKER 가 있다")
    kust = (REPO / "apps" / "parking-guardian" / "kustomization.yaml").read_text(encoding="utf-8")
    m = re.search(r"- name: parking-guardian/worker\s*\n\s+newTag: \"?([^\"\s]+)\"?", kust)
    todo(bool(m) and m.group(1) != "unset", f"images: 의 worker 태그가 \"unset\" 이 아니다 (실제: {m.group(1) if m else '항목 없음'})")
    ecr = INFRA / "terraform" / "stacks" / "data" / "storage_ecr.tf"
    todo(ecr.exists() and re.search(r'ecr_repositories\s*=\s*toset\(\[[^\]]*"worker"', ecr.read_text(encoding="utf-8")) is not None,
         "infra data 스택에 ECR 리포지토리 worker 가 있다 (storage_ecr.tf 의 ecr_repositories. CI 역할의 push 권한은 cicd.tf 가 모든 리포지토리를 대상으로 하므로 같이 생긴다)")
    workflow = APP_REPO / ".github" / "workflows" / "build-push.yml"
    todo(workflow.exists() and "worker" in workflow.read_text(encoding="utf-8"), "앱 리포 CI(build-push.yml)가 worker 이미지(prototype 타깃)를 빌드해 올리고 태그를 config 리포에 쓴다")
    cluster_dir = INFRA / "terraform" / "stacks" / "cluster"
    todo(cluster_dir.exists() and any("keda-operator" in p.read_text(encoding="utf-8") for p in cluster_dir.glob("*.tf")),
         "infra 에 KEDA operator 용 IAM 역할과 Pod Identity 연결(keda/keda-operator, sqs:GetQueueAttributes)이 있다")
    todo(cluster_dir.exists() and (cluster_dir / "iam_phase2.tf").exists(), "infra cluster 스택에 worker·relay 역할 코드가 있다 (apply 는 재구축 때)")
    if (APP_REPO / "backend" / "app" / "cli.py").exists():
        cli = (APP_REPO / "backend" / "app" / "cli.py").read_text(encoding="utf-8")
        worker_py = (APP_REPO / "backend" / "app" / "services" / "worker.py").read_text(encoding="utf-8")
        todo("install_stop_handlers" in cli and "SIGTERM" in cli and "request_stop" in worker_py,
             "앱의 judge-worker / outbox-relay 가 SIGTERM 에서 현재 작업을 끝내고 종료한다 (위험 R2, 2026-11-03 구현)")
        todo("def prepare" in worker_py and "preload" in worker_py and ".serve(" in cli,
             "앱의 judge-worker 가 첫 receive 전에 모델을 읽고 한 번 돌려 본다 (preload, 위험 R1, 2026-11-03 구현)")


def render() -> list[dict]:
    return render_check.render()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--strict", action="store_true", help="배포 전 준비 사항(TODO)이 남아 있으면 실패로 끝낸다")
    args = parser.parse_args()
    objs = render()
    check_phase1_still_there(objs)
    check_workloads(objs)
    check_keda(objs)
    check_references(objs)
    check_terraform_contract(objs)
    check_argocd()
    check_placement(objs)
    check_comparison_mode()
    prerequisites(objs)
    print()
    if failures:
        print(f"실패 {len(failures)}건")
        return 1
    if warnings:
        print(f"렌더링·계약 검사는 모두 통과. 배포 전 준비 사항 {len(warnings)}건이 남아 있다" + (" → --strict 라서 실패" if args.strict else " (배포 전에 끝낸다)"))
        return 1 if args.strict else 0
    print("모두 통과")
    return 0


if __name__ == "__main__":
    sys.exit(main())
