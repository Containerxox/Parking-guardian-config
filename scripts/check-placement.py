#!/usr/bin/env python3
"""3노드에서 API(backend)와 AI(inference) Pod 가 함께 들어가는지 CPU·메모리 "요청"으로 계산한다. 클러스터에는 아무것도 하지 않는다.

    python scripts/check-placement.py

Pod 요청값은 렌더링된 매니페스트(scripts/check-phase1-render.py 와 같은 방법)에서 읽는다. 노드 쪽 숫자는 클러스터가 내려가 있어
다시 잴 수 없으므로 아래 상수를 쓴다. **상수의 출처와 한계를 믿지 말고 읽는다.** 재구축 직후 `kubectl describe nodes` 로 다시 재서 바꾼다.

두 가지를 구분해서 보여 준다:
  best-case  요청을 만족하는 배치가 "존재하는가" (모든 경우의 수를 시도). 존재해도 기본 스케줄러가 그 배치를 찾는다는 보장은 없다.
  scheduler  kube-scheduler 의 기본 점수(LeastAllocated: 남은 비율이 큰 노드)를 흉내 낸 순차 배치. Pod 가 만들어지는 순서에 따라 달라지는
             **모델**이다. 실제 결과는 `kubectl get pods -o wide`(설계 V1)로 확인한다.
요청은 "예약"이다. 실제 사용량이 아니다 (유휴 시 Pod 당 1~4m, 설계 3.7.1).
"""
from __future__ import annotations

import importlib.util
import itertools
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("render_check", REPO / "scripts" / "check-phase1-render.py")
render_check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(render_check)

NODES = 3
# --- 노드 상수 (출처) ----------------------------------------------------------------------------------------------------
ALLOC_CPU_M = 1930        # 설계 1.4: 2026-10-28 `kubectl describe nodes` 의 요청 비율에서 역산한 allocatable (m7i-flex.large, 2 vCPU)
ALLOC_MEM_MI = 7373       # 같은 출처, 약 7.2 GiB
BASE_CPU_M = 650          # 설계 1.4: backend 가 있던 노드의 "비-backend 요청" (DaemonSet + CloudWatch add-on v6.7.0 노드당 300m + kube-system)
BASE_MEM_MI = 640         # 같은 출처
# 중복 구성 요소 (aws-review.md 5절): CloudWatch add-on 의 nodeExporter(노드마다 DaemonSet)와 kubeStateMetrics(클러스터에 1개).
# Terraform 에서 둘 다 끄지만(addons.tf), 켜졌을 때 어떻게 되는지 보여 주기 위해 스키마 기본 요청값(256m / 128Mi)을 쓴다.
EXPORTER_CPU_M, EXPORTER_MEM_MI = 256, 128
# 노드에 묶이지 않는 플랫폼 Pod: CPU 요청이 있는 것은 frontend 뿐이다. Argo CD / 모니터링 / ESO / ALB 컨트롤러는 values 에 CPU 요청이 없다
# (메모리 요청만: Argo CD 약 736Mi, 모니터링 약 960Mi). ALB 컨트롤러·ESO 차트의 기본 요청은 확인하지 못했다(0 으로 가정) [미확인].
FLOATING_MEM_MI = 736 + 960


def effective_request(pod_spec: dict) -> tuple[float, float]:
    """Pod 의 유효 요청 = max(initContainer 최대, 컨테이너 합) (Kubernetes 규칙)."""
    def quantities(containers):
        return [(render_check.cpu_m(c.get("resources", {}).get("requests", {}).get("cpu", 0)),
                 render_check.mem_mib(c.get("resources", {}).get("requests", {}).get("memory", 0))) for c in containers]
    main = quantities(pod_spec["containers"])
    init = quantities(pod_spec.get("initContainers", []))
    cpu = max([sum(c for c, _ in main)] + [c for c, _ in init])
    mem = max([sum(m for _, m in main)] + [m for _, m in init])
    return cpu, mem


def load_pods() -> dict[str, tuple[float, float, int]]:
    """{이름: (cpu m, mem Mi, 평상시 replicas)}"""
    objs = render_check.render()
    pods = {}
    for name in ("backend", "inference", "frontend"):
        dep = render_check.pick(objs, "Deployment", name)
        cpu, mem = effective_request(dep["spec"]["template"]["spec"])
        hpa = render_check.pick(objs, "HorizontalPodAutoscaler", name)
        replicas = hpa["spec"]["minReplicas"] if hpa else dep["spec"].get("replicas", 1)
        pods[name] = (cpu, mem, replicas)
    job = render_check.pick(objs, "Job", "backend-migrate")
    cpu, mem = effective_request(job["spec"]["template"]["spec"])
    pods["migrate"] = (cpu, mem, 1)
    return pods


def node_free(exporters: bool, extra_floating_cpu: float, extra_floating_mem: float):
    cpu = ALLOC_CPU_M - BASE_CPU_M - (EXPORTER_CPU_M if exporters else 0)
    mem = ALLOC_MEM_MI - BASE_MEM_MI - (EXPORTER_MEM_MI if exporters else 0)
    return cpu, mem


