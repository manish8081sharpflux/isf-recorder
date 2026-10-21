"""Raw-piece cleanup after 24 h, only once the m4a's content is verified against the pieces."""
import os
import uuid
from pathlib import Path

from sqlalchemy import text

from backend.database import SessionLocal
from backend.recordings import lifecycle
from backend.recordings.audio_verify import verify_m4a
from backend.recordings.chunk_assembler import assemble_recording
from backend.tests.conftest import wav_bytes

# piece levels: loud tone, silence, noise, loud tone, quiet (below -45: not checked)
PIECES = [dict(amplitude=6000, freq=300), dict(amplitude=0), dict(amplitude=3000, freq=-1),
          dict(amplitude=8000, freq=440), dict(amplitude=40, freq=200)]


def _pieces(folder: Path, silent_copy: bool = False):
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    for i, spec in enumerate(PIECES):
        p = folder / f"chunk_{i:04d}.wav"
        p.write_bytes(wav_bytes(6.0, **({"amplitude": 0} if silent_copy else spec)))
        paths.append(str(p))
    return paths


def _recording(staff, tmp_path: Path, corrupt: bool):
    sid = f"rec_test_{uuid.uuid4().hex}"
    chunk_dir = tmp_path / sid / "chunks"
    pieces = _pieces(chunk_dir)
    m4a = tmp_path / f"{sid}.m4a"
    if corrupt:  # same length, but the audio is gone (what happened to Jameel's 8 Oct file)
        bad = _pieces(tmp_path / "bad", silent_copy=True)
        assert assemble_recording(bad, str(m4a))[0]
    else:
        assert assemble_recording(pieces, str(m4a))[0]
    with SessionLocal() as s:
        rid = str(s.execute(text("""
            INSERT INTO isf_recorder.recording (user_id, org_id, session_id, status, pipeline_stage, started_at,
                   finished_at, audio_file_path, duration_seconds)
            VALUES (:u, :o, :sid, 'completed', 'done', NOW() - INTERVAL '26 hours', NOW() - INTERVAL '25 hours', :p, 30)
            RETURNING id
        """), {"u": staff["id"], "o": staff["org_id"], "sid": sid, "p": str(m4a)}).scalar())
        for i, p in enumerate(pieces):
            s.execute(text("""
                INSERT INTO isf_recorder.audio_chunk (recording_id, chunk_index, file_path, duration_seconds, size_bytes, status)
                VALUES (:r, :i, :p, 6, :sz, 'received')
            """), {"r": rid, "i": i, "p": p, "sz": os.path.getsize(p)})
        s.commit()
    return rid, pieces, m4a


def _row(rid):
    with SessionLocal() as s:
        return s.execute(text("""
            SELECT audio_verify_status, audio_verified_at, chunks_cleaned_at FROM isf_recorder.recording WHERE id = :id
        """), {"id": rid}).fetchone()


def _audit(rid):
    with SessionLocal() as s:
        return [r[0] for r in s.execute(text("SELECT action FROM isf_recorder.audit_log WHERE target_id = :id"),
                                         {"id": rid}).fetchall()]


def test_verify_detects_silent_m4a(tmp_path):
    pieces = _pieces(tmp_path / "p")
    good, bad = tmp_path / "good.m4a", tmp_path / "bad.m4a"
    assert assemble_recording(pieces, str(good))[0]
    assert assemble_recording(_pieces(tmp_path / "s", silent_copy=True), str(bad))[0]
    ok = verify_m4a(str(good), pieces)
    assert ok["status"] == "ok" and ok["voiced"] == 4 and ok["mismatches"] == 0  # quiet piece now checked too
    failed = verify_m4a(str(bad), pieces)
    assert failed["status"] == "failed" and failed["mismatches"] == 4
    assert verify_m4a(str(good), pieces + [str(tmp_path / "missing.wav")])["status"] == "no_pieces"


def test_good_m4a_pieces_deleted(staff, tmp_path):
    rid, pieces, m4a = _recording(staff, tmp_path, corrupt=False)
    res = lifecycle.cleanup_chunk_wavs(recording_ids=[rid])
    assert res["recordings"] == 1 and res["files"] == len(pieces)
    assert not any(os.path.exists(p) for p in pieces) and m4a.exists()
    row = _row(rid)
    assert row.audio_verify_status == "ok" and row.audio_verified_at and row.chunks_cleaned_at


