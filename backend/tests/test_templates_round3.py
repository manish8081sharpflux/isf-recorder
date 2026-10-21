"""Round 3: report templates can't vanish silently (fallback, blocked delete, restore defaults)."""
import uuid

from sqlalchemy import text

from backend.database import SessionLocal
from backend.settings import recorder
from backend.settings.report_prompts import MEETING_MINUTES_PROMPT
from backend.tests.conftest import org_id


def _tpl():
    with SessionLocal() as s:
        return {r.title: str(r.id) for r in s.execute(text(
            "SELECT id, title FROM isf_recorder.chat_template WHERE org_id = :o"), {"o": org_id()}).fetchall()}


def _new_template(client, admin, title):
    r = client.post("/chat/templates", json={"title": title, "prompt": "Write a short report."}, headers=admin["headers"])
    assert r.status_code == 200, r.text
    return _tpl()[title]


def test_mapped_template_cannot_be_deleted_and_unmapped_can(client, admin):
    recorder.seed_recorder_defaults()
    tid = _new_template(client, admin, f"Custom {uuid.uuid4().hex[:6]}")
    meeting = _tpl()[recorder.MEETING_TEMPLATE_TITLE]
    try:
        r = client.put("/settings/recorder-config", json={"report_templates": {"meeting": tid}}, headers=admin["headers"])
        assert r.json()["report_templates"]["meeting"]["id"] == tid  # a brand-new template can be mapped
        r = client.delete(f"/chat/templates/{tid}", headers=admin["headers"])
        assert r.status_code == 409 and "Meeting report" in r.json()["detail"]
    finally:
        client.put("/settings/recorder-config", json={"report_templates": {"meeting": meeting}}, headers=admin["headers"])
    assert client.delete(f"/chat/templates/{tid}", headers=admin["headers"]).status_code == 200


def test_dangling_mapping_falls_back_and_restore_brings_defaults_back(client, admin):
    recorder.seed_recorder_defaults()
    before = _tpl()
    meeting_id = before[recorder.MEETING_TEMPLATE_TITLE]
    with SessionLocal() as s:  # what happened on the phone test: Meeting minutes deleted while mapped
        s.execute(text("UPDATE isf_recorder.recording_report SET template_id = NULL WHERE template_id = :id"), {"id": meeting_id})
        s.execute(text("UPDATE isf_recorder.chat_message SET template_id = NULL WHERE template_id = :id"), {"id": meeting_id})
        s.execute(text("DELETE FROM isf_recorder.chat_template WHERE id = :id"), {"id": meeting_id})
        s.commit()
        assert recorder.template_for_mode(s, org_id(), "meeting") is None   # no default left either
        assert recorder.template_for_mode(s, org_id(), "schedule").title == recorder.COACH_TEMPLATE_TITLE
    r = client.post("/settings/report-templates/restore-defaults", headers=admin["headers"])
    assert r.status_code == 200 and r.json()["created"] == [recorder.MEETING_TEMPLATE_TITLE]
    assert r.json()["config"]["report_templates"]["meeting"]["title"] == recorder.MEETING_TEMPLATE_TITLE
    after = _tpl()
    assert after[recorder.COACH_TEMPLATE_TITLE] == before[recorder.COACH_TEMPLATE_TITLE]  # existing one untouched
    with SessionLocal() as s:
        prompt = s.execute(text("SELECT prompt FROM isf_recorder.chat_template WHERE id = :id"),
                           {"id": after[recorder.MEETING_TEMPLATE_TITLE]}).scalar()
        # a mapping pointing at a deleted template falls back to the default by title
        recorder.update_recorder_settings(s, org_id(), {}, {"meeting": str(uuid.uuid4())})
        s.commit()
        assert recorder.template_for_mode(s, org_id(), "meeting").title == recorder.MEETING_TEMPLATE_TITLE
        recorder.update_recorder_settings(s, org_id(), {}, {"meeting": after[recorder.MEETING_TEMPLATE_TITLE]})
        s.commit()
    assert prompt == MEETING_MINUTES_PROMPT and "## Summary" in prompt and "**Action tasks**" in prompt
    assert client.post("/settings/report-templates/restore-defaults", headers=admin["headers"]).json()["created"] == []
