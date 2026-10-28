"""Grafana 대시보드 JSON 3개를 만든다. 손으로 JSON 을 고치지 말고 이 파일을 고친 뒤 다시 실행한다.

    python build_dashboards.py          (이 폴더에서. 표준 라이브러리만 쓴다)

만드는 것: phase-comparison.json(Phase 비교 핵심), resources.json(리소스), inference.json(추론 파이프라인).
kustomization.yaml 의 configMapGenerator 가 이 JSON 들을 ConfigMap 으로 만들고 Grafana sidecar 가 불러온다.

색 규칙(한 번 정해 바꾸지 않는다. 같은 대상은 모든 대시보드에서 같은 색):
  일반 API = 파랑, 추론 = 주황, p50/p95/p99 처럼 크기 순서가 있는 것은 한 색의 밝기 단계, 오류·한계선 = 상태색 빨강(시리즈 색으로 안 쓴다).
  Pod 이름처럼 매번 달라지는 시리즈는 이름 기준 고정 팔레트(palette-classic-by-name): 색이 순서가 아니라 대상에 붙는다.
한 패널에 축은 하나. 단위가 다른 두 지표는 패널을 나눈다(이중 축 금지).
"""
import json
import pathlib

BLUE, BLUE_LIGHT, BLUE_DARK = "#2a78d6", "#86b6ef", "#104281"      # 일반 API, p50/p95/p99 밝기 단계
ORANGE, ORANGE_LIGHT, ORANGE_DARK = "#eb6834", "#f6b396", "#a8431c"  # 추론
AQUA, YELLOW, GREEN, VIOLET, RED_SERIES = "#1baf7a", "#eda100", "#008300", "#6250d6", "#e34948"
CRITICAL, GOOD = "#d03b3b", "#0ca30c"  # 상태색. 오류·한계선 전용

NS = 'namespace="parking-guardian"'
# "일반 API" = 장비 사진 업로드와 헬스체크를 뺀 모든 라우트. 관리자 UI 가 쓰는 것들이다.
# 라벨 이름은 route(앱 app/metrics.py). endpoint 는 Prometheus Operator 가 포트 이름("metrics")으로 덮어쓰는 타깃 라벨이라 쓰지 않는다.
# 요청률·비율 패널은 "or vector(0)" 로 트래픽이 없을 때 0 을 그린다("No data" 대신). 지연 분위수는 요청이 없으면 비는 것이 맞으므로 그대로 둔다.
GENERAL = 'route!~"/api/v1/device/detections|.*/healthz|.*/readyz|unmatched"'
DETECT = 'route="/api/v1/device/detections"'
READYZ = 'route=~".*/readyz"'


def q(expr, legend, color=None, **extra):
    """Prometheus 쿼리 하나. color 를 주면 그 시리즈의 색을 고정한다(override 로 들어간다)."""
    t = {"expr": expr, "legendFormat": legend, "refId": chr(65 + len(_targets))}
    _targets.append((legend, color, extra))
    return t


_targets = []  # q() 가 호출될 때마다 쌓이고 panel() 이 비운다


