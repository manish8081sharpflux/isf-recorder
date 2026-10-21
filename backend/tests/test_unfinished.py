"""Admin 'Unfinished sessions': list + force-finish."""
import time
import uuid

import pytest
from sqlalchemy import text

from backend.database import SessionLocal
from backend.recordings import lifecycle
from backend.tests.conftest import wav_bytes


@pytest.fixture
def scheduled(monkeypatch):
    calls = []
    monkeypatch.setattr(lifecycle, "schedule_transcription_job", lambda rid, *a, **k: calls.append(str(rid)) or True)
    return calls


def _session(client, user, heartbeat_ago=None, chunks=0):
    sid = f"rec_test_{uuid.uuid4().hex}"
    rid = client.post("/recordings/start", json={"session_id": sid, "mode": "meeting", "title": "Stuck one",
                                                            "participants": [{"name": "Asmita"}]},
                      headers=user["headers"]).json()["recording_id"]
    for i in range(chunks):
        client.post(f"/recordings/{sid}/chunk", data={"chunk_index": str(i), "duration_seconds": "1.5"},
                    files={"chunk_file": ("c.wav", wav_bytes(1.5, 2000, 300), "audio/wav")}, headers=user["headers"])
    with SessionLocal() as s:
        s.execute(text(f"""
            UPDATE isf_recorder.recording SET started_at = NOW() - INTERVAL '2 hours',
                last_heartbeat_at = {f"NOW() - INTERVAL '{heartbeat_ago}'" if heartbeat_ago else 'NULL'}
            WHERE id = :id
        """), {"id": rid})
        s.commit()
    return rid


def _row(rid):
    with SessionLocal() as s:
        return s.execute(text("SELECT * FROM isf_recorder.recording WHERE id = :id"), {"id": rid}).fetchone()


def test_unfinished_list_route_and_contents(client, staff, admin):
    stuck = _session(client, staff, heartbeat_ago="2 hours", chunks=2)
    live = _session(client, staff, heartbeat_ago="30 seconds")
    assert client.get("/recordings/unfinished", headers=staff["headers"]).status_code == 403
    r = client.get("/recordings/unfinished", headers=admin["headers"])
    assert r.status_code == 200  # not routed to GET /recordings/{recording_id}
    items = {i["id"]: i for i in r.json()["items"]}
    assert stuck in items and live not in items
    item = items[stuck]
    assert item["chunks"] == 2 and item["audio_seconds"] == 3.0 and item["recorded_by"] == staff["name"]
    assert item["mode"] == "meeting" and item["title"] == "Stuck one" and item["started_at_formatted"]


def test_force_finish_rules(client, staff, admin, scheduled):
    live = _session(client, staff, heartbeat_ago="1 minute", chunks=1)
    assert client.post(f"/recordings/{live}/force-finish", headers=staff["headers"]).status_code == 403
    assert client.post(f"/recordings/{live}/force-finish", headers=admin["headers"]).status_code == 409

    with_audio = _session(client, staff, heartbeat_ago="1 hour", chunks=2)
    r = client.post(f"/recordings/{with_audio}/force-finish", headers=admin["headers"])
    assert r.status_code == 200 and r.json()["status"] == "uploading"
    end = time.time() + 30
    while time.time() < end and _row(with_audio).pipeline_stage != "queued":
        time.sleep(0.2)
    row = _row(with_audio)
    assert row.pipeline_stage == "queued" and row.finish_source == "admin" and scheduled == [with_audio]
    assert client.post(f"/recordings/{with_audio}/force-finish", headers=admin["headers"]).status_code == 409

    empty = _session(client, staff)
    r = client.post(f"/recordings/{empty}/force-finish", headers=admin["headers"])
    assert r.status_code == 200 and r.json()["status"] == "error" and r.json()["outcome"] == "abandoned"
    assert _row(empty).error_message == lifecycle.ABANDONED_MESSAGE
