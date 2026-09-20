"""Fixture tests for diagnostic evidence, read-only compilation, and gold isolation."""
from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from experiments.diagnose import (
    checked_output_directory, compile_references, diagnose_question, initial_sql_from_trace,
    load_catalog, load_completed_inputs, markdown_report, outcome_from_score,
    read_prediction_trace, run_diagnosis, summarize_diagnostics, trace_schema_observations,
)
from experiments.prepare_dataset import sha256_file, write_json, write_jsonl


class FixtureCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = self.root / "fixture.sqlite"
        with closing(sqlite3.connect(self.database)) as connection:
            with connection:
                connection.execute('CREATE TABLE "Order Details" ("Order ID" INTEGER PRIMARY KEY, "Unit.Price" REAL, "customer""label" TEXT)')
                connection.execute('CREATE TABLE "客户.表" ("列 名" TEXT, "order.id" INTEGER)')
                connection.execute('INSERT INTO "Order Details" VALUES (1, 12.5, ?)', ("fixture-only",))
                connection.execute('INSERT INTO "客户.表" VALUES (?, 1)', ("fixture-only",))
                connection.execute('CREATE VIEW "Order View" AS SELECT "Order ID", "Unit.Price" FROM "Order Details"')
        self.catalog = load_catalog(self.database)

    def tearDown(self):
        self.temporary.cleanup()

    def reference(self, sql):
        return compile_references(sql, self.database, self.catalog)

    @staticmethod
    def pairs(reference):
        return {(row["table"], row["column"]) for row in reference["columns"] if row["coverage_eligible"]}


