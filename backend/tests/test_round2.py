"""Round 2/3: report template per mode, several assistant coaches, DB super-admin flag, Balgruha list."""
import uuid

import pytest
from sqlalchemy import text

from backend.auth import identity
from backend.database import SessionLocal
from backend.reports import service
from backend.tests.conftest import make_user, delete_user, org_id


def _sid():
    return f"rec_test_{uuid.uuid4().hex}"


@pytest.fixture
def super_admin(monkeypatch):
    u = make_user("admin")
    monkeypatch.setattr(identity, "SUPER_ADMIN_PHONES", identity.SUPER_ADMIN_PHONES | {u["phone"][-10:]})
    identity.invalidate_user_cache()
    yield u
    identity.invalidate_user_cache()
    delete_user(u["id"])


# ─── 1. One report for every recording ───────────────────────────────────────

def test_meeting_gets_meeting_minutes_and_legacy_gets_coach_report(staff):
    with SessionLocal() as s:
        ids = [str(s.execute(text("""
            INSERT INTO isf_recorder.recording (user_id, org_id, session_id, status, started_at, mode, transcript)
            VALUES (:u, :o, :sid, 'completed', NOW(), :mode, 'x') RETURNING id
        """), {"u": staff["id"], "o": staff["org_id"], "sid": _sid(), "mode": mode}).scalar()) for mode in (None, "meeting")]
        for rid in ids:
            assert service.mark_generating_for_pipeline(s, rid, None if rid == ids[0] else "meeting", None) is True
        s.commit()
        titles = {str(r[0]): r[1] for r in s.execute(text("""
            SELECT recording_id, template_title FROM isf_recorder.recording_report WHERE recording_id = ANY(CAST(:ids AS uuid[]))
        """), {"ids": ids}).fetchall()}
    assert titles == {ids[0]: "Report template for Coach", ids[1]: "Meeting minutes"}


def test_put_accepts_a_template_per_mode(client, admin):
    with SessionLocal() as s:
        tpl = {r.title: str(r.id) for r in s.execute(text("SELECT id, title FROM isf_recorder.chat_template WHERE org_id = :o"),
                                                    {"o": org_id()}).fetchall()}
    coach, meeting = tpl["Report template for Coach"], tpl["Meeting minutes"]
    try:
        r = client.put("/settings/recorder-config", json={"report_templates": {"schedule": meeting, "meeting": coach}},
                       headers=admin["headers"])
        assert r.status_code == 200
        assert r.json()["report_templates"]["schedule"]["id"] == meeting and r.json()["report_templates"]["meeting"]["id"] == coach
        # only the sent slot changes
        r = client.put("/settings/recorder-config", json={"report_templates": {"schedule": coach}}, headers=admin["headers"])
        assert r.json()["report_templates"]["schedule"]["id"] == coach and r.json()["report_templates"]["meeting"]["id"] == coach
        # an admin's choice survives a restart (the one-time seed does not run again)
        from backend.settings import recorder
        recorder.seed_recorder_defaults()
        cfg = client.get("/settings/recorder-config", headers=admin["headers"]).json()
        assert cfg["report_templates"]["meeting"]["id"] == coach
    finally:
        client.put("/settings/recorder-config", json={"report_templates": {"schedule": coach, "meeting": meeting}},
                   headers=admin["headers"])


def test_report_metadata_lists_all_coaches_and_participants(staff):
    rec = type("R", (), {"started_at": None, "created_at": None, "mode": "schedule", "balgruh_name": "Mohor Boys, Somwar Peth",
                         "assistant_coaches": [{"user_id": None, "name": "Rizwana"}, {"user_id": None, "name": "Deepak"}],
                         "assistant_coach": "Rizwana, Deepak", "participants": [{"name": "Asmita"}],
                         "recorded_by": "Jameel", "title": "t"})()
    block = service._metadata_block(rec)
    assert "Balgruha: Mohor Boys, Somwar Peth" in block and "Assistant coaches: Rizwana, Deepak" in block
    assert "Participants: Asmita" in block and "Recorded by: Jameel" in block


# ─── 2. Several assistant coaches ────────────────────────────────────────────

