"""Offline supervisor completion/recovery boundaries; no worker or API runs."""
from contextlib import ExitStack, nullcontext, redirect_stdout
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from experiments import supervisor
from experiments.evaluate import METRIC, OFFICIAL_COMMIT, OFFICIAL_HASHES, aggregate_scores
from experiments.prepare_dataset import sha256_file, write_json, write_jsonl
from experiments.state import State, single_instance


class SupervisorRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.local = self.root / "local"
        self.dataset = self.root / "dataset"
        self.run = self.local / "runs/completed"
        self.args = SimpleNamespace(run_id="completed", dataset_dir=self.dataset,
                                    config=self.root / "does-not-need-current-env.json", subset="smoke")
        self.questions = [{"question_id": qid, "db_id": "fixture", "question": "question", "evidence": ""}
                          for qid in range(30)]
        self.runtime = {"version": "3.40.1", "dll_sha256": "frozen-dll", "archive_sha256": "frozen-archive"}
        write_jsonl(self.dataset / "generation/smoke_questions.jsonl", self.questions)
        write_json(self.dataset / "dataset_manifest.json", {"source_sha256": "source", "selection_manifest_sha256": "selection",
                    "question_ids": list(range(30)), "smoke_question_ids": list(range(30))})
        self.frozen = {"git_commit": "old-generation-commit", "code_sha256": {"agent.py": "old-code"},
                       "index_manifest_sha256": "frozen-index", "sqlite_runtime": self.runtime,
                       "subset": "smoke", "questions_sha256": sha256_file(self.dataset / "generation/smoke_questions.jsonl"),
                       "dataset_manifest_sha256": sha256_file(self.dataset / "dataset_manifest.json"), "config": {}}
        fingerprint = supervisor.hashlib.sha256(json.dumps(self.frozen, sort_keys=True).encode()).hexdigest()
        self.manifest = {**self.frozen, "fingerprint": fingerprint, "run_id": "completed", "expected_questions": 30, "created_at": 1}
        write_json(self.run / "run_manifest.json", self.manifest)
        self.predictions = [{**q, "run_id": "completed", "status": "succeeded", "submitted_final_sql": "SELECT 1",
                             "final_sql_source": "submit_final_sql", "attempt": 1} for q in self.questions]
        self.scores = [{"question_id": q["question_id"], "db_id": q["db_id"], "sql_idx": i, "difficulty": "simple",
                        "status": "scored", "generation_status": "succeeded", "submitted": True,
                        "prediction_executable": True, "execution_seconds": 0.1, "ex": 1}
                       for i, q in enumerate(self.questions)]
        write_jsonl(self.run / "predictions.jsonl", self.predictions)
        self.write_evaluation()
        state = State(self.local / "state.sqlite")
        try:
            state.initialize("completed", "fixture", fingerprint, self.questions)
            for q, result in zip(self.questions, self.predictions):
                state.start("completed", q["question_id"], 900)
                state.finish("completed", q["question_id"], result)
            state.phase("completed", "DIAGNOSE")
        finally:
            state.db.close()
        write_json(self.run / "next_action.json", {"run_id": "completed", "phase": "DIAGNOSE",
                   "next": "review_smoke_then_freeze_B0"})
        (self.run / "diagnosis.md").write_text("already reviewed; preserve this exact content\n", encoding="utf-8")
        write_json(self.local / "p0_selfcheck_sqlite3401/selfcheck.json", {"passed": True, "count": 300,
                   "source_sha256": "source", "selection_manifest_sha256": "selection",
                   "official_evaluator": {"sqlite_runtime": self.runtime}})
        stack = self.enterContext(ExitStack())
        stack.enter_context(patch.object(supervisor, "LOCAL", self.local))
        stack.enter_context(patch.object(supervisor, "ROOT", self.root))
        stack.enter_context(patch.object(supervisor, "keep_system_awake", return_value=nullcontext()))
        stack.enter_context(patch("experiments.sqlite_runtime.runtime_metadata", return_value=self.runtime))
        self.batch = stack.enter_context(patch.object(supervisor, "run_batch", return_value={"phase": "EVALUATE"}))
        self.process = stack.enter_context(patch.object(supervisor.subprocess, "run", return_value=SimpleNamespace(returncode=0)))

    def write_evaluation(self):
        write_jsonl(self.run / "scores.jsonl", self.scores)
        summary = {**aggregate_scores(self.scores), "source_sha256": "source", "selection_manifest_sha256": "selection",
                   "official_evaluator": {"commit": OFFICIAL_COMMIT, "file_hashes": OFFICIAL_HASHES,
                                          "metric": METRIC, "sqlite_runtime": self.runtime},
                   "subset": "smoke", "timeout_seconds": 30, "completed_generation_records": 30, "scoring_denominator": 30,
                   "predictions_sha256": sha256_file(self.run / "predictions.jsonl"),
                   "scores_sha256": sha256_file(self.run / "scores.jsonl")}
        write_json(self.run / "summary.json", summary)

    def invoke(self):
        with redirect_stdout(io.StringIO()):
            return supervisor.supervise(self.args)

    def snapshot(self):
        return {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.run.iterdir() if p.is_file()}

    def mark_evaluation_pending(self):
        (self.run / "next_action.json").unlink()
        state = State(self.local / "state.sqlite")
        try:
            state.phase("completed", "EVALUATE")
        finally:
            state.db.close()

    def test_repeat_completed_resume_never_dispatches_evaluates_or_rewrites(self):
        before = self.snapshot()
        with patch.object(supervisor, "State", side_effect=AssertionError("no writable State on completed path")):
            self.assertEqual(self.invoke(), 0)
            self.assertEqual(self.invoke(), 0)
        self.batch.assert_not_called()
        self.process.assert_not_called()
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.local / "supervisor.json").exists())

    def test_completed_read_only_resume_works_while_another_run_owns_lock(self):
        control = self.local / "supervisor.json"
        write_json(control, {"run_id": "other-active-run", "pid": 123, "stage": "GENERATE"})
        before = control.read_bytes(), control.stat().st_mtime_ns
        with single_instance(self.local / "supervisor.lock"):
            self.assertEqual(self.invoke(), 0)
        self.assertEqual((control.read_bytes(), control.stat().st_mtime_ns), before)
        self.batch.assert_not_called()

    def test_completed_marker_with_missing_summary_fails_closed(self):
        (self.run / "summary.json").unlink()
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "missing its evaluation summary"):
            self.invoke()
        self.assertEqual(self.snapshot(), before)
        self.batch.assert_not_called()
        self.process.assert_not_called()

    def test_score_or_prediction_hash_drift_never_triggers_regeneration(self):
        for filename in ("scores.jsonl", "predictions.jsonl"):
            with self.subTest(filename=filename):
                path = self.run / filename
                original = path.read_bytes()
                path.write_bytes(original + b"\n")
                with self.assertRaisesRegex(ValueError, "artifact hashes"):
                    self.invoke()
                path.write_bytes(original)
        self.batch.assert_not_called()
        self.process.assert_not_called()

    def test_missing_prediction_cannot_hide_behind_full_score_denominator(self):
        write_jsonl(self.run / "predictions.jsonl", self.predictions[:-1])
        self.write_evaluation()
        with self.assertRaisesRegex(ValueError, "complete prediction/score IDs"):
            self.invoke()
        self.batch.assert_not_called()

    def test_duplicate_scored_id_is_rejected_even_with_rebound_hash(self):
        self.scores[0]["question_id"] = 1
        self.write_evaluation()
        with self.assertRaisesRegex(ValueError, "complete prediction/score IDs"):
            self.invoke()

    def test_cross_database_score_is_rejected_even_with_rebound_summary(self):
        self.scores[0]["db_id"] = "other"
        self.write_evaluation()
        with self.assertRaisesRegex(ValueError, "score/prediction metadata"):
            self.invoke()

    def test_summary_arithmetic_drift_is_rejected(self):
        path = self.run / "summary.json"
        summary = json.loads(path.read_text(encoding="utf-8"))
        summary["overall"]["correct"] = 0
        write_json(path, summary)
        with self.assertRaisesRegex(ValueError, "summary arithmetic"):
            self.invoke()

    def test_pending_durable_question_rejects_completed_artifacts(self):
        state = State(self.local / "state.sqlite")
        try:
            with state.db:
                state.db.execute("UPDATE questions SET status='pending' WHERE question_id=0")
        finally:
            state.db.close()
        with self.assertRaisesRegex(ValueError, "durable generation state"):
            self.invoke()

    def test_missing_durable_result_is_not_a_complete_generation(self):
        state = State(self.local / "state.sqlite")
        try:
            with state.db:
                state.db.execute("UPDATE questions SET result_json=NULL WHERE question_id=0")
        finally:
            state.db.close()
        with self.assertRaisesRegex(ValueError, "durable final result"):
            self.invoke()

    def test_failed_completed_check_does_not_replace_another_runs_global_error(self):
        control = self.local / "supervisor.json"
        global_error = self.local / "supervisor_error.json"
        write_json(control, {"run_id": "other-active-run", "pid": 123})
        write_json(global_error, {"run_id": "other-active-run", "error": "preserve"})
        before = control.read_bytes(), global_error.read_bytes()
        (self.run / "summary.json").unlink()
        argv = ['supervisor', '--run-id', self.args.run_id, '--dataset-dir', str(self.dataset), '--subset', 'smoke']
        with patch.object(supervisor.sys, 'argv', argv), self.assertRaises(ValueError):
            supervisor.main()
        self.assertEqual((control.read_bytes(), global_error.read_bytes()), before)
        self.assertTrue((self.run / 'supervisor_error.json').is_file())

    def test_evaluated_run_missing_final_marker_only_finishes_diagnosis(self):
        self.mark_evaluation_pending()
        before = self.snapshot()
        self.assertEqual(self.invoke(), 0)
        self.batch.assert_not_called()
        self.process.assert_not_called()
        after = self.snapshot()
        self.assertTrue(all(after[name] == value for name, value in before.items()))
        self.assertEqual(json.loads((self.run / "next_action.json").read_text())['phase'], 'DIAGNOSE')

    def test_unfinished_evaluation_resumes_and_then_becomes_idempotent(self):
        self.mark_evaluation_pending()
        (self.run / "summary.json").unlink()
        (self.run / "scores.jsonl").unlink()
        (self.run / "scores.jsonl.partial").write_text('partial old evaluation', encoding='utf-8')
        def evaluate(*args, **kwargs):
            self.write_evaluation()
            (self.run / "scores.jsonl.partial").unlink()
            return SimpleNamespace(returncode=0)
        self.process.side_effect = evaluate
        self.assertEqual(self.invoke(), 0)
        self.batch.assert_not_called()
        self.process.assert_called_once()
        before = self.snapshot()
        self.assertEqual(self.invoke(), 0)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.process.call_count, 1)

    def test_new_run_keeps_original_generation_and_resource_stop_path(self):
        self.args.run_id = "new_run"
        self.batch.return_value = {"run_id": "new_run", "phase": "stopped_insufficient_balance"}
        self.assertEqual(self.invoke(), 20)
        self.batch.assert_called_once_with(self.dataset, 'new_run', self.args.config, 'smoke')
        self.process.assert_not_called()
        self.assertEqual(json.loads((self.local / "supervisor.json").read_text())['run_id'], 'new_run')

    def test_incomplete_generation_keeps_original_recovery_path(self):
        self.mark_evaluation_pending()
        (self.run / "summary.json").unlink()
        state = State(self.local / "state.sqlite")
        try:
            with state.db:
                state.db.execute("UPDATE questions SET status='pending',result_json=NULL WHERE question_id=0")
        finally:
            state.db.close()
        self.batch.return_value = {"phase": "waiting_service_recovery"}
        self.process.return_value = SimpleNamespace(returncode=1)
        self.assertEqual(self.invoke(), 10)
        self.batch.assert_called_once()
        self.assertIn('start-services.ps1', str(self.process.call_args.args[0]))


if __name__ == "__main__":
    unittest.main()
