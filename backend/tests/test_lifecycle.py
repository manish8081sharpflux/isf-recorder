"""Token protection (contract §4.2-4.5, §7): idempotent finish, chunk rules, heartbeat, single-flight."""
import asyncio
import json
import os
import threading
import time
import uuid

import pytest
from sqlalchemy import text

from backend.database import SessionLocal
from backend.tests.conftest import wav_bytes
from backend.recordings import lifecycle
from backend.transcription import engine


def _sid():
    return f"rec_test_{uuid.uuid4().hex}"


def _start(client, user, sid):
    r = client.post("/recordings/start", json={"session_id": sid}, headers=user["headers"])
    assert r.status_code == 200, r.text
    return r.json()["recording_id"]


def _chunk(client, user, sid, idx, data=None):
    data = data or wav_bytes(2.0, amplitude=3000, freq=440)
    return client.post(f"/recordings/{sid}/chunk", data={"chunk_index": str(idx), "duration_seconds": "2.0"},
                       files={"chunk_file": (f"chunk_{idx}.wav", data, "audio/wav")}, headers=user["headers"])


def _row(rec_id):
    with SessionLocal() as s:
        return s.execute(text("SELECT * FROM isf_recorder.recording WHERE id = :id"), {"id": rec_id}).fetchone()


def _wait(pred, timeout=30.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.2)
    return False


@pytest.fixture
def scheduled(monkeypatch):
    calls = []

    def fake_schedule(rec_id, model_override=None, requested_by=None):
        calls.append(str(rec_id))
        return True
    monkeypatch.setattr(lifecycle, "schedule_transcription_job", fake_schedule)
    import backend.recordings.routes as routes
    monkeypatch.setattr(routes, "schedule_transcription_job", fake_schedule)
    return calls


def test_finish_twice_schedules_exactly_one_transcription(client, staff, scheduled):
    sid = _sid()
    rec_id = _start(client, staff, sid)
    for i in range(2):
        assert _chunk(client, staff, sid, i).json()["status"] == "ok"

    t0 = time.time()
    r1 = client.post(f"/recordings/{sid}/finish", json={"total_chunks": 2, "duration_seconds": 4}, headers=staff["headers"])
    first_ms = (time.time() - t0) * 1000
    r2 = client.post(f"/recordings/{sid}/finish", json={"total_chunks": 2, "duration_seconds": 4}, headers=staff["headers"])
    assert r1.status_code == 200 and r2.status_code == 200
    assert first_ms < 1000, f"/finish took {first_ms:.0f} ms"
    body = r1.json()
    for key in ("status", "recording_id", "session_id", "duration_seconds", "chunks_assembled"):
        assert key in body  # v1.2.0 parses these
    assert r2.json()["already_finished"] is True

    assert _wait(lambda: _row(rec_id).pipeline_stage == "queued")
    time.sleep(0.5)
    client.post(f"/recordings/{sid}/finish", json={"total_chunks": 2}, headers=staff["headers"])
    time.sleep(0.5)
    assert scheduled == [rec_id]
    row = _row(rec_id)
    assert row.status == "uploading" and row.finish_source == "client" and row.expected_chunks == 2
    assert os.path.exists(row.audio_file_path) and row.duration_seconds == 4


def test_finish_waits_for_missing_chunk_then_finalizes(client, staff, scheduled):
    sid = _sid()
    rec_id = _start(client, staff, sid)
    _chunk(client, staff, sid, 0)
    r = client.post(f"/recordings/{sid}/finish", json={"total_chunks": 2}, headers=staff["headers"])
    assert r.status_code == 200
    time.sleep(1.0)
    assert _row(rec_id).pipeline_stage == "awaiting_chunks" and scheduled == []
    assert _chunk(client, staff, sid, 1).json()["status"] == "ok"
    assert _wait(lambda: _row(rec_id).pipeline_stage == "queued")
    assert scheduled == [rec_id]