def test_start_with_several_coaches(client, staff):
    body = {"session_id": _sid(), "mode": "schedule", "balgruh_name": "Mauli Balgruha, Wadebolhai",
            "assistant_coaches": [{"user_id": staff["id"], "name": "Rizwana"}, {"name": "Guest Coach"}, {"name": "rizwana"}]}
    rid = client.post("/recordings/start", json=body, headers=staff["headers"]).json()["recording_id"]
    d = client.get(f"/recordings/{rid}", headers=staff["headers"]).json()
    assert d["assistant_coaches"] == [{"user_id": staff["id"], "name": "Rizwana"}, {"user_id": None, "name": "Guest Coach"}]
    assert d["assistant_coach"] == "Rizwana, Guest Coach" and d["title"] == "Mauli Balgruha, Wadebolhai · Rizwana, Guest Coach"
    items = client.get("/recordings/", params={"mode": "schedule"}, headers=staff["headers"]).json()["items"]
    assert next(i for i in items if i["id"] == rid)["assistant_coaches"][1]["name"] == "Guest Coach"
    # the old single field still works
    legacy = client.post("/recordings/start", json={"session_id": _sid(), "mode": "schedule", "balgruh_name": "X",
                                                    "assistant_coach": "Rizwana"}, headers=staff["headers"])
    rid2 = legacy.json()["recording_id"]
    assert client.get(f"/recordings/{rid2}", headers=staff["headers"]).json()["assistant_coaches"] == [
        {"user_id": None, "name": "Rizwana"}]


def test_list_filters_by_balgruh_and_assistant_coach(client, staff):
    def start(balgruh, coaches):
        body = {"session_id": _sid(), "mode": "schedule", "balgruh_name": balgruh,
                "assistant_coaches": [{"name": c} for c in coaches]}
        return client.post("/recordings/start", json=body, headers=staff["headers"]).json()["recording_id"]
    tag = uuid.uuid4().hex[:6]
    a = start(f"Filter Home {tag}", ["Archana", f"Guest {tag}"])
    b = start(f"Filter Home {tag}", ["Deepak"])
    c = start(f"Other Home {tag}", [f"Guest {tag}"])

    def ids(**params):
        items = client.get("/recordings/", params={"limit": 100, **params}, headers=staff["headers"]).json()["items"]
        return {i["id"] for i in items} & {a, b, c}
    assert ids(balgruh=f"Filter Home {tag}") == {a, b}
    assert ids(assistant_coach=f"guest {tag}") == {a, c}                 # case-insensitive, whole name
    assert ids(assistant_coach="Guest") == set()                          # no partial-name matches
    assert ids(balgruh=f"Filter Home {tag}", assistant_coach="deepak") == {b}


def test_start_validation_rules(client, staff):
    def start(**kw):
        return client.post("/recordings/start", json={"session_id": _sid(), **kw}, headers=staff["headers"]).status_code
    assert start(mode="schedule", balgruh_name="X") == 400
    assert start(mode="schedule", balgruh_name="X", assistant_coaches=[]) == 400
    assert start(mode="meeting") == 400
    assert start(mode="meeting", participants=[{"name": "  "}]) == 400
    assert start(mode="meeting", participants=[{"name": "Asmita"}]) == 200
    assert start() == 200  # v1.2.0: no metadata, no validation


# ─── 3. Super-admin flag ─────────────────────────────────────────────────────

def test_super_admin_flag_rules(client, admin, staff, super_admin):
    target = make_user("admin")
    try:
        assert client.put(f"/users/{target['id']}", json={"is_super_admin": True}, headers=admin["headers"]).status_code == 403
        assert client.put(f"/users/{staff['id']}", json={"is_super_admin": True}, headers=super_admin["headers"]).status_code == 409
        r = client.put(f"/users/{target['id']}", json={"is_super_admin": True}, headers=super_admin["headers"])
        assert r.status_code == 200
        me = client.get("/auth/me", headers=target["headers"]).json()
        assert me["is_super_admin"] is True
        users = client.get("/users/", headers=admin["headers"]).json()["users"]
        assert next(u for u in users if u["id"] == target["id"])["is_super_admin"] is True
        # a DB super admin can now change roles
        assert client.put(f"/users/{staff['id']}", json={"role": "admin"}, headers=target["headers"]).status_code == 200
        assert client.put(f"/users/{target['id']}", json={"is_super_admin": False}, headers=super_admin["headers"]).status_code == 200
        assert client.get("/auth/me", headers=target["headers"]).json()["is_super_admin"] is False
        # bootstrap (SUPER_ADMIN_PHONES) super admins can never lose it
        r = client.put(f"/users/{super_admin['id']}", json={"is_super_admin": False}, headers=super_admin["headers"])
        assert r.status_code == 409
    finally:
        delete_user(target["id"])