class CompilerReferenceTests(FixtureCase):
    def test_cte_and_aliases_resolve_to_base_identifiers(self):
        sql = '''WITH "chosen orders" AS (
            SELECT "Order ID" AS key_alias, "Unit.Price" AS price_alias FROM "Order Details"
        ) SELECT c.price_alias FROM "chosen orders" AS c JOIN "客户.表" AS x
          ON c.key_alias=x."order.id" WHERE x."列 名"='literal' '''
        result = self.reference(sql)
        self.assertEqual(result["status"], "compiled")
        self.assertEqual(set(result["tables"]), {"Order Details", "客户.表"})
        self.assertEqual(self.pairs(result), {("Order Details", "Order ID"), ("Order Details", "Unit.Price"),
                                             ("客户.表", "order.id"), ("客户.表", "列 名")})
        self.assertNotIn("chosen orders", result["tables"])

    def test_quotes_dots_and_case_are_canonicalized_against_schema(self):
        result = self.reference('SELECT o.[Order ID], o.`Unit.Price`, o."customer""label" FROM [order details] AS o')
        self.assertEqual(result["status"], "compiled")
        self.assertEqual(self.pairs(result), {("Order Details", "Order ID"), ("Order Details", "Unit.Price"), ("Order Details", 'customer"label')})
        self.assertEqual(self.catalog.resolve('"Order Details"."Unit.Price"'), ("Order Details", "Unit.Price"))
        self.assertEqual(self.catalog.resolve("Order Details.Unit.Price"), ("Order Details", "Unit.Price"))
        self.assertEqual(self.catalog.resolve('main.[ORDER DETAILS].[Order ID]'), ("Order Details", "Order ID"))
        self.assertEqual(self.catalog.resolve('"客户.表"."order.id"'), ("客户.表", "order.id"))

    def test_count_star_is_table_only_not_a_missing_empty_column(self):
        result = self.reference('SELECT COUNT(*) FROM "Order Details"')
        self.assertEqual(result["status"], "compiled")
        self.assertEqual(result["tables"], ["Order Details"])
        self.assertEqual(result["columns"], [])
        self.assertEqual(result["table_only_reads"], [{"database": "main", "table": "Order Details"}])
        self.assertIn("count", result["functions"])

    def test_select_star_and_view_capture_expanded_columns(self):
        result = self.reference('SELECT * FROM "Order View"')
        self.assertEqual(result["status"], "compiled")
        self.assertIn(("Order Details", "Order ID"), self.pairs(result))
        self.assertIn(("Order Details", "Unit.Price"), self.pairs(result))
        self.assertGreater(result["indirect_read_context_count"], 0)

    def test_view_callbacks_do_not_count_as_physical_coverage_requirements(self):
        result = self.reference('SELECT * FROM "Order View"')
        self.assertEqual(result["tables"], ["Order Details"])
        self.assertEqual(self.pairs(result), {("Order Details", "Order ID"), ("Order Details", "Unit.Price")})
        logical = [row for row in result["columns"] if row["table"] == "Order View"]
        self.assertTrue(logical)
        self.assertTrue(all(row["object_type"] == "view" and not row["coverage_eligible"] for row in logical))

    def test_recursive_query_is_not_executed(self):
        started = time.monotonic()
        result = self.reference("WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n) SELECT sum(x) FROM n")
        self.assertEqual(result["status"], "compiled")
        self.assertFalse(result["query_executed"])
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(result["tables"], [])

    def test_all_writes_pragma_attach_and_extensions_are_denied(self):
        before = sha256_file(self.database)
        statements = [
            'UPDATE "Order Details" SET "Unit.Price"=0',
            'WITH n AS (SELECT 1) DELETE FROM "Order Details"',
            'INSERT INTO "Order Details" VALUES (2, 0, NULL)',
            'DROP TABLE "Order Details"', 'ALTER TABLE "Order Details" ADD COLUMN x',
            "PRAGMA writable_schema=ON", "ATTACH DATABASE ':memory:' AS external",
            "SELECT load_extension('untrusted.dll')", 'SELECT 1; DELETE FROM "Order Details"',
        ]
        for sql in statements:
            with self.subTest(sql=sql):
                result = self.reference(sql)
                self.assertNotEqual(result["status"], "compiled")
                self.assertFalse(result["references_complete"])
                self.assertFalse(result["query_executed"])
        self.assertEqual(sha256_file(self.database), before)
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM "Order Details"').fetchone()[0], 1)

    def test_explain_operands_and_compile_errors_do_not_leak_literals(self):
        marker = "GOLD_LITERAL_CANARY_7ea627"
        valid = self.reference(f'SELECT "Order ID" FROM "Order Details" WHERE "customer""label"=\'{marker}\'')
        invalid = self.reference("SELECT '" + marker)
        self.assertNotIn(marker, json.dumps(valid))
        self.assertNotIn(marker, json.dumps(invalid))
        self.assertEqual(valid["status"], "compiled")
        self.assertEqual(invalid["status"], "compile_error")