def test_chunk_after_client_stop_is_ignored(client, staff, scheduled):
    sid = _sid()
    rec_id = _start(client, staff, sid)
    _chunk(client, staff, sid, 0)
    client.post(f"/recordings/{sid}/finish", json={"total_chunks": 1}, headers=staff["headers"])
    r = _chunk(client, staff, sid, 1)
    assert r.status_code == 200 and r.json()["status"] == "ignored_after_stop"
    assert _chunk(client, staff, sid, 0).json()["status"] == "ok"  # a retry of a counted chunk is fine
    with SessionLocal() as s:
        assert s.execute(text("SELECT COUNT(*) FROM isf_recorder.audio_chunk WHERE recording_id = :id"),
                         {"id": rec_id}).scalar() == 1


def test_chunk_for_erased_session_is_discarded(client, staff):
    sid = _sid()
    with SessionLocal() as s:
        s.execute(text("INSERT INTO isf_recorder.deleted_session (session_id) VALUES (:sid)"), {"sid": sid})
        s.commit()
    try:
        r = _chunk(client, staff, sid, 0)
        assert r.status_code == 200 and r.json()["status"] == "discarded"
        f = client.post(f"/recordings/{sid}/finish", json={"total_chunks": 1}, headers=staff["headers"])
        assert f.status_code == 200 and f.json()["status"] == "discarded"
        with SessionLocal() as s:
            assert s.execute(text("SELECT COUNT(*) FROM isf_recorder.recording WHERE session_id = :sid"),
                             {"sid": sid}).scalar() == 0
    finally:
        with SessionLocal() as s:
            s.execute(text("DELETE FROM isf_recorder.deleted_session WHERE session_id = :sid"), {"sid": sid})
            s.commit()


def test_chunk_checksum_and_ownership(client, staff, admin):
    sid = _sid()
    _start(client, staff, sid)
    data = wav_bytes(1.0, amplitude=2000, freq=300)
    bad = client.post(f"/recordings/{sid}/chunk", data={"chunk_index": "0", "checksum": "0" * 64},
                      files={"chunk_file": ("c.wav", data, "audio/wav")}, headers=staff["headers"])
    assert bad.status_code == 400
    import hashlib
    good = client.post(f"/recordings/{sid}/chunk", data={"chunk_index": "0", "checksum": hashlib.sha256(data).hexdigest()},
                       files={"chunk_file": ("c.wav", data, "audio/wav")}, headers=staff["headers"])
    assert good.status_code == 200
    other = __import__("backend.tests.conftest", fromlist=["make_user"]).make_user("staff")
    try:
        assert _chunk(client, other, sid, 1).status_code == 403
    finally:
        __import__("backend.tests.conftest", fromlist=["delete_user"]).delete_user(other["id"])
    assert _chunk(client, admin, sid, 1).status_code == 200  # admin of the org may upload
    assert not list(__import__("pathlib").Path(os.environ["AUDIO_STORAGE_ROOT"]).rglob("*.tmp"))


def test_heartbeat_and_finalized_status(client, staff, scheduled):
    sid = _sid()
    rec_id = _start(client, staff, sid)
    r = client.post(f"/recordings/{sid}/heartbeat", json={"state": "listening", "voice_seconds": 12, "session_seconds": 40},
                    headers=staff["headers"])
    assert r.json() == {"status": "ok", "recording_status": "recording"}
    row = _row(rec_id)
    assert row.last_heartbeat_at is not None and row.voice_seconds == 12
    client.post(f"/recordings/{sid}/finish", json={"total_chunks": 0}, headers=staff["headers"])
    r = client.post(f"/recordings/{sid}/heartbeat", json={"state": "recording"}, headers=staff["headers"])
    assert r.json()["status"] == "finalized"


def test_resume_sets_resumed_at_inside_last_pause(client, staff):
    sid = _sid()
    rec_id = _start(client, staff, sid)
    client.post(f"/recordings/{sid}/pause", json={"reason": "user"}, headers=staff["headers"])
    client.post(f"/recordings/{sid}/resume", headers=staff["headers"])
    client.post(f"/recordings/{sid}/pause", json={"reason": "mic_conflict"}, headers=staff["headers"])
    client.post(f"/recordings/{sid}/resume", headers=staff["headers"])
    pauses = _row(rec_id).pauses
    assert len(pauses) == 2
    assert all(isinstance(p, dict) and p["resumed_at"] and p["paused_at"] for p in pauses)
    assert pauses[1]["reason"] == "mic_conflict"


