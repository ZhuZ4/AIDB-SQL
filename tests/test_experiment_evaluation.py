"""Regression checks for evaluation integrity, isolation, and paired statistics."""
from __future__ import annotations

import copy
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from experiments.compare import bootstrap_interval, compare_runs, mcnemar_exact
from experiments.evaluate import (
    DEFAULT_OFFICIAL_DIR, MISSING_SQL, aggregate_scores, align_predictions,
    evaluate_pair, execute_pair, final_sql, load_official_calculator, readonly_connection,
)
from experiments.prepare_dataset import (
    DEFAULT_MANIFEST, DEFAULT_OUTPUT, GENERATION_FIELDS, read_jsonl,
    select_ids, validate_source, verify_dataset,
)
from experiments.sqlite_runtime import DLL_PATH


class ScoringIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.records = [{"question_id": index, "db_id": "test", "difficulty": "simple"} for index in range(300)]

    def test_missing_failed_and_timeout_records_are_preserved(self):
        predictions = [
            {"question_id": 0, "status": "succeeded", "submitted_final_sql": "SELECT 1"},
            {"question_id": 1, "status": "timeout", "submitted_final_sql": ""},
            {"question_id": 2, "status": "failed", "generated_sql": "SELECT 1"},
        ]
        aligned = align_predictions(predictions, self.records)
        self.assertEqual(len(aligned), 300)
        self.assertEqual(final_sql(aligned[2]), "")
        self.assertEqual(aligned[299]["status"], "missing_prediction")
        scores = [{**record, "ex": int(record["question_id"] == 0), "status": "scored" if record["question_id"] == 0 else "missing_sql"} for record in self.records]
        summary = aggregate_scores(scores)
        self.assertEqual(summary["overall"]["count"], 300)
        self.assertEqual(summary["overall"]["ex"], 1 / 300)

    def test_raw_submission_is_authoritative_and_unmodified(self):
        raw = "  SELECT 1;\n-- final submission\n"
        self.assertEqual(final_sql({"submitted_final_sql": raw, "final_sql": "SELECT 2"}), raw)
        self.assertEqual(final_sql({"submitted_final_sql": "", "final_sql": "SELECT 2", "generated_sql": "SELECT 3"}), "")
        self.assertEqual(final_sql({"generated_sql": "SELECT 3"}), "")

    def test_duplicate_unexpected_and_cross_database_predictions_fail(self):
        for predictions in (
            [{"question_id": 0}, {"question_id": 0}],
            [{"question_id": 300}],
            [{"question_id": 0, "db_id": "other"}],
        ):
            with self.subTest(predictions=predictions), self.assertRaises(ValueError):
                align_predictions(predictions, self.records)


class ReadOnlyExecutionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database = Path(self.directory.name) / "test.sqlite"
        with sqlite3.connect(self.database) as connection:
            connection.execute("CREATE TABLE items (value INTEGER)")
            connection.executemany("INSERT INTO items VALUES (?)", [(1,), (1,), (2,)])
        connection.close()

    def tearDown(self):
        self.directory.cleanup()

    def test_connection_denies_mutation_pragma_and_attach(self):
        with readonly_connection(self.database) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM items").fetchone()[0], 3)
            for sql in ("DELETE FROM items", "UPDATE items SET value=99", "DROP TABLE items", "PRAGMA query_only=OFF", "ATTACH DATABASE ':memory:' AS other"):
                with self.subTest(sql=sql), self.assertRaises(sqlite3.DatabaseError):
                    connection.execute(sql)
        connection.close()
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(connection.execute("SELECT SUM(value) FROM items").fetchone()[0], 4)
        connection.close()

    @unittest.skipUnless(DEFAULT_OFFICIAL_DIR.exists(), "Pinned local official evaluator unavailable")
    def test_official_sets_ignore_duplicate_count_and_order(self):
        result = execute_pair("SELECT DISTINCT value FROM items ORDER BY value DESC", "SELECT value FROM items", self.database)
        self.assertEqual(result["ex"], 1)
        self.assertFalse(result["ordered_equal"])
        self.assertFalse(result["multiset_equal"])

    @unittest.skipUnless(DEFAULT_OFFICIAL_DIR.exists(), "Pinned local official evaluator unavailable")
    def test_missing_placeholder_cannot_pass_empty_gold(self):
        result = execute_pair(MISSING_SQL, "SELECT value FROM items WHERE 0", self.database)
        self.assertEqual(result["ex"], 0)
        self.assertEqual(result["status"], "sql_error")
        self.assertFalse(result["prediction_executable"])

    @unittest.skipUnless(DEFAULT_OFFICIAL_DIR.exists(), "Pinned local official evaluator unavailable")
    def test_windows_spawn_and_bounded_sql_timeout(self):
        result = evaluate_pair("WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r) SELECT sum(n) FROM r", "SELECT 1", self.database, timeout_seconds=0.05)
        self.assertEqual(result["ex"], 0)
        self.assertEqual(result["status"], "timeout")