class EvidenceTests(FixtureCase):
    def trace(self):
        return {
            "linked_schema": ['"Order Details"."Order ID"'],
            "linked_schema_snapshot": ["Order Details.Order ID"],
            "tool_trace": [
                {"kind": "call", "name": "sql_db_value_lookup", "args": {"phrase": "PRIVATE_QUESTION_TEXT"}},
                {"kind": "response", "name": "sql_db_value_lookup", "response": {"result":
                    "### 召回的架构元素\n**Order Details.Unit.Price** | REAL | PRIVATE_RETRIEVAL_VALUE\n"
                    "### 值匹配提示\n**客户.表.列 名** | not actually a schema listing\n"
                    "### 🔑 结构键（JOIN 所需主键/外键）\n**Order Details.Order ID** | 主键 | key\n"}},
            ],
            "initial_sql": 'SELECT "Order ID" FROM "Order Details"',
            "sql_execution_trace": [{"sql": "SELECT 1", "state": "accepted", "is_probe": True},
                                    {"sql": 'SELECT "Order ID" FROM "Order Details"', "state": "accepted", "is_probe": False}],
        }

    def row(self, *, ex=0, trace=None, gold=None, final=None, executable=True):
        record = {"question_id": 1, "db_id": "fixture", "difficulty": "simple",
                  "gold_sql": gold or 'SELECT "Unit.Price" FROM "Order Details"'}
        prediction = {"question_id": 1, "db_id": "fixture", "status": "succeeded",
                      "submitted_final_sql": final or 'SELECT "Order ID" FROM "Order Details"'}
        score = {"ex": ex, "status": "scored", "prediction_executable": executable}
        return diagnose_question(record, prediction, score, self.trace() if trace is None else trace,
                                 self.database, self.catalog)

    def test_visible_candidates_and_structure_are_separate_observations(self):
        observed = trace_schema_observations(self.trace(), self.catalog)
        self.assertEqual(observed["candidates"], {("Order Details", "Unit.Price")})
        self.assertEqual(observed["structural"], {("Order Details", "Order ID")})
        self.assertNotIn(("客户.表", "列 名"), observed["candidates"])
        self.assertFalse(observed["per_route_candidates_available"])

    def test_value_only_lookup_fallback_without_metadata_pipe_is_visible(self):
        trace = self.trace()
        trace["tool_trace"][-1]["response"]["result"] = (
            "### 召回的架构元素\n\n**Order Details.Unit.Price**\n  示例值: PRIVATE_VALUE\n"
            "\n### 值匹配提示（实体对齐）\n**客户.表.列 名**\n"
            "\n### 🔑 结构键（JOIN 所需主键/外键）\n**Order Details.Order ID** | 主键 | key\n"
        )
        observed = trace_schema_observations(trace, self.catalog)
        self.assertEqual(observed["candidates"], {("Order Details", "Unit.Price")})
        self.assertEqual(observed["structural"], {("Order Details", "Order ID")})
        row = self.row(trace=trace)
        self.assertEqual(row["coverage"]["missing_from_visible_lookup_union"], [])
        self.assertNotIn("suspected_visible_candidate_coverage_gap", row["suspected_causes"])
        self.assertNotIn("PRIVATE_VALUE", json.dumps(row))

    def test_structural_foreign_key_target_is_visible_but_not_automatically_linked(self):
        trace = self.trace()
        trace["tool_trace"][-1]["response"]["result"] += (
            '**Order Details.Order ID** | 主键/外键 | 主键 + 外键 → 客户.表.列 名，用于 JOIN Order Details 与 客户.表\n'
        )
        observed = trace_schema_observations(trace, self.catalog, db_id="fixture")
        target = ("客户.表", "列 名")
        self.assertIn(target, observed["structural"])
        self.assertNotIn(target, observed["linked"])
        self.assertNotIn(target, observed["candidates"])
        self.assertEqual(observed["locations"][target][0], {
            "lookup_response_index": 1, "visible_position": 3, "section": "structural_keys",
            "source": "foreign_key_target", "source_table": "Order Details", "source_column": "Order ID",
        })
        row = self.row(trace=trace, gold='SELECT "列 名" FROM "客户.表"')
        self.assertEqual(row["coverage"]["missing_from_visible_lookup_union"], [])
        self.assertEqual(row["coverage"]["missing_from_linked_schema"], [{"table": "客户.表", "column": "列 名"}])
        self.assertIn("reference_columns_visible_but_not_linked", row["suspected_causes"])

    def test_foreign_key_target_supports_quoted_chinese_and_current_database(self):
        for spelling, expected in [
            ('"客户.表"."列 名"', ("客户.表", "列 名")),
            ('`客户.表`.`order.id`', ("客户.表", "order.id")),
            ('[客户.表].[列 名]', ("客户.表", "列 名")),
            ('main."客户.表"."列 名"', ("客户.表", "列 名")),
            ('fixture."客户.表"."列 名"', ("客户.表", "列 名")),
            ('"Order Details"."customer""label"', ("Order Details", 'customer"label')),
        ]:
            with self.subTest(spelling=spelling):
                trace = self.trace()
                trace["tool_trace"][-1]["response"]["result"] += (
                    f'**Order Details.Order ID** | 主键/外键 | 外键 → {spelling}，用于 JOIN left 与 right\n'
                )
                observed = trace_schema_observations(trace, self.catalog, db_id="fixture")
                self.assertIn(expected, observed["structural"])

    def test_foreign_key_target_rejects_missing_arrow_partial_and_foreign_identifiers(self):
        for description in [
            '外键 客户.表.列 名', '被外键引用（客户.表.列 名）',
            '说明：外键 → 客户.表.列 名', '外键 → 列 名',
            '外键 → missing_table.列 名', '外键 → other."客户.表"."列 名"',
            '外键 → temp."客户.表"."列 名"', '外键 → "客户.表"."列 名"; SELECT 1',
            '外键 → "客户.表"."列 名', '外键 → "客户.表"."列 名" WHERE 1=1',
        ]:
            with self.subTest(description=description):
                trace = self.trace()
                trace["tool_trace"][-1]["response"]["result"] += (
                    f'**Order Details.Order ID** | 主键/外键 | {description}\n'
                )
                observed = trace_schema_observations(trace, self.catalog, db_id="fixture")
                self.assertNotIn(("客户.表", "列 名"), observed["structural"])

    def test_foreign_key_target_requires_lookup_structure_row_and_valid_source(self):
        valid_line = '**Order Details.Order ID** | 主键/外键 | 外键 → 客户.表.列 名，用于 JOIN left 与 right\n'
        for name, text in [
            ("sql_db_query", "### 结构键\n" + valid_line),
            ("sql_db_value_lookup", "### 值匹配提示\n" + valid_line),
            ("sql_db_value_lookup", "### 召回的架构元素\n" + valid_line),
            ("sql_db_value_lookup", "### 结构键\n" + valid_line.replace('主键/外键', 'TEXT')),
            ("sql_db_value_lookup", "### 结构键\n" + valid_line.replace('**Order Details.Order ID**', '**unknown.id**')),
            ("sql_db_value_lookup", "### 结构键\n" + valid_line.replace('**Order Details.Order ID**', '**temp."Order Details"."Order ID"**')),
            ("sql_db_value_lookup", "### 结构键\nSELECT 客户.表.列 名 FROM table_name\n"),
        ]:
            with self.subTest(name=name, text=text):
                trace = {"tool_trace": [{"kind": "response", "name": name, "response": {"result": text}}]}
                observed = trace_schema_observations(trace, self.catalog, db_id="fixture")
                self.assertNotIn(("客户.表", "列 名"), observed["structural"])

    def test_lookup_declaring_another_database_is_not_available(self):
        for response in [
            {"db_id": "other", "result": "### 结构键\n**Order Details.Order ID** | 主键/外键 | 外键 → 客户.表.列 名"},
            {"result": "【DB_ID】other\n### 结构键\n**Order Details.Order ID** | 主键/外键 | 外键 → 客户.表.列 名"},
        ]:
            trace = {"tool_trace": [{"kind": "response", "name": "sql_db_value_lookup", "response": response}]}
            observed = trace_schema_observations(trace, self.catalog, db_id="fixture")
            self.assertEqual(observed["candidates"] | observed["structural"], set())
            self.assertEqual(observed["unresolved_identifier_counts"], {"lookup_database": 1})

    def test_view_gold_does_not_report_missing_logical_columns_when_base_is_linked(self):
        trace = self.trace()
        trace["linked_schema"] = ["Order Details.Order ID", "Order Details.Unit.Price"]
        row = self.row(trace=trace, gold='SELECT * FROM "Order View"')
        self.assertEqual(row["coverage"]["missing_from_linked_schema"], [])
        self.assertEqual(row["coverage"]["reference_tables_absent_from_linked_schema"], [])
        self.assertEqual(row["coverage"]["missing_from_visible_lookup_union"], [])
        self.assertEqual(row["suspected_causes"], [])

    def test_wrong_query_has_evidence_not_a_proven_cause(self):
        row = self.row()
        self.assertEqual(row["outcome"], "executable_result_mismatch")
        self.assertIn("suspected_linked_schema_coverage_gap", row["suspected_causes"])
        self.assertIn("reference_columns_visible_but_not_linked", row["suspected_causes"])
        self.assertNotIn("suspected_visible_candidate_coverage_gap", row["suspected_causes"])
        self.assertTrue(row["coverage"]["gold_shape_is_not_unique"])
        self.assertEqual(row["initial_to_submission"]["execution_attempt_count"], 1)
        self.assertEqual(row["initial_to_submission"]["probe_execution_count"], 1)

    def test_correct_query_never_becomes_failure_despite_missing_gold_columns(self):
        row = self.row(ex=1)
        summary = summarize_diagnostics([row])
        self.assertEqual(row["outcome"], "correct")
        self.assertEqual(row["suspected_causes"], [])
        self.assertTrue(row["coverage"]["missing_from_linked_schema"])
        self.assertEqual(summary["overall"]["incorrect"], 0)
        self.assertEqual(summary["overall"]["correct_with_reference_coverage_gaps"], 1)
        self.assertEqual(summary["failure_evidence_question_ids"], {})

    def test_absent_trace_is_unknown_instead_of_missing_every_reference(self):
        row = self.row(trace={})
        self.assertFalse(row["coverage"]["linked_comparison_available"])
        self.assertFalse(row["coverage"]["lookup_comparison_available"])
        self.assertIsNone(row["coverage"]["missing_from_linked_schema"])
        self.assertIsNone(row["coverage"]["missing_from_visible_lookup_union"])
        self.assertEqual(row["suspected_causes"], [])
        partial = self.row(trace={"sql_execution_trace": None, "correction_events": None, "tool_trace": None})
        self.assertEqual(partial["suspected_causes"], [])

    def test_failed_gold_compilation_cannot_assert_complete_coverage(self):
        row = self.row(gold='SELECT missing_column FROM "Order Details"')
        self.assertFalse(row["references"]["gold"]["references_complete"])
        self.assertIsNone(row["coverage"]["missing_from_linked_schema"])
        self.assertEqual(row["suspected_causes"], [])

    def test_initial_to_final_reference_changes_are_explicit(self):
        row = self.row(final='SELECT SUM("Unit.Price") FROM "Order Details"')
        change = row["initial_to_submission"]
        self.assertTrue(change["raw_text_changed"])
        self.assertEqual(change["columns_added"], [{"table": "Order Details", "column": "Unit.Price"}])
        self.assertEqual(change["columns_removed"], [{"table": "Order Details", "column": "Order ID"}])
        self.assertEqual(change["functions_added"], ["sum"])
        self.assertEqual(initial_sql_from_trace({"sql_execution_trace": [{"sql": "SELECT 1", "is_probe": True}]}), ("", "unavailable"))

    def test_no_gold_sql_values_or_question_text_in_any_diagnostic_output(self):
        marker = "GOLD_LITERAL_CANARY_72ae"
        gold = f'SELECT "Unit.Price" FROM "Order Details" WHERE "customer""label"=\'{marker}\''
        row = self.row(gold=gold)
        summary = summarize_diagnostics([row])
        serialized = json.dumps(row) + json.dumps(summary) + markdown_report(summary)
        for secret in (marker, gold, "PRIVATE_QUESTION_TEXT", "PRIVATE_RETRIEVAL_VALUE"):
            self.assertNotIn(secret, serialized)

    def test_timeout_and_generation_failures_remain_in_group_denominators(self):
        rows = [self.row(ex=1), self.row()]
        rows[1]["question_id"] = 2
        summary = summarize_diagnostics(rows)
        self.assertEqual(summary["overall"]["count"], 2)
        self.assertEqual(summary["overall"]["official_ex"], 0.5)
        self.assertEqual(summary["by_db_id"]["fixture"]["count"], 2)
        self.assertEqual(outcome_from_score({"ex": 0, "status": "missing_sql"}, {"status": "timeout", "submitted_final_sql": ""}), "generation_timeout")
        self.assertEqual(outcome_from_score({"ex": 0, "status": "gold_error"}, {"submitted_final_sql": "SELECT 1"}), "evaluation_gold_error")
        self.assertEqual(outcome_from_score({"ex": 1}, {"status": "timeout", "submitted_final_sql": "SELECT 1"}), "correct")

    def test_full_report_pipeline_only_writes_the_diagnostic_directory(self):
        run_dir, dataset_dir = self.root / "run", self.root / "dataset"
        db_root = self.root / "databases"
        business = db_root / "fixture/fixture.sqlite"
        business.parent.mkdir(parents=True)
        shutil.copyfile(self.database, business)
        marker = "GOLD_NEVER_ENTER_GENERATION_5717"
        sql = f'SELECT "Unit.Price" FROM "Order Details" WHERE "customer""label"=\'{marker}\''
        records = [{"question_id": index, "db_id": "fixture", "difficulty": "simple", "gold_sql": sql} for index in range(30)]
        predictions = [{"question_id": index, "db_id": "fixture", "status": "succeeded", "submitted_final_sql": 'SELECT "Order ID" FROM "Order Details"',
                        "question": "PRIVATE_QUESTION_BODY", "trace": self.trace()} for index in range(30)]
        scores = [{"question_id": index, "db_id": "fixture", "difficulty": "simple", "ex": index % 2, "status": "scored", "prediction_executable": True} for index in range(30)]
        dataset = {"db_root": str(db_root), "source_sha256": "fixture-source-hash"}
        runtime = {"version": sqlite3.sqlite_version, "dll_sha256": "fixture-runtime-hash"}
        official = {"official_evaluator": {"sqlite_runtime": runtime}}
        write_json(run_dir / "run_manifest.json", {"run_id": "fixture-run"})
        write_json(dataset_dir / "dataset_manifest.json", dataset)
        write_jsonl(run_dir / "predictions.jsonl", predictions)
        write_jsonl(run_dir / "scores.jsonl", scores)
        write_jsonl(dataset_dir / "evaluation/smoke_records.jsonl", records)
        write_jsonl(dataset_dir / "generation/smoke_questions.jsonl", [{"question_id": index, "question": "fixture"} for index in range(30)])
        protected = [business, run_dir / "predictions.jsonl", run_dir / "scores.jsonl", dataset_dir / "generation/smoke_questions.jsonl", dataset_dir / "evaluation/smoke_records.jsonl"]
        before = {path: sha256_file(path) for path in protected}
        with patch("experiments.diagnose.bootstrap_sqlite_runtime", return_value=runtime), patch(
            "experiments.diagnose.load_completed_inputs", return_value=(dataset, records, predictions, scores, official)
        ):
            summary = run_diagnosis(dataset_dir, run_dir, subset="smoke")
        self.assertEqual(summary["overall"]["count"], 30)
        self.assertEqual(summary["overall"]["correct"], 15)
        for path, digest in before.items():
            self.assertEqual(sha256_file(path), digest)
        for path in (run_dir / "diagnostics").iterdir():
            text = path.read_text(encoding="utf-8")
            self.assertNotIn(marker, text)
            self.assertNotIn("PRIVATE_QUESTION_BODY", text)
            self.assertNotIn(sql, text)
        self.assertEqual(len((run_dir / "diagnostics/diagnostics.jsonl").read_text(encoding="utf-8").splitlines()), 30)


