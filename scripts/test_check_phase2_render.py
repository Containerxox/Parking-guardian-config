"""scripts/check-phase2-render.py 와 export-config-env.py 의 Phase 2 부분 테스트.

순수 함수는 손으로 만든 작은 입력으로, 저장소 전체는 `kubectl kustomize` 렌더링으로 확인한다(kubectl 이 없으면 건너뛴다).
"결함 주입" 테스트: 렌더링된 객체를 일부러 망가뜨려서 검사가 그 문제를 잡는지 본다. 검사가 항상 통과하는 껍데기가 아님을 보이는 것이 목적이다.
"""
import copy
import re
import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


p2 = _load("check_phase2", "check-phase2-render.py")
exporter = _load("export_config_env", "export-config-env.py")

needs_kubectl = pytest.mark.skipif(shutil.which("kubectl") is None, reason="kubectl 이 없다")


@pytest.fixture(scope="module")
def rendered():
    if shutil.which("kubectl") is None:
        pytest.skip("kubectl 이 없다")
    return p2.render()


def failures_from(fn, *args):
    before = len(p2.failures)
    fn(*args)
    new = p2.failures[before:]
    del p2.failures[before:]      # 다른 테스트에 번지지 않게
    return new


# --- 순수 함수 -----------------------------------------------------------------------------------------------------------

def test_the_later_env_source_wins_and_secret_values_are_hidden():
    cms = {"a": {"X": "1", "Y": "a"}, "b": {"Y": "b"}}
    secrets = {"s": {"DB_USER"}}
    container = {"envFrom": [{"configMapRef": {"name": "a"}}, {"configMapRef": {"name": "b"}}, {"secretRef": {"name": "s"}}],
                 "env": [{"name": "X", "value": "2"}]}
    env = p2.merged_env(container, cms, secrets)
    assert env == {"X": "2", "Y": "b", "DB_USER": "<secret>"}


def test_missing_required_variables_and_unknown_names_are_reported():
    env = {"QUEUE_BACKEND": "sqs", "SQS_QEUE_URL": "typo"}
    problems = p2.env_problems("worker", env, known={"QUEUE_BACKEND", "SQS_QUEUE_URL"})
    assert any("SQS_QUEUE_URL" in p and "없다" in p for p in problems)
    assert any("SQS_QEUE_URL" in p and "읽지 않는다" in p for p in problems)


def test_names_that_only_the_shell_or_gunicorn_read_are_not_typos():
    env = {k: "x" for k in p2.REQUIRED_BY_ROLE["relay"]}
    env.update({"SEED_DEMO_DATA": "1", "GUNICORN_CMD_ARGS": "--keep-alive 130", "ECR_WORKER": "r"})
    assert p2.env_problems("relay", env, known=set(p2.REQUIRED_BY_ROLE["relay"])) == []


def test_app_env_names_include_the_config_helpers(tmp_path):
    app = tmp_path / "backend" / "app"
    app.mkdir(parents=True)
    (app / "config.py").write_text('import os\nA = os.environ.get("ONE")\nB = _bool("TWO", False)\nC = os.environ["THREE"]\nD = os.getenv("FOUR")\n', encoding="utf-8")
    assert p2.app_env_names(tmp_path) == {"ONE", "TWO", "THREE", "FOUR"}


def test_terraform_defaults_are_read_from_variable_blocks(tmp_path):
    stack = tmp_path / "terraform" / "stacks" / "cluster"
    stack.mkdir(parents=True)
    (stack / "variables.tf").write_text('variable "worker_service_account" {\n  description = "x"\n  type = string\n  default = "worker"\n}\n'
                                        'variable "no_default" {\n  type = string\n}\n', encoding="utf-8")
    assert p2.terraform_defaults(tmp_path) == {"worker_service_account": "worker"}


def test_worker_registry_uses_the_terraform_output_or_derives_it_from_backend():
    urls = {"backend": "1.dkr.ecr.r.amazonaws.com/parking-guardian/backend"}
    assert exporter.worker_registry(urls) == ("1.dkr.ecr.r.amazonaws.com/parking-guardian/worker", False)
    urls["worker"] = "1.dkr.ecr.r.amazonaws.com/parking-guardian/worker"
    assert exporter.worker_registry(urls) == (urls["worker"], True)


# --- 저장소 전체 ---------------------------------------------------------------------------------------------------------------

def test_the_repository_render_passes_every_phase2_check(rendered):
    for fn in (p2.check_phase1_still_there, p2.check_workloads, p2.check_keda, p2.check_references, p2.check_terraform_contract, p2.check_placement):
        assert failures_from(fn, rendered) == [], fn.__name__
    assert failures_from(p2.check_argocd) == []
    assert failures_from(p2.check_comparison_mode) == []


