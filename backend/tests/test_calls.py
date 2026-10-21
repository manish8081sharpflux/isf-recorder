"""Calls in Jomi (14 Oct): upload + dedupe, privacy (owner only; admin reads only shared calls), tasks, reminder."""
import uuid

import pytest
from sqlalchemy import text

from backend.database import SessionLocal
from backend.reports import service
from backend.tests.conftest import make_user, delete_user, wav_bytes


@pytest.fixture
def no_processing(monkeypatch):
    from backend.recordings import routes
    monkeypatch.setattr(routes, "_finish_import", lambda *a, **k: None)   # no ffmpeg / AI in tests


@pytest.fixture
def coach_b():
    u = make_user("staff", name="Coach B")
    yield u
    delete_user(u["id"])


def _upload(client, user, contact="Bhavnesh Desai Sir", key=None):
    files = {"file": ("Call recording Bhavnesh Desai Sir_261011_135446.m4a", wav_bytes(1.0, 2000, 440), "audio/mp4")}
    data = {"contact_name": contact, "contact_phone": "+91 98765 43210", "call_type": "outgoing",
            "call_app": "samsung", "call_started_at": "2026-10-11T13:54:46+05:30", "file_key": key or uuid.uuid4().hex}
    return client.post("/calls/upload", files=files, data=data, headers=user["headers"])


def test_upload_is_saved_for_the_uploader_and_deduped(client, staff, no_processing):
    key = uuid.uuid4().hex
    r1 = _upload(client, staff, key=key)
    assert r1.status_code == 200 and r1.json()["status"] == "processing"
    rid = r1.json()["recording_id"]
    r2 = _upload(client, staff, key=key)
    assert r2.json() == {"status": "duplicate", "recording_id": rid}       # same phone file never stored twice
    d = client.get(f"/recordings/{rid}", headers=staff["headers"]).json()
    assert d["title"] == "Call with Bhavnesh Desai Sir" and d["source"] == "call"
    assert d["contact_phone"] == "+919876543210" and d["call_type"] == "outgoing" and d["shared_with_admin"] is False
    calls = client.get("/recordings/", params={"source": "call", "limit": 100}, headers=staff["headers"]).json()["items"]
    assert rid in {c["id"] for c in calls}
    normal = client.get("/recordings/", params={"limit": 100}, headers=staff["headers"]).json()["items"]
    assert rid not in {c["id"] for c in normal}                          # calls never mix with classes/meetings
    contacts = client.get("/calls/contacts", headers=staff["headers"]).json()["items"]
    assert any(c["name"] == "Bhavnesh Desai Sir" for c in contacts)


def test_calls_are_private_until_shared(client, staff, coach_b, admin, no_processing):
    rid = _upload(client, staff).json()["recording_id"]
    for who in (coach_b, admin):
        for path in (f"/recordings/{rid}", f"/recordings/{rid}/report", f"/recordings/{rid}/audio",
                     f"/recordings/{rid}/runs", f"/recordings/{rid}/gemini-usage"):
            assert client.get(path, headers=who["headers"]).status_code == 404, (who["name"], path)
    admin_calls = client.get("/recordings/", params={"source": "call", "limit": 100}, headers=admin["headers"]).json()["items"]
    assert rid not in {c["id"] for c in admin_calls}
    assert client.post(f"/calls/{rid}/share", json={"shared": True}, headers=coach_b["headers"]).status_code == 404
    assert client.post(f"/calls/{rid}/share", json={"shared": True}, headers=staff["headers"]).json()["shared_with_admin"]
    # shared: admin can read, coach B still cannot, nobody but the owner can change it
    assert client.get(f"/recordings/{rid}", headers=admin["headers"]).status_code == 200
    shared = client.get("/recordings/", params={"source": "call", "shared_only": True, "limit": 100},
                        headers=admin["headers"]).json()["items"]
    assert rid in {c["id"] for c in shared}
    assert client.get(f"/recordings/{rid}", headers=coach_b["headers"]).status_code == 404
    assert client.put(f"/recordings/{rid}/title", json={"title": "x"}, headers=admin["headers"]).status_code == 404
    assert client.delete(f"/recordings/{rid}", headers=admin["headers"]).status_code == 404
    client.post(f"/calls/{rid}/share", json={"shared": False}, headers=staff["headers"])
    assert client.get(f"/recordings/{rid}", headers=admin["headers"]).status_code == 404


def test_ask_jomi_context_never_includes_other_peoples_calls(staff, admin, no_processing, client):
    from backend.chat.routes import _all_sessions_context
    rid = _upload(client, staff, contact=f"Secret Contact {uuid.uuid4().hex[:6]}").json()["recording_id"]
    with SessionLocal() as db:
        title = db.execute(text("SELECT title FROM isf_recorder.recording WHERE id = :i"), {"i": rid}).scalar()
        assert title not in _all_sessions_context(db, {**admin, "user_id": admin["id"], "org_id": admin["org_id"], "role": "admin"})


