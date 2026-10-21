"""Name memory (21 Oct): names sent to the AI, corrections learned silently from report edits, heard names counted."""
import uuid

import pytest
from sqlalchemy import text

from backend.database import SessionLocal
from backend.names import memory


@pytest.fixture
def balgruh():
    name = f"Test Balgruha {uuid.uuid4().hex[:6]}"
    yield name
    with SessionLocal() as s:
        s.execute(text("DELETE FROM isf_recorder.name_memory WHERE balgruh_name = :b"), {"b": name})
        s.commit()


def _recording(user, balgruh, report="**Pavnesh** read the story. Asmita helped."):
    with SessionLocal() as s:
        rid = str(s.execute(text("""
            INSERT INTO isf_recorder.recording (user_id, org_id, session_id, status, started_at, mode, balgruh_name,
                assistant_coaches, title, source)
            VALUES (:u, :o, :sid, 'completed', NOW(), 'schedule', :b, '[{"user_id": null, "name": "Rizwana"}]'::jsonb,
                    'Test', 'recording') RETURNING id
        """), {"u": user["id"], "o": user["org_id"], "sid": f"rec_test_{uuid.uuid4().hex}", "b": balgruh}).scalar())
        s.execute(text("INSERT INTO isf_recorder.recording_report (recording_id, content, status) VALUES (:id, :c, 'draft')"),
                  {"id": rid, "c": report})
        s.commit()
    return rid


def test_name_corrections_only_pick_names():
    pairs = memory.name_corrections("Pavnesh read a good story with avnesh", "Bhavnesh read a great story with Bhavnesh")
    assert ("Pavnesh", "Bhavnesh") in pairs and ("avnesh", "Bhavnesh") in pairs
    assert all(w != "good" for w, _ in pairs)                     # good -> great is not a name


def test_apply_fixes_whole_words_any_case():
    out = memory.apply_fixes("pavnesh and Pavnesh, not Pavneshji", [("Pavnesh", "Bhavnesh")])
    assert out == "Bhavnesh and Bhavnesh, not Pavneshji"


def test_report_edit_is_learned_and_fixes_the_next_transcript(client, staff, admin, balgruh):
    rid = _recording(staff, balgruh)
    r = client.put(f"/recordings/{rid}/report", json={"content": "**Bhavnesh** read the story. Asmita helped."},
                   headers=staff["headers"])
    assert r.status_code == 200
    with SessionLocal() as s:
        pairs = memory.fixes(s, staff["org_id"], balgruh)
        assert ("Pavnesh", "Bhavnesh") in pairs
        assert memory.apply_fixes("Pavnesh was absent", pairs) == "Bhavnesh was absent"
        # the corrected name goes into the transcription prompt of this Balgruha
        rid2 = _recording(staff, balgruh, report="")
        names = memory.names_for_recording(s, rid2)
        assert "Bhavnesh" in names and "Rizwana" in names and staff["name"] in names
        assert "Bhavnesh" in memory.prompt_hint(names)
        # another Balgruha does not get this child's fix
        assert ("Pavnesh", "Bhavnesh") not in memory.fixes(s, staff["org_id"], balgruh + " other")

    # admins can see and forget it
    items = client.get("/names", params={"balgruh": balgruh}, headers=admin["headers"]).json()["items"]
    row = next(i for i in items if i["name"] == "Bhavnesh")
    assert row["wrong_spellings"] == ["Pavnesh"] and row["corrected_count"] == 1
    assert client.get("/names", headers=staff["headers"]).status_code == 403
    assert client.delete(f"/names/{row['id']}", headers=admin["headers"]).status_code == 200
    with SessionLocal() as s:
        assert memory.fixes(s, staff["org_id"], balgruh) == []


def test_heard_names_need_two_recordings_and_merge_close_spellings(staff, balgruh):
    with SessionLocal() as s:
        memory.learn_heard(s, staff["org_id"], balgruh, ["Shravani", "Kartik"])
        assert "Shravani" not in memory.known_names(s, staff["org_id"], balgruh)       # heard once: not yet
        memory.learn_heard(s, staff["org_id"], balgruh, ["Shravni", "Kartik"])           # close spelling = same child
        names = memory.known_names(s, staff["org_id"], balgruh)
        assert "Shravani" in names and "Kartik" in names and "Shravni" not in names


def test_parse_name_list():
    assert memory.parse_name_list("- Asmita\n2. Bhavnesh\nNONE\nHere are the names:\n") == ["Asmita", "Bhavnesh"]


def test_report_generation_learns_heard_names(staff, balgruh, monkeypatch):
    from backend.reports import service
    calls = []

    def fake_call(org_id, recording_id, prompt, temperature=0.2, operation_type="REPORT"):
        calls.append(prompt[:40])
        return "Shravani\nKartik" if prompt.startswith(memory.NAMES_PROMPT[:20]) else "**Shravani** read. Kartik sang."
    monkeypatch.setattr(service, "_call_model", fake_call)
    monkeypatch.setattr(memory, "start_learning_from_report",
                        lambda *a: memory.learn_from_report(*a))                     # run now, not in a thread
    rid = _recording(staff, balgruh, report="")
    with SessionLocal() as s:
        s.execute(text("UPDATE isf_recorder.recording SET transcript = 'S1: Shravani padh rahi hai' WHERE id = :id"), {"id": rid})
        s.execute(text("UPDATE isf_recorder.recording_report SET status = 'generating' WHERE recording_id = :id"), {"id": rid})
        s.commit()
    assert service.generate_report(rid) is True
    with SessionLocal() as s:
        heard = s.execute(text("SELECT name, heard_count FROM isf_recorder.name_memory WHERE balgruh_name = :b ORDER BY name"),
                          {"b": balgruh}).fetchall()
    assert [(h.name, h.heard_count) for h in heard] == [("Kartik", 1), ("Shravani", 1)]


def test_transcription_prompt_carries_the_names(monkeypatch):
    from backend.transcription import chunked_stt
    seen = []

    class FakeModels:
        def generate_content(self, model, contents, config):
            seen.append(contents[1])
            return None

    class FakeClient:
        models = FakeModels()
    monkeypatch.setattr("backend.transcription.usage_tracker.track_gemini_call",
                        lambda recording_id, operation_type, gemini_model, call_fn, audio_duration_seconds=0, **k: call_fn())
    token = chunked_stt.NAME_HINT.set(memory.prompt_hint(["Bhavnesh", "Asmita"]))
    try:
        chunked_stt._call_once(FakeClient(), "gemini-2.5-flash", b"RIFF", None, 1.0)
    finally:
        chunked_stt.NAME_HINT.reset(token)
    assert seen and seen[0].startswith(chunked_stt.COMPACT_PROMPT) and "Bhavnesh, Asmita" in seen[0]
