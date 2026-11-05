"""PrometheusRule(CR) YAML 에서 spec.groups 만 꺼내 promtool 이 읽는 일반 규칙 파일로 쓴다. PyYAML 필요.

    python tests/extract_rules.py [출력 파일]      (이 폴더의 부모 rules/ 에서. 기본 출력 build/pg-rules.yaml, build/ 는 임시 폴더)
그 뒤:  promtool check rules build/pg-rules.yaml   /   promtool test rules tests/pg-alerts.test.yaml
(운영 Prometheus 는 이 도구가 만든 파일이 아니라 CR 그대로를 Operator 가 변환해 읽는다. 이 변환은 시험용이다.)
"""
import pathlib
import sys

import yaml

here = pathlib.Path(__file__).resolve().parent.parent
out = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else here / "build" / "pg-rules.yaml"
groups = []
for f in sorted(here.glob("*.yaml")):
    doc = yaml.safe_load(f.read_text(encoding="utf-8"))
    if doc and doc.get("kind") == "PrometheusRule":
        groups += doc["spec"]["groups"]
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(yaml.safe_dump({"groups": groups}, allow_unicode=True, sort_keys=False), encoding="utf-8")
print(f"{len(groups)} group(s), {sum(len(g['rules']) for g in groups)} rule(s) -> {out}")
