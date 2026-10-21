"""Reports (contract §5): endpoints, pipeline generation rules, REPORT usage logging."""
import time
import uuid

import pytest
from sqlalchemy import text

from backend.database import SessionLocal
from backend.reports import service
from backend.tests.conftest import FakeGenaiClient, FakeResponse


@pytest.fixture
def fake_ai(monkeypatch):
    prompts = []

    def responder(model, contents, config):
        prompts.append(contents[0])
        if "INSTRUCTION:" in contents[0]:
            return FakeResponse("**General Overview** - shorter version")
        return FakeResponse("```markdown\n**General Overview of the Complete Schedule** - Reading practice.\n\n**Asmita**\n*3. ISF Coins awarded* - 10\n```")
    monkeypatch.setattr(service, "get_genai_client", lambda: FakeGenaiClient(responder))
    return prompts


def _recording(user, mode="schedule"):
    with SessionLocal() as s:
        rid = str(s.execute(text("""
            INSERT INTO isf_recorder.recording (user_id, org_id, session_id, status, started_at, mode, balgruh_name,
                assistant_coach, participants, title, transcript, recorded_by_name)
            VALUES (:u, :o, :sid, 'completed', NOW(), :mode, 'Mohor Boys', 'Rizwana',
                    '[{"user_id": null, "name": "Asmita"}]'::jsonb, 'Mohor Boys · Daily schedule',
                    '[00:01] SPEAKER_00: Asmita ko das coins milte hain', :n) RETURNING id
        """), {"u": user["id"], "o": user["org_id"], "sid": f"rec_test_{uuid.uuid4().hex}", "mode": mode,
               "n": user["name"]}).scalar())
        s.execute(text("""INSERT INTO isf_recorder.transcript_segment (recording_id, speaker, text, start_time)
                          VALUES (:id, 'SPEAKER_00', 'Asmita ko das coins milte hain', 1.0)"""), {"id": rid})
        s.commit()
    return rid