def test_last_super_admin_cannot_be_removed(client, monkeypatch):
    """Only one flagged super admin in the org and no bootstrap phone for them -> 409."""
    lonely = make_user("admin")
    caller = make_user("admin")
    monkeypatch.setattr(identity, "SUPER_ADMIN_PHONES", {caller["phone"][-10:]})
    identity.invalidate_user_cache()
    with SessionLocal() as s:
        flagged = [str(r[0]) for r in s.execute(text(
            "SELECT id FROM isf_recorder.user WHERE org_id = :o AND is_super_admin"), {"o": org_id()}).fetchall()]
        s.execute(text("UPDATE isf_recorder.user SET is_super_admin = (id = :id) WHERE org_id = :o"),
                  {"id": lonely["id"], "o": org_id()})
        s.commit()
    try:
        r = client.put(f"/users/{lonely['id']}", json={"is_super_admin": False}, headers=caller["headers"])
        assert r.status_code == 409
    finally:
        with SessionLocal() as s:
            s.execute(text("UPDATE isf_recorder.user SET is_super_admin = (id = ANY(CAST(:ids AS uuid[]))) WHERE org_id = :o"),
                      {"ids": flagged, "o": org_id()})
            s.commit()
        identity.invalidate_user_cache()
        delete_user(lonely["id"])
        delete_user(caller["id"])


# ─── 4. Balgruha list ─────────────────────────────────────────────────────────

def test_balgruh_list_renamed_and_ordered(client, admin):
    items = client.get("/settings/lists/balgruh?include_inactive=true", headers=admin["headers"]).json()["items"]
    names = [i["name"] for i in items]
    assert names[0] == "Mohor Boys, Somwar Peth" and names[-1] == "ShriRamkrushna Varkari Sanstha"
    assert len(names) == len({n.lower() for n in names}) == 15
    assert not {"Mohor Boys", "Mohor Girls", "Nirmal Girls"} & set(names)


def test_silence_stop_minutes_per_mode(client, admin, staff):
    cfg = client.get("/settings/recorder-config", headers=staff["headers"]).json()
    assert set(cfg["silence_stop_minutes"]) == {"schedule", "meeting"}
    r = client.put("/settings/recorder-config", json={"silence_stop_minutes": {"schedule": 15}}, headers=admin["headers"])
    assert r.status_code == 200 and r.json()["silence_stop_minutes"]["schedule"] == 15
    r = client.put("/settings/recorder-config", json={"silence_stop_minutes": {"meeting": 5}}, headers=admin["headers"])
    assert r.json()["silence_stop_minutes"] == {"schedule": 15, "meeting": 5}      # partial update keeps the other
    assert client.put("/settings/recorder-config", json={"silence_stop_minutes": {"meeting": 999}},
                      headers=admin["headers"]).status_code == 400
    assert client.put("/settings/recorder-config", json={"silence_stop_minutes": {"schedule": 1}},
                      headers=staff["headers"]).status_code == 403
    client.put("/settings/recorder-config", json={"silence_stop_minutes": {"schedule": 0, "meeting": 0}}, headers=admin["headers"])


def test_report_translation_is_cached_per_report_version(client, staff, monkeypatch):
    calls = []
    monkeypatch.setattr(service, "translate", lambda rid, lang, content: calls.append(lang) or f"[{lang}] {content}")
    rid = client.post("/recordings/start", json={"session_id": _sid(), "mode": "schedule", "balgruh_name": "X",
                                                 "assistant_coaches": [{"name": "A"}]}, headers=staff["headers"]).json()["recording_id"]
    url = f"/recordings/{rid}/report/translate"
    assert client.post(url, json={"lang": "hi"}, headers=staff["headers"]).status_code == 409    # no report yet
    with SessionLocal() as db:
        db.execute(text("""INSERT INTO isf_recorder.recording_report (recording_id, content, status)
                           VALUES (:id, '**1. Learning** Story', 'draft')"""), {"id": rid})
        db.commit()
    r1 = client.post(url, json={"lang": "hi"}, headers=staff["headers"]).json()
    r2 = client.post(url, json={"lang": "hi"}, headers=staff["headers"]).json()
    assert r1["content"] == "[hi] **1. Learning** Story" and r2["cached"] is True and calls == ["hi"]
    client.put(f"/recordings/{rid}/report", json={"content": "**1. Learning** New story"}, headers=staff["headers"])
    r3 = client.post(url, json={"lang": "hi"}, headers=staff["headers"]).json()
    assert r3["content"] == "[hi] **1. Learning** New story" and calls == ["hi", "hi"]
    assert client.post(url, json={"lang": "mr"}, headers=staff["headers"]).status_code == 400   # Marathi not offered


def test_clean_translation_drops_prompt_wrapper():
    raw = 'रिपोर्ट: """\n**1. सीखना**\nकहानी\n"""'
    assert service.clean_translation(raw) == "**1. सीखना**\nकहानी"
    assert service.clean_translation('"""\n**A**\nB\n"""') == "**A**\nB"
    assert service.clean_translation("**General: overview of a long heading line**\ntext") == \
        "**General: overview of a long heading line**\ntext"          # real first lines are kept
