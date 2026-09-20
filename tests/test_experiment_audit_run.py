"""Artifact-only engineering audit: tampering, isolation, and complete denominators."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest

from experiments.audit_run import AGGREGATED, DATA_LINK_SKILL_PATHS, FROZEN_KEYS, audit_run, digest_object
from experiments.prepare_dataset import sha256_file, write_json, write_jsonl


class EngineeringAuditTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.dataset = self.root / "dataset"
        self.run = self.root / "runs/fixture-smoke"
        self.questions = [{"question_id": index, "db_id": "first" if index % 2 else "second",
                           "question": "PRIVATE_QUESTION_BODY", "evidence": "PRIVATE_EVIDENCE"} for index in range(30)]
        write_jsonl(self.dataset / "generation/smoke_questions.jsonl", self.questions)
        write_json(self.dataset / "dataset_manifest.json", {"generation_metadata_only": True})
        self.config = {"model": "deepseek-v4.1-flash", "db_root": str(self.root / "business_databases_never_opened"),
                       "env_file": str(self.root / "secret_env_never_opened"), "max_llm_calls": 40,
                       "question_timeout_seconds": 900, "sql_timeout_seconds": 30, "max_sql_query_calls": 4,
                       "max_transient_retries": 3, "temperature": 0,
                       "index_table": "columns_fixture", "index_version": "fixture-v1"}
        self.runtime = {"version": "3.40.1", "dll_sha256": "frozen-dll-hash", "archive_sha256": "frozen-archive-hash"}
        frozen = {"git_commit": "fixture-commit", "code_sha256": {}, "questions_sha256": sha256_file(self.dataset / "generation/smoke_questions.jsonl"),
                  "subset": "smoke", "dataset_manifest_sha256": sha256_file(self.dataset / "dataset_manifest.json"),
                  "sqlite_runtime": self.runtime, "index_manifest_sha256": "fixture-index-hash",
                  "index_manifest": {"table": self.config["index_table"], "index_version": self.config["index_version"]}, "config": self.config}
        self.manifest = {**frozen, "fingerprint": digest_object(frozen), "run_id": "fixture-smoke", "created_at": 0, "expected_questions": 30}
        write_json(self.run / "run_manifest.json", self.manifest)
        self.predictions = []
        for question in self.questions:
            qid = question["question_id"]
            usage = {"llm_calls": 1, "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
                     "cached_tokens": 0, "reasoning_tokens": 0, "usage_complete": True, "calls_without_usage": 0,
                     "calls": [{"call": 1, "model": "openai/deepseek-v4.1-flash", "status": "completed", "duration_seconds": 0.2,
                                "usage": {"prompt_token_count": 10, "candidates_token_count": 5, "total_token_count": 15}}]}
            events = []
            for index, skill in enumerate(("data-link", "database-query-helper", "correct")):
                events.extend([{"kind": "call", "name": "load_skill", "id": f"skill{index}", "args": {"skill_name": skill}},
                               {"kind": "response", "name": "load_skill", "id": f"skill{index}", "response": {"result": "fixture instructions"}}])
            events.extend([
                {"kind": "call", "name": "sql_db_query", "id": "query", "args": {"query": "SELECT 1"}},
                {"kind": "response", "name": "sql_db_query", "id": "query", "response": {"result": "✅ 查询成功 (1 row)"}},
                {"kind": "call", "name": "submit_final_sql", "id": "submit", "args": {"sql": "SELECT 1"}},
                {"kind": "response", "name": "submit_final_sql", "id": "submit", "response": {"status": "success", "final_sql": "SELECT 1"}},
            ])
            trace = {"tool_trace": events, "sql_execution_trace": [{"sql": "SELECT 1", "source": "sql_db_query", "state": "accepted", "is_probe": False}],
                     "submitted_final_sql": "SELECT 1", "tool_stats": {"session_id": f"session-{qid}"}}
            result = {"question_id": qid, "db_id": question["db_id"], "run_id": "fixture-smoke", "attempt": 1,
                      "session_id": f"session-{qid}", "status": "succeeded", "submitted_final_sql": "SELECT 1", "final_sql": "SELECT 1",
                      "final_sql_source": "submit_final_sql", "trace": trace, "usage": usage, "llm_calls": 1,
                      "prompt_tokens": 10, "completion_tokens": 5, "duration_seconds": 1.0,
                      "sqlite_runtime": self.runtime, "model": self.config["model"], "index_version": self.config["index_version"],
                      "error_category": "", "usage_unknown": False}
            payload = {**question, "run_id": "fixture-smoke", "attempt": 1,
                       "config": {**{k: v for k, v in self.config.items() if k not in {"model", "max_transient_retries"}}, "model_name": self.config["model"]}}
            write_json(self.path(qid, "input"), payload)
            write_json(self.path(qid, "result"), result)
            write_json(self.path(qid, "usage"), {"question_id": qid, "attempt": 1, "session_id": result["session_id"], "usage": usage})
            prediction = {**question, **result, "attempt_count": 1}
            for metric in AGGREGATED:
                value = result.get(metric, usage.get(metric))
                prediction[metric] = value
                prediction[metric + "_known"] = value if value is not None else 0
            self.predictions.append(prediction)
        self.save_predictions()

    def tearDown(self):
        self.temporary.cleanup()

    def path(self, qid, kind):
        return self.run / "traces" / f"{qid}.attempt1.{kind}.json"

    def save_predictions(self):
        write_jsonl(self.run / "predictions.jsonl", self.predictions)

    def mutate_result(self, mutate, *, question_id=0):
        path = self.path(question_id, "result")
        result = json.loads(path.read_text(encoding="utf-8"))
        mutate(result)
        write_json(path, result)
        # Keep the export aligned when deliberately testing a deeper invariant.
        for key in ("status", "error_category", "session_id", "submitted_final_sql", "final_sql", "final_sql_source", "trace", "usage", "sqlite_runtime", "model", "index_version", "metadata"):
            if key in result:
                self.predictions[question_id][key] = copy.deepcopy(result[key])
        self.save_predictions()
        return result

    def execute(self, filename="engineering_audit.json"):
        return audit_run(self.dataset, self.run, subset="smoke", output=self.run / filename)

    @staticmethod
    def codes(report):
        return {row["code"] for row in report["engineering_findings"] + report["unverified_evidence"]}

    def save_manifest_fingerprint(self):
        self.manifest["fingerprint"] = digest_object({key: self.manifest[key] for key in FROZEN_KEYS})
        write_json(self.run / "run_manifest.json", self.manifest)

    def configure_data_link_policy(self, policy="explicit_projection_v1"):
        self.config["data_link_policy"] = policy
        self.manifest["code_sha256"].update({DATA_LINK_SKILL_PATHS["baseline"]: "a" * 64,
                                           DATA_LINK_SKILL_PATHS["explicit_projection_v1"]: "b" * 64})
        self.save_manifest_fingerprint()
        for qid in range(30):
            path = self.path(qid, "input")
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["config"]["data_link_policy"] = policy
            write_json(path, payload)
            result = json.loads(self.path(qid, "result").read_text(encoding="utf-8"))
            result["metadata"] = {"sqlite_runtime": self.runtime, "data_link_policy": policy,
                                  "data_link_skill_sha256": ("a" if policy == "baseline" else "b") * 64}
            write_json(self.path(qid, "result"), result)
            self.predictions[qid]["metadata"] = copy.deepcopy(result["metadata"])
        self.save_predictions()

    def test_complete_artifact_audit_does_not_need_gold_scores_sqlite_or_env(self):
        before = sha256_file(self.run / "predictions.jsonl")
        report = self.execute()
        self.assertTrue(report["passed"])
        self.assertEqual(report["summary"]["terminal_count"], 30)
        self.assertEqual(report["summary"]["unique_sessions_observed"], 30)
        self.assertFalse((self.dataset / "evaluation").exists())
        self.assertFalse((self.run / "scores.jsonl").exists())
        self.assertFalse(Path(self.config["env_file"]).exists())
        self.assertFalse(Path(self.config["db_root"]).exists())
        self.assertEqual(sha256_file(self.run / "predictions.jsonl"), before)
        text = json.dumps(report)
        self.assertNotIn("SELECT 1", text)
        self.assertNotIn("PRIVATE_QUESTION_BODY", text)
        self.assertNotIn("PRIVATE_EVIDENCE", text)

    def test_failed_and_timeout_without_submission_are_legal_terminal_records(self):
        for qid, status in ((0, "failed"), (1, "timeout")):
            def failed(result):
                result.update(status=status, submitted_final_sql="", final_sql="", final_sql_source="", error_category="semantic" if status == "failed" else "timeout")
                result["trace"].update(tool_trace=[], sql_execution_trace=[], submitted_final_sql="")
            self.mutate_result(failed, question_id=qid)
        report = self.execute()
        self.assertTrue(report["passed"])
        self.assertEqual(report["summary"]["status_counts"], {"failed": 1, "timeout": 1, "succeeded": 28})
        self.assertEqual(report["summary"]["submitted_count"], 28)

    def test_missing_prediction_cannot_claim_complete_pass(self):
        self.predictions.pop()
        self.save_predictions()
        report = self.execute()
        self.assertFalse(report["passed"])
        self.assertIn("run_incomplete_or_question_ids_mismatch", self.codes(report))

    def test_missing_worker_artifact_is_detected(self):
        self.path(0, "result").unlink()
        report = self.execute()
        self.assertFalse(report["passed"])
        self.assertIn("attempt_input_or_result_missing", self.codes(report))

    def test_cross_database_input_is_detected(self):
        path = self.path(0, "input")
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["db_id"] = "other_database"
        write_json(path, payload)
        report = self.execute()
        self.assertIn("worker_input_question_or_database_changed", self.codes(report))

    def test_session_reuse_is_detected(self):
        result = self.mutate_result(lambda row: row.update(session_id="session-1"))
        checkpoint = json.loads(self.path(0, "usage").read_text(encoding="utf-8"))
        checkpoint["session_id"] = result["session_id"]
        write_json(self.path(0, "usage"), checkpoint)
        report = self.execute()
        self.assertIn("session_reused_across_workers", self.codes(report))

    def test_raw_submission_tampering_is_detected(self):
        self.predictions[0]["submitted_final_sql"] = " SELECT 1 "
        self.save_predictions()
        report = self.execute()
        self.assertIn("exported_raw_submission_changed", self.codes(report))

    def test_submission_before_accepted_execution_is_detected(self):
        def move_submission_before_query(result):
            events = result["trace"]["tool_trace"]
            result["trace"]["tool_trace"] = events[:6] + events[8:10] + events[6:8]
        self.mutate_result(move_submission_before_query)
        report = self.execute()
        self.assertIn("successful_submission_not_preceded_by_accepted_execution", self.codes(report))

    def test_usage_aggregate_tampering_is_detected(self):
        self.predictions[0]["prompt_tokens"] += 100
        self.save_predictions()
        report = self.execute()
        self.assertIn("exported_attempt_total_mismatch", self.codes(report))

    def test_missing_attempt_duration_cannot_verify_cumulative_time_budget(self):
        result = json.loads(self.path(0, "result").read_text(encoding="utf-8"))
        result.pop("duration_seconds")
        write_json(self.path(0, "result"), result)
        self.predictions[0].update(duration_seconds=None, duration_seconds_known=0)
        self.save_predictions()
        report = self.execute()
        self.assertEqual(report["result"], "incomplete_evidence")
        self.assertIn("worker_duration_unreported", self.codes(report))

    def test_batched_submission_before_query_feedback_is_an_observation(self):
        def overlap(result):
            events = result["trace"]["tool_trace"]
            result["trace"]["tool_trace"] = events[:6] + [events[6], events[8], events[7], events[9]]
        self.mutate_result(overlap)
        report = self.execute()
        self.assertTrue(report["passed"])
        self.assertIn("agent_submission_requested_before_query_feedback", {row["code"] for row in report["observations"]})
        self.assertNotIn("successful_submission_not_preceded_by_accepted_execution", self.codes(report))

    def test_accepted_submission_lost_from_failed_worker_is_detected(self):
        self.mutate_result(lambda row: row.update(status="failed", submitted_final_sql="", final_sql=""))
        report = self.execute()
        self.assertFalse(report["passed"])
        self.assertIn("accepted_submission_lost_from_worker_result", self.codes(report))

    def test_cross_database_linked_schema_is_detected(self):
        def wrong_schema(result):
            result["trace"]["tool_trace"][6:6] = [
                {"kind": "call", "name": "build_linked_mschema", "id": "schema", "args": {}},
                {"kind": "response", "name": "build_linked_mschema", "id": "schema",
                 "response": {"result": "【DB_ID】other_database\n"}},
            ]
        self.mutate_result(wrong_schema)
        report = self.execute()
        self.assertFalse(report["passed"])
        self.assertIn("linked_mschema_database_mismatch", self.codes(report))

    def test_runtime_and_index_changes_are_detected(self):
        self.mutate_result(lambda row: row.update(sqlite_runtime={**row["sqlite_runtime"], "dll_sha256": "different"}, index_version="different"))
        report = self.execute()
        self.assertIn("worker_sqlite_runtime_changed", self.codes(report))
        self.assertIn("worker_reported_identity_changed", self.codes(report))

    def test_gold_field_in_generation_input_is_rejected_without_reading_gold(self):
        path = self.path(0, "input")
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["gold_sql"] = "LEAK_MARKER_NEVER_LOGGED"
        write_json(path, payload)
        report = self.execute()
        self.assertIn("worker_input_contains_answer_or_unexpected_fields", self.codes(report))
        self.assertNotIn("LEAK_MARKER_NEVER_LOGGED", json.dumps(report))

    def test_skill_order_deviation_is_an_observation_not_data_corruption(self):
        def swap_skills(result):
            events = result["trace"]["tool_trace"]
            result["trace"]["tool_trace"] = events[2:4] + events[:2] + events[4:]
        self.mutate_result(swap_skills)
        report = self.execute()
        self.assertTrue(report["passed"])
        self.assertIn("agent_pipeline_skill_order_or_completion_deviation", {row["code"] for row in report["observations"]})

    def test_resource_stop_can_be_resumed_without_being_semantic_resampling(self):
        original = json.loads(self.path(0, "result").read_text(encoding="utf-8"))
        prior = copy.deepcopy(original)
        prior.update(status="failed", submitted_final_sql="", final_sql="", final_sql_source="", error_category="insufficient_balance")
        prior["trace"].update(tool_trace=[], sql_execution_trace=[], submitted_final_sql="")
        write_json(self.path(0, "result"), prior)
        result = copy.deepcopy(original)
        result.update(attempt=2, session_id="session-0-after-resource-resume")
        result["trace"]["tool_stats"]["session_id"] = result["session_id"]
        input_payload = json.loads(self.path(0, "input").read_text(encoding="utf-8"))
        input_payload["attempt"] = 2
        input_payload["config"].update(max_llm_calls=39, question_timeout_seconds=899)
        write_json(self.run / "traces/0.attempt2.input.json", input_payload)
        write_json(self.run / "traces/0.attempt2.result.json", result)
        write_json(self.run / "traces/0.attempt2.usage.json", {"question_id": 0, "attempt": 2, "session_id": result["session_id"], "usage": result["usage"]})
        prediction = {**self.questions[0], **result, "attempt_count": 2}
        for metric in AGGREGATED:
            total = sum(row.get(metric, row.get("usage", {}).get(metric)) for row in (prior, result))
            prediction[metric] = total
            prediction[metric + "_known"] = total
        self.predictions[0] = prediction
        self.save_predictions()
        report = self.execute()
        self.assertTrue(report["passed"], report["engineering_findings"])
        self.assertIn("observed_resume_after_external_stop", {row["code"] for row in report["observations"]})

    def test_incomplete_pending_record_is_rejected(self):
        self.predictions[0]["status"] = "running"
        self.save_predictions()
        report = self.execute()
        self.assertIn("prediction_is_not_terminal", self.codes(report))
        self.assertFalse(report["passed"])

    def test_frozen_manifest_tampering_and_output_overwrite_are_rejected(self):
        self.manifest["config"]["temperature"] = 0.7
        write_json(self.run / "run_manifest.json", self.manifest)
        report = self.execute()
        self.assertIn("frozen_manifest_fingerprint_mismatch", self.codes(report))
        with self.assertRaises(FileExistsError):
            self.execute()

    def test_projection_policy_metadata_is_bound_to_manifest_skill_hash(self):
        self.configure_data_link_policy()
        report = self.execute()
        self.assertTrue(report["passed"], report["engineering_findings"])
        self.assertEqual(report["summary"]["data_link_policy"], "explicit_projection_v1")
        self.assertEqual(report["summary"]["data_link_skill_path"], "skill_variants/projection_roles/data-link/SKILL.md")
        self.assertEqual(report["summary"]["data_link_skill_sha256"], "b" * 64)

    def test_unknown_manifest_policy_is_rejected_even_when_workers_agree(self):
        self.configure_data_link_policy("unknown_variant")
        report = self.execute()
        self.assertFalse(report["passed"])
        self.assertIn("manifest_unknown_data_link_policy", self.codes(report))
        self.assertIn("worker_unknown_data_link_policy", self.codes(report))

    def test_attempt_policy_must_match_the_frozen_manifest(self):
        self.configure_data_link_policy()
        path = self.path(0, "input")
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["config"]["data_link_policy"] = "baseline"
        write_json(path, payload)
        report = self.execute()
        self.assertFalse(report["passed"])
        self.assertTrue(any(row["code"] == "worker_frozen_config_changed" and row.get("field") == "data_link_policy"
                            for row in report["engineering_findings"]))

    def test_policy_and_skill_hash_metadata_tampering_are_detected(self):
        self.configure_data_link_policy()
        self.mutate_result(lambda result: result["metadata"].update(data_link_policy="baseline", data_link_skill_sha256="c" * 64))
        report = self.execute()
        self.assertIn("worker_data_link_policy_metadata_mismatch", self.codes(report))
        self.assertIn("worker_data_link_skill_hash_mismatch", self.codes(report))

    def test_candidate_missing_or_partial_metadata_is_unverified_after_model_calls(self):
        self.configure_data_link_policy()
        self.mutate_result(lambda result: result.update(metadata={"sqlite_runtime": self.runtime}))
        self.mutate_result(lambda result: result["metadata"].pop("data_link_skill_sha256"), question_id=1)
        report = self.execute()
        self.assertEqual(report["result"], "incomplete_evidence")
        self.assertEqual(report["engineering_findings"], [])
        self.assertIn("worker_data_link_metadata_missing_after_model_calls", self.codes(report))
        self.assertIn("worker_data_link_metadata_incomplete", self.codes(report))

    def test_candidate_skill_hash_requires_a_frozen_manifest_entry(self):
        self.configure_data_link_policy()
        self.manifest["code_sha256"].pop(DATA_LINK_SKILL_PATHS["explicit_projection_v1"])
        self.save_manifest_fingerprint()
        report = self.execute()
        self.assertEqual(report["result"], "incomplete_evidence")
        self.assertIn("manifest_data_link_skill_hash_missing", self.codes(report))
        self.assertIn("worker_data_link_skill_hash_not_bound_by_manifest", self.codes(report))

    def test_exported_policy_metadata_must_equal_the_final_worker_record(self):
        self.configure_data_link_policy()
        self.predictions[0]["metadata"]["data_link_skill_sha256"] = "c" * 64
        self.save_predictions()
        report = self.execute()
        self.assertTrue(any(row["code"] == "exported_final_worker_record_changed" and row.get("field") == "metadata"
                            for row in report["engineering_findings"]))

    def test_legacy_baseline_without_policy_fields_still_passes(self):
        for qid in range(30):
            path = self.path(qid, "result")
            result = json.loads(path.read_text(encoding="utf-8"))
            result["metadata"] = {"sqlite_runtime": self.runtime}
            write_json(path, result)
            self.predictions[qid]["metadata"] = copy.deepcopy(result["metadata"])
        self.save_predictions()
        report = self.execute()
        self.assertTrue(report["passed"], report["engineering_findings"])
        self.assertEqual(report["summary"]["data_link_policy"], "baseline")

    def test_baseline_policy_metadata_is_checked_when_present(self):
        self.configure_data_link_policy("baseline")
        self.mutate_result(lambda result: result["metadata"].update(data_link_skill_sha256="b" * 64))
        report = self.execute()
        self.assertIn("worker_data_link_skill_hash_mismatch", self.codes(report))


if __name__ == "__main__":
    unittest.main()
