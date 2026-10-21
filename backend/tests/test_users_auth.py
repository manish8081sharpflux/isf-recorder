"""Auth & users (contract §2): live re-check, super admin, archive, permanent delete."""
import time
import uuid

import pytest
from sqlalchemy import text

from backend.auth import identity, otp
from backend.database import SessionLocal
from backend.tests.conftest import make_user, delete_user


@pytest.fixture
def super_admin(monkeypatch):
    u = make_user("admin")
    monkeypatch.setattr(identity, "SUPER_ADMIN_PHONES", {u["phone"][-10:]})
    identity.invalidate_user_cache()
    yield u
    identity.invalidate_user_cache()
    delete_user(u["id"])


def _recording_for(user, title="Kept recording"):
    with SessionLocal() as s:
        rid = s.execute(text("""
            INSERT INTO isf_recorder.recording (user_id, org_id, session_id, title, status, started_at, recorded_by_name)
            VALUES (:u, :o, :sid, :t, 'completed', NOW(), :n) RETURNING id
        """), {"u": user["id"], "o": user["org_id"], "sid": f"rec_test_{uuid.uuid4().hex}", "t": title,
               "n": user["name"]}).scalar()
        s.commit()
    return str(rid)


def test_me_has_super_admin_and_role_label(client, staff, super_admin):
    me = client.get("/auth/me", headers=staff["headers"]).json()
    assert me["is_super_admin"] is False and me["role_label"] == "Coach"
    me = client.get("/auth/me", headers=super_admin["headers"]).json()
    assert me["is_super_admin"] is True and me["role_label"] == "Admin"


def test_archived_user_gets_401_and_403_at_login(client, admin, staff):
    assert client.get("/auth/me", headers=staff["headers"]).status_code == 200
    r = client.post(f"/users/{staff['id']}/archive", headers=admin["headers"])
    assert r.status_code == 200
    r = client.get("/recordings/", headers=staff["headers"])
    assert r.status_code == 401 and r.json()["detail"] == "Account archived or removed"
    otp._otp_store[staff["phone"]] = ("123456", time.time())
    r = client.post("/auth/verify-otp", json={"phone": staff["phone"], "code": "123456"})
    assert r.status_code == 403 and "archived" in r.json()["detail"].lower()
    assert client.post(f"/users/{staff['id']}/unarchive", headers=admin["headers"]).status_code == 200
    assert client.get("/auth/me", headers=staff["headers"]).status_code == 200


def test_master_otp_removed():
    ok, _ = otp.verify_otp("919421187857", "999888")
    assert ok is False
    assert not hasattr(otp, "MASTER_OTP")


def test_login_returns_is_super_admin(client, staff):
    otp._otp_store[staff["phone"]] = ("654321", time.time())
    r = client.post("/auth/verify-otp", json={"phone": staff["phone"], "code": "654321"})
    assert r.status_code == 200
    assert r.json()["user"]["is_super_admin"] is False and r.json()["user"]["role"] == "staff"


def test_role_change_rules(client, admin, staff, super_admin):
    # a normal admin re-sending the unchanged role (v1.2.0 Edit dialog) is fine
    r = client.put(f"/users/{staff['id']}", json={"name": "Renamed Coach", "role": "staff"}, headers=admin["headers"])
    assert r.status_code == 200
    r = client.put(f"/users/{staff['id']}", json={"role": "admin"}, headers=admin["headers"])
    assert r.status_code == 403
    r = client.put(f"/users/{staff['id']}", json={"role": "admin"}, headers=super_admin["headers"])
    assert r.status_code == 200
    assert client.get("/auth/me", headers=staff["headers"]).json()["role"] == "admin"  # cache invalidated
    r = client.put(f"/users/{super_admin['id']}", json={"role": "staff"}, headers=super_admin["headers"])
    assert r.status_code == 409


def test_create_duplicate_phone_is_409(client, admin, staff):
    r = client.post("/users/", json={"name": "Someone", "phone": staff["phone"][-10:], "role": "staff"},
                    headers=admin["headers"])
    assert r.status_code == 409
    assert r.json()["existing_user_id"] == staff["id"] and staff["name"] in r.json()["detail"]
    r = client.post("/users/", json={"name": "New Admin", "phone": "9" + str(uuid.uuid4().int)[:9], "role": "admin"},
                    headers=admin["headers"])
    assert r.status_code == 403


def test_phone_change_duplicate_is_409(client, admin, staff):
    other = make_user("staff")
    try:
        r = client.put(f"/users/{staff['id']}", json={"phone": other["phone"]}, headers=admin["headers"])
        assert r.status_code == 409
    finally:
        delete_user(other["id"])


