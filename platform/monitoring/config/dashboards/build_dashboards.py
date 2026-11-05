"""Grafana 대시보드 JSON 7개를 만든다(Phase 1 용 3개 + Phase 2 용 4개). 손으로 JSON 을 고치지 말고 이 파일을 고친 뒤 다시 실행한다.

    python build_dashboards.py          (이 폴더에서. 표준 라이브러리만 쓴다)

만드는 것: phase-comparison.json(Phase 비교 핵심), resources.json(리소스), inference.json(추론 파이프라인) — Phase 1 용, 폴더 'Parking Guardian (Phase 1 참고)'.
overview-phase2.json(Service Overview), infrastructure-phase2.json(Infrastructure), pipeline-phase2.json(SQS & Async Pipeline), ai-inference-phase2.json(AI Inference & E2E) — Phase 2 용, 폴더 'Parking Guardian (Phase 2)'.
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
    # rate 창 2분: 15초 스크레이프 × 8점. 1분이면 요청이 드문 구간마다 NaN(빈 창)이 되어 그래프가 자주 끊긴다.
    # 부하 테스트 단계(3분)보다는 짧아 knee 는 그대로 보인다.
    return f'histogram_quantile({qt}, sum by (le{by}) (rate({metric}_bucket{{{selector}}}[2m])))'


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
        # 분모가 0 이면(추론 요청 없음) 0/0 = NaN 이 되므로 "> 0" 으로 비운 뒤 전체를 0 으로 대체한다.
        q(f'((sum(rate(http_request_duration_seconds_count{{{DETECT}, status="503"}}[2m])) or vector(0)) / (sum(rate(http_request_duration_seconds_count{{{DETECT}}}[2m])) > 0)) or vector(0)',
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


# ---------------------------------------------------------------------------
# Phase 2 (2026-11-06 관측성 1단계): 이미 수집 중인 지표(backend /metrics, kube-state-metrics, cAdvisor, node-exporter)만 쓴다.
# relay·worker 지표(큐 깊이, E2E 등)는 2·3단계에서 앱 계측과 함께 별도 대시보드로 추가한다.
# ---------------------------------------------------------------------------
P2_GENERAL = 'route!~"/api/v1/device/.*|.*/healthz|.*/readyz|unmatched"'   # 관리자 UI 가 쓰는 라우트
P2_INTAKE = 'route="/api/v1/device/detection-jobs"'                     # 사진 접수(202). 판정은 Worker 가 나중에 한다
P2_HEARTBEAT = 'route="/api/v1/device/heartbeat"'
P2_TRAFFIC = 'route!~".*/(healthz|readyz)"'                              # 헬스체크를 뺀 실제 트래픽(알림 규칙 PGHttp5xxRateHigh 와 같은 정의)
APP_PODS = 'container=~"backend|worker|relay|frontend"'


def overview_phase2():
    P = []
    P.append(text(
        "**Service Overview (Phase 2)** 위에서 아래로: ① 접수(장비→backend, 202 = 접수 성공이며 판정은 Worker 가 나중에 한다) → ② 응답 시간 → ③ 오류 → ④ 앱별 Ready·재시작. "
        "큐 깊이·E2E 는 relay/worker 계측(2·3단계) 후 `SQS & Async Pipeline` 에 추가된다. 아래 값은 모두 이미 수집 중인 지표다.", 0, 0, 24, 3))

    P.append(panel("① 사진 접수 (req/s, 상태 코드별)", [
        q(f'sum by (status) (rate(http_request_duration_seconds_count{{{NS}, {P2_INTAKE}}}[1m]))', "{{status}}", None),
    ], 0, 3, 8, 8, unit="reqps", color_by_name=True,
        desc="202 = 새 접수, 200 = 같은 event_id 의 중복(재시도), 4xx = 요청 오류, 503 = 접수 실패(저장·DB). 202 가 장비 도착률이다."))
    P.append(panel("① Heartbeat (req/s)", [
        q(f'sum(rate(http_request_duration_seconds_count{{{NS}, {P2_HEARTBEAT}}}[1m])) or vector(0)', "heartbeat", GREEN),
    ], 8, 3, 8, 8, unit="reqps", desc="장비 1대 = 분당 1회. 장비 N대면 약 N/60 req/s."))
    P.append(panel("① 관리자 API 요청률 (req/s)", [
        q(f'sum(rate(http_request_duration_seconds_count{{{NS}, {P2_GENERAL}}}[1m])) or vector(0)', "관리자 API", BLUE),
    ], 16, 3, 8, 8, unit="reqps"))

    P.append(panel("② 사진 접수 응답 시간 (장비 관점)", [
        q(hist_quantile(0.5, "http_request_duration_seconds", f'{NS}, {P2_INTAKE}'), "p50", ORANGE_LIGHT),
        q(hist_quantile(0.95, "http_request_duration_seconds", f'{NS}, {P2_INTAKE}'), "p95", ORANGE),
        q(hist_quantile(0.99, "http_request_duration_seconds", f'{NS}, {P2_INTAKE}'), "p99", ORANGE_DARK),
    ], 0, 11, 8, 8, unit="s", desc="접수(검증 + S3 업로드 + DB + SQS 발행 요청)까지. 판정(E2E)은 이 값에 들어가지 않는다. 히스토그램 분위수는 버킷 보간 값이다."))
    P.append(panel("② Heartbeat 응답 시간", [
        q(hist_quantile(0.95, "http_request_duration_seconds", f'{NS}, {P2_HEARTBEAT}'), "p95", GREEN),
        q(hist_quantile(0.99, "http_request_duration_seconds", f'{NS}, {P2_HEARTBEAT}'), "p99", BLUE_DARK),
    ], 8, 11, 8, 8, unit="s", thresholds=[0.5], desc="점선 0.5s = 목표(p95)."))
    P.append(panel("② 관리자 API 응답 시간", [
        q(hist_quantile(0.5, "http_request_duration_seconds", f'{NS}, {P2_GENERAL}'), "p50", BLUE_LIGHT),
        q(hist_quantile(0.95, "http_request_duration_seconds", f'{NS}, {P2_GENERAL}'), "p95", BLUE),
        q(hist_quantile(0.99, "http_request_duration_seconds", f'{NS}, {P2_GENERAL}'), "p99", BLUE_DARK),
    ], 16, 11, 8, 8, unit="s", thresholds=[0.5], desc="점선 0.5s = 목표(p95). 요청이 없는 구간은 비는 것이 맞다."))

    P.append(panel("③ 5xx 비율 (전체 트래픽)", [
        q(f'(sum(rate(http_request_duration_seconds_count{{{NS}, status=~"5..", {P2_TRAFFIC}}}[5m])) or vector(0)) / sum(rate(http_request_duration_seconds_count{{{NS}, {P2_TRAFFIC}}}[5m]))',
          "5xx / 전체", CRITICAL),
    ], 0, 19, 8, 7, unit="percentunit", thresholds=[0.05], desc="점선 5% = 알림 PGHttp5xxRateHigh 임계값(5분 평균, 요청률 0.2 req/s 이상일 때만 알림)."))
    P.append(panel("③ 5xx / 4xx (req/s, 라우트별)", [
        q(f'sum by (route) (rate(http_request_duration_seconds_count{{{NS}, status=~"5..", {P2_TRAFFIC}}}[2m]))', "5xx {{route}}", CRITICAL),
        q(f'sum by (route) (rate(http_request_duration_seconds_count{{{NS}, status=~"4..", {P2_TRAFFIC}}}[2m]))', "4xx {{route}}", YELLOW),
    ], 8, 19, 8, 7, unit="reqps", legend_calcs=("max", "lastNotNull"), desc="오류가 어느 라우트에서 나는지. 4xx 는 대부분 로그인 실패·권한(정상 동작)이다."))
    P.append(panel("③ 처리 중인 요청 수 (backend 전체)", [
        q(f'sum(http_requests_in_flight{{{NS}}})', "in flight", BLUE),
    ], 16, 19, 8, 7, unit="short", decimals=0, desc="Pod 당 gunicorn 스레드 8개(복제 수를 곱해 읽는다). 상한에 붙으면 요청이 대기한다."))

    P.append(panel("④ 앱별 Ready Pod 수 (목표 대비)", [
        q(f'sum by (deployment) (kube_deployment_status_replicas_ready{{{NS}}})', "Ready {{deployment}}", None),
        q(f'sum by (deployment) (kube_deployment_spec_replicas{{{NS}}})', "목표 {{deployment}}", None, **{"custom.lineStyle": {"fill": "dash", "dash": [10, 10]}}),
    ], 0, 26, 8, 8, unit="short", decimals=0, color_by_name=True, legend_calcs=("lastNotNull",), desc="실선 = Ready, 점선 = 목표(spec). Worker 가 목표보다 낮으면 알림 PGWorkerNotReady."))
    P.append(panel("④ Worker: KEDA/HPA 현재·목표 replica", [
        q(f'kube_horizontalpodautoscaler_status_current_replicas{{{NS}, horizontalpodautoscaler="keda-hpa-worker"}}', "현재", BLUE),
        q(f'kube_horizontalpodautoscaler_status_desired_replicas{{{NS}, horizontalpodautoscaler="keda-hpa-worker"}}', "목표", ORANGE),
        q(f'kube_horizontalpodautoscaler_spec_max_replicas{{{NS}, horizontalpodautoscaler="keda-hpa-worker"}}', "최대", BLUE_LIGHT),
    ], 8, 26, 8, 8, unit="short", decimals=0, legend_calcs=("lastNotNull",), desc="기본 구성은 고정 2(min=max=2). 확장 실험 때만 움직인다."))
    P.append(panel("④ 컨테이너 재시작 (최근 1시간 증가분)", [
        q(f'sum by (pod, container) (increase(kube_pod_container_status_restarts_total{{{NS}}}[1h]))', "{{pod}}/{{container}}", None),
    ], 16, 26, 8, 8, unit="short", decimals=0, color_by_name=True, legend_calcs=("max", "lastNotNull"), desc="0 이어야 한다. 15분 안에 3번 넘으면 알림 PGPodCrashLooping."))

    return dashboard("pg2-overview", "Parking Guardian / Service Overview (Phase 2)", P, ["parking-guardian", "phase2"], time_from="now-1h")


def infrastructure_phase2():
    P = []
    P.append(text(
        "**Infrastructure (Phase 2)** 노드와 앱 Pod 의 CPU·메모리, 스케줄 가능성(CPU 요청률), 재시작·Pending. "
        "RDS(연결·CPU·크레딧)는 CloudWatch 연동(4단계, 선택) 후 이 대시보드에 추가한다 — 지금은 Prometheus 에 RDS 지표가 없다.", 0, 0, 24, 3))

    P.append(panel("노드 CPU 사용률", [
        q('1 - avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[2m]))', "{{instance}}"),
    ], 0, 3, 8, 8, unit="percentunit", color_by_name=True, decimals=0))
    P.append(panel("노드 메모리 사용률", [
        q('1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)', "{{instance}}"),
    ], 8, 3, 8, 8, unit="percentunit", color_by_name=True, decimals=0, thresholds=[0.9], desc="점선 90%."))
    P.append(panel("노드 CPU 요청 / allocatable", [
        q('sum by (node) (kube_pod_container_resource_requests{resource="cpu"}) / on (node) kube_node_status_allocatable{resource="cpu"}', "{{node}}"),
    ], 16, 3, 8, 8, unit="percentunit", color_by_name=True, decimals=0, thresholds=[1],
        desc="스케줄러는 요청 합이 allocatable 을 넘는 노드에 Pod 를 올리지 않는다. 100% 근처에 새 Worker(요청 1코어)가 뜰 자리는 없다."))

    P.append(panel("앱 CPU 사용 (코어, Pod 별)", [
        q(f'sum by (pod) (rate(container_cpu_usage_seconds_total{{{NS}, {APP_PODS}}}[2m]))', "{{pod}}"),
    ], 0, 11, 12, 8, unit="short", decimals=2, color_by_name=True, desc="backend·worker·relay·frontend. Worker 는 추론 중 1코어 근처(요청 1코어)."))
    P.append(panel("앱 메모리 working set (Pod 별)", [
        q(f'sum by (pod) (container_memory_working_set_bytes{{{NS}, {APP_PODS}}})', "{{pod}}"),
    ], 12, 11, 12, 8, unit="bytes", color_by_name=True, desc="Worker 는 모델로 약 0.9 GiB(limit 2 GiB), backend 약 0.15 GiB."))

    P.append(panel("Pending / NotReady Pod", [
        q(f'sum(kube_pod_status_phase{{{NS}, phase="Pending"}}) or vector(0)', "Pending", YELLOW),
        q(f'count(kube_pod_status_ready{{{NS}, condition="false"}} == 1) or vector(0)', "NotReady", CRITICAL),
    ], 0, 19, 8, 7, unit="short", decimals=0, legend_calcs=("max", "lastNotNull"), desc="Pending 이 10분 넘으면 알림 PGPodPending."))
    P.append(panel("컨테이너 재시작 (최근 1시간 증가분)", [
        q(f'sum by (pod, container) (increase(kube_pod_container_status_restarts_total{{{NS}}}[1h]))', "{{pod}}/{{container}}"),
    ], 8, 19, 8, 7, unit="short", decimals=0, color_by_name=True, legend_calcs=("max", "lastNotNull")))
    P.append(panel("수집 대상 상태 (DOWN 개수)", [
        q('count(up == 0) or vector(0)', "DOWN 타깃", CRITICAL),
        q('count(up == 1)', "UP 타깃", GOOD),
    ], 16, 19, 8, 7, unit="short", decimals=0, legend_calcs=("lastNotNull",), desc="Prometheus 가 스크레이프하지 못하는 대상. 0 이어야 한다. 수집이 끊기면 알림도 믿을 수 없다."))

    return dashboard("pg2-infra", "Parking Guardian / Infrastructure (Phase 2)", P, ["parking-guardian", "phase2"], time_from="now-1h")


def pipeline_phase2():
    """SQS & Async Pipeline (관측성 2단계): relay /metrics(PodMonitor relay). 값을 알 수 없을 때는 'No data' 로 보이게 한다(0 으로 대체하지 않는다)."""
    P = []
    P.append(text(
        "**SQS & Async Pipeline** 접수(202) 뒤 사진이 SQS → Worker 로 가는 길. **빈 패널(No data)은 0 이 아니라 '알 수 없음'이다**(Relay 스크레이프 실패·SQS 조회 권한 없음). "
        "맨 아래 '지표 신선도' 패널과 알림 PGPipelineMetricsMissing 이 그 상태를 알린다. "
        "**두 가지 '오래된' 값을 섞지 않는다**: (1) 아래 '가장 오래된 미완료 작업 나이' = **DB 기준**(pending_publish·queued·processing 중 가장 오래된 detection_jobs.created_at, Relay 회차마다 계산). "
        "(2) SQS 의 ApproximateAgeOfOldestMessage = SQS 안의 메시지 나이(CloudWatch, 1~2분 지연·근사) — 이 대시보드에는 없다(4단계 선택).", 0, 0, 24, 4))

    P.append(panel("SQS 큐 깊이 (메인): 대기 / 처리 중 / 지연", [
        q('max by (state) (pg_queue_messages{queue="main"})', "{{state}}", None),
    ], 0, 4, 8, 8, unit="short", decimals=0, color_by_name=True, legend_calcs=("max", "lastNotNull"),
        desc="visible = 받을 수 있는 메시지, not_visible = Worker 가 처리 중(visibility timeout), delayed = 지연. SQS GetQueueAttributes 의 근사값. 알림 PGQueueBacklogSustained = 대기 + 처리 중 > 30 이 5분."))
    P.append(panel("DLQ: SQS 메시지 vs DB dead_lettered 작업", [
        q('sum(max by (state) (pg_queue_messages{queue="dlq"}))', "SQS DLQ 메시지(대기+처리 중+지연)", ORANGE),
        q('max(pg_jobs{status="dead_lettered"})', "DB dead_lettered 작업(지속)", CRITICAL),
    ], 8, 4, 8, 8, unit="short", decimals=0, legend_calcs=("max", "lastNotNull"),
        desc="Relay 가 DLQ 를 2초마다 읽어 작업을 dead_lettered 로 옮기고 메시지를 지운다. SQS 쪽은 거의 항상 0 이고, 지속 신호는 DB 의 dead_lettered(재처리 전까지 남음). 알림 PGDLQHasMessages 는 둘 중 하나라도 > 0 이 1분."))
    P.append(panel("가장 오래된 미완료 작업 나이 (DB 기준)", [
        q('max(pg_oldest_open_job_age_seconds)', "미완료 작업 중 최대 나이", ORANGE),
    ], 16, 4, 8, 8, unit="s", thresholds=[30],
        desc="미완료 = status pending_publish·queued·processing, 기준 = detection_jobs.created_at(DB 시각), Relay 회차(2초)마다 계산, 없으면 0. SQS 의 ApproximateAgeOfOldestMessage 가 아니다: 발행 대기·판정 중인 작업도 포함한다. 점선 30초 = 알림 PGPipelineOldestJobAge(3분)."))

    P.append(panel("작업 상태별 개수 (DB)", [
        q('max by (status) (pg_jobs)', "{{status}}", None),
    ], 0, 12, 8, 7, unit="short", decimals=0, color_by_name=True, legend_calcs=("max", "lastNotNull"),
        desc="Relay 가 15초마다 센다. pending_publish = 아직 SQS 에 못 보낸 작업(Relay 가 재시도), queued = SQS 에 있음, processing = Worker 가 판정 중, dead_lettered = 3회 실패."))
    P.append(panel("Relay 발행 (건/s)", [
        q('sum by (outcome) (rate(pg_relay_publish_total[2m]))', "{{outcome}}", None),
    ], 8, 12, 8, 7, unit="reqps", color_by_name=True, legend_calcs=("max", "lastNotNull"),
        desc="Relay 가 SQS 로 보낸 작업. 접수(API) 즉시 발행은 세지 않는다 — 대부분의 작업은 API 가 바로 보내므로 이 값은 보통 0 이고, SQS 오류로 API 가 못 보낸 작업을 Relay 가 재발행할 때만 오른다."))
    P.append(panel("Relay 발행 지연 (접수 → 발행)", [
        q(hist_quantile(0.95, "pg_relay_publish_delay_seconds", ""), "p95", ORANGE),
        q(hist_quantile(0.99, "pg_relay_publish_delay_seconds", ""), "p99", ORANGE_DARK),
    ], 16, 12, 8, 7, unit="s", desc="Relay 가 재발행한 작업의 (발행 시각 − 접수 시각). 재발행이 없으면 데이터가 없다(No data = 해당 사건 없음). 히스토그램 분위수는 버킷 보간 값."))

    P.append(panel("SQS API 오류 (건/s, 작업별)", [
        q('sum by (operation) (rate(pg_sqs_api_errors_total[5m]))', "{{operation}}", None),
    ], 0, 19, 8, 7, unit="reqps", color_by_name=True, legend_calcs=("max", "lastNotNull"),
        desc="Relay 가 만든 SQS 호출 실패: send(발행), receive_dead_letters(DLQ 읽기), get_queue_attributes(큐 깊이 조회). get_queue_attributes 오류가 계속되면 relay IAM 에 sqs:GetQueueAttributes 가 없는 것."))
    P.append(panel("지표 신선도 (초)", [
        q('time() - max(pg_relay_last_pass_timestamp_seconds)', "Relay 마지막 회차 이후", BLUE),
        q('time() - max(pg_queue_depth_last_success_timestamp_seconds)', "큐 깊이 조회 마지막 성공 이후", ORANGE),
    ], 8, 19, 8, 7, unit="s", thresholds=[120], legend_calcs=("max", "lastNotNull"),
        desc="정상: 회차 약 2초, 큐 조회 약 15초 이내. 값이 커지거나 No data 면 Relay 가 멈췄거나 큐 깊이를 못 읽는 중이다(위 큐 패널이 비는 이유). 알림 PGPipelineMetricsMissing."))
    P.append(panel("Relay 스크레이프 상태 (초록 = UP)", [
        q('max by (pod) (up{job="parking-guardian/relay"})', "{{pod}}"),
    ], 16, 19, 8, 7, kind="state-timeline", desc="Prometheus 가 Relay /metrics 를 긁는 중인가. 빨강 = DOWN, No data = 대상 없음(PodMonitor 미배포)."))

    P.append(panel("Worker: Ready 대 목표", [
        q(f'sum(kube_deployment_status_replicas_ready{{{NS}, deployment="worker"}})', "Ready", BLUE),
        q(f'sum(kube_deployment_spec_replicas{{{NS}, deployment="worker"}})', "목표", BLUE_LIGHT),
    ], 0, 26, 12, 7, unit="short", decimals=0, legend_calcs=("lastNotNull",), desc="적체가 늘 때 함께 본다. Ready < 목표 = 알림 PGWorkerNotReady."))
    P.append(panel("Worker: KEDA/HPA 현재·목표 replica", [
        q(f'kube_horizontalpodautoscaler_status_current_replicas{{{NS}, horizontalpodautoscaler="keda-hpa-worker"}}', "현재", BLUE),
        q(f'kube_horizontalpodautoscaler_status_desired_replicas{{{NS}, horizontalpodautoscaler="keda-hpa-worker"}}', "목표", ORANGE),
    ], 12, 26, 12, 7, unit="short", decimals=0, legend_calcs=("lastNotNull",), desc="기본 구성은 고정 2. 확장 실험에서만 움직인다."))

    return dashboard("pg2-pipeline", "Parking Guardian / SQS & Async Pipeline (Phase 2)", P, ["parking-guardian", "phase2"], time_from="now-1h")


def ai_inference_e2e():
    """AI Inference & E2E (관측성 3단계): worker /metrics(PodMonitor worker). 값을 알 수 없을 때는 'No data'(0 으로 대체하지 않는다)."""
    W = 'job="parking-guardian/worker"'
    P = []
    P.append(text(
        "**AI Inference & E2E** 사진이 Worker 에서 판정되어 DB 에 저장되기까지. **E2E = 서버 기준**: detection_jobs.received_at(API 가 사진을 받은 서버 시각)부터 "
        "Worker 가 판정 트랜잭션을 **커밋한 직후**까지. 장비 시각 기준이 아니고(장비 시계는 믿지 않는다), 증거 사진 복사·알림·SQS 삭제는 포함하지 않는다. "
        "**p50/p95/p99 는 히스토그램 버킷 보간 추정값**이다(실측 개별 값이 아님). 사진 1장은 한 번만 센다(재시도·재전달 제외). "
        "큐 대기는 SQS SentTimestamp 기준이라 AWS·Pod 시계 차이만큼 근사다. **No data = 해당 사건 없음 또는 수집 실패(맨 아래 수집 상태 패널로 구분)**.", 0, 0, 24, 4))

    P.append(panel("E2E 지연 (서버 기준 접수 → 판정 저장, 추정값)", [
        q(f'histogram_quantile(0.50, sum by (le) (rate(pg_job_e2e_seconds_bucket[5m])))', "p50 (추정)", BLUE_LIGHT),
        q(f'histogram_quantile(0.95, sum by (le) (rate(pg_job_e2e_seconds_bucket[5m])))', "p95 (추정)", BLUE),
        q(f'histogram_quantile(0.99, sum by (le) (rate(pg_job_e2e_seconds_bucket[5m])))', "p99 (추정)", BLUE_DARK),
    ], 0, 4, 12, 8, unit="s", thresholds=[10], desc="5분 창의 버킷 보간 추정값. 점선 10초 = 목표이자 알림 PGE2EP95High 기준. 사진이 없으면 No data."))
    P.append(panel("10초 이내 판정 비율 (실측 버킷)", [
        q('sum(rate(pg_job_e2e_seconds_bucket{le="10.0"}[5m])) / sum(rate(pg_job_e2e_seconds_count[5m]))', "10초 이내 비율", GOOD),
    ], 12, 4, 6, 8, unit="percentunit", decimals=2, legend_calcs=("lastNotNull",),
        desc="le 10.0 버킷(prometheus_client 는 경계를 10.0 으로 표기한다)은 정확한 경계라 보간 없는 실측 비율이다. 사진이 없으면 No data."))
    P.append(panel("처리량 (건/s) — 판정 저장됨", [
        q('sum by (outcome) (rate(pg_worker_messages_total{outcome=~"done|stale"}[2m]))', "{{outcome}}", None),
    ], 18, 4, 6, 8, unit="reqps", color_by_name=True, legend_calcs=("max", "lastNotNull"),
        desc="done = 판정이 DB 에 커밋됨(반영), stale = 판정은 저장했지만 순서 보호로 상태는 안 바꿈. 커밋 뒤에만 센다."))

    P.append(panel("오류·재시도 (건/s)", [
        q('sum by (outcome) (rate(pg_worker_messages_total{outcome=~"retry|effects_retry|rejected|lost_claim"}[2m]))', "{{outcome}}", None),
    ], 0, 12, 12, 7, unit="reqps", color_by_name=True, legend_calcs=("max", "lastNotNull"),
        desc="retry = 판정 저장 전 실패(저장소·추론·DB), 메시지는 큐에 남아 다시 전달(3회 뒤 DLQ). effects_retry = 판정은 저장됐고 증거 사진·알림만 실패. "
             "rejected = 사진 없음/손상(판정 없음). lost_claim = 다른 Worker 가 이어받음."))
    P.append(panel("Worker 처리 시간 (claim → 처리 끝, 추정값)", [
        q('histogram_quantile(0.50, sum by (le) (rate(pg_worker_process_seconds_bucket{outcome="done"}[5m])))', "p50 (추정)", ORANGE_LIGHT),
        q('histogram_quantile(0.95, sum by (le) (rate(pg_worker_process_seconds_bucket{outcome="done"}[5m])))', "p95 (추정)", ORANGE),
    ], 12, 12, 12, 7, unit="s", desc="claim 에 성공한 메시지의 사진 읽기 + 추론 + 판정 트랜잭션 + 부수 효과 + 삭제 전체(성공한 건만). 큐 대기는 포함하지 않는다."))

    P.append(panel("큐 대기 (SQS 발행 → Worker 수신, 첫 전달만, 추정값)", [
        q('histogram_quantile(0.50, sum by (le) (rate(pg_queue_wait_seconds_bucket[5m])))', "p50 (추정)", BLUE_LIGHT),
        q('histogram_quantile(0.95, sum by (le) (rate(pg_queue_wait_seconds_bucket[5m])))', "p95 (추정)", BLUE),
    ], 0, 19, 12, 7, unit="s", desc="SQS SentTimestamp(AWS 시계)와 Worker 시계의 차이라 근사다. 같은 장비의 앞선 메시지 뒤에서 기다린 시간(FIFO 그룹)도 포함한다. "
                                    "재전달은 세지 않는다. Relay 가 재발행한 작업의 접수 → 발행 지연은 SQS & Async Pipeline 대시보드에 있다."))
    P.append(panel("추론 단계별 시간 p95 (추정값)", [
        q(f'histogram_quantile(0.95, sum by (le) (rate(pg_inference_stage_seconds_bucket{{{W},stage="decode"}}[5m])))', "디코드", AQUA),
        q(f'histogram_quantile(0.95, sum by (le) (rate(pg_inference_stage_seconds_bucket{{{W},stage="window"}}[5m])))', "앞유리 탐지", BLUE),
        q(f'histogram_quantile(0.95, sum by (le) (rate(pg_inference_stage_seconds_bucket{{{W},stage="sticker"}}[5m])))', "스티커 탐지", ORANGE),
    ], 12, 19, 12, 7, unit="s", desc="Worker 프로세스 안 YOLO 단계별 시간(기존 pg_inference_stage_seconds 재사용). 락 대기는 Worker 가 한 번에 한 장이라 거의 0."))

    P.append(panel("판정 결과 (건/s, 추론 기준)", [
        q(f'sum by (verdict, outcome) (rate(pg_inference_requests_total{{{W}}}[5m]))', "{{verdict}} / {{outcome}}", None),
    ], 0, 26, 8, 7, unit="reqps", color_by_name=True, legend_calcs=("max", "lastNotNull"),
        desc="추론 호출 결과(ok 는 verdict 별, 실패는 invalid_image·error). DB 저장 전 값이라 재시도되면 같은 사진이 다시 보일 수 있다 — 저장 기준 처리량은 위 '처리량' 패널."))
    P.append(panel("모델 준비 상태 (초록 = Ready)", [
        q(f'max by (pod) (pg_inference_model_state{{{W},pg_inference_model_state="ready"}})', "{{pod}}"),
    ], 8, 26, 8, 7, kind="state-timeline", desc="Worker 는 모델을 읽고 첫 추론까지 마친 뒤에야 큐를 받는다. 빨강 = 아직(로딩 중·실패). 롤링 때 1~2분 빨강은 정상."))
    P.append(panel("모델 로딩 / 첫 추론 시간 (초)", [
        q(f'max by (pod) (pg_model_load_seconds{{{W}}})', "로딩 {{pod}}", ORANGE),
        q(f'max by (pod) (pg_model_first_inference_seconds{{{W}}})', "첫 추론 {{pod}}", ORANGE_DARK),
    ], 16, 26, 8, 7, unit="s", legend_calcs=("lastNotNull",), desc="마지막 성공한 로딩의 디스크 → 메모리 시간과 로딩 직후 첫 추론 시간."))

    P.append(panel("Worker 수집 상태 (초록 = UP)", [
        q('max by (pod) (up{job="parking-guardian/worker"})', "{{pod}}"),
    ], 0, 33, 12, 6, kind="state-timeline", desc="Prometheus 가 Worker /metrics 를 긁는 중인가. 빨강 = DOWN, No data = 대상 없음(PodMonitor 미배포). 알림 PGWorkerMetricsMissing."))
    P.append(panel("시간 측정 건너뜀 (최근 1시간, 건)", [
        q('sum by (metric, reason) (increase(pg_worker_timing_skipped_total[1h]))', "{{metric}} / {{reason}}", None),
    ], 12, 33, 12, 6, unit="short", decimals=0, color_by_name=True, legend_calcs=("lastNotNull",),
        desc="시각이 없거나(missing) 시계 차이로 음수(negative)라 E2E·큐 대기 표본에 넣지 않은 건수. 0 이 아니면 위 지연 값은 이만큼 빠진 표본이다."))

    return dashboard("pg2-inference", "Parking Guardian / AI Inference & E2E (Phase 2)", P, ["parking-guardian", "phase2"], time_from="now-1h")


if __name__ == "__main__":
    here = pathlib.Path(__file__).parent
    for name, build in (("phase-comparison", phase_comparison), ("resources", resources), ("inference", inference),
                        ("overview-phase2", overview_phase2), ("infrastructure-phase2", infrastructure_phase2),
                        ("pipeline-phase2", pipeline_phase2), ("ai-inference-phase2", ai_inference_e2e)):
        out = here / f"{name}.json"
        out.write_text(json.dumps(build(), ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print("wrote", out.name)
