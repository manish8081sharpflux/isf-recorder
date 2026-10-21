"""Recorder config, lists and the template seed (contract §3, §9)."""
import uuid

from sqlalchemy import text

from backend.database import SessionLocal
from backend.settings import recorder
from backend.settings.report_prompts import COACH_REPORT_PROMPT, MEETING_MINUTES_PROMPT
from backend.tests.conftest import org_id


def _templates():
    with SessionLocal() as s:
        return s.execute(text("""
            SELECT id, title, prompt FROM isf_recorder.chat_template WHERE org_id = :o ORDER BY created_at
        """), {"o": org_id()}).fetchall()


def test_seed_is_idempotent_and_fills_templates(client):
    recorder.seed_recorder_defaults()
    recorder.seed_recorder_defaults()
    rows = _templates()
    coach = [r for r in rows if r.title == recorder.COACH_TEMPLATE_TITLE]
    meeting = [r for r in rows if r.title == recorder.MEETING_TEMPLATE_TITLE]
    assert len(coach) == 1 and coach[0].prompt == COACH_REPORT_PROMPT
    assert len(meeting) == 1 and meeting[0].prompt == MEETING_MINUTES_PROMPT
    with SessionLocal() as s:
        rec = recorder.get_recorder_settings(s, org_id())
    # round 3: Schedule -> Coach Report, Meeting -> Meeting minutes
    assert rec["report_templates"] == {"schedule": str(coach[0].id), "meeting": str(meeting[0].id)}
    assert rec["meeting_report_v1"] is True
    assert rec["templates_v2_seeded"] is True


def test_recorder_config_for_coach(client, staff):
    r = client.get("/settings/recorder-config", headers=staff["headers"])
    assert r.status_code == 200
    cfg = r.json()
    assert cfg["role_label"] == "Coach" and cfg["modes"] == ["schedule", "meeting"] and cfg["default_mode"] == "schedule"
    assert [b["name"] for b in cfg["balgruhs"]][:3] == ["Mohor Boys, Somwar Peth", "Mohor Girls, Budhwar Peth", "Nirmal Girls Balgruha, Kesnand"]
    assert len(cfg["balgruhs"]) == 15
    assert any(c["user_id"] == staff["id"] for c in cfg["coaches"])
    assert all(c.get("name") for c in cfg["participants"])
    assert cfg["report_templates"]["schedule"]["title"] == recorder.COACH_TEMPLATE_TITLE
    assert cfg["is_super_admin"] is False


def test_put_recorder_config_validates_and_keeps_other_keys(client, admin, staff):
    assert client.put("/settings/recorder-config", json={"role_label": "Teacher"}, headers=staff["headers"]).status_code == 403
    bad = client.put("/settings/recorder-config", json={"report_templates": {"meeting": str(uuid.uuid4())}},
                     headers=admin["headers"])
    assert bad.status_code == 400
    meeting_id = next(t.id for t in _templates() if t.title == recorder.MEETING_TEMPLATE_TITLE)
    coach_id = next(t.id for t in _templates() if t.title == recorder.COACH_TEMPLATE_TITLE)
    try:
        r = client.put("/settings/recorder-config",
                       json={"role_label": "Teacher", "report_templates": {"schedule": str(meeting_id)}},
                       headers=admin["headers"])
        assert r.status_code == 200
        assert r.json()["role_label"] == "Teacher"
        assert r.json()["report_templates"]["schedule"]["id"] == str(meeting_id)
        assert r.json()["report_templates"]["meeting"]["id"] == str(meeting_id)  # untouched slot kept (seeded)
        with SessionLocal() as s:
            settings = s.execute(text("SELECT settings FROM isf_recorder.organization WHERE id = :o"),
                                 {"o": org_id()}).scalar()
        assert "ai_models" in settings and settings.get("chat_templates_seeded") is True
        assert client.get("/auth/me", headers=staff["headers"]).json()["role_label"] == "Teacher"
    finally:
        client.put("/settings/recorder-config",
                   json={"role_label": "Coach", "report_templates": {"schedule": str(coach_id)}},
                   headers=admin["headers"])


def test_lists_crud_and_duplicates(client, admin, staff):
    name = f"Test Balgruha {uuid.uuid4().hex[:5]}"
    assert client.get("/settings/lists/balgruh", headers=staff["headers"]).status_code == 403
    assert client.get("/settings/lists/unknown", headers=admin["headers"]).status_code == 400
    r = client.post("/settings/lists/balgruh", json={"name": name}, headers=admin["headers"])
    assert r.status_code == 200
    item_id = r.json()["id"]
    try:
        dup = client.post("/settings/lists/balgruh", json={"name": name.upper()}, headers=admin["headers"])
        assert dup.status_code == 409 and dup.json()["existing_id"] == item_id
        r = client.put(f"/settings/lists/balgruh/{item_id}", json={"name": name + " Two", "sort_order": 9},
                       headers=admin["headers"])
        assert r.status_code == 200 and r.json()["sort_order"] == 9
        assert client.delete(f"/settings/lists/balgruh/{item_id}", headers=admin["headers"]).json()["is_active"] is False
        names = [i["name"] for i in client.get("/settings/lists/balgruh", headers=admin["headers"]).json()["items"]]
        assert name + " Two" not in names
        all_names = [i["name"] for i in client.get("/settings/lists/balgruh?include_inactive=true",
                                                    headers=admin["headers"]).json()["items"]]
        assert name + " Two" in all_names
        coach_name = client.post("/settings/lists/coach_name", json={"name": f"Guest Coach {item_id[:4]}"},
                                 headers=admin["headers"]).json()
        cfg = client.get("/settings/recorder-config", headers=staff["headers"]).json()
        assert {"user_id": None, "name": coach_name["name"]} in cfg["coaches"]
    finally:
        with SessionLocal() as s:
            s.execute(text("DELETE FROM isf_recorder.recorder_list_item WHERE id = :id OR name LIKE 'Guest Coach %'"),
                      {"id": item_id})
            s.commit()
