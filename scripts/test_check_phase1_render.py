#!/usr/bin/env python3
"""scripts/check-phase1-render.py 의 sync-wave 판단 테스트. 실행: python scripts/test_check_phase1_render.py (kubectl kustomize 만 필요하다)."""
import importlib.util
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location("render_check", Path(__file__).resolve().parent / "check-phase1-render.py")
render_check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(render_check)

WAVE = "argocd.argoproj.io/sync-wave"


def deployment(name, wave=None):
    annotations = {WAVE: wave} if wave is not None else {}
    return {"kind": "Deployment", "metadata": {"name": name, "annotations": annotations}}


def hpa(name, wave=None, target=None):
    annotations = {WAVE: wave} if wave is not None else {}
    return {"kind": "HorizontalPodAutoscaler", "metadata": {"name": name, "annotations": annotations},
            "spec": {"scaleTargetRef": {"kind": "Deployment", "name": target or name}}}


def test_an_hpa_without_a_wave_is_reported():
    """No annotation = wave 0, so Argo CD waits for an HPA whose Deployment (wave 2) does not exist yet: the sync deadlocks."""
    problems = render_check.hpa_wave_problems([deployment("backend", "2"), hpa("backend")])
    assert any("backend" in p for p in problems)


def test_an_hpa_in_the_same_wave_as_its_deployment_is_reported():
    assert render_check.hpa_wave_problems([deployment("backend", "2"), hpa("backend", "2")]) != []


def test_an_hpa_after_its_deployment_passes():
    assert render_check.hpa_wave_problems([deployment("backend", "2"), hpa("backend", "3")]) == []


def test_a_deployment_without_a_wave_counts_as_wave_zero():
    assert render_check.hpa_wave_problems([deployment("frontend"), hpa("frontend", "1")]) == []
    assert render_check.hpa_wave_problems([deployment("frontend"), hpa("frontend")]) != []


def test_an_hpa_whose_target_is_missing_from_the_render_is_reported():
    assert render_check.hpa_wave_problems([hpa("backend", "3")]) != []


def test_the_rendered_repository_orders_every_hpa_after_its_deployment():
    assert render_check.hpa_wave_problems(render_check.render()) == []


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok    {name}")
            except (AssertionError, AttributeError) as exc:
                failed += 1
                print(f"  FAIL  {name} {exc!r}")
    print("모두 통과" if not failed else f"실패 {failed}건")
    sys.exit(1 if failed else 0)