def panel(title, targets, x, y, w, h, unit="short", kind="timeseries", desc="", thresholds=None,
          stack=False, min_zero=True, legend_calcs=("mean", "max", "lastNotNull"), decimals=None, color_by_name=False):
    overrides = []
    for legend, color, extra in _targets:
        if color:
            overrides.append({"matcher": {"id": "byName", "options": legend},
                              "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": color}}]})
        for pid, val in extra.items():
            overrides.append({"matcher": {"id": "byName", "options": legend}, "properties": [{"id": pid, "value": val}]})
    _targets.clear()

    defaults = {
        "unit": unit,
        "color": {"mode": "palette-classic-by-name" if color_by_name else "fixed", "fixedColor": BLUE},
        "custom": {
            "lineWidth": 2, "fillOpacity": 0, "pointSize": 4, "showPoints": "never", "spanNulls": True,
            "stacking": {"mode": "normal" if stack else "none"},
            "thresholdsStyle": {"mode": "line+area" if thresholds else "off"},
        },
    }
    if min_zero:
        defaults["min"] = 0
    if decimals is not None:
        defaults["decimals"] = decimals
    if thresholds:
        defaults["thresholds"] = {"mode": "absolute", "steps": [{"color": "transparent", "value": None}] +
                                 [{"color": CRITICAL, "value": v} for v in thresholds]}
        defaults["custom"]["thresholdsStyle"] = {"mode": "dashed"}
    if stack:
        defaults["custom"]["fillOpacity"] = 35

    p = {
        "type": kind, "title": title, "description": desc,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": targets,
        "fieldConfig": {"defaults": defaults, "overrides": overrides},
        "options": {
            "legend": {"displayMode": "table", "placement": "bottom", "calcs": list(legend_calcs)},
            "tooltip": {"mode": "multi", "sort": "desc"},
        },
    }
    if kind == "stat":
        p["options"] = {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                        "graphMode": "area", "colorMode": "none", "textMode": "value", "justifyMode": "center"}
        defaults["color"] = {"mode": "fixed", "fixedColor": BLUE}
    if kind == "state-timeline":
        p["options"] = {"mergeValues": True, "showValue": "never", "rowHeight": 0.8,
                        "legend": {"displayMode": "list", "placement": "bottom"}}
        defaults["custom"] = {"fillOpacity": 70, "lineWidth": 0}
        defaults["color"] = {"mode": "thresholds"}
        defaults["thresholds"] = {"mode": "absolute", "steps": [{"color": CRITICAL, "value": None}, {"color": GOOD, "value": 1}]}
        defaults.pop("min", None)
    return p


def text(md, x, y, w, h):
    return {"type": "text", "title": "", "gridPos": {"x": x, "y": y, "w": w, "h": h},
            "options": {"mode": "markdown", "content": md}}


def dashboard(uid, title, panels, tags, refresh="15s", time_from="now-30m"):
    for i, p in enumerate(panels, start=1):
        p["id"] = i
    return {
        "uid": uid, "title": title, "tags": tags, "timezone": "Asia/Seoul", "editable": True,
        "graphTooltip": 1,  # 패널 사이 커서 공유: 같은 시각을 모든 패널에서 본다
        "refresh": refresh, "time": {"from": time_from, "to": "now"},
        "schemaVersion": 39, "version": 1, "panels": panels, "templating": {"list": []},
        "annotations": {"list": []},
    }


def hist_quantile(qt, metric, selector, by=""):
    return f'histogram_quantile({qt}, sum by (le{by}) (rate({metric}_bucket{{{selector}}}[1m])))'