def best_case(pods: list[tuple[str, float, float]], free_cpu: float, free_mem: float, floating_cpu: float, floating_mem: float):
    """모든 배치를 시도한다. (가능한가, 한 예, 노드별 남는 CPU 의 최솟값). 노드에 묶이지 않는 Pod 는 한 노드에 모두 놓는다고 가정하지 않고
    합계만 "어느 노드에든 들어갈 만큼" 남아야 한다고 본다(보수적: 가장 여유가 큰 노드에서 차감)."""
    best = None
    for assign in itertools.product(range(NODES), repeat=len(pods)):
        cpu = [free_cpu] * NODES
        mem = [free_mem] * NODES
        for (name, c, m), node in zip(pods, assign):
            cpu[node] -= c
            mem[node] -= m
        if min(cpu) < 0 or min(mem) < 0:
            continue
        roomiest = max(range(NODES), key=lambda i: cpu[i])
        cpu[roomiest] -= floating_cpu
        mem[roomiest] -= floating_mem
        if min(cpu) < 0 or min(mem) < 0:
            continue
        slack = min(cpu)
        if best is None or slack > best[2]:
            best = (assign, [list(cpu), list(mem)], slack)
    return best


def scheduler_model(pods: list[tuple[str, float, float]], free_cpu: float, free_mem: float, floating_cpu: float):
    """LeastAllocated 흉내: 요청을 만족하는 노드 중 남은 CPU 비율이 가장 큰 노드(preferred anti-affinity: 같은 이름 Pod 가 적은 노드 우선)."""
    cpu = [free_cpu - (floating_cpu if i == NODES - 1 else 0) for i in range(NODES)]  # 플랫폼 Pod 는 N3 에 있다고 둔다 (10-28 관찰)
    mem = [free_mem] * NODES
    placed = [[] for _ in range(NODES)]
    for name, c, m in pods:
        candidates = [i for i in range(NODES) if cpu[i] >= c and mem[i] >= m]
        if not candidates:
            return None, name
        node = min(candidates, key=lambda i: (placed[i].count(name), -cpu[i]))
        cpu[node] -= c
        mem[node] -= m
        placed[node].append(name)
    return placed, None


def describe(placed) -> str:
    return " ; ".join(f"N{i + 1}: " + (", ".join(f"{n}×{p.count(n)}" for n in dict.fromkeys(p)) or "-") for i, p in enumerate(placed))


