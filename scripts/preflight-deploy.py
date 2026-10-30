#!/usr/bin/env python3
"""Phase 1 배포의 "단계 사이" 점검. 서비스가 잘못된 조합으로 전환되는 것을 막는다. 아무것도 바꾸지 않는다(AWS 는 읽기 전용 describe 만).

    python scripts/preflight-deploy.py --stage before-config-push [--aws]
    python scripts/preflight-deploy.py --stage before-rebuild     [--aws]

두 단계 모두 통과해야 다음 단계로 넘어간다 (aws-review/deploy-checklist.md 의 순서):

  before-config-push  config 레포를 push 하기 직전.
      - config.env 에 ECR_INFERENCE 가 있다(없으면 kustomize 가 실패한다)
      - 저장소 그대로(가짜 값 없이) `kubectl kustomize` 가 통과한다
      - [--aws] **클러스터가 없다** — 켜져 있으면 Argo CD 가 새 구조를 옛 이미지 태그로 동기화해 backend 가 Prototype 이미지에
        INFERENCE_BACKEND=http 를 받게 된다. 그리고 ECR inference 리포지토리가 있다

  before-rebuild      클러스터를 다시 만들기 직전(= Argo CD 가 config 레포를 처음 읽기 직전).
      - images: 의 태그 세 개가 "unset" 이 아니고, 같은 커밋 해시이고, Prototype 태그(d354c49)가 아니다 (CI 가 쓴 것)
      - [--aws] 세 이미지가 ECR 에 그 태그로 있고, backend 이미지는 api 크기다(Prototype 이미지가 아니다)
      - [--aws] RDS 상태를 알려 준다(정지면 켜야 한다)

AWS_PROFILE=parking-guardian 을 설정한다. 종료 코드 1 이면 다음 단계로 가지 않는다.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
APP_DIR = REPO / "apps" / "parking-guardian"
CLUSTER_NAME = "parking-guardian"
PROTOTYPE_TAG = "d354c49"            # Prototype 의 마지막 코드 커밋. 이 태그의 backend 이미지는 torch 가 든 Prototype 이다
API_MAX_BYTES = 300 * 1024 * 1024    # 압축 크기: api 약 84 MB, Prototype 약 660 MB (2026-10-30 로컬 빌드). 그 사이에 선을 긋는다
IMAGES = ("backend", "frontend", "inference")


def parse_tags(kustomization: str) -> dict[str, str]:
    """images: 의 newTag (따옴표 유무 무관) → {backend: 태그, ...}"""
    tags = {}
    for name, tag in re.findall(r"- name: parking-guardian/(\w+)\s*\n\s+newTag: \"?([^\"\s]+)\"?", kustomization):
        tags[name] = tag
    return tags


def tag_problems(tags: dict[str, str]) -> list[str]:
    problems = []
    for name in IMAGES:
        if name not in tags:
            problems.append(f"images: 에 {name} 항목이 없다")
    if problems:
        return problems
    unset = [n for n in IMAGES if tags[n] == "unset"]
    if unset:
        problems.append(f"newTag 가 아직 \"unset\" 이다: {unset} — CI 의 태그 갱신(update-config)이 끝나지 않았다")
    elif len({tags[n] for n in IMAGES}) != 1:
        problems.append(f"세 이미지가 같은 커밋 해시여야 한다(CI 가 한 번에 쓴다): {tags}")
    if tags["backend"] == PROTOTYPE_TAG:
        problems.append(f"backend 태그가 Prototype 이미지({PROTOTYPE_TAG})다 — HttpInference 가 없어 INFERENCE_BACKEND=http 로 뜰 수 없다")
    return problems


def env_problems(config_env: str) -> list[str]:
    values = dict(line.split("=", 1) for line in config_env.splitlines() if "=" in line and not line.lstrip().startswith("#"))
    if not values.get("ECR_INFERENCE", "").strip():
        return ["config.env 에 ECR_INFERENCE 가 없거나 비어 있다 — ECR 을 만든 뒤 scripts/export-config-env.py 로 다시 만든다"]
    return []


def backend_size_problems(size_bytes: int) -> list[str]:
    if size_bytes > API_MAX_BYTES:
        return [f"ECR 의 backend 이미지가 {size_bytes / 1024 / 1024:.0f} MB 다 — api 타깃(약 84 MB)이 아니라 Prototype 이미지(약 660 MB)로 보인다. CI 의 target 설정을 확인한다"]
    return []


# --- 점검 실행 (위의 판단 함수 + 실제 환경) ------------------------------------------------------------------------

failures: list[str] = []


def report(problems: list[str], ok_message: str) -> None:
    if problems:
        for p in problems:
            print(f"  FAIL  {p}")
            failures.append(p)
    else:
        print(f"  ok    {ok_message}")


def aws(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["aws", *args, "--output", "json"], capture_output=True, text=True, encoding="utf-8")


def cluster_exists() -> bool | None:
    done = aws("eks", "describe-cluster", "--name", CLUSTER_NAME)
    if done.returncode == 0:
        return True
    return False if "ResourceNotFoundException" in done.stderr else None


def stage_before_config_push(use_aws: bool) -> None:
    print("[before-config-push]")
    report(env_problems((APP_DIR / "config.env").read_text(encoding="utf-8")), "config.env 에 ECR_INFERENCE 가 있다")
    done = subprocess.run(["kubectl", "kustomize", str(APP_DIR)], capture_output=True, text=True, encoding="utf-8")
    report([] if done.returncode == 0 else [f"kubectl kustomize 실패: {done.stderr.strip()[:200]}"], "저장소 그대로 kubectl kustomize 가 통과한다")
    if not use_aws:
        print("  (--aws 없이 실행: 클러스터가 내려가 있는지는 확인하지 않았다 — 사람이 확인한다)")
        return
    exists = cluster_exists()
    if exists is None:
        report(["EKS 클러스터 상태를 읽지 못했다(AWS_PROFILE, 권한 확인)"], "")
    else:
        report(["클러스터 %s 가 켜져 있다 — 지금 push 하면 Argo CD 가 새 구조를 옛 이미지 태그로 동기화한다. 먼저 내린다" % CLUSTER_NAME] if exists else [],
               f"클러스터 {CLUSTER_NAME} 가 없다 (push 가 어떤 것도 동기화하지 않는다)")
    repo = aws("ecr", "describe-repositories", "--repository-names", "parking-guardian/inference")
    report([] if repo.returncode == 0 else ["ECR parking-guardian/inference 가 없다 — data 스택 apply 가 먼저다"], "ECR parking-guardian/inference 가 있다")


def stage_before_rebuild(use_aws: bool) -> None:
    print("[before-rebuild]")
    tags = parse_tags((APP_DIR / "kustomization.yaml").read_text(encoding="utf-8"))
    report(tag_problems(tags), f"images: 태그 세 개가 같은 새 커밋 해시다 ({tags.get('backend')})")
    if tag_problems(tags) or not use_aws:
        if not use_aws:
            print("  (--aws 없이 실행: ECR 의 이미지 존재·크기는 확인하지 않았다)")
        return
    for name in IMAGES:
        done = aws("ecr", "describe-images", "--repository-name", f"parking-guardian/{name}", "--image-ids", f"imageTag={tags[name]}")
        if done.returncode:
            report([f"ECR parking-guardian/{name}:{tags[name]} 가 없다 — CI 빌드가 끝나지 않았다"], "")
            continue
        size = json.loads(done.stdout)["imageDetails"][0]["imageSizeInBytes"]
        problems = backend_size_problems(size) if name == "backend" else []
        report(problems, f"ECR parking-guardian/{name}:{tags[name]} 가 있다 ({size / 1024 / 1024:.0f} MB)")
    rds = aws("rds", "describe-db-instances", "--db-instance-identifier", "parking-guardian", "--query", "DBInstances[0].DBInstanceStatus")
    status = json.loads(rds.stdout) if rds.returncode == 0 else "읽지 못함"
    print(f"  info  RDS 상태: {status} (stopped 면 6-2 에서 켜고 `SHOW max_connections` 를 확인한다. 2026-11-04 에 자동으로 켜진다)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--stage", required=True, choices=["before-config-push", "before-rebuild"])
    parser.add_argument("--aws", action="store_true", help="읽기 전용 AWS 조회까지 한다(AWS_PROFILE 필요)")
    args = parser.parse_args()
    if args.aws and not os.environ.get("AWS_PROFILE"):
        print("경고: AWS_PROFILE 이 비어 있다. 기본 프로필(다른 계정일 수 있음)로 조회한다.", file=sys.stderr)
    (stage_before_config_push if args.stage == "before-config-push" else stage_before_rebuild)(args.aws)
    print("\n" + ("통과: 다음 단계로 갈 수 있다" if not failures else f"실패 {len(failures)}건: 다음 단계로 가지 않는다"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