class IsolationAndCompletionTests(unittest.TestCase):
    def test_output_directory_cannot_target_generation_or_retrieval(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run = root / "runs" / "B0"
            self.assertEqual(checked_output_directory(run, run / "diagnostics"), (run / "diagnostics").resolve())
            for target in (root / "generation", root / "retrieval", run, run / "diagnostics" / "generation", run / "diagnostics" / ".." / "traces"):
                with self.subTest(target=target), self.assertRaises(ValueError):
                    checked_output_directory(run, target)

    def test_trace_fallback_must_match_the_scored_submission(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prediction = {"question_id": 1, "attempt": 1, "db_id": "db", "session_id": "s1", "submitted_final_sql": "SELECT 1"}
            path = root / "traces/1.attempt1.result.json"
            write_json(path, {**prediction, "submitted_final_sql": "SELECT 2", "trace": {"linked_schema": []}})
            with self.assertRaises(ValueError):
                read_prediction_trace(prediction, root)
            write_json(path, {**prediction, "trace": {"linked_schema": []}})
            trace, source = read_prediction_trace(prediction, root)
            self.assertEqual(trace, {"linked_schema": []})
            self.assertEqual(source, path.name)

    def test_partial_or_changed_predictions_are_rejected_before_diagnosis(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset_dir, run_dir = root / "dataset", root / "run"
            source = root / "source.json"
            source.write_text("test source marker", encoding="utf-8")
            source_hash = sha256_file(source)
            ids = list(range(300))
            selection = root / "selection.json"
            write_json(selection, {"question_ids": ids})
            records = [{"question_id": index, "db_id": "db", "difficulty": "simple", "gold_sql": "SELECT 1"} for index in ids]
            records_path = dataset_dir / "evaluation/records.jsonl"
            write_jsonl(records_path, records)
            dataset = {"question_ids": ids, "source_path": str(source), "source_sha256": source_hash,
                       "selection_manifest": str(selection), "selection_manifest_sha256": sha256_file(selection),
                       "file_hashes": {"evaluation/records.jsonl": sha256_file(records_path)}, "databases": {}, "db_root": str(root)}
            write_json(dataset_dir / "dataset_manifest.json", dataset)
            write_json(run_dir / "run_manifest.json", {"dataset_manifest_sha256": sha256_file(dataset_dir / "dataset_manifest.json"), "subset": "all"})
            predictions = [{"question_id": index, "db_id": "db", "status": "succeeded", "submitted_final_sql": "SELECT 1"} for index in ids]
            scores = [{"question_id": index, "db_id": "db", "difficulty": "simple", "ex": 1} for index in ids]
            write_jsonl(run_dir / "predictions.jsonl", predictions[:-1])
            write_jsonl(run_dir / "scores.jsonl", scores)
            summary = {"subset": "all", "source_sha256": source_hash, "selection_manifest_sha256": sha256_file(selection),
                       "predictions_sha256": sha256_file(run_dir / "predictions.jsonl"), "scores_sha256": sha256_file(run_dir / "scores.jsonl"),
                       "overall": {"count": 300, "correct": 300}}
            write_json(run_dir / "summary.json", summary)
            with patch("experiments.diagnose.SOURCE_SHA256", source_hash):
                with self.assertRaisesRegex(ValueError, "complete 300-question predictions"):
                    load_completed_inputs(dataset_dir, run_dir, "all")
                write_jsonl(run_dir / "predictions.jsonl", predictions)
                with self.assertRaisesRegex(ValueError, "input was changed"):
                    load_completed_inputs(dataset_dir, run_dir, "all")
                summary["predictions_sha256"] = sha256_file(run_dir / "predictions.jsonl")
                write_json(run_dir / "summary.json", summary)
                loaded = load_completed_inputs(dataset_dir, run_dir, "all")
                self.assertEqual(len(loaded[2]), 300)


if __name__ == "__main__":
    unittest.main()
