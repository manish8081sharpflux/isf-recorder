"""Ask Jomi conversations (contract §6) and legacy chat mapping."""
import uuid

import pytest
from sqlalchemy import text

from backend.database import SessionLocal
from backend.tests.conftest import FakeGenaiClient, FakeResponse


@pytest.fixture
def fake_chat(monkeypatch):
    import backend.chat.gemini_chat as gchat
    fake = FakeGenaiClient(lambda model, contents, config: FakeResponse(f"answer {len(contents)}"))
    monkeypatch.setattr(gchat, "get_genai_client", lambda: fake)
    return fake


def _recording(user):
    with SessionLocal() as s:
        rid = str(s.execute(text("""
            INSERT INTO isf_recorder.recording (user_id, org_id, session_id, status, started_at, title, transcript)
            VALUES (:u, :o, :sid, 'completed', NOW(), 'Chat test', '[00:01] SPEAKER_00: hello') RETURNING id
        """), {"u": user["id"], "o": user["org_id"], "sid": f"rec_test_{uuid.uuid4().hex}"}).scalar())
        s.execute(text("INSERT INTO isf_recorder.transcript_segment (recording_id, speaker, text, start_time) VALUES (:id, 'SPEAKER_00', 'hello', 1)"),
                  {"id": rid})
        s.commit()
    return rid


def test_legacy_all_sessions_history_still_works(client, staff, fake_chat):
    r = client.get("/chat/all-sessions/history", headers=staff["headers"])
    assert r.status_code == 200 and r.json() == []
    r = client.post("/chat/all-sessions/message", json={"message": "What happened this week?"}, headers=staff["headers"])
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"id", "conversation_id", "role", "content", "template_id", "created_at"}
    hist = client.get("/chat/all-sessions/history", headers=staff["headers"]).json()
    assert [m["role"] for m in hist] == ["user", "assistant"]


def test_conversations_create_list_message(client, staff, fake_chat):
    rid = _recording(staff)
    c1 = client.post("/chat/conversations", json={"mode": "session", "recording_id": rid}, headers=staff["headers"])
    assert c1.status_code == 200 and c1.json()["title"] is None and c1.json()["message_count"] == 0
    conv = c1.json()["id"]
    msg = "Kaun kaun bacche aaye the aur kisko coins mile? Please list everything in detail for me today"
    r = client.post(f"/chat/conversations/{conv}/message", json={"message": msg}, headers=staff["headers"])
    assert r.status_code == 200 and r.json()["conversation_id"] == conv and r.json()["role"] == "assistant"
    client.post(f"/chat/conversations/{conv}/message", json={"message": "and tomorrow?"}, headers=staff["headers"])
    # the expensive context (instructions + transcript) is the same prefix on every message
    sys1 = fake_chat.calls[0]["config"].system_instruction
    sys2 = fake_chat.calls[1]["config"].system_instruction
    assert sys1 == sys2 and sys1.index("TRANSCRIPT:") < sys1.index("RECORDING SESSION METADATA:")

    data = client.get(f"/chat/conversations/{conv}/messages", headers=staff["headers"]).json()
    assert data["conversation"]["title"] == msg[:60] and data["conversation"]["message_count"] == 4
    assert [m["role"] for m in data["messages"]] == ["user", "assistant", "user", "assistant"]

    c2 = client.post("/chat/conversations", json={"mode": "all_sessions"}, headers=staff["headers"]).json()
    items = client.get("/chat/conversations", headers=staff["headers"]).json()["items"]
    assert items[0]["id"] == c2["id"] and {conv, c2["id"]} <= {i["id"] for i in items}
    only = client.get("/chat/conversations", params={"mode": "session", "recording_id": rid}, headers=staff["headers"]).json()
    assert [i["id"] for i in only["items"]] == [conv]
    assert only["items"][0]["recording_title"] == "Chat test"  # the app's session-chat pill
    assert c1.json()["recording_title"] == "Chat test" and c2["recording_title"] is None

    # legacy endpoint maps to the most recently updated conversation of the scope
    hist = client.get(f"/chat/session/{rid}/history", headers=staff["headers"]).json()
    assert len(hist) == 4

    r = client.patch(f"/chat/conversations/{conv}", json={"title": "Coins today"}, headers=staff["headers"])
    assert r.json()["title"] == "Coins today"
    with SessionLocal() as s:
        usage = s.execute(text("SELECT COUNT(*) FROM isf_recorder.gemini_usage_log WHERE recording_id = :id AND operation_type = 'CHAT'"),
                          {"id": rid}).scalar()
    assert usage == 2


def test_conversations_are_private(client, staff, admin, fake_chat):
    conv = client.post("/chat/conversations", json={"mode": "all_sessions"}, headers=staff["headers"]).json()["id"]
    assert client.get(f"/chat/conversations/{conv}/messages", headers=admin["headers"]).status_code == 404
    assert client.delete(f"/chat/conversations/{conv}", headers=admin["headers"]).status_code == 404
    assert client.delete(f"/chat/conversations/{conv}", headers=staff["headers"]).json()["status"] == "deleted"
    assert client.post("/chat/conversations", json={"mode": "session"}, headers=staff["headers"]).status_code == 400
    other_rec = _recording(admin)
    r = client.post("/chat/conversations", json={"mode": "session", "recording_id": other_rec}, headers=staff["headers"])
    assert r.status_code == 403


def test_template_without_example_output_does_not_crash(monkeypatch):
    """Admin-made templates can have no example output (NULL): chat must still answer (was a 500)."""
    from backend.chat import gemini_chat
    fake = FakeGenaiClient(lambda model, contents, config: FakeResponse("fine"))
    monkeypatch.setattr(gemini_chat, "get_genai_client", lambda: fake)
    tpl = {"title": "My template", "prompt": "Summarise", "example_output": None}
    out = gemini_chat.chat_with_gemini("context", [], "hello", tpl)
    assert out and len(fake.calls) >= 1