def test_tasks_crud_is_owner_only_and_reminder(client, staff, coach_b, no_processing):
    rid = _upload(client, staff).json()["recording_id"]
    t = client.post("/tasks", json={"text": "Send the coin list", "due_text": "Friday", "recording_id": rid},
                    headers=staff["headers"]).json()
    assert t["origin"] == "user" and t["contact_name"] == "Bhavnesh Desai Sir"
    assert client.post("/tasks", json={"text": "x", "recording_id": rid}, headers=coach_b["headers"]).status_code == 404
    assert client.put(f"/tasks/{t['id']}", json={"done": True}, headers=coach_b["headers"]).status_code == 404
    done = client.put(f"/tasks/{t['id']}", json={"done": True, "text": "Send the coin list to Asmita"},
                      headers=staff["headers"]).json()
    assert done["done"] and done["text"] == "Send the coin list to Asmita"
    assert t["id"] not in {x["id"] for x in client.get("/tasks", headers=staff["headers"]).json()["items"]}   # open only
    assert t["id"] in {x["id"] for x in client.get("/tasks", params={"status": "done"}, headers=staff["headers"]).json()["items"]}
    assert client.delete(f"/tasks/{t['id']}", headers=coach_b["headers"]).status_code == 404
    assert client.delete(f"/tasks/{t['id']}", headers=staff["headers"]).status_code == 200
    assert t["id"] not in {x["id"] for x in client.get("/tasks", params={"status": "all"}, headers=staff["headers"]).json()["items"]}
    r = client.get("/task-reminder", headers=staff["headers"]).json()
    assert r["on"] is True and r["time"] == "09:00"
    assert client.put("/task-reminder", json={"on": False, "time": "18:30"}, headers=staff["headers"]).json()["on"] is False
    assert client.put("/task-reminder", json={"time": "25:00"}, headers=staff["headers"]).status_code == 400


def test_ai_tasks_are_added_once(staff, client, no_processing):
    rid = _upload(client, staff).json()["recording_id"]
    content = "**Summary**\n- talked\n**Tasks**\n- Rizwana · send the coin list · by Friday\n- book the hall\n**Updates**\nNone"
    with SessionLocal() as db:
        assert service.save_call_tasks(db, rid, staff["id"], service.parse_call_tasks(content)) == 2
        assert service.save_call_tasks(db, rid, staff["id"], service.parse_call_tasks(content)) == 0   # a regeneration
        db.commit()
    items = client.get("/tasks", params={"recording_id": rid}, headers=staff["headers"]).json()["items"]
    assert {(i["assigned_to"], i["text"], i["due_text"]) for i in items} == {
        ("Rizwana", "send the coin list", "by Friday"), (None, "book the hall", None)}


def test_call_report_uses_call_summary_prompt(staff, client, no_processing, monkeypatch):
    rid = _upload(client, staff).json()["recording_id"]
    seen = {}
    monkeypatch.setattr(service, "_call_model", lambda org, rec, prompt, **k: seen.setdefault("p", prompt) and
                        "**Summary**\n- ok\n**Tasks**\n- Jay · call the warden\n**Updates**\nNone\n**Issues**\nNone")
    with SessionLocal() as db:
        db.execute(text("UPDATE isf_recorder.recording SET transcript = 'Speaker 1: please call the warden' WHERE id = :i"), {"i": rid})
        assert service.mark_generating_for_pipeline(db, rid, None, None)
        db.commit()
    assert service.generate_report(rid)
    assert "recorded on" in seen["p"] and "with Bhavnesh Desai Sir" in seen["p"] and "Call with: Bhavnesh Desai Sir" in seen["p"]
    rep = client.get(f"/recordings/{rid}/report", headers=staff["headers"]).json()
    assert rep["template_title"] == "Call summary"
    assert [i["text"] for i in client.get("/tasks", params={"recording_id": rid}, headers=staff["headers"]).json()["items"]] == ["call the warden"]


def test_bandhu_rows_become_call_summary_layout():
    from types import SimpleNamespace
    from backend.calls import import_bandhu as ib
    row = SimpleNamespace(contact_name="Call recording Bhavnesh Desai Sir", employee_name=None,
                          ai_summary="Manager asks Jay to focus on app work.",
                          extracted_tasks=[{"text": "Focus on recipes", "assigned_to": "Jay", "deadline": None}],
                          extracted_updates='[{"by": "Jay", "text": "Arrived at Ground Up"}]',
                          extracted_issues=[{"text": "Communication gap", "severity": "medium"}])
    assert ib._contact(row) == "Bhavnesh Desai Sir" and "bhavnesh desai sir" in ib.WORK_CONTACTS
    rep = ib._report(row)
    assert "**Summary**\n- Manager asks Jay" in rep and "- Jay · Focus on recipes" in rep
    assert "- Arrived at Ground Up (Jay)" in rep and "- Communication gap" in rep
    assert [t["text"] for t in service.parse_call_tasks(rep)] == ["Focus on recipes"]


def test_none_deadline_is_no_deadline():
    t = service.parse_call_tasks("**Tasks**\n- Speaker 2 · stick to app work · None\n**Updates**\nNone")
    assert t == [{"text": "stick to app work", "assigned_to": "Speaker 2", "due_text": None}]