KUSTOMIZATION = """images:
  - name: parking-guardian/backend
    newTag: "{backend}"
  - name: parking-guardian/worker
    newTag: {worker}
"""


def test_the_worker_tag_guard_rejects_unset_missing_and_placeholder_tags():
    """The guard that stops a rebuild before the CI has built the worker image (or if its entry vanished). It must not depend on today's state."""
    ok = KUSTOMIZATION.format(backend="abc1234", worker='"abc1234"')
    assert p2.worker_tag_problem(ok) is None
    assert p2.worker_tag_problem(KUSTOMIZATION.format(backend="abc1234", worker="abc1234")) is None      # unquoted is read too
    assert "unset" in p2.worker_tag_problem(KUSTOMIZATION.format(backend="abc1234", worker='"unset"'))
    assert "placeholder" in p2.worker_tag_problem(KUSTOMIZATION.format(backend="abc1234", worker='"placeholder"'))
    assert "항목 없음" in p2.worker_tag_problem('images:\n  - name: parking-guardian/backend\n    newTag: "abc1234"\n')
    assert "같은" in p2.worker_tag_problem(KUSTOMIZATION.format(backend="abc1234", worker='"def5678"'))   # half-finished CI run
    # equal to the backend tag is not enough: two identical non-hash tags (nothing was built) must still be refused
    assert p2.worker_tag_problem(KUSTOMIZATION.format(backend="placeholder", worker='"placeholder"')) is not None
    assert p2.worker_tag_problem(KUSTOMIZATION.format(backend="unset", worker='"unset"')) is not None
    assert p2.worker_tag_problem(KUSTOMIZATION.format(backend="abc1234", worker='"ABC1234"')) is not None   # not a lower-case commit hash


def _prerequisites_for(monkeypatch, tmp_path, worker):
    """Run prerequisites() against a copy of the repo's config files whose worker tag is `worker`; return the TODO lines it reports."""
    app = tmp_path / "apps" / "parking-guardian"
    app.mkdir(parents=True)
    real = p2.REPO / "apps" / "parking-guardian"
    (app / "config.env").write_text((real / "config.env").read_text(encoding="utf-8"), encoding="utf-8")
    text = (real / "kustomization.yaml").read_text(encoding="utf-8")
    text = re.sub(r"(- name: parking-guardian/worker\n\s+newTag: )\S+", lambda m: m.group(1) + worker, text)
    (app / "kustomization.yaml").write_text(text, encoding="utf-8")
    monkeypatch.setattr(p2, "REPO", tmp_path)
    before = len(p2.warnings)
    p2.prerequisites([])
    new = p2.warnings[before:]
    del p2.warnings[before:]
    return new


def test_prerequisites_report_an_unset_worker_tag_as_a_todo(monkeypatch, tmp_path):
    new = _prerequisites_for(monkeypatch, tmp_path, '"unset"')
    assert any("worker 태그" in line and "unset" in line for line in new), new


def test_prerequisites_accept_a_real_worker_tag(monkeypatch, tmp_path):
    tag = re.search(r"- name: parking-guardian/backend\s+newTag: \"?([0-9a-f]{7})", (p2.REPO / "apps" / "parking-guardian" / "kustomization.yaml").read_text(encoding="utf-8"))
    assert tag, "the repository's backend tag should be a 7-character commit hash"
    new = _prerequisites_for(monkeypatch, tmp_path, f'"{tag.group(1)}"')
    assert not any("worker 태그" in line for line in new), new


def test_strict_mode_fails_when_a_prerequisite_is_open_and_the_plain_run_does_not(monkeypatch):
    """--strict is the pre-deploy gate: any open TODO must turn into exit code 1. Without it the same state is only a report."""
    for name in ("check_phase1_still_there", "check_workloads", "check_keda", "check_references", "check_terraform_contract", "check_placement"):
        monkeypatch.setattr(p2, name, lambda objs: None)
    monkeypatch.setattr(p2, "check_argocd", lambda: None)
    monkeypatch.setattr(p2, "check_comparison_mode", lambda: None)
    monkeypatch.setattr(p2, "render", lambda: [])
    monkeypatch.setattr(p2, "prerequisites", lambda objs: p2.todo(False, "open prerequisite (test)"))
    for argv, expected in ((["check", "--strict"], 1), (["check"], 0)):
        monkeypatch.setattr(sys, "argv", argv)
        before_f, before_w = list(p2.failures), list(p2.warnings)
        try:
            assert p2.main() == expected, argv
        finally:
            p2.failures[:] = before_f
            p2.warnings[:] = before_w


@needs_kubectl
def test_the_repositorys_own_todo_list_only_contains_known_gaps(rendered):
    before = len(p2.warnings)
    p2.prerequisites(rendered)
    new = p2.warnings[before:]
    del p2.warnings[before:]
    known = ("worker 태그", "ECR 리포지토리 worker", "build-push", "KEDA operator", "SIGTERM", "preload", "config.env")
    assert all(any(k in line for k in known) for line in new), new      # nothing unexpected is reported; which items are open depends on the day