# ---------------------------------------------------------------------------
# 1. Phase 비교 핵심: 가설 1·4. 추론 도착률이 오를 때 일반 API 와 readyz 가 어떻게 되는가
# ---------------------------------------------------------------------------
def phase_comparison():
    P = []
    P.append(text(
        "**읽는 법** 위에서 아래로: ① 부하(추론 도착률·일반 API 요청률) → ② 일반 API 응답 시간(점선 0.5s = k6 통과 기준) → "
        "③ 그 원인(추론 락 대기, 동시 처리 수. 점선 4 = gunicorn 스레드 수) → ④ 결과(readyz 지연, Ready Pod 수, 5xx). "
        "Phase 1 가설: 추론 도착률이 처리율(약 1 req/s)을 넘으면 ②③④가 함께 나빠진다.", 0, 0, 24, 3))

    P.append(panel("① 요청률 (req/s)", [
        q(f'sum(rate(http_request_duration_seconds_count{{{GENERAL}}}[1m])) or vector(0)', "일반 API", BLUE),
        q(f'sum(rate(http_request_duration_seconds_count{{{DETECT}}}[1m])) or vector(0)', "추론 요청(사진 업로드)", ORANGE),
    ], 0, 3, 12, 8, unit="reqps", desc="라우트 기준. 일반 API = 장비 업로드·헬스체크를 뺀 모든 라우트."))
    P.append(panel("① 추론 처리 완료율 (req/s)", [
        q('sum(rate(pg_inference_requests_total{outcome="ok"}[1m])) or vector(0)', "처리 완료", ORANGE),
        q('sum(rate(pg_inference_requests_total{outcome!="ok"}[1m])) or vector(0)', "실패", CRITICAL),
    ], 12, 3, 12, 8, unit="reqps", desc="도착률(왼쪽)보다 낮게 유지되면 큐가 쌓이고 있다는 뜻."))

    P.append(panel("② 일반 API 응답 시간", [
        q(hist_quantile(0.5, "http_request_duration_seconds", GENERAL), "p50", BLUE_LIGHT),
        q(hist_quantile(0.95, "http_request_duration_seconds", GENERAL), "p95", BLUE),
        q(hist_quantile(0.99, "http_request_duration_seconds", GENERAL), "p99", BLUE_DARK),
    ], 0, 11, 12, 8, unit="s", thresholds=[0.5], desc="점선 0.5s = 사전 등록한 k6 통과 기준(일반 API p95)."))
    P.append(panel("② 추론 요청 응답 시간 (장비 관점)", [
        q(hist_quantile(0.5, "http_request_duration_seconds", DETECT), "p50", ORANGE_LIGHT),
        q(hist_quantile(0.95, "http_request_duration_seconds", DETECT), "p95", ORANGE),
        q(hist_quantile(0.99, "http_request_duration_seconds", DETECT), "p99", ORANGE_DARK),
    ], 12, 11, 12, 8, unit="s", desc="디코드 + 락 대기 + 추론 + DB·S3. 120s 에 닿으면 gunicorn/ALB 타임아웃."))

    P.append(panel("③ 추론 락 대기 시간", [
        q(hist_quantile(0.5, "pg_inference_lock_wait_seconds", ""), "p50", ORANGE_LIGHT),
        q(hist_quantile(0.95, "pg_inference_lock_wait_seconds", ""), "p95", ORANGE),
    ], 0, 19, 12, 8, unit="s", desc="사진이 단일 추론 락을 기다린 시간. 0 에 가까우면 직렬화가 병목이 아니다. Head-of-line blocking 의 직접 증거."))
    P.append(panel("③ 동시 처리 중인 요청 수", [
        q(f'sum(http_requests_in_flight{{{NS}}})', "Flask 처리 중(전체)", BLUE),
        q(f'sum(pg_inference_in_flight{{{NS}}})', "추론 대기+처리 중", ORANGE),
    ], 12, 19, 12, 8, unit="short", thresholds=[4], decimals=0,
        desc="점선 4 = Pod 당 gunicorn 스레드 수(복제 수를 곱해 읽는다). 전체가 상한에 붙고 대부분이 추론이면 일반 API 는 수락 큐에서 기다리는 중."))

    P.append(panel("④ readyz 응답 시간 p95", [
        q(hist_quantile(0.95, "http_request_duration_seconds", READYZ), "readyz p95", BLUE),
    ], 0, 27, 8, 7, unit="s", thresholds=[1], desc="ALB·kubelet 헬스체크가 쓰는 라우트. 점선 1s = probe 기본 timeout. 넘으면 NotReady → 503."))
    P.append(panel("④ Ready 인 backend Pod 수", [
        q(f'sum(kube_pod_status_ready{{{NS}, pod=~"backend-.*", condition="true"}})', "Ready", BLUE),
        q(f'sum(kube_deployment_status_replicas{{{NS}, deployment="backend"}})', "전체", BLUE_LIGHT),
    ], 8, 27, 8, 7, unit="short", decimals=0, desc="Ready 가 전체보다 작아지는 구간 = 트래픽을 받지 못하는 Pod 가 있다."))
    P.append(panel("④ 5xx 비율", [
        q(f'(sum(rate(http_request_duration_seconds_count{{{NS}, status=~"5.."}}[1m])) or vector(0)) / sum(rate(http_request_duration_seconds_count{{{NS}}}[1m]))',
          "5xx / 전체", CRITICAL),
        q(f'(sum(rate(http_request_duration_seconds_count{{{DETECT}, status="503"}}[1m])) or vector(0)) / (sum(rate(http_request_duration_seconds_count{{{DETECT}}}[1m])) or vector(1))',
          "추론 503 / 추론 요청", ORANGE),
    ], 16, 27, 8, 7, unit="percentunit", thresholds=[0.01], desc="점선 1% = k6 통과 기준(오류율)."))

    return dashboard("pg-phase-comparison", "Parking Guardian / Phase 비교 핵심", P, ["parking-guardian", "phase1"])


