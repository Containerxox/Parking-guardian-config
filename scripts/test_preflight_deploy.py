#!/usr/bin/env python3
"""scripts/preflight-deploy.py 의 판단 함수 테스트. 실행: python scripts/test_preflight_deploy.py (AWS·클러스터 없이 돈다)."""
import importlib.util
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location("preflight", Path(__file__).resolve().parent / "preflight-deploy.py")
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)

KUSTOMIZATION = '''
images:
  - name: parking-guardian/backend
    newTag: "{backend}"
  - name: parking-guardian/frontend
    newTag: {frontend}
  - name: parking-guardian/inference
    newTag: "{inference}"
'''


def kustomization(backend="abc1234", frontend="abc1234", inference="abc1234"):
    return KUSTOMIZATION.format(backend=backend, frontend=frontend, inference=inference)


def test_tags_are_read_whether_quoted_or_not():
    assert preflight.parse_tags(kustomization("d354c49", "3993669", "unset")) == {
        "backend": "d354c49", "frontend": "3993669", "inference": "unset"}


def test_a_consistent_new_set_of_tags_is_ready():
    assert preflight.tag_problems(preflight.parse_tags(kustomization())) == []


def test_the_unset_placeholder_blocks_a_rebuild():
    problems = preflight.tag_problems(preflight.parse_tags(kustomization(inference="unset")))
    assert any("unset" in p for p in problems)


def test_the_three_tags_must_be_the_same_commit():
    """The CI writes one commit hash into all three lines. Different tags mean a half-finished CI run."""
    problems = preflight.tag_problems(preflight.parse_tags(kustomization(backend="abc1234", frontend="abc1234", inference="def5678")))
    assert any("같은" in p for p in problems)


def test_the_prototype_backend_tag_blocks_a_rebuild():
    """backend:d354c49 is the Prototype image (torch inside, no HttpInference): it must never meet INFERENCE_BACKEND=http."""
    problems = preflight.tag_problems(preflight.parse_tags(kustomization("d354c49", "d354c49", "d354c49")))
    assert any("Prototype" in p for p in problems)


def test_a_missing_image_entry_is_reported():
    text = "images:\n  - name: parking-guardian/backend\n    newTag: \"abc1234\"\n"
    assert any("inference" in p for p in preflight.tag_problems(preflight.parse_tags(text)))


def test_config_env_must_name_the_inference_registry():
    assert preflight.env_problems("AWS_REGION=x\nECR_BACKEND=y\n") != []
    assert preflight.env_problems("AWS_REGION=x\nECR_INFERENCE=z\n# comment\n") == []
    assert preflight.env_problems("ECR_INFERENCE=\n") != []  # present but empty


def test_an_api_sized_backend_image_passes_and_a_prototype_sized_one_does_not():
    api_bytes, prototype_bytes = 84_326_847, 661_592_038  # compressed sizes measured locally on 2026-10-30
    assert preflight.backend_size_problems(api_bytes) == []
    assert any("Prototype" in p for p in preflight.backend_size_problems(prototype_bytes))


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok    {name}")
            except AssertionError as exc:
                failed += 1
                print(f"  FAIL  {name} {exc}")
    print("모두 통과" if not failed else f"실패 {failed}건")
    sys.exit(1 if failed else 0)
