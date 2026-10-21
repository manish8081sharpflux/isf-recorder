"""Phone notifications: inbox, switches, announcements, event hooks and the scheduled jobs."""
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from backend.database import SessionLocal
from backend.notifications import inbox
from backend.tests.conftest import make_user, delete_user

IST = timezone(timedelta(hours=5, minutes=30))


def _rows(user, kind=None):
    with SessionLocal() as s:
        sql = "SELECT kind, title, body, recording_id FROM isf_recorder.user_notification WHERE user_id = :u"
        if kind:
            sql += " AND kind = :k"
        return s.execute(text(sql + " ORDER BY id"), {"u": user["id"], "k": kind}).fetchall()


def _recording(user, status="completed", source="recording", started=None, report=None):
    with SessionLocal() as s:
        rid = str(s.execute(text("""
            INSERT INTO isf_recorder.recording (user_id, org_id, session_id, status, started_at, mode, title, source,
                recorded_by_name, error_message, contact_name)
            VALUES (:u, :o, :sid, :st, COALESCE(:t, NOW()), 'schedule', 'Mohor Boys · Rizwana', :src, :n,
                    CASE WHEN :st = 'error' THEN 'Gemini timed out' END, 'Bhavnesh') RETURNING id
        """), {"u": user["id"], "o": user["org_id"], "sid": f"rec_test_{uuid.uuid4().hex}", "st": status, "src": source,
               "n": user["name"], "t": started}).scalar())
        if report:
            s.execute(text("""INSERT INTO isf_recorder.recording_report (recording_id, content, status, generated_at, updated_at)
                              VALUES (:id, 'Report', :st, NOW(), NOW())"""), {"id": rid, "st": report})
        s.commit()
    return rid


def _clear_org_noise():
    """Other tests leave failed recordings behind; the scan must only be judged on ours."""
    with SessionLocal() as s:
        s.execute(text("DELETE FROM isf_recorder.user_notification"))
        s.commit()


def test_inbox_starts_from_now_then_returns_new_ones(client, staff):
    first = client.get("/notifications/inbox", headers=staff["headers"]).json()
    assert first["items"] == []
    with SessionLocal() as s:
        inbox.notify(s, inbox._people(s, staff["org_id"], [staff["id"]]), "announcement", "Hello", "Meeting at 4")
    assert first["latest_id"] == 0                       # a new person: cursor 0 must still receive
    got = client.get(f"/notifications/inbox?after_id={first['latest_id']}", headers=staff["headers"]).json()
    assert [i["title"] for i in got["items"]] == ["Hello"]
    again = client.get(f"/notifications/inbox?after_id={got['latest_id']}", headers=staff["headers"]).json()
    assert again["items"] == [] and again["latest_id"] == got["latest_id"]


def test_settings_per_role_and_switch_off(client, staff, admin):
    coach_kinds = {i["kind"] for i in client.get("/notifications/settings", headers=staff["headers"]).json()["items"]}
    assert coach_kinds == {"no_recording", "report_edited", "upload_stuck"}
    a = client.get("/notifications/settings", headers=admin["headers"]).json()
    assert {"admin_daily_summary", "admin_failed", "call_shared", "admin_report_ready", "upload_stuck"} == {i["kind"] for i in a["items"]}
    assert {i["kind"]: i["on"] for i in a["items"]}["admin_report_ready"] is False      # off by default
    r = client.put("/notifications/settings", json={"prefs": {"report_edited": False}}, headers=staff["headers"]).json()
    assert {i["kind"]: i["on"] for i in r["items"]}["report_edited"] is False
    assert client.put("/notifications/settings", json={"prefs": {"nope": True}}, headers=staff["headers"]).status_code == 400


def test_admin_edit_and_finalize_notify_the_coach_once_per_hour(client, staff, admin):
    rid = _recording(staff, report="draft")
    for _ in range(2):
        assert client.put(f"/recordings/{rid}/report", json={"content": "Better"}, headers=admin["headers"]).status_code == 200
    assert client.post(f"/recordings/{rid}/report/finalize", headers=admin["headers"]).status_code == 200
    rows = _rows(staff, "report_edited")
    assert [r.title.split(":")[0] for r in rows] == ["✏️ Report edited", "✅ Report finalized"]
    assert str(rows[0].recording_id) == rid and admin["name"] in rows[0].body
    # the coach editing their own report notifies nobody
    rid2 = _recording(staff, report="draft")
    client.put(f"/recordings/{rid2}/report", json={"content": "Mine"}, headers=staff["headers"])
    assert len(_rows(staff, "report_edited")) == 2


