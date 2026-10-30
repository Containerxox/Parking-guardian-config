#!/usr/bin/env python3
"""Phase 1 매니페스트를 렌더링(kubectl kustomize)해서 설계대로인지 확인한다. 클러스터에는 아무것도 하지 않는다.

    python scripts/check-phase1-render.py            # apps/parking-guardian 을 임시 폴더로 복사해서 렌더링
    python scripts/check-phase1-render.py --print    # 렌더링 결과 YAML 도 출력

config.env 에 ECR_INFERENCE 가 아직 없으면(infra data 스택 apply 전) 임시 복사본에만 가짜 값을 넣는다. 저장소의 config.env 는
건드리지 않는다. 실제 값은 apply 뒤 `scripts/export-config-env.py` 로 만든다.

확인하는 것 (설계 3.2, 3.3, 3.5.1, 3.7, 9.1):
  - inference 에 DB·Secret·사진 버킷 정보가 들어가지 않는다 (AI 는 판정만)
  - inference Deployment / Service / HPA / PDB / PodMonitor / ServiceAccount 가 있고 값이 설계와 같다
  - backend 에서 모델 initContainer 와 모델 볼륨이 빠졌고, 요청이 줄었으며, AI 를 http 로 부른다
  - 이미지 3개의 레지스트리·태그 규칙, ConfigMap 키
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
APP_DIR = REPO / "apps" / "parking-guardian"
FAKE_ECR_INFERENCE = "000000000000.dkr.ecr.ap-northeast-2.amazonaws.com/parking-guardian/inference"

failures: list[str] = []


def check(ok: bool, message: str) -> None:
    print(("  ok    " if ok else "  FAIL  ") + message)
    if not ok:
        failures.append(message)


def cpu_m(value) -> float:
    text = str(value)
    return float(text[:-1]) if text.endswith("m") else float(text) * 1000


def mem_mib(value) -> float:
    text = str(value)
    for suffix, factor in (("Gi", 1024), ("Mi", 1), ("Ki", 1 / 1024)):
        if text.endswith(suffix):
            return float(text[: -len(suffix)]) * factor
    return float(text) / 1024 / 1024


def render() -> list[dict]:
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "parking-guardian"
        shutil.copytree(APP_DIR, work)
        env = work / "config.env"
        text = env.read_text(encoding="utf-8")
        if "ECR_INFERENCE=" not in text:
            env.write_text(text.rstrip("\n") + f"\nECR_INFERENCE={FAKE_ECR_INFERENCE}\n", encoding="utf-8", newline="\n")
        done = subprocess.run(["kubectl", "kustomize", str(work)], capture_output=True, text=True, encoding="utf-8")
    if done.returncode:
        sys.exit(f"kubectl kustomize 실패:\n{done.stderr}")
    return [d for d in yaml.safe_load_all(done.stdout) if d]


def pick(objs: list[dict], kind: str, name: str) -> dict | None:
    return next((o for o in objs if o["kind"] == kind and o["metadata"]["name"] == name), None)


def pick_prefix(objs: list[dict], kind: str, prefix: str) -> dict | None:
    return next((o for o in objs if o["kind"] == kind and o["metadata"]["name"].startswith(prefix)), None)


def pod_spec(deployment: dict) -> dict:
    return deployment["spec"]["template"]["spec"]


def container(deployment: dict, name: str) -> dict:
    return next(c for c in pod_spec(deployment)["containers"] if c["name"] == name)


def check_inference(objs: list[dict]) -> None:
    print("\n[inference] 있어야 하는 것")
    names = {k: pick(objs, k, "inference") for k in ("Deployment", "Service", "HorizontalPodAutoscaler",
                                                    "PodDisruptionBudget", "PodMonitor", "ServiceAccount")}
    for kind, obj in names.items():
        check(obj is not None, f"{kind} inference 가 있다")
    if not all(names.values()):
        return
    dep = names["Deployment"]
    spec = pod_spec(dep)
    main = container(dep, "inference")

    print("\n[inference] 권한과 노출 범위")
    check(spec["serviceAccountName"] == "inference", "ServiceAccount inference (backend 와 다르다)")
    check(spec.get("automountServiceAccountToken") is False, "ServiceAccount 토큰을 마운트하지 않는다 (Kubernetes API 호출 없음)")
    secret_refs = [s for c in spec["containers"] + spec.get("initContainers", [])
                   for s in ([e["secretRef"]["name"] for e in c.get("envFrom", []) if "secretRef" in e] +
                             [e["valueFrom"]["secretKeyRef"]["name"] for e in c.get("env", []) if "secretKeyRef" in e.get("valueFrom", {})])]
    check(not secret_refs, f"Secret 을 참조하지 않는다 (실제: {secret_refs})")
    check(not any("secret" in v for v in spec.get("volumes", []) for v in [v]), "Secret 볼륨이 없다")
    check([v["name"] for v in spec.get("volumes", [])] == ["models"], f"볼륨은 모델용 emptyDir 하나뿐이다 (실제: {[v['name'] for v in spec.get('volumes', [])]})")
    env_from_maps = [e["configMapRef"]["name"] for c in spec["containers"] for e in c.get("envFrom", []) if "configMapRef" in e]
    check(len(env_from_maps) == 1 and env_from_maps[0].startswith("inference-config"),
          f"envFrom 은 전용 ConfigMap inference-config 하나뿐이다 — backend-config(DB 호스트, 사진 버킷, 비밀 이름)를 통째로 받지 않는다 (실제: {env_from_maps})")
    key_refs = {e["valueFrom"]["configMapKeyRef"]["key"] for c in spec["containers"] + spec.get("initContainers", [])
                for e in c.get("env", []) if "configMapKeyRef" in e.get("valueFrom", {})}
    check(key_refs == {"MODELS_BUCKET", "MODEL_VERSION", "AWS_REGION"},
          f"backend-config 에서 가져오는 키는 모델 다운로드에 필요한 셋뿐이다 (실제: {sorted(key_refs)})")
    forbidden = re.compile(r"^(DB_|DATABASE|SECRET|JWT|SEED|S3_BUCKET|CORS|COOKIE|RDS_|ACM_|ALB_)")
    plain_env = [e["name"] for c in spec["containers"] + spec.get("initContainers", []) for e in c.get("env", [])]
    check(not [n for n in plain_env if forbidden.match(n)], f"DB·비밀·사진 버킷 환경변수가 없다 (실제 env: {plain_env})")
    cfg = pick_prefix(objs, "ConfigMap", "inference-config")
    check(cfg is not None and not [k for k in cfg["data"] if forbidden.match(k)],
          f"inference-config 에 DB·비밀 키가 없다 (키: {sorted(cfg['data']) if cfg else None})")
    sc = main.get("securityContext", {})
    check(sc.get("allowPrivilegeEscalation") is False and sc.get("capabilities", {}).get("drop") == ["ALL"],
          "권한 상승 금지, capability 전부 제거")
    svc = names["Service"]
    check(svc["spec"].get("type", "ClusterIP") == "ClusterIP" and svc["spec"]["ports"][0]["port"] == 8000,
          "Service 는 ClusterIP 8000 (클러스터 안에서만)")
    check(not any(k.startswith("alb.ingress") for k in svc["metadata"].get("annotations", {})), "ALB 가 이 Service 를 보지 않는다")
    ingress = pick(objs, "Ingress", "parking-guardian")
    backends = [p["backend"]["service"]["name"] for r in ingress["spec"]["rules"] for p in r["http"]["paths"]] if ingress else []
    check("inference" not in backends, f"Ingress 가 inference 를 노출하지 않는다 (백엔드: {sorted(set(backends))})")

    print("\n[inference] 자원·Probe·배치 (설계 3.5.1, 3.7)")
    res = main["resources"]
    check(cpu_m(res["requests"]["cpu"]) == 1000 and mem_mib(res["requests"]["memory"]) == 1536, f"요청 1000m / 1.5Gi (추정값, E0 뒤 조정) — 실제 {res['requests']}")
    check(mem_mib(res["limits"]["memory"]) == 2048 and "cpu" not in res["limits"], f"메모리 limit 2Gi, CPU limit 없음 — 실제 {res['limits']}")
    startup, ready, live = main["startupProbe"], main["readinessProbe"], main["livenessProbe"]
    check(startup["httpGet"]["path"] == "/readyz" and startup["periodSeconds"] * startup["failureThreshold"] == 180,
          f"startupProbe /readyz 5초 × 36회 = 180초 (실제 {startup['periodSeconds']}초 × {startup['failureThreshold']})")
    check(ready["httpGet"]["path"] == "/readyz", "readinessProbe /readyz (모델 ready 일 때만 Service 에 들어간다)")
    check(live["httpGet"]["path"] == "/healthz" and live["periodSeconds"] == 10 and live["failureThreshold"] == 3,
          "livenessProbe /healthz 10초 × 3회 (모델 로딩 포기 시 503 → 재시작)")
    check({p["name"]: p["containerPort"] for p in main["ports"]} == {"http": 8000, "metrics": 9102}, "포트 http 8000, metrics 9102")
    check(spec["terminationGracePeriodSeconds"] >= 45, f"종료 유예 ≥ 45초 (gunicorn graceful_timeout 30초 + 여유) — 실제 {spec['terminationGracePeriodSeconds']}")
    aff = spec["affinity"]["podAntiAffinity"]
    check("requiredDuringSchedulingIgnoredDuringExecution" not in aff and
          aff["preferredDuringSchedulingIgnoredDuringExecution"][0]["weight"] == 100, "분산은 preferred(가중치 100)이고 required 가 아니다 (3노드 공존, 설계 3.7.1)")
    check(spec["topologySpreadConstraints"][0]["whenUnsatisfiable"] == "ScheduleAnyway" and
          spec["topologySpreadConstraints"][0]["topologyKey"] == "topology.kubernetes.io/zone", "AZ 분산은 ScheduleAnyway")
    rolling = dep["spec"]["strategy"]["rollingUpdate"]
    check(rolling == {"maxSurge": 1, "maxUnavailable": 0}, f"롤링 maxSurge 1 / maxUnavailable 0 (실제 {rolling})")
    check("replicas" not in dep["spec"], "replicas 를 적지 않는다 (HPA 가 정한다)")
    fetch = next((c for c in spec.get("initContainers", []) if c["name"] == "fetch-models"), None)
    check(fetch is not None and "s3 sync" in " ".join(fetch["args"]), "initContainer fetch-models 가 모델 버킷에서 받는다")
    check(dep["metadata"]["annotations"].get("argocd.argoproj.io/sync-wave") == "2", "Argo CD sync-wave 2")

    print("\n[inference] HPA / PDB / PodMonitor")
    hpa = names["HorizontalPodAutoscaler"]["spec"]
    check((hpa["minReplicas"], hpa["maxReplicas"]) == (2, 3), f"HPA 2 ~ 3 (실제 {hpa['minReplicas']} ~ {hpa['maxReplicas']})")
    check(hpa["metrics"][0]["resource"]["name"] == "cpu", "HPA 지표는 CPU (시작값. 대기 수 기준 KEDA 는 Phase 2)")
    check(names["PodDisruptionBudget"]["spec"].get("minAvailable") == 1, "PDB minAvailable 1")
    pm = names["PodMonitor"]["spec"]
    check(pm["podMetricsEndpoints"][0]["port"] == "metrics" and
          pm["selector"]["matchLabels"] == {"app.kubernetes.io/name": "inference"}, "PodMonitor 가 inference 의 metrics 포트를 긁는다")


def check_backend(objs: list[dict]) -> None:
    print("\n[backend] Phase 1 변경")
    dep = pick(objs, "Deployment", "backend")
    spec = pod_spec(dep)
    main = container(dep, "backend")
    check(not spec.get("initContainers"), "모델 다운로드 initContainer 가 없다")
    check(not any(v["name"] == "models" for v in spec.get("volumes", [])) and
          not any(m["name"] == "models" for m in main.get("volumeMounts", [])), "모델 볼륨·마운트가 없다")
    res = main["resources"]
    check(cpu_m(res["requests"]["cpu"]) == 200 and mem_mib(res["requests"]["memory"]) == 256 and mem_mib(res["limits"]["memory"]) == 512,
          f"요청 200m / 256Mi, 메모리 limit 512Mi (추정값, S0 뒤 조정) — 실제 {res}")
    aff = spec["affinity"]["podAntiAffinity"]
    check("requiredDuringSchedulingIgnoredDuringExecution" not in aff, "api 끼리 분산도 preferred 다 (required 가 아니다)")
    check(spec["terminationGracePeriodSeconds"] >= 45, f"종료 유예 ≥ 45초 — 실제 {spec['terminationGracePeriodSeconds']}")
    check(spec["serviceAccountName"] == "backend", "ServiceAccount backend (사진 권한만)")
    cfg = pick_prefix(objs, "ConfigMap", "backend-config")["data"]
    want = {"INFERENCE_BACKEND": "http", "INFERENCE_URL": "http://inference:8000", "GUNICORN_THREADS": "8",
            "AI_CLIENT_DEADLINE": "10", "AI_CLIENT_MAX_CONCURRENCY": "4", "MIGRATE_ON_START": "0"}
    check(all(cfg.get(k) == v for k, v in want.items()), f"backend-config 의 AI 연결 설정 (없거나 다른 것: {[k for k, v in want.items() if cfg.get(k) != v]})")
    check(int(cfg.get("AI_CLIENT_MAX_CONCURRENCY", 999)) < int(cfg.get("GUNICORN_THREADS", 0)),
          "AI_CLIENT_MAX_CONCURRENCY 가 API 스레드 수보다 작다 (모든 스레드가 AI 를 기다리지 않게. 값 자체는 T2 로 정한다)")
    hpa = pick(objs, "HorizontalPodAutoscaler", "backend")["spec"]
    check((hpa["minReplicas"], hpa["maxReplicas"]) == (2, 3),
          f"HPA 2 ~ 3 — 3노드 실험 환경의 시작 기준이고 운영의 최종 한도가 아니다 (실제 {hpa['minReplicas']} ~ {hpa['maxReplicas']})")
    check(hpa["behavior"]["scaleDown"]["stabilizationWindowSeconds"] == 120, "축소 안정화 120초")
    check(pick(objs, "PodDisruptionBudget", "backend")["spec"].get("minAvailable") == 1, "PDB minAvailable 1")


def hpa_wave_problems(objs: list[dict]) -> list[str]:
    """HPA 는 대상 Deployment 보다 늦은 sync-wave 여야 한다.

    wave 표시가 없으면 0 이다. Argo CD 는 wave 0 이 Healthy 가 되기를 기다리는데, 대상 Deployment(wave 2)가 아직 없는 HPA 는
    Degraded(FailedGetScale)라서 wave 1·2 로 넘어가지 못한다 — 첫 배포가 멈춘다 (2026-10-30 G6 에서 겪었다).
    """
    wave = lambda o: int(o["metadata"].get("annotations", {}).get("argocd.argoproj.io/sync-wave", "0"))
    problems = []
    for h in (o for o in objs if o["kind"] == "HorizontalPodAutoscaler"):
        name, target = h["metadata"]["name"], h["spec"]["scaleTargetRef"]["name"]
        dep = pick(objs, "Deployment", target)
        if dep is None:
            problems.append(f"HPA {name}: 대상 Deployment {target} 가 렌더링 결과에 없다")
        elif wave(h) <= wave(dep):
            problems.append(f"HPA {name}: sync-wave {wave(h)} 는 대상 Deployment {target}(wave {wave(dep)}) 보다 뒤여야 한다 — "
                            "아니면 Argo CD 가 Degraded HPA 를 기다리며 Deployment 를 만들지 못한다")
    return problems


def check_sync_waves(objs: list[dict]) -> None:
    print("\n[sync-wave] HPA 는 대상 Deployment 뒤")
    problems = hpa_wave_problems(objs)
    for p in problems:
        check(False, p)
    if not problems:
        check(True, "모든 HPA 가 대상 Deployment 보다 늦은 wave 다")


def check_images_and_secrets(objs: list[dict]) -> None:
    print("\n[이미지·비밀] 전체")
    images = {}
    for o in objs:
        if o["kind"] in ("Deployment", "Job"):
            for c in o["spec"]["template"]["spec"]["containers"]:
                images[f"{o['metadata']['name']}/{c['name']}"] = c["image"]
    check(re.search(r"/parking-guardian/backend:\S+$", images["backend/backend"]) is not None and images["backend/backend"] == images["backend-migrate/migrate"],
          "backend 와 마이그레이션 Job 은 같은 이미지(api 타깃이 올라간 backend 리포지토리)")
    check(re.search(r"/parking-guardian/inference:\S+$", images["inference/inference"]) is not None, f"inference 이미지는 inference 리포지토리 ({images['inference/inference'].split('/')[-1]})")
    check(re.search(r"/parking-guardian/frontend:\S+$", images["frontend/frontend"]) is not None, "frontend 이미지 그대로")
    check(not [i for i in images.values() if i.startswith("parking-guardian/")], "레지스트리가 비어 있는 이미지가 없다 (replacements 가 모두 채웠다)")
    users = sorted(o["metadata"]["name"] for o in objs if o["kind"] in ("Deployment", "Job") and
                   "backend-secrets" in yaml.safe_dump(o["spec"]["template"]["spec"]))
    check(users == ["backend", "backend-migrate"], f"backend-secrets 를 쓰는 워크로드는 backend 와 마이그레이션 Job 뿐이다 (실제: {users})")
    check({o["metadata"]["namespace"] for o in objs if o["kind"] != "Namespace"} == {"parking-guardian"}, "모든 리소스가 parking-guardian Namespace 에 있다")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--print", action="store_true", help="렌더링 결과 YAML 을 출력")
    args = parser.parse_args()
    objs = render()
    if args.print:
        print(yaml.safe_dump_all(objs, sort_keys=False, allow_unicode=True))
    print(f"렌더링된 오브젝트 {len(objs)}개: " + ", ".join(sorted({f"{o['kind']}" for o in objs})))
    check_inference(objs)
    check_backend(objs)
    check_sync_waves(objs)
    check_images_and_secrets(objs)
    print(f"\n{'실패 ' + str(len(failures)) + '건' if failures else '모두 통과'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