def _completed_recording(user, with_audio=True):
    sid = _sid()
    path = None
    if with_audio:
        path = os.path.join(os.environ["AUDIO_STORAGE_ROOT"], "isf", f"{sid}.m4a")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(b"\0" * 5000)
    with SessionLocal() as s:
        rid = s.execute(text("""
            INSERT INTO isf_recorder.recording (user_id, org_id, session_id, status, started_at, audio_file_path,
                transcript, finish_requested_at, finish_source, pipeline_stage)
            VALUES (:u, :o, :sid, 'completed', NOW(), :p, 'old transcript', NOW(), 'client', 'done') RETURNING id
        """), {"u": user["id"], "o": user["org_id"], "sid": sid, "p": path}).scalar()
        s.commit()
    return str(rid)


def test_retranscribe_is_single_flight(client, staff, scheduled):
    rec_id = _completed_recording(staff)
    r1 = client.post(f"/recordings/{rec_id}/re-transcribe", headers=staff["headers"])
    r2 = client.post(f"/recordings/{rec_id}/re-transcribe", headers=staff["headers"])
    assert r1.status_code == 200 and r2.status_code == 409
    assert scheduled == [rec_id]
    row = _row(rec_id)
    assert row.status == "processing" and row.pipeline_stage == "queued"