# --- 결함 주입: 검사가 실제로 문제를 잡는다 --------------------------------------------------------------------------------------------

def _dep(objs, name):
    return next(o for o in objs if o["kind"] == "Deployment" and o["metadata"]["name"] == name)


def test_a_worker_whose_env_order_lets_backend_config_win_is_caught(rendered):
    objs = copy.deepcopy(rendered)
    c = _dep(objs, "worker")["spec"]["template"]["spec"]["containers"][0]
    c["envFrom"].sort(key=lambda s: 0 if "worker-config" in s.get("configMapRef", {}).get("name", "") else 1)   # worker-config 를 맨 앞으로
    assert any("INFERENCE_BACKEND=yolo" in f or "worker-config 가 backend-config 뒤" in f for f in failures_from(p2.check_workloads, objs))


def test_a_worker_that_borrows_the_api_secret_is_caught(rendered):
    objs = copy.deepcopy(rendered)
    c = _dep(objs, "worker")["spec"]["template"]["spec"]["containers"][0]
    c["envFrom"].append({"secretRef": {"name": "backend-secrets"}})
    new = failures_from(p2.check_phase1_still_there, objs) + failures_from(p2.check_workloads, objs)
    assert any("pipeline-secrets 뿐이다" in f or "backend-secrets 를 쓰는 것은" in f for f in new)


def test_a_wrong_service_account_name_breaks_the_terraform_contract(rendered):
    objs = copy.deepcopy(rendered)
    _dep(objs, "worker")["spec"]["template"]["spec"]["serviceAccountName"] = "workers"
    assert any("ServiceAccount worker" in f for f in failures_from(p2.check_terraform_contract, objs))


def test_a_reference_to_a_missing_config_map_or_key_is_caught(rendered):
    objs = copy.deepcopy(rendered)
    c = _dep(objs, "relay")["spec"]["template"]["spec"]["containers"][0]
    c["envFrom"].append({"configMapRef": {"name": "does-not-exist"}})
    c["env"] = [{"name": "X", "valueFrom": {"configMapKeyRef": {"name": "queue-config-xx", "key": "NOPE"}}}]
    problems = p2.reference_problems(objs)
    assert any("does-not-exist" in p for p in problems)


def test_an_hpa_and_a_scaled_object_on_the_same_deployment_are_caught(rendered):
    objs = copy.deepcopy(rendered)
    hpa = next(o for o in objs if o["kind"] == "HorizontalPodAutoscaler" and o["metadata"]["name"] == "backend")
    hpa["spec"]["scaleTargetRef"]["name"] = "worker"
    assert any("worker 를 2개가 조절한다" in p for p in p2.hpa_conflicts(objs))


def test_replicas_on_a_scaled_deployment_are_caught(rendered):
    objs = copy.deepcopy(rendered)
    _dep(objs, "worker")["spec"]["replicas"] = 2
    assert any("replicas 가 적혀 있다" in p for p in p2.hpa_conflicts(objs))


def test_a_worker_that_starts_before_the_migration_is_caught(rendered):
    objs = copy.deepcopy(rendered)
    _dep(objs, "worker")["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"] = "1"
    assert any("마이그레이션 Job" in p for p in p2.wave_problems(objs))


def test_a_scaled_object_in_the_same_wave_as_its_target_is_caught(rendered):
    objs = copy.deepcopy(rendered)
    so = next(o for o in objs if o["kind"] == "ScaledObject")
    so["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"] = "4"
    assert any("ScaledObject worker" in p for p in p2.wave_problems(objs))


def test_an_hpa_that_would_be_blocked_by_an_unhealthy_worker_is_caught(rendered):
    objs = copy.deepcopy(rendered)
    hpa = next(o for o in objs if o["kind"] == "HorizontalPodAutoscaler" and o["metadata"]["name"] == "backend")
    hpa["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"] = "4"
    assert any("Phase 1 의 HPA 적용이 막힌다" in p for p in p2.wave_problems(objs))


def test_raising_the_worker_cpu_request_changes_the_placement_conclusion(rendered):
    """공존 구성에서 Worker 가 늘면 못 들어간다는 결론이 요청값에 묶여 있다. 요청이 달라지면 기대와 어긋나 알려 준다."""
    objs = copy.deepcopy(rendered)
    c = _dep(objs, "worker")["spec"]["template"]["spec"]["containers"][0]
    c["resources"]["requests"]["cpu"] = "400m"
    assert failures_from(p2.check_placement, objs), "요청을 낮췄는데도 기대(Worker 2 불가능 등)와 같다면 검사가 요청값을 보지 않는 것이다"
