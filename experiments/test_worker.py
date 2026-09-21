"""Offline contracts for prediction isolation, budgets, and raw submissions."""

import asyncio
from contextlib import closing, ExitStack
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from experiments.worker import classify_error, database_uri, predict, redact, validate_input
from experiments.model_contract import endpoint_sha256, validate_model_contract


class WorkerInputTests(unittest.TestCase):
    def test_gold_cannot_enter_generation(self):
        for name in ("SQL", "gold", "gold_sql", "difficulty"):
            with self.assertRaises(ValueError):
                validate_input({"question": "How many rows?", "db_id": "db", name: "secret"})

    def test_database_path_traversal_rejected(self):
        with self.assertRaises(ValueError):
            validate_input({"question": "Count?", "db_id": "../another"})

    def test_balance_and_throttling_are_distinct(self):
        self.assertEqual(classify_error({"status_code": 429, "message": "insufficient_quota"}),
                         ("insufficient_balance", False))
        self.assertEqual(classify_error({"status_code": 429, "message": "rate limited"}),
                         ("transient_api", True))
        self.assertEqual(classify_error({"status_code": 401, "message": "bad credentials"}),
                         ("authentication", False))
        self.assertEqual(classify_error("retrieval_service_error: embedding unavailable"),
                         ("service_error", True))

    def test_secret_redaction_in_nested_logs(self):
        with patch.dict(os.environ, {"TEST_API_KEY": "private-credential-123"}):
            result = redact({"logs": ["private-credential-123", "postgresql://reader:password@host/db"]})
        self.assertNotIn("private-credential-123", str(result))
        self.assertNotIn(":password@", str(result))

    def test_explicit_tpm_throttle_is_not_account_exhaustion(self):
        self.assertEqual(classify_error({"status_code": 429, "code": "insufficient_quota",
                                        "message": "Tokens per minute limit exceeded"}),
                         ("transient_api", True))
        self.assertEqual(classify_error({"status_code": 429, "code": "insufficient_quota",
                                        "message": "The token-plan 1-week quota is exhausted"}),
                         ("insufficient_balance", False))