# ---------------------------------------------------------------------------
# 2. 리소스: 가설 2·3. CPU·메모리 경합과 확장 단위
# ---------------------------------------------------------------------------
def resources():
    P = []
    P.append(text(
        "**읽는 법** backend Pod 별 CPU(점선 1 = 요청, limit 없음)와 메모리(점선 2 GiB = limit). torch 는 스레드 1개라 추론 중 CPU 는 1코어 근처에 머문다. "
        "HPA 는 CPU 70% 에서 복제를 늘린다(최대 = 노드 수). 노드 steal 은 m7i-flex 버스트 제한의 신호: 길어진 테스트에서 성능이 떨어지면 앱이 아니라 인스턴스일 수 있다.",
        0, 0, 24, 3))

    P.append(panel("backend CPU 사용 (코어, Pod 별)", [
        q(f'sum by (pod) (rate(container_cpu_usage_seconds_total{{{NS}, container="backend"}}[2m]))', "{{pod}}"),
    ], 0, 3, 12, 8, unit="short", thresholds=[1], decimals=2, color_by_name=True, desc="점선 1 = CPU 요청. limit 이 없어 남는 코어를 더 쓸 수 있다(노드 2 vCPU)."))
    P.append(panel("backend 메모리 working set (Pod 별)", [
        q(f'sum by (pod) (container_memory_working_set_bytes{{{NS}, container="backend"}})', "{{pod}}"),
    ], 12, 3, 12, 8, unit="bytes", thresholds=[2147483648], color_by_name=True, desc="점선 2 GiB = limit(OOMKilled). 모델이 약 1 GiB 를 고정 점유한다."))

    P.append(panel("HPA: backend 복제 수", [
        q(f'kube_horizontalpodautoscaler_status_current_replicas{{{NS}, horizontalpodautoscaler="backend"}}', "현재", BLUE),
        q(f'kube_horizontalpodautoscaler_status_desired_replicas{{{NS}, horizontalpodautoscaler="backend"}}', "HPA 가 원하는 수", ORANGE),
        q(f'kube_horizontalpodautoscaler_spec_max_replicas{{{NS}, horizontalpodautoscaler="backend"}}', "최대", BLUE_LIGHT),
    ], 0, 11, 12, 7, unit="short", decimals=0, desc="원하는 수 > 현재 가 길게 이어지면 노드 자리가 없어 Pending 인 것."))
    P.append(panel("backend 컨테이너 재시작 (누적)", [
        q(f'sum by (pod) (kube_pod_container_status_restarts_total{{{NS}, container="backend"}})', "{{pod}}"),
    ], 12, 11, 12, 7, unit="short", decimals=0, color_by_name=True, desc="OOMKilled·liveness 실패. 0 이어야 한다."))

    P.append(panel("노드 CPU 사용률", [
        q('1 - avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[2m]))', "{{instance}}"),
    ], 0, 18, 8, 7, unit="percentunit", color_by_name=True, decimals=0))
    P.append(panel("노드 CPU steal (버스트 제한 신호)", [
        q('avg by (instance) (rate(node_cpu_seconds_total{mode="steal"}[2m]))', "{{instance}}"),
    ], 8, 18, 8, 7, unit="percentunit", color_by_name=True, decimals=1,
        desc="m7i-flex 는 베이스라인 40%. 지속 부하에서 steal 이 올라가면 측정값에 인스턴스 영향이 섞인 것."))
    P.append(panel("노드 CPU 요청 / allocatable", [
        q('sum by (node) (kube_pod_container_resource_requests{resource="cpu"}) / on (node) kube_node_status_allocatable{resource="cpu"}', "{{node}}"),
    ], 16, 18, 8, 7, unit="percentunit", color_by_name=True, decimals=0, thresholds=[1],
        desc="backend(1코어)가 더 뜰 자리가 있는 노드가 있는가. 100% 근처면 HPA·롤링 surge 가 Pending 된다."))

    return dashboard("pg-resources", "Parking Guardian / 리소스", P, ["parking-guardian", "phase1"], time_from="now-1h")


