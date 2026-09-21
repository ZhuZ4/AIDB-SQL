"""Offline export boundaries; synthetic artifacts only, no Gold/SQL/API access."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from experiments import audit_run as engineering
from experiments.evaluate import METRIC, OFFICIAL_COMMIT, OFFICIAL_HASHES, aggregate_scores
from experiments.export_results import export_results
from experiments.prepare_dataset import DEFAULT_MANIFEST, SOURCE_SHA256, sha256_file
from experiments.sqlite_runtime import DLL_SHA256, VERSION


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


class ExportResultsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.dataset = self.root / "dataset"
        self.run = self.root / "runs/fixture-full"
        self.output = self.root / "deliverables/new"
        self.ids = json.loads(DEFAULT_MANIFEST.read_text(encoding="utf-8"))["question_ids"]
        self.sql = " \nSELECT '中文 😀' AS \"带 空格\";\n "
        self.questions = [{"question_id": qid, "db_id": "first" if i % 2 else "second",
                           "question": f"Question {i} 中文", "evidence": "Evidence only"}
                          for i, qid in enumerate(self.ids)]
        write_jsonl(self.dataset / "generation/questions.jsonl", self.questions)
        self.gold = self.dataset / "evaluation/gold.sql"
        self.gold.parent.mkdir(parents=True)
        self.gold.write_text("FORBIDDEN_SYNTHETIC_GOLD_SENTINEL", encoding="utf-8")
        self.metadata = {"source_sha256": SOURCE_SHA256, "source_path": str(self.gold),
                         "question_ids": self.ids, "count": 300,
                         "selection_manifest_sha256": sha256_file(DEFAULT_MANIFEST),
                         "file_hashes": {"generation/questions.jsonl": sha256_file(self.dataset / "generation/questions.jsonl"),
                                         "evaluation/gold.sql": sha256_file(self.gold)}}
        write_json(self.dataset / "dataset_manifest.json", self.metadata)
        runtime = {"version": VERSION, "dll_sha256": DLL_SHA256}
        frozen = {"git_commit": "a" * 40, "code_sha256": {"agent.py": "b" * 64},
                  "questions_sha256": sha256_file(self.dataset / "generation/questions.jsonl"),
                  "dataset_manifest_sha256": sha256_file(self.dataset / "dataset_manifest.json"),
                  "index_manifest_sha256": "c" * 64, "index_manifest": {"table": "columns_fixture"},
                  "subset": "all", "sqlite_runtime": runtime,
                  "config": {"model": "deepseek-flash", "max_llm_calls": 40,
                             "question_timeout_seconds": 900, "sql_timeout_seconds": 30, "temperature": 0,
                             "repetition_count": 2, "candidate_cost_ratio_limit": None}}
        self.manifest = {**frozen, "fingerprint": engineering.digest_object(frozen), "run_id": "fixture-full",
                         "created_at": 1, "expected_questions": 300}
        write_json(self.run / "run_manifest.json", self.manifest)
        self.predictions, self.scores = [], []
        for index, question in enumerate(self.questions):
            qid = question["question_id"]
            status = "succeeded" if index < 298 else ("failed" if index == 298 else "timeout")
            sql = self.sql if status == "succeeded" else ""
            events = [] if not sql else [
                {"kind": "call", "name": "sql_db_query", "id": "q", "args": {"query": sql}},
                {"kind": "response", "name": "sql_db_query", "id": "q", "response": {"state": "accepted"}},
                {"kind": "call", "name": "submit_final_sql", "id": "s", "args": {"sql": sql}},
                {"kind": "response", "name": "submit_final_sql", "id": "s", "response": {"status": "success", "final_sql": sql}}]
            trace = {"submitted_final_sql": sql, "generated_sql": "SELECT 'never export this fallback'",
                     "tool_trace": events, "sql_execution_trace": [] if not sql else [
                         {"sql": sql, "source": "sql_db_query", "state": "accepted", "is_probe": False, "row_count": 1}]}
            result = {"question_id": qid, "db_id": question["db_id"], "run_id": "fixture-full", "attempt": 1,
                      "session_id": f"session-{qid}", "status": status, "submitted_final_sql": sql,
                      "final_sql": sql, "final_sql_source": "submit_final_sql" if sql else "",
                      "error_category": "" if sql else ("semantic" if status == "failed" else "timeout"), "trace": trace}
            prediction = {**question, **result, "attempt_count": 1}
            self.predictions.append(prediction)
            self.scores.append({"question_id": qid, "db_id": question["db_id"], "sql_idx": index,
                                "difficulty": "simple" if index % 2 else "moderate", "ex": int(index < 200),
                                "status": "scored" if sql else "missing_sql", "submitted": bool(sql),
                                "generation_status": status, "prediction_executable": bool(sql), "execution_seconds": 0.1})
            write_json(self.run / "traces" / f"{qid}.attempt1.input.json", {**question, "run_id": "fixture-full", "attempt": 1, "config": {}})
            write_json(self.run / "traces" / f"{qid}.attempt1.result.json", result)
            # Real audits allow absent usage checkpoint files; full usage checks
            # belong to audit_run, whose passing hash-bound report is consumed.
        self.summary = {**aggregate_scores(self.scores), "subset": "all", "completed_generation_records": 300,
                        "scoring_denominator": 300, "source_sha256": SOURCE_SHA256,
                        "selection_manifest_sha256": sha256_file(DEFAULT_MANIFEST), "timeout_seconds": 30,
                        "official_evaluator": {"commit": OFFICIAL_COMMIT, "file_hashes": OFFICIAL_HASHES,
                                               "metric": METRIC, "sqlite_runtime": runtime}}
        self.audit = {"schema": engineering.SCHEMA, "run_id": "fixture-full", "subset": "all",
                      "passed": True, "result": "passed", "engineering_findings": [], "unverified_evidence": [],
                      "audit_code_sha256": sha256_file(Path(engineering.__file__)), "observations": [],
                      "summary": {"expected_count": 300, "record_count": 300, "terminal_count": 300,
                                  "engineering_error_count": 0, "unverified_evidence_count": 0,
                                  "status_counts": {"succeeded": 298, "failed": 1, "timeout": 1}, "submitted_count": 298},
                      "questions": [{"question_id": p["question_id"], "db_id": p["db_id"], "status": p["status"],
                                     "attempt_count": 1, "submitted": bool(p["submitted_final_sql"])} for p in self.predictions]}
        self.refresh()

    def refresh(self):
        write_jsonl(self.run / "predictions.jsonl", self.predictions)
        write_jsonl(self.run / "scores.jsonl", self.scores)
        self.summary.update(predictions_sha256=sha256_file(self.run / "predictions.jsonl"),
                            scores_sha256=sha256_file(self.run / "scores.jsonl"))
        write_json(self.run / "summary.json", self.summary)
        paths = [self.run / "run_manifest.json", self.run / "predictions.jsonl",
                 self.dataset / "dataset_manifest.json", self.dataset / "generation/questions.jsonl"]
        paths += list((self.run / "traces").glob("*.json"))
        self.audit["source_artifact_sha256"] = {str(p.resolve()): sha256_file(p) for p in paths}
        write_json(self.run / "engineering_audit.json", self.audit)

    def export(self, output=None):
        return export_results(self.run, output or self.output, dataset_dir=self.dataset)

    def source_snapshot(self):
        return {str(p): (sha256_file(p), p.stat().st_mtime_ns) for folder in (self.run, self.dataset)
                for p in folder.rglob("*") if p.is_file()}

    def rejected(self, message, output=None):
        before = self.source_snapshot()
        with self.assertRaisesRegex(ValueError, message):
            self.export(output)
        self.assertEqual(before, self.source_snapshot())
        if output is None:
            self.assertFalse(self.output.exists())

    def directory_alias(self, alias, target):
        try:
            alias.symlink_to(target, target_is_directory=True)
        except OSError:
            if os.name != "nt":
                self.skipTest("Directory aliases unavailable on this host")
            # Junction creation needs no symlink privilege on Windows. Both
            # paths are fixed descendants of this test's temporary directory.
            result = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(alias), str(target)],
                                    capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
            if result.returncode:
                self.skipTest("Directory junction creation unavailable")
        self.addCleanup(lambda: alias.rmdir() if os.path.lexists(alias) and not alias.is_symlink()
                        else alias.unlink() if alias.is_symlink() else None)

    def test_complete_export_preserves_raw_unicode_sql_and_failures_without_gold(self):
        before = self.source_snapshot()
        original_open = Path.open

        def no_gold(path, *args, **kwargs):
            if path.resolve().is_relative_to(self.dataset / "evaluation"):
                raise AssertionError("Gold must never be opened")
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", no_gold), patch("sqlite3.connect", side_effect=AssertionError("No state or DB")):
            manifest = self.export()
        self.assertEqual(before, self.source_snapshot())
        rows = [json.loads(line) for line in (self.output / "question_sql_scores.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([r["question_id"] for r in rows], self.ids)
        self.assertEqual(rows[0]["submitted_final_sql"], self.sql)
        self.assertEqual([r["status"] for r in rows[-2:]], ["failed", "timeout"])
        self.assertTrue(all(r["submitted_final_sql"] == "" and r["official_ex"] == 0 for r in rows[-2:]))
        self.assertNotIn("generated_sql", rows[0])
        self.assertNotIn("trace", rows[0])
        self.assertEqual(manifest["record_count"], 300)
        self.assertEqual(manifest["files"]["question_sql_scores.jsonl"]["sha256"], sha256_file(self.output / "question_sql_scores.jsonl"))

    def test_existing_output_including_empty_directory_and_source_overlap_rejected(self):
        for output in (self.run / "new", self.dataset / "new", self.run, self.dataset):
            with self.subTest(output=output):
                self.rejected("already exists|outside|overlaps", output)
        empty = self.root / "empty"
        empty.mkdir()
        self.rejected("already exists", empty)
        self.assertEqual(list(empty.iterdir()), [])
        existing = self.root / "existing-file"
        existing.write_text("KEEP", encoding="utf-8")
        self.rejected("already exists", existing)
        self.assertEqual(existing.read_text(), "KEEP")

    def test_output_symlink_alias_into_source_rejected(self):
        alias = self.root / "source-alias"
        self.directory_alias(alias, self.run)
        self.rejected("outside", alias / "new-output")

    def test_missing_reordered_duplicate_and_unfinished_predictions_rejected(self):
        original = copy.deepcopy(self.predictions)
        variants = [original[:-1], [original[1], original[0], *original[2:]], [*original[:-1], original[0]]]
        pending = copy.deepcopy(original)
        pending[0]["status"] = "pending"
        variants.append(pending)
        for variant in variants:
            with self.subTest(rows=len(variant), status=variant[0]["status"]):
                self.predictions = variant
                self.refresh()
                self.rejected("300 IDs/order|pending prediction")

    def test_question_evidence_database_and_mixed_run_tampering_rejected_even_with_new_hashes(self):
        original = copy.deepcopy(self.predictions)
        for key, value in (("question", "changed"), ("evidence", "changed"), ("db_id", "other"), ("run_id", "another-run")):
            with self.subTest(field=key):
                self.predictions = copy.deepcopy(original)
                self.predictions[0][key] = value
                self.refresh()
                self.rejected("identity changed|Mixed-run|cross-database")

    def test_score_summary_and_frozen_manifest_tampering_rejected(self):
        score_path = self.run / "scores.jsonl"
        score_path.write_bytes(score_path.read_bytes() + b"\n")
        self.rejected("does not bind")
        self.refresh()
        self.summary["by_db_id"]["first"]["correct"] += 1
        write_json(self.run / "summary.json", self.summary)
        self.rejected("totals or groups")
        self.summary.update(aggregate_scores(self.scores))
        self.manifest["git_commit"] = "b" * 40
        write_json(self.run / "run_manifest.json", self.manifest)
        self.refresh()
        self.rejected("fingerprint")

    def test_smoke_manifest_rejected_before_output_creation(self):
        self.manifest.update(subset="smoke", expected_questions=30)
        write_json(self.run / "run_manifest.json", self.manifest)
        self.rejected("smoke is unsupported")

    def test_failed_unverified_or_old_validator_audit_rejected(self):
        original = copy.deepcopy(self.audit)
        for key, value in (("passed", False), ("unverified_evidence", [{"code": "missing"}]),
                           ("audit_code_sha256", "f" * 64)):
            with self.subTest(field=key):
                self.audit = copy.deepcopy(original)
                self.audit[key] = value
                write_json(self.run / "engineering_audit.json", self.audit)
                self.rejected("passing|unverified|different audit code")

    def test_audit_hash_tampering_missing_binding_and_new_trace_rejected(self):
        result_path = self.run / "traces" / f"{self.ids[0]}.attempt1.result.json"
        original = result_path.read_bytes()
        result_path.write_bytes(original + b"\n")
        self.rejected("source hash mismatch")
        result_path.write_bytes(original)
        saved = self.audit["source_artifact_sha256"].pop(str(self.dataset / "generation/questions.jsonl"))
        write_json(self.run / "engineering_audit.json", self.audit)
        self.rejected("bindings are missing")
        self.audit["source_artifact_sha256"][str(self.dataset / "generation/questions.jsonl")] = saved
        write_json(self.run / "engineering_audit.json", self.audit)
        write_json(self.run / "traces" / f"{self.ids[0]}.attempt1.usage.json", {"late": True})
        self.rejected("stale worker artifacts")

    def test_audit_gold_target_is_rejected_before_open(self):
        self.audit["source_artifact_sha256"][str(self.gold)] = sha256_file(self.gold)
        write_json(self.run / "engineering_audit.json", self.audit)
        original_open = Path.open

        def no_gold(path, *args, **kwargs):
            if path.resolve() == self.gold.resolve():
                raise AssertionError("Untrusted audit path opened Gold")
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", no_gold):
            with self.assertRaisesRegex(ValueError, "outside the generation allowlist"):
                self.export()
        self.assertFalse(self.output.exists())

    def test_submission_cannot_be_replaced_or_invented_by_refreshing_hashes(self):
        self.predictions[0]["submitted_final_sql"] = "SELECT 'replacement'"
        self.predictions[0]["final_sql"] = "SELECT 'replacement'"
        self.refresh()
        self.rejected("original terminal worker result")
        result_path = self.run / "traces" / f"{self.ids[0]}.attempt1.result.json"
        result = json.loads(result_path.read_text(encoding="utf-8"))
        result["submitted_final_sql"] = self.predictions[0]["submitted_final_sql"]
        result["final_sql"] = self.predictions[0]["final_sql"]
        self.predictions[0]["trace"] = result["trace"]
        write_json(result_path, result)
        self.refresh()
        self.rejected("Original tool submission")

    def test_generation_directory_alias_to_gold_rejected_before_read(self):
        generation = self.dataset / "generation/questions.jsonl"
        generation.unlink()
        generation.parent.rmdir()
        (self.gold.parent / "questions.jsonl").write_text("FORBIDDEN_GOLD_SENTINEL", encoding="utf-8")
        self.directory_alias(generation.parent, self.gold.parent)
        original_open = Path.open

        def no_gold(path, *args, **kwargs):
            if path.resolve().is_relative_to(self.gold.parent):
                raise AssertionError("Generation alias opened Gold")
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", no_gold):
            with self.assertRaisesRegex(ValueError, "aliases are not allowed"):
                self.export()
        self.assertFalse(self.output.exists())

    def test_valid_retry_with_partly_unknown_usage_remains_exportable(self):
        qid = self.ids[0]
        first_path = self.run / "traces" / f"{qid}.attempt1.result.json"
        successful = json.loads(first_path.read_text(encoding="utf-8"))
        successful.update(attempt=2, session_id="retry-success", llm_calls=2,
                          usage={"llm_calls": 2, "prompt_tokens": 20, "completion_tokens": 5,
                                 "total_tokens": 25, "usage_complete": True, "calls_without_usage": 0})
        first = {**successful, "attempt": 1, "session_id": "retry-failure", "status": "failed",
                 "submitted_final_sql": "", "final_sql": "", "final_sql_source": "",
                 "error_category": "transient_api", "trace": {}, "llm_calls": 2,
                 "usage": {"llm_calls": 2, "prompt_tokens": 10, "completion_tokens": 5,
                           "total_tokens": 15, "usage_complete": False, "calls_without_usage": 1}}
        write_json(first_path, first)
        write_json(self.run / "traces" / f"{qid}.attempt2.result.json", successful)
        write_json(self.run / "traces" / f"{qid}.attempt2.input.json",
                   {**self.questions[0], "run_id": "fixture-full", "attempt": 2, "config": {}})
        self.predictions[0].update(successful)
        self.predictions[0].update(attempt_count=2, llm_calls=4, usage_unknown=True,
                                   prompt_tokens=None, completion_tokens=None, total_tokens=None,
                                   prompt_tokens_known=30, completion_tokens_known=10, total_tokens_known=40)
        self.audit["questions"][0]["attempt_count"] = 2
        self.audit["summary"]["all_recorded_usage_complete"] = False
        self.refresh()
        self.export()
        rows = [json.loads(line) for line in (self.output / "question_sql_scores.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(rows[0]["attempt_count"], 2)
        self.assertEqual(rows[0]["submitted_final_sql"], self.sql)
        self.assertNotIn("total_tokens", rows[0])

    def test_audited_synthetic_budget_terminal_does_not_require_a_worker_result(self):
        qid = self.ids[-1]
        prior_path = self.run / "traces" / f"{qid}.attempt1.result.json"
        prior = json.loads(prior_path.read_text(encoding="utf-8"))
        prior.update(status="failed", error_category="interrupted", llm_calls=40, duration_seconds=900)
        write_json(prior_path, prior)
        self.predictions[-1].update(attempt=2, attempt_count=2, error_category="timeout")
        self.audit["questions"][-1]["attempt_count"] = 2
        self.audit["observations"] = [{"code": "scheduler_budget_terminal_without_worker_dispatch",
                                       "question_id": qid, "attempt": 2}]
        self.refresh()
        self.export()
        rows = [json.loads(line) for line in (self.output / "question_sql_scores.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual((rows[-1]["status"], rows[-1]["submitted_final_sql"], rows[-1]["official_ex"]), ("timeout", "", 0))


if __name__ == "__main__":
    unittest.main()