class SQLiteIsolationTests(unittest.TestCase):
    def setUp(self):
        # Local fixtures never need credentials from the project's real .env.
        with patch("dotenv.load_dotenv"):
            from tools import native_sql_tools
        self.tools = native_sql_tools
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        for name, value in (("first", 3), ("second", 9)):
            path = self.base / name / f"{name}.sqlite"
            path.parent.mkdir()
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("CREATE TABLE sample (value INTEGER)")
                connection.execute("INSERT INTO sample VALUES (?)", (value,))
                connection.commit()

    def tearDown(self):
        if self.tools._db_instance:
            self.tools._db_instance._engine.dispose()
            self.tools._db_instance = None
        self.temporary.cleanup()

    def test_sqlite_is_readonly_and_cannot_attach_other_database(self):
        self.tools.set_database_uri(database_uri(self.base, "first"), "readonly")
        db = self.tools._get_database()
        self.assertEqual(db.run("SELECT value FROM sample"), "[(3,)]")
        for statement in ("DELETE FROM sample", "CREATE TEMP TABLE forbidden (x)",
                          "ATTACH DATABASE ':memory:' AS other", "PRAGMA query_only=OFF"):
            with self.assertRaises(Exception):
                db.run(statement)
        with closing(sqlite3.connect(self.base / "first" / "first.sqlite")) as connection:
            self.assertEqual(connection.execute("SELECT value FROM sample").fetchall(), [(3,)])

    def test_database_and_submission_are_session_isolated(self):
        self.tools.set_database_uri(database_uri(self.base, "first"), "first-session")
        self.tools.reset_session("first-session")
        self.tools.get_tool_call_manager().reset_session("first-session")
        query = " \nSELECT value FROM sample LIMIT 1;\n "
        self.assertIn("查询成功", self.tools.sql_db_query(query))
        self.assertEqual(self.tools.submit_final_sql(query)["status"], "success")
        self.assertEqual(self.tools.get_final_sql("first-session")["sql"], query)
        self.tools.set_database_uri(database_uri(self.base, "second"), "second-session")
        self.tools.reset_session("second-session")
        self.assertEqual(self.tools._infer_current_db_id(), "second")
        self.assertEqual(self.tools._get_database().run("SELECT value FROM sample"), "[(9,)]")
        self.assertEqual(self.tools.get_final_sql("second-session"), {})
        self.assertEqual(self.tools.get_linked_schema("second-session"), set())

    def test_statement_timeout_interrupts_sqlite(self):
        self.tools.set_database_uri(database_uri(self.base, "first"), "sql-timeout")
        with patch.dict(os.environ, {"SQL_QUERY_TIMEOUT_SECONDS": "0.001"}):
            with self.assertRaisesRegex(Exception, "interrupted"):
                self.tools._get_database().run(
                    "WITH RECURSIVE seq(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM seq WHERE n<100000000) SELECT SUM(n) FROM seq"
                )

    def test_sql_execution_budget_is_enforced(self):
        self.tools.set_database_uri(database_uri(self.base, "first"), "sql-budget")
        self.tools.reset_session("sql-budget")
        self.tools.get_tool_call_manager().reset_session("sql-budget")
        with patch.dict(os.environ, {"MAX_SQL_QUERY_CALLS": "1"}):
            self.assertIn("查询成功", self.tools.sql_db_query("SELECT value FROM sample"))
            self.assertIn("预算已用尽", self.tools.sql_db_query("SELECT value+1 FROM sample"))
        self.assertEqual(len(self.tools.get_sql_execution_trace("sql-budget")), 1)

    def test_keywords_in_literals_identifiers_and_comments_are_not_commands(self):
        for query in (
            "SELECT date_created, updated_at FROM sample",
            "SELECT 'create; update; delete', \"DROP\", [INSERT], `ALTER` FROM sample",
            "-- DROP TABLE sample\n SELECT value FROM sample /* DELETE */; -- trailing",
            "SELECT replace('UPDATE', 'UP', 'NEW') FROM sample",
            "WITH src AS (SELECT value FROM sample) SELECT value FROM src",
        ):
            with self.subTest(query=query):
                self.assertEqual(self.tools._read_only_query_error(query), "")
        for query in (
            "WITH src AS (SELECT 1) DELETE FROM sample",
            "WITH src AS (DELETE FROM sample RETURNING value) SELECT value FROM src",
            "SELECT value FROM sample; DELETE FROM sample",
            "WITH src AS (SELECT 1) REPLACE sample VALUES(2)",
            "ATTACH DATABASE ':memory:' AS other",
            "SELECT load_extension('extension')",
        ):
            with self.subTest(query=query):
                self.assertTrue(self.tools._read_only_query_error(query))
        self.tools.set_database_uri(database_uri(self.base, "first"), "cte-session")
        self.tools.reset_session("cte-session")
        self.tools.get_tool_call_manager().reset_session("cte-session")
        self.assertIn("查询成功", self.tools.sql_db_query(
            "/* read only */ WITH src AS (SELECT value FROM sample) SELECT value FROM src"))