# ---------------------------------------------------------------------------
# 3. 추론 파이프라인: 단계별 시간, 판정 분포, 모델 상태
# ---------------------------------------------------------------------------
def inference():
    P = []
    P.append(text(
        "**읽는 법** 사진 1장 = 디코드 → (락 대기) → 앞유리 탐지 → 스티커 탐지. 단계별 p95 가 순수 추론 비용이고, 락 대기는 큐잉 비용이다. "
        "Phase 2 에서는 이 패널들이 추론 서비스 쪽으로 옮겨 가고, backend 에는 HTTP 호출 시간만 남는다.", 0, 0, 24, 3))

    P.append(panel("단계별 처리 시간 p95", [
        q(hist_quantile(0.95, "pg_inference_stage_seconds", 'stage="decode"'), "디코드", AQUA),
        q(hist_quantile(0.95, "pg_inference_stage_seconds", 'stage="window"'), "앞유리 탐지", BLUE),
        q(hist_quantile(0.95, "pg_inference_stage_seconds", 'stage="sticker"'), "스티커 탐지", ORANGE),
        q(hist_quantile(0.95, "pg_inference_lock_wait_seconds", ""), "락 대기", ORANGE_DARK),
    ], 0, 3, 12, 8, unit="s"))
    P.append(panel("단계별 처리 시간 p50", [
        q(hist_quantile(0.5, "pg_inference_stage_seconds", 'stage="decode"'), "디코드", AQUA),
        q(hist_quantile(0.5, "pg_inference_stage_seconds", 'stage="window"'), "앞유리 탐지", BLUE),
        q(hist_quantile(0.5, "pg_inference_stage_seconds", 'stage="sticker"'), "스티커 탐지", ORANGE),
        q(hist_quantile(0.5, "pg_inference_lock_wait_seconds", ""), "락 대기", ORANGE_DARK),
    ], 12, 3, 12, 8, unit="s"))

    P.append(panel("판정 분포 (req/s, 누적)", [
        q('sum(rate(pg_inference_requests_total{outcome="ok", verdict="violation"}[2m])) or vector(0)', "violation", RED_SERIES),
        q('sum(rate(pg_inference_requests_total{outcome="ok", verdict="permitted"}[2m])) or vector(0)', "permitted", GREEN),
        q('sum(rate(pg_inference_requests_total{outcome="ok", verdict="no_vehicle"}[2m])) or vector(0)', "no_vehicle", YELLOW),
    ], 0, 11, 12, 7, unit="reqps", stack=True, desc="부하 테스트는 sample_a(permitted)·sample_b(violation)를 섞어 보낸다."))
    P.append(panel("추론 실패 (req/s)", [
        q('sum(rate(pg_inference_requests_total{outcome="error"}[2m])) or vector(0)', "error → 503", CRITICAL),
        q('sum(rate(pg_inference_requests_total{outcome="invalid_image"}[2m])) or vector(0)', "invalid_image → 400", YELLOW),
    ], 12, 11, 12, 7, unit="reqps"))

    P.append(panel("모델 상태 (Pod 별, 초록 = ready)", [
        q(f'max by (pod) (pg_inference_model_state{{{NS}, pg_inference_model_state="ready"}})', "{{pod}}"),
    ], 0, 18, 12, 6, kind="state-timeline", desc="새 Pod 가 뜬 뒤 ready 가 되기까지의 구간이 콜드 스타트. 빨강 = cold/loading/failed."))
    P.append(panel("모델 로딩 시간 (마지막 성공, Pod 별)", [
        q(f'max by (pod) (pg_model_load_seconds{{{NS}}})', "{{pod}}"),
    ], 12, 18, 12, 6, unit="s", kind="stat", desc="PC 측정은 25~35s, EKS 노드는 약 5s(2026-10-28)."))

    return dashboard("pg-inference", "Parking Guardian / 추론 파이프라인", P, ["parking-guardian", "phase1"])


if __name__ == "__main__":
    here = pathlib.Path(__file__).parent
    for name, build in (("phase-comparison", phase_comparison), ("resources", resources), ("inference", inference)):
        out = here / f"{name}.json"
        out.write_text(json.dumps(build(), ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print("wrote", out.name)