def test_concurrent_claims_only_one_wins(staff):
    rec_id = _completed_recording(staff)
    results = []
    barrier = threading.Barrier(6)

    def claim():
        with SessionLocal() as s:
            barrier.wait()
            results.append(engine.queue_for_transcription(s, rec_id))
    threads = [threading.Thread(target=claim) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count(True) == 1


def test_schedule_never_queues_same_recording_twice(monkeypatch):
    started = []
    gate = threading.Event()

    async def slow_pipeline(rec_id, model_override=None, requested_by=None):
        started.append(rec_id)
        await asyncio.to_thread(gate.wait, 5)
    monkeypatch.setattr(engine, "process_recording_pipeline", slow_pipeline)
    rid = str(uuid.uuid4())
    assert engine.schedule_transcription_job(rid) is True
    assert engine.schedule_transcription_job(rid) is False
    assert engine.is_inflight(rid)
    gate.set()
    assert _wait(lambda: not engine.is_inflight(rid), 10)
    assert started == [rid]


def test_pipeline_exits_when_not_queued(staff, monkeypatch):
    """A stray second pipeline for a completed recording does nothing (no model calls)."""
    rec_id = _completed_recording(staff)
    asyncio.run(engine.process_recording_pipeline(rec_id))
    row = _row(rec_id)
    assert row.status == "completed" and row.transcript == "old transcript"
    with SessionLocal() as s:
        assert s.execute(text("SELECT COUNT(*) FROM isf_recorder.transcription_run WHERE recording_id = :id"),
                         {"id": rec_id}).scalar() == 0


def test_sweeper_idle_rules(staff, scheduled):
    # Idle finalize only applies to sessions started after 'idle_finalize_since' (old orphans are left alone)
    with SessionLocal() as s0:
        old_cutoff = s0.execute(text("SELECT value FROM isf_recorder.app_config WHERE key='idle_finalize_since'")).scalar()
        s0.execute(text("UPDATE isf_recorder.app_config SET value = CAST(NOW() - INTERVAL '2 days' AS text) WHERE key='idle_finalize_since'"))
        s0.commit()
    try:
        _sweeper_idle_rules_body(staff, scheduled)
    finally:
        with SessionLocal() as s0:
            s0.execute(text("UPDATE isf_recorder.app_config SET value = :v WHERE key='idle_finalize_since'"), {"v": old_cutoff})
            s0.commit()


def test_sweeper_ignores_sessions_before_cutoff(staff, scheduled):
    sid = _sid()
    with SessionLocal() as s:
        rid = s.execute(text("""
            INSERT INTO isf_recorder.recording (user_id, org_id, session_id, status, started_at, last_chunk_at)
            VALUES (:u, :o, :sid, 'recording', NOW() - INTERVAL '10 days', NOW() - INTERVAL '10 days') RETURNING id
        """), {"u": staff["id"], "o": staff["org_id"], "sid": sid}).scalar()
        s.commit()
    lifecycle.run_sweeper(only_ids=[str(rid)])
    assert _row(str(rid)).status == "recording"


def _sweeper_idle_rules_body(staff, scheduled):
    def make(hb_ago, chunk_ago, started_ago="5 hours"):
        sid = _sid()
        with SessionLocal() as s:
            rid = s.execute(text(f"""
                INSERT INTO isf_recorder.recording (user_id, org_id, session_id, status, started_at,
                    last_heartbeat_at, last_chunk_at)
                VALUES (:u, :o, :sid, 'recording', NOW() - INTERVAL '{started_ago}',
                        {f"NOW() - INTERVAL '{hb_ago}'" if hb_ago else 'NULL'},
                        {f"NOW() - INTERVAL '{chunk_ago}'" if chunk_ago else 'NULL'})
                RETURNING id
            """), {"u": staff["id"], "o": staff["org_id"], "sid": sid}).scalar()
            s.commit()
        return str(rid)

    live = make("1 minute", "40 minutes")            # heartbeat recent: never touched
    dead_new_app = make("31 minutes", "45 minutes")  # new app killed: finalized after 30 min
    old_app_recent = make(None, "2 hours")           # old APK, no heartbeats: waits 3 h
    old_app_dead = make(None, "4 hours")
    ids = [live, dead_new_app, old_app_recent, old_app_dead]
    summary = lifecycle.run_sweeper(only_ids=ids)
    assert _row(live).status == "recording"
    assert _row(old_app_recent).status == "recording"
    for rid in (dead_new_app, old_app_dead):
        row = _row(rid)
        assert row.status == "error" and row.error_message == lifecycle.ABANDONED_MESSAGE
        assert row.finish_source == "sweeper"
    assert summary["abandoned"] == 2 and scheduled == []


def test_no_speech_session_completes_without_ai(client, staff, scheduled):
    """New app: on-device VAD heard nothing -> /finish total_chunks=0, no chunks ever uploaded."""
    sid = _sid()
    rid = client.post("/recordings/start", json={"session_id": sid, "mode": "meeting", "participants": [{"name": "Asmita"}]},
                      headers=staff["headers"]).json()["recording_id"]
    r = client.post(f"/recordings/{sid}/finish", json={"total_chunks": 0, "duration_seconds": 0, "voice_seconds": 0,
                                                       "silence_skipped_seconds": 300},
                    headers={**staff["headers"], "X-Finish-Source": "client"})
    assert r.status_code == 200
    assert _wait(lambda: _row(rid).status == "completed", 10)
    row = _row(rid)
    assert row.title == "No speech detected" and row.overview == "No speech detected in this recording."
    assert row.pipeline_stage == "done" and row.silence_skipped_seconds == 300
    assert scheduled == []
    with SessionLocal() as s:
        assert s.execute(text("SELECT COUNT(*) FROM isf_recorder.recording_report WHERE recording_id = :id"),
                         {"id": rid}).scalar() == 0


def test_chunk_before_start_then_start_attaches_metadata(client, staff, scheduled):
    sid = _sid()
    assert _chunk(client, staff, sid, 0).json()["status"] == "ok"  # /start failed on the phone
    r = client.post("/recordings/start", json={"session_id": sid, "mode": "schedule", "balgruh_name": "Nirmal Girls",
                                               "assistant_coach": "Rizwana"}, headers=staff["headers"])
    assert r.json()["status"] == "exists"
    row = _row(r.json()["recording_id"])
    assert row.mode == "schedule" and row.title == "Nirmal Girls · Rizwana"
    assert row.recorded_by_name == staff["name"]


def test_sync_worker_finish_source(client, staff, scheduled):
    sid = _sid()
    rid = _start(client, staff, sid)
    _chunk(client, staff, sid, 0)
    client.post(f"/recordings/{sid}/finish", json={"total_chunks": 1},
                headers={**staff["headers"], "X-Finish-Source": "sync_worker"})
    assert _row(rid).finish_source == "sync_worker"
    assert _chunk(client, staff, sid, 1).json()["status"] == "ignored_after_stop"
