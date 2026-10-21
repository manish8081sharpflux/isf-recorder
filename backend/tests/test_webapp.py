"""Jomi web app (21 Oct): pages served at /web, signed 1-hour audio links with the same access rule as the app."""
import time
import uuid
from pathlib import Path

from sqlalchemy import text

from backend.database import SessionLocal
from backend.tests.conftest import AUDIO_ROOT, make_user, delete_user
from backend.webapp import routes as web


def _recording_with_audio(user, source="recording"):
    path = Path(AUDIO_ROOT) / f"web_{uuid.uuid4().hex}.m4a"
    path.write_bytes(b"\x00\x00\x00\x18ftypM4A " + b"x" * 2000)
    with SessionLocal() as s:
        rid = str(s.execute(text("""
            INSERT INTO isf_recorder.recording (user_id, org_id, session_id, status, started_at, title, source, audio_file_path)
            VALUES (:u, :o, :sid, 'completed', NOW(), 'Web test', :src, :p) RETURNING id
        """), {"u": user["id"], "o": user["org_id"], "sid": f"rec_test_{uuid.uuid4().hex}", "src": source, "p": str(path)}).scalar())
        s.commit()
    return rid


def test_pages_are_served(client):
    page = client.get("/web/")
    assert page.status_code == 200 and "<title>Jomi</title>" in page.text and "app.js" in page.text
    assert client.get("/web/app.js").status_code == 200
    assert client.get("/web/style.css").status_code == 200
    assert client.get("/app/latest").status_code == 200          # the app update check is not shadowed


def test_signed_audio_link(client, staff, admin):
    rid = _recording_with_audio(staff)
    link = client.post(f"/web/audio-link/{rid}", headers=staff["headers"])
    assert link.status_code == 200
    path = link.json()["path"]
    audio = client.get(path)                                         # no Authorization header: the signature is enough
    assert audio.status_code in (200, 206) and len(audio.content) > 1000
    assert client.get(path.replace("sig=", "sig=0")).status_code == 403          # tampered
    other = make_user("staff")
    try:
        assert client.post(f"/web/audio-link/{rid}", headers=other["headers"]).status_code == 403
    finally:
        delete_user(other["id"])
    assert client.post(f"/web/audio-link/{rid}", headers=admin["headers"]).status_code == 200
    assert client.post(f"/web/audio-link/{rid}").status_code in (401, 403)


def test_expired_link_is_refused(client, staff):
    rid = _recording_with_audio(staff)
    exp = int(time.time()) - 5
    sig = web._sign(rid, staff["id"], exp)
    assert client.get(f"/web/audio/{rid}?uid={staff['id']}&exp={exp}&sig={sig}").status_code == 403


def test_calls_stay_private(client, staff, admin):
    call = _recording_with_audio(staff, source="call")
    assert client.post(f"/web/audio-link/{call}", headers=admin["headers"]).status_code == 404
    assert client.post(f"/web/audio-link/{call}", headers=staff["headers"]).status_code == 200
