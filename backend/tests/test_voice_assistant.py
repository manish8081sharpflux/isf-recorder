"""Jomi voice assistant: admins only, answers about the app, every voice chat kept for Settings."""
from backend.assistant import routes as assistant
from backend.assistant.knowledge import GREETING, SYSTEM_PROMPT
from tests.conftest import FakeGenaiClient, FakeResponse, make_user, delete_user


def _fake_model(monkeypatch, answer="First tap Record, then pick the Balgruha."):
    import backend.transcription.gemini_stt as gstt
    fake = FakeGenaiClient(lambda model, contents, config: FakeResponse(answer))
    monkeypatch.setattr(gstt, "get_genai_client", lambda: fake)
    return fake


def test_staff_cannot_use_the_voice_assistant(client, staff):
    assert client.get("/assistant/greeting", headers=staff["headers"]).status_code == 403
    assert client.post("/assistant/message", json={"text": "hi"}, headers=staff["headers"]).status_code == 403
    assert client.get("/assistant/people", headers=staff["headers"]).status_code == 403


def test_voice_chat_is_answered_and_kept(client, admin, monkeypatch):
    fake = _fake_model(monkeypatch, "**First** tap Record,\n- then pick the Balgruha.")
    assert client.get("/assistant/greeting", headers=admin["headers"]).json()["text"] == GREETING

    r1 = client.post("/assistant/message", json={"text": "How do I start a recording?"}, headers=admin["headers"]).json()
    assert r1["reply"] == "First tap Record, then pick the Balgruha."           # spoken text: no Markdown
    assert fake.calls[0]["config"].system_instruction == SYSTEM_PROMPT
    assert fake.calls[0]["contents"][0]["parts"][0]["text"] == GREETING          # the chat starts with the greeting

    r2 = client.post("/assistant/message", json={"text": "And then?", "chat_id": r1["chat_id"]},
                     headers=admin["headers"]).json()
    assert r2["chat_id"] == r1["chat_id"]
    assert [c["parts"][0]["text"] for c in fake.calls[1]["contents"]] == [
        GREETING, "How do I start a recording?", r1["reply"], "And then?"]

    other = make_user("admin")
    try:
        people = client.get("/assistant/people", headers=other["headers"]).json()["people"]
        me = next(p for p in people if p["user_id"] == admin["id"])
        assert me["chats"] == 1 and me["name"] == admin["name"]
        chats = client.get(f"/assistant/chats?user_id={admin['id']}", headers=other["headers"]).json()["chats"]
        assert chats[0]["questions"] == 2 and chats[0]["first_question"] == "How do I start a recording?"
        msgs = client.get(f"/assistant/chats/{r1['chat_id']}/messages", headers=other["headers"]).json()["messages"]
        assert [m["role"] for m in msgs] == ["assistant", "user", "assistant", "user", "assistant"]
        # another admin can read the chat but cannot talk in it
        assert client.post("/assistant/message", json={"text": "x", "chat_id": r1["chat_id"]},
                           headers=other["headers"]).status_code == 404
    finally:
        delete_user(other["id"])


def test_failed_answer_keeps_nothing(client, admin, monkeypatch):
    _fake_model(monkeypatch, "")
    r = client.post("/assistant/message", json={"text": "Hello"}, headers=admin["headers"])
    assert r.status_code == 502
    assert client.get(f"/assistant/chats?user_id={admin['id']}", headers=admin["headers"]).json()["chats"] == []
    assert client.post("/assistant/message", json={"text": "  "}, headers=admin["headers"]).status_code == 400


def test_speakable_drops_markdown():
    assert assistant.speakable("# Steps\n1. Tap **Record**\n* Pick `Balgruha`") == "Steps 1. Tap Record Pick Balgruha"
