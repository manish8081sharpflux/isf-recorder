"""First login asks for the name: needs_name flag + PATCH /auth/me."""
from sqlalchemy import text

from backend.auth.routes import needs_name
from backend.database import SessionLocal
from backend.tests.conftest import make_user, delete_user


def test_needs_name_rules():
    assert needs_name("Staff 5678", None) and needs_name("staff  1234", {}) and needs_name("", {})
    assert needs_name("Rizwana", {"name_set": False})          # new sign-up flag
    assert not needs_name("Staff 5678", {"name_set": True})    # they chose it
    assert not needs_name("Rizwana", None) and not needs_name("Rizwana", '{"x": 1}')


def test_placeholder_user_is_asked_and_can_set_own_name(client):
    u = make_user("staff", name="Staff 4321")
    try:
        h = u["headers"]
        assert client.get("/auth/me", headers=h).json()["needs_name"] is True
        assert client.patch("/auth/me", json={"name": " a "}, headers=h).status_code == 400
        assert client.patch("/auth/me", json={"name": "x" * 61}, headers=h).status_code == 400
        assert client.patch("/auth/me", json={"name": "Staff 9999"}, headers=h).status_code == 400
        r = client.patch("/auth/me", json={"name": "  Rizwana   Shaikh "}, headers=h)
        assert r.status_code == 200 and r.json()["name"] == "Rizwana Shaikh" and r.json()["needs_name"] is False
        assert client.get("/auth/me", headers=h).json()["needs_name"] is False
        with SessionLocal() as s:
            assert s.execute(text("SELECT name FROM isf_recorder.user WHERE id = :id"), {"id": u["id"]}).scalar() == "Rizwana Shaikh"
    finally:
        delete_user(u["id"])


def test_admin_rename_clears_the_question(client, admin):
    u = make_user("staff", name="Staff 1111")
    try:
        with SessionLocal() as s:
            s.execute(text("""UPDATE isf_recorder.user SET settings = '{"name_set": false}'::jsonb WHERE id = :id"""), {"id": u["id"]})
            s.commit()
        assert client.put(f"/users/{u['id']}", json={"name": "Archana"}, headers=admin["headers"]).status_code == 200
        assert client.get("/auth/me", headers=u["headers"]).json()["needs_name"] is False
    finally:
        delete_user(u["id"])