class PairedComparisonTests(unittest.TestCase):
    def setUp(self):
        self.baseline = [{"question_id": index, "db_id": "a" if index % 2 else "b", "difficulty": "simple", "ex": int(index < 100), "status": "scored"} for index in range(300)]

    def test_exact_flips_and_full_denominator(self):
        candidate = copy.deepcopy(self.baseline)
        candidate[0]["ex"] = 0
        for index in range(100, 108):
            candidate[index]["ex"] = 1
        result = compare_runs([self.baseline], [candidate], bootstrap_samples=200, checks_passed=True)
        self.assertEqual((result["wrong_to_right"], result["right_to_wrong"], result["net_correct"]), (8, 1, 7))
        self.assertAlmostEqual(result["delta_ex_percentage_points"], 7 / 3)
        self.assertEqual(result["adoption_recommendation"], "provisional_requires_matched_repeats")
        self.assertEqual(sum(group["net_correct"] for group in result["by_db_id"].values()), 7)

    def test_missing_score_ids_and_unmatched_repeats_are_rejected(self):
        with self.assertRaises(ValueError):
            compare_runs([self.baseline], [self.baseline[:-1]], bootstrap_samples=100)
        with self.assertRaises(ValueError):
            compare_runs([self.baseline, self.baseline], [self.baseline], bootstrap_samples=100)
        wrong_db = copy.deepcopy(self.baseline)
        wrong_db[0]["db_id"] = "different"
        with self.assertRaises(ValueError):
            compare_runs([self.baseline], [wrong_db], bootstrap_samples=100)

    def test_repeat_mean_does_not_pick_the_best_run(self):
        better, worse = copy.deepcopy(self.baseline), copy.deepcopy(self.baseline)
        for index in range(100, 110):
            better[index]["ex"] = 1
        for index in range(10):
            worse[index]["ex"] = 0
        result = compare_runs([self.baseline, self.baseline], [better, worse], bootstrap_samples=100, checks_passed=True)
        self.assertEqual(result["mean_net_correct"], 0)
        self.assertEqual(result["adoption_recommendation"], "reject_or_diagnose")

    def test_unknown_costs_do_not_satisfy_an_explicit_ceiling(self):
        candidate = copy.deepcopy(self.baseline)
        candidate[100]["ex"] = 1
        result = compare_runs([self.baseline], [candidate], bootstrap_samples=100, checks_passed=True, max_cost_ratio=2)
        self.assertFalse(result["cost_check_passed"])
        self.assertIsNone(result["cost"]["candidate"]["cost_usd"]["total"])

    def test_mcnemar_and_bootstrap_known_cases(self):
        self.assertEqual(mcnemar_exact(0, 0), 1)
        self.assertEqual(mcnemar_exact(6, 0), 0.03125)
        self.assertEqual(bootstrap_interval([0] * 300, iterations=100), [0, 0])


@unittest.skipUnless(sys.platform == "win32" and DLL_PATH.exists(), "Pinned Windows runtime unavailable")
class RuntimeIsolationTests(unittest.TestCase):
    def test_metadata_does_not_import_or_load_sqlite(self):
        code = "from experiments.sqlite_runtime import runtime_metadata; import sys; runtime_metadata(); print('sqlite3' in sys.modules or '_sqlite3' in sys.modules)"
        output = subprocess.check_output([sys.executable, "-c", code], text=True).strip()
        self.assertEqual(output, "False")

    def test_fresh_bootstrap_is_idempotent_and_rejects_late_import(self):
        code = "from experiments.sqlite_runtime import bootstrap_sqlite_runtime; a=bootstrap_sqlite_runtime(); b=bootstrap_sqlite_runtime(); import sqlite3; assert a==b; print(sqlite3.sqlite_version)"
        output = subprocess.check_output([sys.executable, "-c", code], text=True).strip()
        self.assertEqual(output, "3.40.1")
        result = subprocess.run([sys.executable, "-c", "import sqlite3; from experiments.sqlite_runtime import bootstrap_sqlite_runtime; bootstrap_sqlite_runtime()"], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SQLite was imported before bootstrap", result.stderr)


@unittest.skipUnless(DEFAULT_OUTPUT.exists(), "Prepared frozen dataset unavailable")
class FrozenDatasetTests(unittest.TestCase):
    def test_source_ids_and_generation_gold_isolation(self):
        manifest, rows, _ = validate_source(DEFAULT_MANIFEST)
        prepared = verify_dataset(DEFAULT_OUTPUT, verify_databases=False)
        generation = read_jsonl(DEFAULT_OUTPUT / "generation/questions.jsonl")
        self.assertEqual([row["question_id"] for row in rows], manifest["question_ids"])
        self.assertTrue(all(set(row) == set(GENERATION_FIELDS) for row in generation))
        self.assertEqual(len(prepared["question_ids"]), 300)
        smoke = read_jsonl(DEFAULT_OUTPUT / "generation/smoke_questions.jsonl")
        self.assertEqual(len(smoke), 30)
        self.assertEqual(len({row["db_id"] for row in smoke}), 11)
        self.assertEqual(select_ids(rows, 30, prepared["smoke_seed"]), prepared["smoke_question_ids"])


if __name__ == "__main__":
    unittest.main()