def _wait_status(client, rid, user, want, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        r = client.get(f"/recordings/{rid}/report", headers=user["headers"])
        if r.status_code == 200 and r.json()["status"] == want:
            return r.json()
        time.sleep(0.2)
    raise AssertionError(f"report never reached {want}")


def test_report_lifecycle(client, staff, admin, fake_ai):
    rid = _recording(staff)
    assert client.get(f"/recordings/{rid}/report", headers=staff["headers"]).status_code == 404
    r = client.post(f"/recordings/{rid}/report/regenerate", json={}, headers=staff["headers"])
    assert r.status_code == 200 and r.json()["status"] == "generating"
    rep = _wait_status(client, rid, staff, "draft")
    assert rep["content"].startswith("**General Overview of the Complete Schedule**")  # code fence stripped
    assert rep["template_title"] == "Report template for Coach" and rep["can_edit"] is True
    prompt = fake_ai[0]
    assert "Balgruha: Mohor Boys" in prompt and "Assistant coaches: Rizwana" in prompt and "[00:01]" not in prompt
    assert "SPEAKER_00: Asmita ko das coins" in prompt
    with SessionLocal() as s:
        ops = [r[0] for r in s.execute(text("SELECT operation_type FROM isf_recorder.gemini_usage_log WHERE recording_id = :id"),
                                       {"id": rid}).fetchall()]
    assert [o for o in ops if o != "NAMES"] == ["REPORT"]   # NAMES = name memory, in the background

    r = client.put(f"/recordings/{rid}/report", json={"content": "edited by coach"}, headers=staff["headers"])
    assert r.status_code == 200 and r.json()["updated_by_name"] == staff["name"] and r.json()["status"] == "draft"
    edit = client.post(f"/recordings/{rid}/report/ai-edit", json={"instruction": "make it shorter"}, headers=staff["headers"])
    assert edit.status_code == 200 and edit.json()["proposed_content"] == "**General Overview** - shorter version"
    assert client.get(f"/recordings/{rid}/report", headers=staff["headers"]).json()["content"] == "edited by coach"

    assert client.post(f"/recordings/{rid}/report/finalize", headers=staff["headers"]).json()["status"] == "final"
    assert client.put(f"/recordings/{rid}/report", json={"content": "x"}, headers=staff["headers"]).status_code == 409
    assert client.post(f"/recordings/{rid}/report/reopen", headers=staff["headers"]).status_code == 403
    assert client.post(f"/recordings/{rid}/report/reopen", headers=admin["headers"]).json()["status"] == "draft"
    detail = client.get(f"/recordings/{rid}", headers=staff["headers"]).json()
    assert detail["report"]["status"] == "draft" and detail["report_status"] == "draft"


def test_pipeline_never_overwrites_edited_or_final_report(staff):
    rid = _recording(staff)
    with SessionLocal() as s:
        assert service.mark_generating_for_pipeline(s, rid, "schedule", None) is True
        s.commit()
        s.execute(text("UPDATE isf_recorder.recording_report SET status = 'draft', updated_by = :u WHERE recording_id = :id"),
                  {"u": staff["id"], "id": rid})
        s.commit()
        assert service.mark_generating_for_pipeline(s, rid, "schedule", None) is False
        s.execute(text("UPDATE isf_recorder.recording_report SET status = 'final', updated_by = NULL WHERE recording_id = :id"),
                  {"id": rid})
        s.commit()
        assert service.mark_generating_for_pipeline(s, rid, "schedule", None) is False
        # legacy recordings (no mode) get no automatic report
        assert service.mark_generating_for_pipeline(s, rid, None, None) is False


def test_report_error_keeps_recording_completed(client, staff, monkeypatch):
    rid = _recording(staff, mode="meeting")
    monkeypatch.setattr(service, "get_genai_client", lambda: FakeGenaiClient(lambda *a: FakeResponse("")))
    with SessionLocal() as s:
        service.mark_generating_for_pipeline(s, rid, "meeting", None)
        s.commit()
    assert service.generate_report(rid) is False
    rep = client.get(f"/recordings/{rid}/report", headers=staff["headers"]).json()
    assert rep["status"] == "error" and "empty" in rep["error_message"]
    assert rep["template_title"] == "Meeting minutes"  # round 3: meetings have their own report
    assert client.get(f"/recordings/{rid}", headers=staff["headers"]).json()["status"] == "completed"


def test_report_access_and_processing_guard(client, staff, admin, fake_ai):
    rid = _recording(staff)
    from backend.tests.conftest import make_user, delete_user
    other = make_user("staff")
    try:
        assert client.get(f"/recordings/{rid}/report", headers=other["headers"]).status_code == 403
    finally:
        delete_user(other["id"])
    with SessionLocal() as s:
        s.execute(text("UPDATE isf_recorder.recording SET status = 'processing' WHERE id = :id"), {"id": rid})
        s.commit()
    assert client.post(f"/recordings/{rid}/report/regenerate", json={}, headers=admin["headers"]).status_code == 409


def test_prompt_repeats_section_definitions_after_transcript():
    from backend.reports.service import build_report_prompt
    from backend.settings.report_prompts import COACH_REPORT_PROMPT, MEETING_MINUTES_PROMPT
    p = build_report_prompt(COACH_REPORT_PROMPT, "META", "TRANSCRIPT-BODY")
    tail = p[p.index("TRANSCRIPT-BODY"):]
    assert "*5. Observations*" in tail and "Karan was crying" in tail and "*7. General Tasks*" in tail
    m = build_report_prompt(MEETING_MINUTES_PROMPT, "META", "TRANSCRIPT-BODY")
    assert "re-read the section definitions" not in m   # no numbered sections -> nothing repeated


def test_observations_line_updated_once_and_edited_lines_kept(staff):
    from sqlalchemy import text as _t
    from backend.database import SessionLocal
    from backend.settings import recorder
    from backend.settings.report_prompts import OBSERVATIONS_OLD_LINE, OBSERVATIONS_NEW_LINE
    with SessionLocal() as s:
        old_id = s.execute(_t("""INSERT INTO isf_recorder.chat_template (org_id, title, prompt) VALUES (:o, 'Obs old copy', :p)
                                 RETURNING id"""), {"o": staff["org_id"], "p": "x\n" + OBSERVATIONS_OLD_LINE + "\ny"}).scalar()
        own_id = s.execute(_t("""INSERT INTO isf_recorder.chat_template (org_id, title, prompt) VALUES (:o, 'Obs own words', :p)
                                 RETURNING id"""), {"o": staff["org_id"], "p": "*5. Observations* - my own rule"}).scalar()
        s.execute(_t("""UPDATE isf_recorder.organization SET settings = settings #- '{recorder,observations_v3}' WHERE id = :o"""),
                  {"o": staff["org_id"]})
        s.commit()
    try:
        recorder.seed_recorder_defaults()
        with SessionLocal() as s:
            got_old = s.execute(_t("SELECT prompt FROM isf_recorder.chat_template WHERE id = :i"), {"i": old_id}).scalar()
            got_own = s.execute(_t("SELECT prompt FROM isf_recorder.chat_template WHERE id = :i"), {"i": own_id}).scalar()
        assert OBSERVATIONS_NEW_LINE in got_old and OBSERVATIONS_OLD_LINE not in got_old
        assert got_own == "*5. Observations* - my own rule"
    finally:
        with SessionLocal() as s:
            s.execute(_t("DELETE FROM isf_recorder.chat_template WHERE id IN (:a, :b)"), {"a": old_id, "b": own_id})
            s.commit()
