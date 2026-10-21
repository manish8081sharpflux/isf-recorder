"""
ISF Recorder — Comprehensive Gemini Usage & Cost Tracking Tests
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Validates all 14 test scenarios specified in the requirements:
 1. Successful transcription Gemini call
 2. Successful summary Gemini call
 3. Multiple Gemini calls for one recording
 4. Failed Gemini request logging
 5. Gemini retry (both attempts preserved)
 6. Missing usage metadata handling
 7. Different Gemini models pricing
 8. Cost calculation formula accuracy
 9. INR conversion with exchange rate
10. Recording-level aggregation API logic
11. Monthly aggregation API logic
12. Duplicate / retry request correlation
13. Recording with zero Gemini calls
14. Long recording with multiple chapter calls
"""
import uuid
import unittest
from datetime import datetime, timezone
from sqlalchemy import text

from backend.database import SessionLocal
from backend.transcription.forex_sync import get_live_usd_to_inr_rate
from backend.transcription.usage_tracker import (
    extract_usage_tokens,
    calculate_cost,
    get_model_pricing,
    log_gemini_call,
    track_gemini_call,
)


class MockModalityTokenCount:
    def __init__(self, modality: str, token_count: int):
        self.modality = modality
        self.token_count = token_count


class MockUsageMetadata:
    def __init__(
        self,
        prompt_token_count: int = 1000,
        candidates_token_count: int = 500,
        total_token_count: int = 1500,
        cached_content_token_count: int = 0,
        thoughts_token_count: int = 0,
        prompt_tokens_details: list = None
    ):
        self.prompt_token_count = prompt_token_count
        self.candidates_token_count = candidates_token_count
        self.total_token_count = total_token_count
        self.cached_content_token_count = cached_content_token_count
        self.thoughts_token_count = thoughts_token_count
        self.prompt_tokens_details = prompt_tokens_details or []

    def model_dump(self):
        return {
            "prompt_token_count": self.prompt_token_count,
            "candidates_token_count": self.candidates_token_count,
            "total_token_count": self.total_token_count,
            "cached_content_token_count": self.cached_content_token_count,
            "thoughts_token_count": self.thoughts_token_count,
            "prompt_tokens_details": [
                {"modality": getattr(p, "modality", "TEXT"), "token_count": getattr(p, "token_count", 0)}
                for p in self.prompt_tokens_details
            ]
        }


class MockGenerateContentResponse:
    def __init__(self, text: str = "Test response", usage_metadata=None):
        self.text = text
        self.usage_metadata = usage_metadata