def test_cannot_archive_self_or_super_admin(client, admin, super_admin):
    assert client.post(f"/users/{admin['id']}/archive", headers=admin["headers"]).status_code == 403
    assert client.post(f"/users/{super_admin['id']}/archive", headers=admin["headers"]).status_code == 403
    assert client.delete(f"/users/{super_admin['id']}?permanent=true", headers=admin["headers"]).status_code == 403


def test_check_endpoint_matches_name_and_phone(client, admin, staff):
    r = client.get("/users/check", params={"name": f"  {staff['name'].upper()} "}, headers=admin["headers"])
    m = [x for x in r.json()["matches"] if x["id"] == staff["id"]]
    assert m and m[0]["match_on"] == ["name"]
    r = client.get("/users/check", params={"phone": "+91 " + staff["phone"][-10:]}, headers=admin["headers"])
    assert any(x["id"] == staff["id"] and "phone" in x["match_on"] for x in r.json()["matches"])
    assert client.get("/users/check", params={"name": "x"}, headers=staff["headers"]).status_code == 403


def test_non_admin_sees_masked_phones(client, staff, admin):
    users = client.get("/users/", headers=staff["headers"]).json()["users"]
    assert all(u["phone"].startswith("******") for u in users)
    users = client.get("/users/", headers=admin["headers"]).json()["users"]
    me = next(u for u in users if u["id"] == admin["id"])
    assert me["phone"] == admin["phone"] and me["role_label"] == "Admin" and me["is_archived"] is False


def test_permanent_delete_keeps_recordings(client, admin):
    coach = make_user("staff", name=f"Coach {uuid.uuid4().hex[:5]}")
    rid = _recording_for(coach)
    with SessionLocal() as s:  # a chat of the deleted user goes with them
        s.execute(text("""
            INSERT INTO isf_recorder.chat_conversation (user_id, org_id, recording_id, mode) VALUES (:u, :o, :r, 'session')
        """), {"u": coach["id"], "o": coach["org_id"], "r": rid})
        s.commit()
    r = client.delete(f"/users/{coach['id']}?permanent=true", headers=admin["headers"])
    assert r.status_code == 200 and r.json() == {"status": "deleted", "recordings_kept": 1}
    try:
        assert client.get("/auth/me", headers=coach["headers"]).status_code == 401
        items = client.get("/recordings/", params={"limit": 100}, headers=admin["headers"]).json()["items"]
        mine = next(i for i in items if i["id"] == rid)
        assert mine["user_id"] is None and mine["user_name"] == coach["name"]
        detail = client.get(f"/recordings/{rid}", headers=admin["headers"])
        assert detail.status_code == 200 and detail.json()["user_name"] == coach["name"]
        # self sign-up again with the same phone creates a fresh staff account
        otp._otp_store[coach["phone"]] = ("111222", time.time())
        login = client.post("/auth/verify-otp", json={"phone": coach["phone"], "code": "111222"})
        assert login.status_code == 200 and login.json()["user"]["user_id"] != coach["id"]
        new_id = login.json()["user"]["user_id"]
        delete_user(new_id)
    finally:
        with SessionLocal() as s:
            s.execute(text("DELETE FROM isf_recorder.recording WHERE id = :id"), {"id": rid})
            s.commit()


def test_last_admin_cannot_be_demoted(client, super_admin, monkeypatch):
    import backend.users.routes as users_routes
    monkeypatch.setattr(users_routes, "_active_admin_count", lambda db, org: 1)
    other_admin = make_user("admin")
    try:
        r = client.put(f"/users/{other_admin['id']}", json={"role": "staff"}, headers=super_admin["headers"])
        assert r.status_code == 409
    finally:
        delete_user(other_admin["id"])


def test_request_otp_only_for_real_indian_mobiles(client, monkeypatch):
    from backend.auth import routes as auth_routes
    sent = []

    async def fake_send(phone):
        sent.append(phone)
        return True, "OTP sent"
    monkeypatch.setattr(auth_routes, "send_otp_whatsapp", fake_send)
    for bad in ("98765432", "919876543", "91987654321", "1234567890", "5876543210", "00000000000"):
        assert client.post("/auth/request-otp", json={"phone": bad}).status_code == 400, bad
    assert sent == []                                            # nothing was sent for a wrong number
    for good in ("9876543210", "+91 98765 43210", "09876543210", "919876543210"):
        assert client.post("/auth/request-otp", json={"phone": good}).status_code == 200, good
    assert sent == ["919876543210"] * 4
