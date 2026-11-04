"""테스트 장비 사전 점검 (읽기 전용). Phase 2 접수 테스트를 보내기 전에 "이 장비로 보내도 기존 데이터가 안 바뀌는가" 를 본다.

backend Pod 안에서 실행한다(앱의 DB 설정을 그대로 쓴다). 로컬에서 실행하는 스크립트가 아니다:

    kubectl exec -i deploy/backend -n parking-guardian -- python - RPI-A-PANGYO-01 < scripts/test-device-preflight.py
    kubectl exec -i deploy/backend -n parking-guardian -- python -                  < scripts/test-device-preflight.py   # 장비 전체 요약

DB 에 쓰지 않는다(SELECT 만). 장비 키·비밀번호·해시를 읽거나 출력하지 않는다. 절차와 판정 기준은 docs/test-device-procedure.md.
종료 코드: 0 = 점검을 끝냈다(판정은 출력의 GO / 주의 / 중단을 읽는다), 2 = 장비를 찾지 못했다.
"""
import sys
from datetime import datetime, timezone

from app import create_app
from app.extensions import db
from sqlalchemy import text


def aware(value):
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def fmt(value):
    return "-" if value is None else str(value)[:19]


def inspect(q, serial_row, gap_minutes, now):
    m = serial_row
    mid = m["id"]
    notes_stop, notes_warn = [], []

    if m["status"] != "ACTIVE" or m["lot_status"] != "ACTIVE" or m["company_status"] != "ACTIVE":
        notes_stop.append(f"장비·주차장·회사 중 ACTIVE 가 아닌 것이 있다 (장비 {m['status']}, 주차장 {m['lot_status']}, 회사 {m['company_status']})")

    open_episodes = q("select id, status, detected_at, last_seen_at from violations where machine_id=:m and ended_at is null order by id", m=mid)
    sessions = q("select session_id, status, first_received_at, left_reported_at from device_sessions where machine_id=:m order by first_received_at desc limit 5", m=mid)
    jobs = q("select seq, event, status, received_at from detection_jobs where machine_id=:m order by received_at desc limit 3", m=mid)
    by_status = q("select status, count(*) n from detection_jobs where machine_id=:m group by status order by status", m=mid)
    pending_commands = q("select count(*) n from device_commands where machine_id=:m and status in ('pending','delivered')", m=mid)[0]["n"]

    last_signal = max([t for t in (aware(m["last_detection_at"]), aware(m["last_heartbeat_at"])) if t is not None], default=None)
    gap = None if last_signal is None else int((now - last_signal).total_seconds() // 60)

    print(f"== {m['serial_number']} (id {mid})")
    print(f"   상태: 장비 {m['status']} / 주차장 {m['lot_status']} / 회사 {m['company_status']}")
    print(f"   카운터: pending_violation_count={m['pending_violation_count']}, current_session_id={fmt(m['current_session_id'])}, last_applied_seq={m['last_applied_seq']}")
    print(f"   마지막 신호: 사진 {fmt(m['last_detection_at'])} / 하트비트 {fmt(m['last_heartbeat_at'])} → 공백 {('-' if gap is None else str(gap) + '분')} (EPISODE_GAP_MINUTES={gap_minutes})")
    print(f"   열린 사건: {len(open_episodes)}건 " + ", ".join(f"#{r['id']}({r['status']}, 마지막 목격 {fmt(r['last_seen_at'])})" for r in open_episodes))
    print(f"   세션: " + (", ".join(f"{str(r['session_id'])[:8]}…({r['status']}, 첫 수신 {fmt(r['first_received_at'])})" for r in sessions) or "없음"))
    print(f"   최근 작업: " + (", ".join(f"seq {r['seq']} {r['event']} {r['status']} {fmt(r['received_at'])}" for r in jobs) or "없음"))
    print(f"   작업 상태별 건수: " + (", ".join(f"{r['status']}={r['n']}" for r in by_status) or "없음") + f" / 대기 중 명령 {pending_commands}건")

    if open_episodes and (gap is None or gap > gap_minutes):
        notes_warn.append(f"열린 사건 {len(open_episodes)}건이 있고 신호 공백이 EPISODE_GAP_MINUTES 를 넘었다 → 첫 신호가 오면 SIGNAL_LOST 로 종료되고 이력이 추가된다(2026-11-04 위반 #4 가 이렇게 닫혔다)")
    elif open_episodes:
        notes_warn.append(f"열린 사건 {len(open_episodes)}건이 있다 → 사진의 판정에 따라 이 사건의 카운터·종료가 바뀐다")
    if m["pending_violation_count"]:
        notes_warn.append("pending_violation_count 가 0 이 아니다 → 위반 판정 사진이 오면 확인 횟수가 채워져 위반·알림이 만들어질 수 있다")
    active = [r for r in sessions if r["status"] == "active"]
    if active:
        notes_warn.append(f"활성 세션이 {len(active)}개 있다 → 같은 장비로 보내는 새 session_id 는 기존 세션을 대체할 수 있다(seq 는 세션 안에서 1부터)")
    if pending_commands:
        notes_warn.append("대기 중인 장비 명령이 있다 → 새 접수가 명령 상태에 영향을 줄 수 있다")
    inflight = [r for r in by_status if r["status"] in ("pending_publish", "queued", "processing")]
    if inflight:
        notes_stop.append("처리 중이거나 대기 중인 작업이 있다: " + ", ".join(f"{r['status']}={r['n']}" for r in inflight))

    if notes_stop:
        verdict = "중단"
    elif notes_warn:
        verdict = "주의"
    else:
        verdict = "GO"
    for n in notes_stop:
        print(f"   [중단] {n}")
    for n in notes_warn:
        print(f"   [주의] {n}")
    print(f"   판정: {verdict}")
    return verdict


def main():
    serial = (sys.argv[1] if len(sys.argv) > 1 else "").strip().upper()
    app = create_app()
    with app.app_context():
        def q(sql, **params):
            return db.session.execute(text(sql), params).mappings().all()

        now = datetime.now(timezone.utc)
        gap_minutes = int(app.config.get("EPISODE_GAP_MINUTES", 0))
        rows = q("""select m.id, m.serial_number, m.status, m.pending_violation_count, m.current_session_id, m.last_applied_seq,
                           m.last_detection_at, m.last_heartbeat_at, p.status as lot_status, c.status as company_status
                    from machines m join parking_lots p on p.id = m.parking_lot_id join companies c on c.id = p.company_id
                    where (:s = '' or upper(m.serial_number) = :s) order by m.id""", s=serial)
        if not rows:
            print(f"장비를 찾지 못했다: {serial or '(전체)'}")
            return 2
        verdicts = {r["serial_number"]: inspect(q, r, gap_minutes, now) for r in rows}
        db.session.rollback()   # 읽기만 했다. 열린 트랜잭션을 남기지 않는다
        print("\n요약: " + ", ".join(f"{s}={v}" for s, v in verdicts.items()))
        return 0


if __name__ == "__main__":
    sys.exit(main())