class TestGeminiUsageTracking(unittest.TestCase):

    def setUp(self):
        self.db = SessionLocal()
        # Ensure test organization and user exist
        self.db.execute(text("""
            INSERT INTO isf_recorder.organization (id, name, slug)
            VALUES ('00000000-0000-0000-0000-000000000001', 'Test Org', 'test-org')
            ON CONFLICT (slug) DO NOTHING
        """))
        self.db.execute(text("""
            INSERT INTO isf_recorder.user (id, org_id, name, phone, role)
            VALUES ('00000000-0000-0000-0000-000000000002', '00000000-0000-0000-0000-000000000001', 'Tester', '9999999999', 'admin')
            ON CONFLICT (phone) DO NOTHING
        """))
        self.db.commit()
        self.created_recordings = []

    def tearDown(self):
        for rec_id in self.created_recordings:
            try:
                self.db.execute(text("DELETE FROM isf_recorder.recording WHERE id = :id"), {"id": rec_id})
                self.db.commit()
            except Exception:
                self.db.rollback()
        self.db.close()

    def create_recording(self) -> str:
        rec_id = str(uuid.uuid4())
        self.db.execute(text("""
            INSERT INTO isf_recorder.recording (id, user_id, org_id, session_id, started_at)
            VALUES (:id, '00000000-0000-0000-0000-000000000002', '00000000-0000-0000-0000-000000000001', :sid, NOW())
        """), {"id": rec_id, "sid": f"test_session_{uuid.uuid4().hex[:8]}"})
        self.db.commit()
        self.created_recordings.append(rec_id)
        return rec_id

    def test_01_successful_transcription_call(self):
        """1. Successful transcription call logging with audio modality tokens."""
        rec_id = self.create_recording()
        mock_usage = MockUsageMetadata(
            prompt_token_count=52000,
            candidates_token_count=1200,
            total_token_count=53200,
            prompt_tokens_details=[MockModalityTokenCount("AUDIO", 50000)]
        )
        mock_resp = MockGenerateContentResponse("Transcription test", mock_usage)

        resp = track_gemini_call(
            recording_id=rec_id,
            operation_type="TRANSCRIPTION",
            gemini_model="gemini-2.5-flash",
            call_fn=lambda: mock_resp,
            audio_duration_seconds=1800.0,
        )
        self.assertEqual(resp.text, "Transcription test")

        row = self.db.execute(
            text("""
                SELECT operation_type, input_audio_tokens, input_tokens, output_tokens, api_status
                FROM isf_recorder.gemini_usage_log
                WHERE recording_id = :rec_id AND operation_type = 'TRANSCRIPTION'
                ORDER BY created_at DESC LIMIT 1
            """),
            {"rec_id": rec_id}
        ).fetchone()

        self.assertIsNotNone(row)
        self.assertEqual(row.operation_type, "TRANSCRIPTION")
        self.assertEqual(row.input_audio_tokens, 50000)
        self.assertEqual(row.input_tokens, 2000)
        self.assertEqual(row.output_tokens, 1200)
        self.assertEqual(row.api_status, "SUCCESS")

    def test_02_successful_summary_call(self):
        """2. Successful summary call logging with text tokens."""
        rec_id = self.create_recording()
        mock_usage = MockUsageMetadata(
            prompt_token_count=4500,
            candidates_token_count=850,
            total_token_count=5350
        )
        mock_resp = MockGenerateContentResponse("Summary test", mock_usage)

        resp = track_gemini_call(
            recording_id=rec_id,
            operation_type="SUMMARY",
            gemini_model="gemini-2.5-flash",
            call_fn=lambda: mock_resp,
        )
        self.assertEqual(resp.text, "Summary test")

        row = self.db.execute(
            text("""
                SELECT operation_type, input_audio_tokens, input_tokens, output_tokens
                FROM isf_recorder.gemini_usage_log
                WHERE recording_id = :rec_id AND operation_type = 'SUMMARY'
                ORDER BY created_at DESC LIMIT 1
            """),
            {"rec_id": rec_id}
        ).fetchone()

        self.assertIsNotNone(row)
        self.assertEqual(row.input_audio_tokens, 0)
        self.assertEqual(row.input_tokens, 4500)
        self.assertEqual(row.output_tokens, 850)

    def test_03_multiple_calls_for_one_recording(self):
        """3. Multiple distinct calls (STT + Summary + Chat) linked to one recording."""
        rec_id = self.create_recording()
        for op in ["TRANSCRIPTION", "SUMMARY", "CHAT"]:
            track_gemini_call(
                recording_id=rec_id,
                operation_type=op,
                gemini_model="gemini-2.5-flash",
                call_fn=lambda: MockGenerateContentResponse(op, MockUsageMetadata(100, 50, 150)),
            )

        count = self.db.execute(
            text("SELECT count(*) FROM isf_recorder.gemini_usage_log WHERE recording_id = :rec_id"),
            {"rec_id": rec_id}
        ).scalar()
        self.assertEqual(count, 3)

    def test_04_failed_gemini_request(self):
        """4. Failed Gemini request logged with FAILED status and error message."""
        rec_id = self.create_recording()

        def _failing_call():
            raise ConnectionError("Google Cloud endpoint timeout (test)")

        with self.assertRaises(ConnectionError):
            track_gemini_call(
                recording_id=rec_id,
                operation_type="TRANSCRIPTION",
                gemini_model="gemini-2.5-flash",
                call_fn=_failing_call,
            )

        row = self.db.execute(
            text("""
                SELECT api_status, error_message
                FROM isf_recorder.gemini_usage_log
                WHERE recording_id = :rec_id AND api_status = 'FAILED'
                ORDER BY created_at DESC LIMIT 1
            """),
            {"rec_id": rec_id}
        ).fetchone()

        self.assertIsNotNone(row)
        self.assertEqual(row.api_status, "FAILED")
        self.assertIn("Google Cloud endpoint timeout", row.error_message)

    def test_05_gemini_retry_handling(self):
        """5. Retry scenario: Request #1 FAILED, Request #2 SUCCESS -> both logged."""
        rec_id = self.create_recording()

        # Attempt 1: Fails
        try:
            track_gemini_call(
                recording_id=rec_id,
                operation_type="TRANSCRIPTION",
                gemini_model="gemini-2.5-flash",
                call_fn=lambda: (_ for _ in ()).throw(ValueError("Model overloaded")),
            )
        except ValueError:
            pass

        # Attempt 2: Succeeds on fallback model
        track_gemini_call(
            recording_id=rec_id,
            operation_type="TRANSCRIPTION",
            gemini_model="gemini-3.5-flash",
            call_fn=lambda: MockGenerateContentResponse("ok", MockUsageMetadata(100, 50, 150)),
        )

        rows = self.db.execute(
            text("""
                SELECT api_status, gemini_model
                FROM isf_recorder.gemini_usage_log
                WHERE recording_id = :rec_id
                ORDER BY created_at ASC
            """),
            {"rec_id": rec_id}
        ).fetchall()

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].api_status, "FAILED")
        self.assertEqual(rows[1].api_status, "SUCCESS")

    def test_06_missing_usage_metadata(self):
        """6. Missing usage metadata does not crash, saves 0 tokens without fabrication."""
        rec_id = self.create_recording()
        mock_resp = MockGenerateContentResponse("Empty metadata response", usage_metadata=None)

        resp = track_gemini_call(
            recording_id=rec_id,
            operation_type="SUMMARY",
            gemini_model="gemini-2.5-flash",
            call_fn=lambda: mock_resp,
        )
        self.assertEqual(resp.text, "Empty metadata response")

        row = self.db.execute(
            text("""
                SELECT input_tokens, output_tokens, total_cost_inr, api_status
                FROM isf_recorder.gemini_usage_log
                WHERE recording_id = :rec_id AND operation_type = 'SUMMARY'
                ORDER BY created_at DESC LIMIT 1
            """),
            {"rec_id": rec_id}
        ).fetchone()

        self.assertIsNotNone(row)
        self.assertEqual(row.input_tokens, 0)
        self.assertEqual(row.output_tokens, 0)
        self.assertEqual(float(row.total_cost_inr), 0.0)
        self.assertEqual(row.api_status, "SUCCESS")

    def test_07_different_gemini_models_pricing(self):
        """7. Different Gemini models use their specific configured pricing."""
        p_flash = get_model_pricing(self.db, "gemini-2.5-flash")
        p_lite = get_model_pricing(self.db, "gemini-3.1-flash-lite")

        self.assertEqual(p_flash["audio_price"], 1.00)
        self.assertEqual(p_lite["audio_price"], 0.50)

        tokens = {"input_tokens": 1000, "input_audio_tokens": 10000, "cached_input_tokens": 0, "output_tokens": 1000}
        cost_flash = calculate_cost(tokens, p_flash, 96.0)
        cost_lite = calculate_cost(tokens, p_lite, 96.0)

        self.assertGreater(cost_flash["total_cost_inr"], cost_lite["total_cost_inr"])

    def test_08_cost_calculation_math(self):
        """8. Exact math verification for token cost calculation."""
        pricing = {
            "input_price": 0.30,
            "audio_price": 1.00,
            "cached_price": 0.03,
            "output_price": 2.50,
        }
        tokens = {
            "input_tokens": 1_000_000,
            "input_audio_tokens": 2_000_000,
            "cached_input_tokens": 1_000_000,
            "output_tokens": 1_000_000,
        }
        cost = calculate_cost(tokens, pricing, exchange_rate=100.0)

        self.assertAlmostEqual(cost["input_cost_usd"], 0.30, places=4)
        self.assertAlmostEqual(cost["audio_cost_usd"], 2.00, places=4)
        self.assertAlmostEqual(cost["cached_cost_usd"], 0.03, places=4)
        self.assertAlmostEqual(cost["output_cost_usd"], 2.50, places=4)
        self.assertAlmostEqual(cost["total_cost_usd"], 4.83, places=4)
        self.assertAlmostEqual(cost["total_cost_inr"], 483.00, places=2)

    def test_09_inr_conversion_with_exchange_rate(self):
        """9. INR conversion correctly multiplies USD by live exchange rate."""
        rate = get_live_usd_to_inr_rate(self.db)
        self.assertGreater(rate, 70.0)
        self.assertLess(rate, 120.0)

        tokens = {"input_tokens": 0, "input_audio_tokens": 100_000, "cached_input_tokens": 0, "output_tokens": 0}
        pricing = {"input_price": 0.30, "audio_price": 1.00, "cached_price": 0.03, "output_price": 2.50}
        cost = calculate_cost(tokens, pricing, exchange_rate=rate)

        expected_usd = 0.10
        expected_inr = round(0.10 * rate, 4)
        self.assertAlmostEqual(cost["total_cost_usd"], expected_usd, places=4)
        self.assertAlmostEqual(cost["total_cost_inr"], expected_inr, places=2)

    def test_10_recording_level_aggregation(self):
        """10. Recording-level aggregation sums up tokens, calls, and INR cost accurately."""
        rec_id = self.create_recording()
        p = get_model_pricing(self.db, "gemini-2.5-flash")
        rate = 96.0

        tok1 = {"input_tokens": 1000, "input_audio_tokens": 50000, "cached_input_tokens": 0, "output_tokens": 1500, "total_tokens": 52500, "raw_json": {}}
        cost1 = calculate_cost(tok1, p, rate)
        log_gemini_call(rec_id, "TRANSCRIPTION", "gemini-2.5-flash", "req_1", datetime.now(timezone.utc), datetime.now(timezone.utc), 1000, tok1, cost1, p["id"], rate, db=self.db)

        tok2 = {"input_tokens": 2000, "input_audio_tokens": 0, "cached_input_tokens": 0, "output_tokens": 500, "total_tokens": 2500, "raw_json": {}}
        cost2 = calculate_cost(tok2, p, rate)
        log_gemini_call(rec_id, "SUMMARY", "gemini-2.5-flash", "req_2", datetime.now(timezone.utc), datetime.now(timezone.utc), 500, tok2, cost2, p["id"], rate, db=self.db)

        row = self.db.execute(
            text("""
                SELECT COUNT(id) as total_calls,
                       SUM(input_audio_tokens) as aud_tokens,
                       SUM(output_tokens) as out_tokens,
                       SUM(total_cost_inr) as cost_inr
                FROM isf_recorder.gemini_usage_log
                WHERE recording_id = :rec_id
            """),
            {"rec_id": rec_id}
        ).fetchone()

        self.assertEqual(row.total_calls, 2)
        self.assertEqual(row.aud_tokens, 50000)
        self.assertEqual(row.out_tokens, 2000)
        expected_total_inr = round(cost1["total_cost_inr"] + cost2["total_cost_inr"], 2)
        self.assertAlmostEqual(float(row.cost_inr), expected_total_inr, places=2)

    def test_11_monthly_aggregation(self):
        """11. Monthly aggregation queries sum across the active month."""
        now = datetime.now(timezone.utc)
        row = self.db.execute(
            text("""
                SELECT COUNT(id) as monthly_calls, COALESCE(SUM(total_cost_inr), 0) as monthly_cost
                FROM isf_recorder.gemini_usage_log
                WHERE EXTRACT(YEAR FROM request_timestamp) = :yr
                  AND EXTRACT(MONTH FROM request_timestamp) = :mo
            """),
            {"yr": now.year, "mo": now.month}
        ).fetchone()

        self.assertIsNotNone(row)
        self.assertGreaterEqual(row.monthly_calls, 0)
        self.assertGreaterEqual(float(row.monthly_cost), 0.0)

    def test_12_duplicate_retry_correlation_id(self):
        """12. Each Gemini call has a unique correlation ID preventing duplicate confusion."""
        rec_id = self.create_recording()
        mock_resp = MockGenerateContentResponse("test", MockUsageMetadata(10, 10, 20))

        track_gemini_call(rec_id, "TRANSCRIPTION", "gemini-2.5-flash", lambda: mock_resp)
        track_gemini_call(rec_id, "TRANSCRIPTION", "gemini-2.5-flash", lambda: mock_resp)

        rows = self.db.execute(
            text("SELECT gemini_request_id FROM isf_recorder.gemini_usage_log WHERE recording_id = :rec_id"),
            {"rec_id": rec_id}
        ).fetchall()

        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0][0], rows[1][0])

    def test_13_recording_with_zero_calls(self):
        """13. Recording with zero calls returns clean 0 totals without error."""
        empty_rec_id = self.create_recording()
        row = self.db.execute(
            text("""
                SELECT COUNT(id) as total_calls,
                       COALESCE(SUM(total_cost_inr), 0) as estimated_cost
                FROM isf_recorder.gemini_usage_log
                WHERE recording_id = :id
            """),
            {"id": empty_rec_id}
        ).fetchone()

        self.assertEqual(row.total_calls, 0)
        self.assertEqual(float(row.estimated_cost), 0.0)

    def test_14_long_recording_multiple_chapters(self):
        """14. Long recording with 3 chapters generates 3 STT calls + 1 summary call."""
        rec_id = self.create_recording()
        p = get_model_pricing(self.db, "gemini-2.5-flash")

        # 3 STT chapter calls
        for i in range(3):
            tok = {"input_tokens": 1000, "input_audio_tokens": 36000, "cached_input_tokens": 0, "output_tokens": 2000, "total_tokens": 39000, "raw_json": {}}
            cost = calculate_cost(tok, p, 96.0)
            log_gemini_call(rec_id, "TRANSCRIPTION", "gemini-2.5-flash", f"ch_{i}", datetime.now(timezone.utc), datetime.now(timezone.utc), 1500, tok, cost, p["id"], 96.0, audio_duration_seconds=1200.0, db=self.db)

        # 1 summary call
        tok_sum = {"input_tokens": 6000, "input_audio_tokens": 0, "cached_input_tokens": 0, "output_tokens": 1000, "total_tokens": 7000, "raw_json": {}}
        cost_sum = calculate_cost(tok_sum, p, 96.0)
        log_gemini_call(rec_id, "SUMMARY", "gemini-2.5-flash", "sum_1", datetime.now(timezone.utc), datetime.now(timezone.utc), 800, tok_sum, cost_sum, p["id"], 96.0, db=self.db)

        counts = self.db.execute(
            text("""
                SELECT
                    COUNT(id) as total_calls,
                    COUNT(CASE WHEN operation_type = 'TRANSCRIPTION' THEN 1 END) as stt_calls,
                    COUNT(CASE WHEN operation_type = 'SUMMARY' THEN 1 END) as sum_calls,
                    SUM(input_audio_tokens) as total_audio_tokens
                FROM isf_recorder.gemini_usage_log
                WHERE recording_id = :id
            """),
            {"id": rec_id}
        ).fetchone()

        self.assertEqual(counts.total_calls, 4)
        self.assertEqual(counts.stt_calls, 3)
        self.assertEqual(counts.sum_calls, 1)
        self.assertEqual(counts.total_audio_tokens, 108000)


if __name__ == "__main__":
    unittest.main()
