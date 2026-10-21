"""Director's POV: one tap for admins, saved, regenerate; coaches never get it (button, endpoint or AI chat)."""
import uuid

import pytest
from sqlalchemy import text

from backend.chat import gemini_chat
from backend.database import SessionLocal
from backend.reports import director_pov


@pytest.fixture
def pov_template(admin):
    with SessionLocal() as s:
        tid = str(s.execute(text("""
            INSERT INTO isf_recorder.chat_template (org_id, created_by, title, prompt, is_active, sort_order)
            VALUES (:o, :u, :t, 'Write the director''s point of view.', TRUE, 99) RETURNING id
        """), {"o": admin["org_id"], "u": admin["id"], "t": f"🎬 Director's POV {uuid.uuid4().hex[:4]}"}).scalar())
        s.commit()
    yield tid
    with SessionLocal() as s:
        s.execute(text("DELETE FROM isf_recorder.chat_template WHERE id = :id"), {"id": tid})
        s.commit()


@pytest.fixture
def fake_ai(monkeypatch):
    calls = []

    def chat(context, history, message, template, recording_id=None, org_id=None):
        calls.append(template["title"])
        if template["prompt"].startswith("FAIL"):
            raise RuntimeError("AI down")
        return f"Director view #{len(calls)}"
    monkeypatch.setattr(gemini_chat, "chat_with_gemini", chat)
    monkeypatch.setattr(director_pov, "start_generation", lambda rid, tid, admin: director_pov.generate(rid, tid, admin))
    return calls


def _recording(user, source="recording", transcript="[00:01] SPEAKER_00: Aaj hum kahani padhenge"):
    with SessionLocal() as s:
        rid = str(s.execute(text("""
            INSERT INTO isf_recorder.recording (user_id, org_id, session_id, status, started_at, mode, title, source, transcript)
            VALUES (:u, :o, :sid, 'completed', NOW(), 'schedule', 'Mohor Boys · Rizwana', :src, :tr) RETURNING id
        """), {"u": user["id"], "o": user["org_id"], "sid": f"rec_test_{uuid.uuid4().hex}", "src": source, "tr": transcript}).scalar())
        s.commit()
    return rid


def test_admin_makes_it_once_then_it_is_saved(client, staff, admin, pov_template, fake_ai):
    rid = _recording(staff)
    assert client.get(f"/recordings/{rid}/director-pov", headers=admin["headers"]).json()["status"] == "none"
    r = client.post(f"/recordings/{rid}/director-pov", headers=admin["headers"]).json()
    assert r["status"] == "ready" and r["content"] == "Director view #1" and r["generated_by_name"] == admin["name"]
    again = client.post(f"/recordings/{rid}/director-pov", headers=admin["headers"]).json()
    assert again["content"] == "Director view #1" and len(fake_ai) == 1          # saved: no second AI call
    fresh = client.post(f"/recordings/{rid}/director-pov?regenerate=true", headers=admin["headers"]).json()
    assert fresh["content"] == "Director view #2"
    assert client.get(f"/recordings/{rid}/director-pov", headers=admin["headers"]).json()["content"] == "Director view #2"


def test_coaches_can_never_get_it(client, staff, admin, pov_template, fake_ai):
    rid = _recording(staff)
    assert client.get(f"/recordings/{rid}/director-pov", headers=staff["headers"]).status_code == 403
    assert client.post(f"/recordings/{rid}/director-pov", headers=staff["headers"]).status_code == 403
    # hidden from the coach's template list, visible to admins
    coach_titles = [t["title"] for t in client.get("/chat/templates", headers=staff["headers"]).json()]
    admin_titles = [t["title"] for t in client.get("/chat/templates", headers=admin["headers"]).json()]
    assert not any("Director" in t for t in coach_titles) and any("Director" in t for t in admin_titles)
    # refused in AI chat even with the template id
    r = client.post(f"/chat/session/{rid}/message", json={"message": "", "template_id": pov_template}, headers=staff["headers"])
    assert r.status_code == 403
    assert fake_ai == []


def test_failure_saves_error_and_regenerate_retries(client, staff, admin, pov_template, fake_ai):
    with SessionLocal() as s:
        s.execute(text("UPDATE isf_recorder.chat_template SET prompt = 'FAIL' WHERE id = :id"), {"id": pov_template})
        s.commit()
    rid = _recording(staff)
    r = client.post(f"/recordings/{rid}/director-pov", headers=admin["headers"]).json()
    assert r["status"] == "error" and "Regenerate" in r["error_message"]
    with SessionLocal() as s:
        s.execute(text("UPDATE isf_recorder.chat_template SET prompt = 'ok' WHERE id = :id"), {"id": pov_template})
        s.commit()
    assert client.post(f"/recordings/{rid}/director-pov", headers=admin["headers"]).json()["status"] == "ready"


def test_no_template_no_transcript_and_calls(client, staff, admin, fake_ai):
    with SessionLocal() as s:
        s.execute(text("UPDATE isf_recorder.chat_template SET is_active = FALSE WHERE org_id = :o AND title ILIKE '%director%'"),
                  {"o": admin["org_id"]})
        s.commit()
    rid = _recording(staff)
    r = client.post(f"/recordings/{rid}/director-pov", headers=admin["headers"])
    assert r.status_code == 404 and "Director" in r.json()["detail"]
    call = _recording(staff, source="call")
    assert client.post(f"/recordings/{call}/director-pov", headers=admin["headers"]).status_code == 404


def test_no_transcript_yet(client, staff, admin, pov_template, fake_ai):
    rid = _recording(staff, transcript=None)
    assert client.post(f"/recordings/{rid}/director-pov", headers=admin["headers"]).status_code == 409