def main() -> int:
    pods = load_pods()
    (api_cpu, api_mem, _), (inf_cpu, inf_mem, _), (fe_cpu, fe_mem, fe_n), (job_cpu, job_mem, _) = (
        pods["backend"], pods["inference"], pods["frontend"], pods["migrate"])
    print(f"요청 (렌더링된 매니페스트): api {api_cpu:.0f}m / {api_mem:.0f}Mi | inference {inf_cpu:.0f}m / {inf_mem:.0f}Mi | "
          f"frontend {fe_cpu:.0f}m / {fe_mem:.0f}Mi × {fe_n} | 마이그레이션 Job {job_cpu:.0f}m / {job_mem:.0f}Mi (배포 때만)")
    print(f"노드 3대 상수: allocatable {ALLOC_CPU_M}m / {ALLOC_MEM_MI}Mi, 기본 예약 {BASE_CPU_M}m / {BASE_MEM_MI}Mi → "
          f"앱이 쓸 수 있는 양 노드당 {ALLOC_CPU_M - BASE_CPU_M}m / {ALLOC_MEM_MI - BASE_MEM_MI}Mi\n")

    scenarios = [
        ("평상시 (api 2 + inference 2)", 2, 2, 0),
        ("S4 프로필 1 (api 2 + inference 3)", 2, 3, 0),
        ("S4 프로필 2 (api 4 + inference 2)", 4, 2, 0),
        ("HPA 최대 동시 (api 3 + inference 3)", 3, 3, 0),
        ("[참고] 이전 HPA 최대 (api 4 + inference 3)", 4, 3, 0),
        ("최대 상태에서 api 롤링 (api 3+surge 1 + inference 3)", 4, 3, 0),
        ("최대 상태에서 inference 롤링 (api 3 + inference 3+surge 1)", 3, 4, 0),
        ("inference 롤링 (api 2 + inference 2+surge 1)", 2, 3, 0),
        ("api 롤링 (api 2+surge 1 + inference 2)", 3, 2, 0),
        ("둘 다 롤링 (api 3 + inference 3)", 3, 3, 0),
        ("평상시 + 마이그레이션 Job (배포 때)", 2, 2, 1),
    ]
    results = {}
    for exporters in (False, True):
        label = "CloudWatch 중복 구성 요소 켜짐 (Terraform 이 끄는 것의 반대)" if exporters else "현행 설계: CloudWatch v6.7.0 고정 + 중복 구성 요소 끔"
        free_cpu, free_mem = node_free(exporters, 0, 0)
        floating_cpu = fe_cpu * fe_n + (EXPORTER_CPU_M if exporters else 0)   # kubeStateMetrics 1개
        floating_mem = fe_mem * fe_n + FLOATING_MEM_MI + (EXPORTER_MEM_MI if exporters else 0)
        print(f"=== {label}\n    노드당 남는 양 {free_cpu}m / {free_mem}Mi, 노드에 묶이지 않는 Pod 합 {floating_cpu:.0f}m / {floating_mem:.0f}Mi")
        print("| 시나리오 | best-case | 남는 CPU(최소 노드) | scheduler 모델 (순서: api → inference → 늘어난 쪽) |\n|---|---|---|---|")
        for title, n_api, n_inf, n_job in scenarios:
            wanted = [("api", api_cpu, api_mem)] * n_api + [("inference", inf_cpu, inf_mem)] * n_inf + [("migrate", job_cpu, job_mem)] * n_job
            best = best_case(wanted, free_cpu, free_mem, floating_cpu, floating_mem)
            modelled, blocked = scheduler_model(wanted, free_cpu, free_mem, floating_cpu)
            results[(exporters, title)] = (best is not None, modelled is not None)
            if best:
                example = describe([[w[0] for w, node in zip(wanted, best[0]) if node == i] for i in range(NODES)])
                best_text, slack = f"가능 ({example})", f"{best[2]:.0f}m"
            else:
                best_text, slack = "**불가능**", "-"
            sched_text = describe(modelled) if modelled else f"**{blocked} 가 Pending**"
            print(f"| {title} | {best_text} | {slack} | {sched_text} |")
        print()

    # 민감도: "api 3 + inference 3" 은 노드마다 inference 1 + api 1 이고 여유가 얼마 안 남는다. 결론이 노드에 묶이지 않는 Pod 의 CPU 요청 합
    # (지금은 frontend 50m 뿐이라고 가정)에 얼마나 민감한지 본다. 설계 3.9 의 옛 서술은 N3 여유를 130m 로 잡아 "들어가지 않는다" 였다.
    free_cpu, free_mem = node_free(False, 0, 0)
    margin = free_cpu - (api_cpu + inf_cpu)
    print(f"=== 민감도: api 3 + inference 3 (노드마다 inference 1 + api 1 → 노드마다 {margin:.0f}m 남는다)")
    print(f"    노드에 묶이지 않는 Pod 의 CPU 요청 합이 {margin:.0f}m 를 넘으면 들어가지 않는다. 지금 가정은 {fe_cpu * fe_n:.0f}m (frontend 뿐) → 남는 여유 {margin - fe_cpu * fe_n:.0f}m")
    print("| 노드에 묶이지 않는 Pod 의 CPU 합 | api 3 + inference 3 |\n|---|---|")
    for floating in (50, 80, 81, 130, 250):
        fits = best_case([("api", api_cpu, api_mem)] * 3 + [("inference", inf_cpu, inf_mem)] * 3, free_cpu, free_mem, floating, 1760) is not None
        print(f"| {floating}m | {'들어간다' if fits else '**들어가지 않는다**'} |")
    print("    (ALB 컨트롤러·ESO 의 차트 기본 요청과 Argo CD·모니터링의 CPU 요청은 확인하지 못했다. 재구축 직후 `kubectl describe nodes` 의 노드별 요청 합으로 확인한다)\n")

    # 회귀 보호: 설계가 기대하는 결론. 요청값이 바뀌어 결론이 달라지면 여기서 알려 준다.
    expect = {
        (False, "평상시 (api 2 + inference 2)"): True,
        (False, "S4 프로필 1 (api 2 + inference 3)"): True,
        (False, "S4 프로필 2 (api 4 + inference 2)"): True,
        (False, "HPA 최대 동시 (api 3 + inference 3)"): True,     # 시작 기준(2026-10-30 사용자 결정): 둘 다 최대 3. 여유 30m
        (False, "[참고] 이전 HPA 최대 (api 4 + inference 3)"): False,
        (False, "최대 상태에서 api 롤링 (api 3+surge 1 + inference 3)"): False,        # surge Pod 가 Pending → 롤링이 기다린다(중단이 아니라 지연)
        (False, "최대 상태에서 inference 롤링 (api 3 + inference 3+surge 1)"): False,
        (False, "inference 롤링 (api 2 + inference 2+surge 1)"): True,
        (False, "둘 다 롤링 (api 3 + inference 3)"): True,        # 여유 30m. 위 "HPA 최대 동시" 와 같은 상태이고, 롤링 surge 가 더해지면 못 들어간다
        (True, "평상시 (api 2 + inference 2)"): True,             # 단 api 둘을 한 노드에 몰아야 하고 여유가 24m 뿐이다
        (True, "S4 프로필 1 (api 2 + inference 3)"): False,
        (True, "inference 롤링 (api 2 + inference 2+surge 1)"): False,
    }
    bad = {k: (want, results[k][0]) for k, want in expect.items() if results[k][0] != want}
    print("설계가 기대하는 결론과 계산이 " + ("같다" if not bad else f"다르다: {bad}"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
