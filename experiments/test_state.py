import json
import os
from pathlib import Path
import tempfile
import unittest

from experiments.state import State, atomic_json, process_identity, single_instance


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = State(self.root / "state.sqlite")
        self.questions = [{"question_id": 7, "db_id": "example", "question": "Count rows", "evidence": ""}]
        self.state.initialize("run", "B0", "version1", self.questions)

    def tearDown(self):
        self.state.db.close()
        self.temp.cleanup()

    def test_completed_question_cannot_restart(self):
        self.state.start("run", 7, 900)
        self.state.finish("run", 7, {"status": "succeeded", "submitted_final_sql": "SELECT 2", "llm_calls": 2})
        self.state.initialize("run", "B0", "version1", self.questions)
        with self.assertRaises(ValueError):
            self.state.start("run", 7, 900)
        self.assertEqual(len(self.state.export("run", self.questions, self.root / "predictions.jsonl")), 1)

    def test_changed_code_rejected(self):
        with self.assertRaises(ValueError):
            self.state.initialize("run", "B0", "version2", self.questions)

    def test_orphan_output_recovers_without_rebilling(self):
        self.state.start("run", 7, 900)
        atomic_json(self.root / "traces" / "7.attempt1.result.json", {"status": "succeeded", "submitted_final_sql": "SELECT 2"})
        self.state.recover("run", self.root)
        self.assertEqual(self.state.rows("run")[0]["status"], "succeeded")
        self.assertEqual(self.state.rows("run")[0]["attempt"], 1)

    def test_alive_worker_blocks_recovery(self):
        self.state.start("run", 7, 900)
        self.state.attach_worker("run", 7, os.getpid())
        with self.assertRaises(RuntimeError):
            self.state.recover("run", self.root)
        self.assertIsNotNone(process_identity(os.getpid()))

    def test_lost_worker_records_unknown_cost(self):
        self.state.start("run", 7, 900)
        self.state.recover("run", self.root)
        self.assertEqual(self.state.rows("run")[0]["status"], "pending")
        self.state.start("run", 7, 900)
        self.state.finish("run", 7, {"status": "failed", "submitted_final_sql": "", "llm_calls": 2})
        rows = self.state.export("run", self.questions, self.root / "predictions.jsonl")
        self.assertEqual(rows[0]["attempt_count"], 2)
        self.assertTrue(rows[0]["usage_unknown"])
        self.assertIsNone(rows[0]["llm_calls"])
        self.assertEqual(rows[0]["llm_calls_known"], 2)

    def test_structured_quota_error_is_durable(self):
        self.state.phase("run", "stopped_insufficient_balance", {"code": "insufficient_quota", "message": "No credits"})
        row = self.state.db.execute("SELECT * FROM runs WHERE run_id='run'").fetchone()
        self.assertEqual(row["phase"], "stopped_insufficient_balance")
        self.assertEqual(json.loads(row["reason"])["code"], "insufficient_quota")

    def test_retry_costs_are_accumulated(self):
        for calls, pending in ((3, True), (5, False)):
            self.state.start("run", 7, 900)
            self.state.finish("run", 7, {"status": "failed", "submitted_final_sql": "", "llm_calls": calls}, pending=pending)
        self.assertEqual(self.state.export("run", self.questions, self.root / "predictions.jsonl")[0]["llm_calls"], 8)

    def test_partly_reported_failed_attempt_keeps_retry_token_totals_unknown(self):
        # Actual worker shape: token fields are reported subtotals even though
        # another provider request failed, with no top-level usage_unknown flag.
        reported = {"prompt_token_count": 10, "candidates_token_count": 5, "total_token_count": 15}
        usage = {"llm_calls": 2, "prompt_tokens": 10, "completion_tokens": 5,
                 "total_tokens": 15, "cached_tokens": 0, "reasoning_tokens": 0,
                 "usage_complete": False, "calls_without_usage": 1,
                 "calls": [{"usage": reported}, {"usage": None}]}
        failed = {"status": "failed", "submitted_final_sql": "", "error_category": "transient_api",
                  "llm_calls": 2, "prompt_tokens": 10, "completion_tokens": 5,
                  "duration_seconds": 2.0, "usage": usage}
        self.state.start("run", 7, 900)
        self.state.finish("run", 7, failed, pending=True)
        success = {"status": "succeeded", "submitted_final_sql": "SELECT 2", "llm_calls": 1,
                   "prompt_tokens": 10, "completion_tokens": 5, "duration_seconds": 1.0,
                   "usage": {**usage, "llm_calls": 1, "usage_complete": True,
                             "calls_without_usage": 0, "calls": [{"usage": reported}]}}
        self.state.start("run", 7, 900)
        self.state.finish("run", 7, success)
        result, = self.state.export("run", self.questions, self.root / "predictions.jsonl")
        self.assertEqual(result["attempt_count"], 2)
        self.assertTrue(result["usage_unknown"])
        self.assertTrue(result["usage"]["usage_complete"])  # Last attempt alone.
        for metric, known in (("prompt_tokens", 20), ("completion_tokens", 10),
                              ("total_tokens", 30), ("cached_tokens", 0), ("reasoning_tokens", 0)):
            self.assertIsNone(result[metric], metric)
            self.assertEqual(result[metric + "_known"], known, metric)
        self.assertEqual(result["llm_calls"], 3)
        self.assertEqual(result["duration_seconds"], 3.0)
        original = json.loads(self.state.db.execute("SELECT result_json FROM attempts WHERE attempt=1").fetchone()[0])
        self.assertEqual(original, failed)  # Export never rewrites attempt evidence.

    def test_explicit_unknown_flag_overrides_nonnull_token_subtotals_only(self):
        self.state.start("run", 7, 900)
        self.state.finish("run", 7, {"status": "failed", "submitted_final_sql": "", "usage_unknown": True,
                                    "llm_calls": 2, "duration_seconds": 4.0, "prompt_tokens": 12,
                                    "usage": {"usage_complete": True}})
        result, = self.state.export("run", self.questions, self.root / "predictions.jsonl")
        self.assertTrue(result["usage_unknown"])
        self.assertIsNone(result["prompt_tokens"])
        self.assertEqual(result["prompt_tokens_known"], 12)
        self.assertEqual(result["llm_calls"], 2)
        self.assertEqual(result["duration_seconds"], 4.0)

    def test_interrupted_worker_restores_pre_request_call_checkpoint(self):
        self.state.start("run", 7, 900)
        atomic_json(self.root / "traces" / "7.attempt1.usage.json", {
            "question_id": 7, "usage": {"llm_calls": 6, "prompt_tokens": 123,
                                       "completion_tokens": 9, "usage_complete": False}})
        self.state.recover("run", self.root)
        attempt = json.loads(self.state.db.execute("SELECT result_json FROM attempts").fetchone()[0])
        self.assertEqual(attempt["llm_calls"], 6)
        self.assertEqual(attempt["reserved_llm_calls"], 0)
        self.assertTrue(attempt["usage_unknown"])

    def test_os_lock_prevents_second_owner(self):
        with single_instance(self.root / "owner.lock"):
            with self.assertRaises(OSError):
                with single_instance(self.root / "owner.lock"):
                    self.fail("Second owner entered")
        with single_instance(self.root / "owner.lock"):
            pass


if __name__ == "__main__":
    unittest.main()