def test_corrupted_m4a_rebuilt_then_pieces_deleted(staff, tmp_path):
    rid, pieces, m4a = _recording(staff, tmp_path, corrupt=True)
    assert verify_m4a(str(m4a), pieces)["status"] == "failed"
    res = lifecycle.cleanup_chunk_wavs(recording_ids=[rid])
    assert res["rebuilt"] == 1 and res["recordings"] == 1
    assert _row(rid).audio_verify_status == "rebuilt" and _row(rid).chunks_cleaned_at
    assert "audio_rebuilt_from_pieces" in _audit(rid)
    assert not any(os.path.exists(p) for p in pieces)
    # the replaced m4a now really holds the audio: louder than -45 dBFS where the tone was
    from backend.recordings.audio_verify import _ffmpeg_decode, _dbfs
    assert _dbfs(_ffmpeg_decode(str(m4a))[: 6 * 16000]) > -30


def test_rebuild_that_fails_keeps_pieces(staff, tmp_path, monkeypatch):
    rid, pieces, m4a = _recording(staff, tmp_path, corrupt=True)
    monkeypatch.setattr(lifecycle, "assemble_recording", lambda paths, out: (False, 0.0, 0))
    res = lifecycle.cleanup_chunk_wavs(recording_ids=[rid])
    assert res["failed"] == 1 and res["recordings"] == 0 and res["files"] == 0
    assert all(os.path.exists(p) for p in pieces)
    row = _row(rid)
    assert row.audio_verify_status == "rebuild_failed" and row.chunks_cleaned_at is None
    assert "audio_verify_failed" in _audit(rid)
    # not retried within 24 h
    assert lifecycle.cleanup_chunk_wavs(recording_ids=[rid])["checked"] == 0


def test_dry_run_and_age(staff, tmp_path):
    rid, pieces, m4a = _recording(staff, tmp_path, corrupt=True)
    res = lifecycle.cleanup_chunk_wavs(recording_ids=[rid], dry_run=True)
    assert res["checked"] == 1 and res["details"][0]["status"] == "failed" and res["files"] == 0
    assert all(os.path.exists(p) for p in pieces) and _row(rid).audio_verify_status is None
    with SessionLocal() as s:  # younger than 24 h: not touched
        s.execute(text("UPDATE isf_recorder.recording SET finished_at = NOW() - INTERVAL '23 hours' WHERE id = :id"), {"id": rid})
        s.commit()
    assert lifecycle.cleanup_chunk_wavs(recording_ids=[rid])["checked"] == 0


def test_quiet_recording_with_silent_m4a_fails(tmp_path):
    """Soft speakers in a quiet room (~-53 dBFS): a silent m4a must NOT pass by default."""
    folder = tmp_path / "quiet"; folder.mkdir()
    pieces = []
    for i in range(3):
        p = folder / f"chunk_{i:04d}.wav"; p.write_bytes(wav_bytes(6.0, amplitude=100, freq=250)); pieces.append(str(p))
    silent = tmp_path / "silent"; silent.mkdir()
    zeros = []
    for i in range(3):
        p = silent / f"chunk_{i:04d}.wav"; p.write_bytes(wav_bytes(6.0, amplitude=0)); zeros.append(str(p))
    good, bad = tmp_path / "q_good.m4a", tmp_path / "q_bad.m4a"
    assert assemble_recording(pieces, str(good))[0] and assemble_recording(zeros, str(bad))[0]
    assert verify_m4a(str(good), pieces)["status"] == "ok"
    res = verify_m4a(str(bad), pieces)
    assert res["voiced"] == 3 and res["status"] == "failed"


def test_truncated_m4a_fails(tmp_path):
    """An m4a missing the end (even if that part is quiet) fails the length check."""
    pieces = _pieces(tmp_path / "p")
    short = tmp_path / "short.m4a"
    assert assemble_recording(pieces[:3], str(short))[0]
    res = verify_m4a(str(short), pieces)
    assert res["status"] == "failed" and res["length_ratio"] < 0.98
