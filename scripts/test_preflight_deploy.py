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
  - name: parking-guardian/worker
    newTag: "{worker}"
'''


def kustomization(backend="abc1234", frontend="abc1234", inference="abc1234", worker="abc1234"):
    return KUSTOMIZATION.format(backend=backend, frontend=frontend, inference=inference, worker=worker)


def test_tags_are_read_whether_quoted_or_not():
    assert preflight.parse_tags(kustomization("d354c49", "3993669", "unset", "abc1234")) == {
        "backend": "d354c49", "frontend": "3993669", "inference": "unset", "worker": "abc1234"}


def test_a_consistent_new_set_of_tags_is_ready():
    assert preflight.tag_problems(preflight.parse_tags(kustomization())) == []


def test_the_unset_placeholder_blocks_a_rebuild():
    problems = preflight.tag_problems(preflight.parse_tags(kustomization(inference="unset")))
    assert any("unset" in p for p in problems)


def test_the_four_tags_must_be_the_same_commit():
    """The CI writes one commit hash into all four lines. Different tags mean a half-finished CI run."""
    problems = preflight.tag_problems(preflight.parse_tags(kustomization(backend="abc1234", frontend="abc1234", inference="def5678")))
    assert any("같은" in p for p in problems)
    problems = preflight.tag_problems(preflight.parse_tags(kustomization(worker="def5678")))
    assert any("같은" in p for p in problems)


def test_the_unset_worker_tag_blocks_a_rebuild():
    """Phase 2: the worker image only exists once the CI builds it (ECR repository worker). Until then the tag is "unset"."""
    problems = preflight.tag_problems(preflight.parse_tags(kustomization(worker="unset")))
    assert any("unset" in p and "worker" in p for p in problems)


def test_the_prototype_backend_tag_blocks_a_rebuild():
    """backend:d354c49 is the Prototype image (torch inside, no HttpInference): it must never meet INFERENCE_BACKEND=http."""
    problems = preflight.tag_problems(preflight.parse_tags(kustomization("d354c49", "d354c49", "d354c49", "d354c49")))
    assert any("Prototype" in p for p in problems)


def test_a_missing_image_entry_is_reported():
    text = "images:\n  - name: parking-guardian/backend\n    newTag: \"abc1234\"\n"
    problems = preflight.tag_problems(preflight.parse_tags(text))
    assert any("inference" in p for p in problems) and any("worker" in p for p in problems)


def test_config_env_must_name_the_inference_and_worker_registries():
    assert preflight.env_problems("AWS_REGION=x\nECR_BACKEND=y\n") != []
    assert preflight.env_problems("AWS_REGION=x\nECR_INFERENCE=z\n# comment\n") != []   # Phase 2: ECR_WORKER is required too
    assert preflight.env_problems("AWS_REGION=x\nECR_INFERENCE=z\nECR_WORKER=w\n# comment\n") == []
    assert preflight.env_problems("ECR_INFERENCE=\nECR_WORKER=w\n") != []  # present but empty
    assert preflight.env_problems("ECR_INFERENCE=z\nECR_WORKER=\n") != []


def test_an_api_sized_backend_image_passes_and_a_prototype_sized_one_does_not():
    api_bytes, prototype_bytes = 84_326_847, 661_592_038  # compressed sizes measured locally on 2026-10-30
    assert preflight.backend_size_problems(api_bytes) == []
    assert any("Prototype" in p for p in preflight.backend_size_problems(prototype_bytes))


def test_a_worker_image_the_size_of_the_api_image_means_the_wrong_target():
    """The worker is the `prototype` stage (torch inside, about 660 MB compressed). An api-sized image would crash on the first inference."""
    assert preflight.worker_size_problems(661_592_038) == []
    assert any("api" in p for p in preflight.worker_size_problems(84_326_847))


def test_a_stopped_rds_notice_computes_the_automatic_restart_from_the_real_stop_time():
    """AWS starts a stopped instance again after 7 days. The date must come from the instance's own stop event, not from the script."""
    from datetime import datetime, timezone
    stopped = datetime(2026, 11, 1, 12, 48, 45, tzinfo=timezone.utc)
    text = preflight.rds_notice("stopped", stopped, now=datetime(2026, 11, 3, tzinfo=timezone.utc))
    assert "stopped" in text and "시작이 필요" in text            # the action is spelled out, not just mentioned
    assert "2026-11-08" in text                    # stop + 7 days
    assert "2026-11-04" not in text                # the old hard-coded date


def test_a_stopped_rds_without_a_stop_event_still_says_it_must_be_started():
    text = preflight.rds_notice("stopped", None)
    assert "시작이 필요" in text and "읽지 못" in text
    assert "자동" not in text.replace("자동 시작 시각", "")   # no invented restart date


def test_an_available_rds_needs_no_action_and_other_states_are_shown_as_they_are():
    assert "available" in preflight.rds_notice("available", None) and "켜져 있다" in preflight.rds_notice("available", None)
    assert "starting" in preflight.rds_notice("starting", None)
    assert "읽지 못" in preflight.rds_notice(None, None)


def test_the_script_no_longer_contains_a_hard_coded_restart_date():
    source = (Path(__file__).resolve().parent / "preflight-deploy.py").read_text(encoding="utf-8")
    assert "2026-11-04" not in source


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