class ModelConstructionTests(unittest.TestCase):
    def test_real_model_constructor_keeps_provider_specific_nonthinking_and_limits(self):
        with patch("socket.socket.connect", side_effect=AssertionError("Network forbidden")), \
                patch("socket.socket.connect_ex", side_effect=AssertionError("Network forbidden")), \
                patch.dict(os.environ, {"LITE_LLM_TEMPERATURE": "0", "LITE_LLM_REQUEST_TIMEOUT": "120"}):
            from utils import create_model, TrackedLiteLlm
            for name, url, body in (
                ("deepseek-v4.1-flash", "http://offline.invalid/v1",
                 {"chat_template_kwargs": {"enable_thinking": False}}),
                ("deepseek-flash", "https://api.deepseek.com/v1", {"thinking": {"type": "disabled"}}),
                ("offline-app-model", "http://offline.invalid/v1",
                 {"chat_template_kwargs": {"enable_thinking": False}}),
            ):
                with self.subTest(model=name):
                    model = create_model(model_name=name, base_url=url, api_key="offline-unused")
                    self.assertIsInstance(model, TrackedLiteLlm)
                    self.assertEqual(model.model, "openai/" + name)
                    args = model._additional_args
                    self.assertEqual(args["api_base"], url)
                    self.assertEqual(args["extra_body"], body)
                    self.assertEqual(args["temperature"], 0)
                    self.assertEqual((args["num_retries"], args["max_retries"], args["timeout"]), (0, 0, 120))

    def test_model_contract_rejects_unverified_aliases_and_official_endpoint_impersonation(self):
        for name in ("deepseek-chat", "deepseek-v4-pro", "", None):
            with self.subTest(model=name), self.assertRaises(ValueError):
                validate_model_contract(name, "https://api.deepseek.com/v1")
        for url in (
            "http://api.deepseek.com/v1", "https://api.deepseek.com.evil.invalid/v1",
            "https://proxy.invalid/v1", "https://user:unused@api.deepseek.com/v1",
            "https://api.deepseek.com:8443/v1", "https://api.deepseek.com/v1?redirect=elsewhere",
            "https://api.deepseek.com/v1#fragment", "https://api.deepseek.\ncom/v1",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_model_contract("deepseek-flash", url)
        for url in ("https://api.deepseek.com", "https://api.deepseek.com/v1",
                    "https://api.deepseek.com:443/v1/"):
            with self.subTest(url=url):
                self.assertEqual(validate_model_contract("deepseek-flash", url)["provider_endpoint_sha256"],
                                 endpoint_sha256(url))


class WorkerProviderTests(unittest.IsolatedAsyncioTestCase):
    async def invoke(self, model, url, config, *, pass_runtime=False):
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            env = Path(temporary) / "fixture.env"
            env.write_text(f"LITE_LLM_MODEL_NAME={model}\nLITE_LLM_BASE_URL={url}\n"
                           "LITE_LLM_API_KEY=offline-unused\n", encoding="utf-8")
            stack.enter_context(patch.dict(os.environ, {}, clear=True))
            stack.enter_context(patch("socket.socket.connect", side_effect=AssertionError("Network forbidden")))
            stack.enter_context(patch("socket.socket.connect_ex", side_effect=AssertionError("Network forbidden")))
            boundary = stack.enter_context(patch("experiments.worker.database_uri",
                                                 return_value="sqlite:///offline?mode=ro&uri=true"))
            if not pass_runtime:
                boundary.side_effect = RuntimeError("offline validation boundary")
            else:
                class FakeService:
                    def __init__(self, **kwargs):
                        self.last_run_diagnostics = {}
                        self._adk = types.SimpleNamespace(get_available_skills=lambda: [
                            {"name": name} for name in
                            ("data-link", "database-query-helper", "correct", "schema-exploration")])

                    async def run_query(self, **kwargs):
                        return {"execution_error": {"status_code": 401, "message": "offline authentication fixture"}}

                native = types.SimpleNamespace(
                    get_final_sql=lambda *_: {}, get_sql_execution_trace=lambda *_: [],
                    get_correction_events=lambda *_: [], get_linked_schema=lambda *_: set(),
                    get_linked_schema_snapshot=lambda *_: set(),
                )
                stack.enter_context(patch.dict(sys.modules, {
                    "agent": types.SimpleNamespace(AgentService=FakeService),
                    "tools": types.SimpleNamespace(native_sql_tools=native),
                }))
                stack.enter_context(patch("experiments.sqlite_runtime.bootstrap_sqlite_runtime",
                                          return_value={"version": "3.40.1", "dll_sha256": "offline"}))
            result = await predict({"question": "Count rows", "evidence": "Fixture", "db_id": "example",
                                    "config": {"env_file": str(env), **config}})
            return result, boundary.call_count

    async def test_endpoint_drift_is_rejected_before_model_or_database_construction(self):
        actual = "https://api.deepseek.com/v1"
        for name in ("deepseek-flash", "deepseek-v4.1-flash"):
            result, calls = await self.invoke(name, actual, {
                "model_name": name, "provider_endpoint_sha256": endpoint_sha256("https://old.invalid/v1")})
            self.assertEqual(calls, 0)
            self.assertEqual(result["llm_calls"], 0)
            self.assertEqual(result["metadata"]["provider_endpoint_sha256"], endpoint_sha256(actual))
            self.assertIn("frozen endpoint hash", result["error"]["message"])

    async def test_new_alias_requires_frozen_hash_and_model_must_match_environment(self):
        for config in ({"model_name": "deepseek-flash"},
                       {"model_name": "deepseek-v4.1-flash", "provider_endpoint_sha256": "0" * 64},
                       {"model_name": None, "provider_endpoint_sha256": "0" * 64}):
            result, calls = await self.invoke("deepseek-flash", "https://api.deepseek.com/v1", config)
            self.assertEqual(calls, 0)
            self.assertEqual(result["llm_calls"], 0)
            self.assertEqual(result["status"], "failed")

    async def test_legacy_payload_without_hash_still_passes_contract(self):
        result, calls = await self.invoke("deepseek-v4.1-flash", "http://offline.invalid/v1", {})
        self.assertEqual(calls, 1)
        self.assertIn("offline validation boundary", result["error"]["message"])

    async def test_identity_metadata_survives_runtime_and_failed_service_result(self):
        url = "https://api.deepseek.com/v1"
        result, calls = await self.invoke("deepseek-flash", url, {
            "model_name": "deepseek-flash", "provider_endpoint_sha256": endpoint_sha256(url)}, pass_runtime=True)
        self.assertEqual(calls, 1)
        self.assertEqual(result["error_category"], "authentication")
        self.assertEqual(result["model"], "deepseek-flash")
        self.assertEqual(result["metadata"]["model_name"], "deepseek-flash")
        self.assertEqual(result["metadata"]["provider_endpoint_sha256"], endpoint_sha256(url))
        self.assertEqual(result["metadata"]["sqlite_runtime"]["version"], "3.40.1")
        self.assertEqual(result["llm_calls"], 0)


class ModelTrackingTests(unittest.IsolatedAsyncioTestCase):
    async def test_usage_is_per_call_and_budget_blocks_before_request(self):
        from google.adk.models.lite_llm import LiteLlm
        from google.genai import types as genai_types
        from utils import ModelUsageTracker, model_usage_tracker, TrackedLiteLlm, LlmCallBudgetExceeded
        responses = 0

        async def fake_generate(_self, _request, stream=False):
            nonlocal responses
            responses += 1
            usage = genai_types.GenerateContentResponseUsageMetadata(
                prompt_token_count=17, candidates_token_count=5, total_token_count=22)
            yield types.SimpleNamespace(usage_metadata=None, error_code=None)
            yield types.SimpleNamespace(usage_metadata=usage, error_code=None)

        checkpoints = []
        tracker = ModelUsageTracker(max_calls=2, on_update=lambda snapshot: checkpoints.append(snapshot))
        token = model_usage_tracker.set(tracker)
        model = TrackedLiteLlm(model="openai/offline-fake", api_key="unused")
        try:
            with patch.object(LiteLlm, "generate_content_async", fake_generate):
                for _ in range(2):
                    _ = [part async for part in model.generate_content_async(None)]
                with self.assertRaises(LlmCallBudgetExceeded):
                    _ = [part async for part in model.generate_content_async(None)]
        finally:
            model_usage_tracker.reset(token)
        usage = tracker.snapshot()
        self.assertEqual(responses, 2)
        self.assertEqual(usage["llm_calls"], 2)
        self.assertEqual(usage["prompt_tokens"], 34)
        self.assertEqual(usage["completion_tokens"], 10)
        self.assertTrue(usage["usage_complete"])
        self.assertEqual([item["llm_calls"] for item in checkpoints], [1, 1, 2, 2])
        self.assertIsNone(checkpoints[0]["prompt_tokens"])

    async def test_failed_calls_count_and_missing_usage_is_not_zero(self):
        from google.adk.models.lite_llm import LiteLlm
        from utils import ModelUsageTracker, model_usage_tracker, TrackedLiteLlm

        async def fail(_self, _request, stream=False):
            raise RuntimeError("insufficient_quota")
            yield

        tracker = ModelUsageTracker(max_calls=2)
        token = model_usage_tracker.set(tracker)
        try:
            with patch.object(LiteLlm, "generate_content_async", fail):
                with self.assertRaisesRegex(RuntimeError, "insufficient_quota"):
                    _ = [part async for part in TrackedLiteLlm(model="openai/offline-fake").generate_content_async(None)]
        finally:
            model_usage_tracker.reset(token)
        self.assertEqual(tracker.snapshot()["llm_calls"], 1)
        self.assertIsNone(tracker.snapshot()["prompt_tokens"])
        self.assertFalse(tracker.snapshot()["usage_complete"])


if __name__ == "__main__":
    unittest.main()