def test_switched_off_means_no_row(client, staff, admin):
    client.put("/notifications/settings", json={"prefs": {"report_edited": False}}, headers=staff["headers"])
    rid = _recording(staff, report="draft")
    client.put(f"/recordings/{rid}/report", json={"content": "x"}, headers=admin["headers"])
    assert _rows(staff, "report_edited") == []


def test_call_shared_notifies_admins_not_on_unshare(client, staff, admin):
    rid = _recording(staff, source="call")
    assert client.post(f"/calls/{rid}/share", json={"shared": True}, headers=staff["headers"]).status_code == 200
    rows = _rows(admin, "call_shared")
    assert len(rows) == 1 and staff["name"] in rows[0].title and "Bhavnesh" in rows[0].body
    client.post(f"/calls/{rid}/share", json={"shared": False}, headers=staff["headers"])
    client.post(f"/calls/{rid}/share", json={"shared": True}, headers=staff["headers"])
    assert len(_rows(admin, "call_shared")) == 1


def test_announce_super_admin_only_and_audience(client, staff, admin):
    sup = make_user("admin")
    with SessionLocal() as s:
        s.execute(text("UPDATE isf_recorder.user SET is_super_admin = TRUE WHERE id = :u"), {"u": sup["id"]})
        s.commit()
    from backend.auth import identity
    identity._user_cache.clear()
    try:
        assert client.post("/notifications/announce", json={"message": "Hi"}, headers=admin["headers"]).status_code == 403
        r = client.post("/notifications/announce", json={"message": "Meeting at 4 PM", "audience": "coaches"}, headers=sup["headers"])
        assert r.status_code == 200 and r.json()["sent"] >= 1
        assert _rows(staff, "announcement")[-1].body == "Meeting at 4 PM"
        assert _rows(admin, "announcement") == []
        # announcements cannot be switched off
        client.put("/notifications/settings", json={"prefs": {"announcement": False}}, headers=staff["headers"])
        assert client.post("/notifications/announce", json={"message": " "}, headers=sup["headers"]).status_code == 400
    finally:
        delete_user(sup["id"])


def test_scan_failures_and_reports(staff, admin):
    _clear_org_noise()
    failed = _recording(staff, status="error")
    bad_report = _recording(staff, report="error")
    ready = _recording(staff, report="draft")
    call_failed = _recording(staff, status="error", source="call")
    inbox.scan_events()
    inbox.scan_events()                                   # same events: no duplicates
    titles = [r.title for r in _rows(admin)]
    assert any(t.startswith("⚠️ Recording failed") for t in titles)
    assert any(t.startswith("⚠️ Report failed") for t in titles)
    assert not any(t.startswith("📝 New report") for t in titles)          # off by default
    ids = {str(r.recording_id) for r in _rows(admin)}
    assert failed in ids and bad_report in ids and call_failed not in ids and ready not in ids
    assert len([r for r in _rows(admin) if str(r.recording_id) == failed]) == 1
    assert _rows(staff) == []                             # coaches do not get admin notifications
    with SessionLocal() as s:
        s.execute(text("UPDATE isf_recorder.user SET notify_prefs = '{\"admin_report_ready\": true}' WHERE id = :u"), {"u": admin["id"]})
        s.commit()
    inbox.scan_events()
    assert ready in {str(r.recording_id) for r in _rows(admin, "admin_report_ready")}


def test_daily_summary_and_no_recording_reminder(staff, admin):
    _clear_org_noise()
    idle = make_user("staff")
    try:
        _recording(staff, report="draft")
        _recording(staff, status="error")
        _recording(idle, started=datetime.now(IST) - timedelta(days=3))     # recorded recently, not today
        never = make_user("staff")                                         # never recorded: no reminder
        inbox.daily_summary()
        inbox.daily_summary()
        rows = _rows(admin, "admin_daily_summary")
        assert len(rows) == 1 and "ready" in rows[0].body and staff["name"] in rows[0].body and "failed" in rows[0].body
        inbox.no_recording_reminder()
        assert len(_rows(idle, "no_recording")) == 1
        assert _rows(staff, "no_recording") == [] and _rows(never, "no_recording") == []
        delete_user(never["id"])
    finally:
        delete_user(idle["id"])
