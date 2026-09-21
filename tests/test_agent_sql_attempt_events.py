"""Exercise real run_query event handling without model or database access."""
import contextlib
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

import agent
from google.genai import types as genai_types


def call(identifier, sql):
    return genai_types.Part(function_call=genai_types.FunctionCall(
        id=identifier, name="sql_db_query", args={"query": sql}))


def response(identifier, text):
    return genai_types.Part(function_response=genai_types.FunctionResponse(
        id=identifier, name="sql_db_query", response={"result": text}))


def event(*parts):
    return types.SimpleNamespace(content=genai_types.Content(parts=list(parts)), partial=False)


def accepted(rows):
    return f"✅ 查询成功，共 {rows} 行"


class SqlAttemptEventTests(unittest.IsolatedAsyncioTestCase):
    async def replay(self, events, *, executions=None, declared=None):
        async def run_async(**kwargs):
            self.assertEqual(kwargs["new_message"].parts[0].text, "question\n\nEvidence:\nevidence")
            self.assertEqual(kwargs["run_config"].max_llm_calls, 40)
            for item in events:
                yield item

        service = object.__new__(agent.AgentService)
        service._experiment_profile = "full"
        service._agent = object()
        service._adk = types.SimpleNamespace(tool_manager=Mock(get_stats=Mock(return_value={})))
        sessions = types.SimpleNamespace(
            create_session=AsyncMock(return_value=types.SimpleNamespace(id="local-test")),
            delete_session=AsyncMock())
        with contextlib.ExitStack() as stack:
            for name in ("set_database_uri", "reset_linked_schema", "reset_sql_execution_trace",
                         "reset_report_content", "reset_correction_events", "reset_final_sql",
                         "set_experiment_profile", "append_report_content"):
                stack.enter_context(patch.object(agent, name))
            stack.enter_context(patch.object(agent, "Runner", return_value=types.SimpleNamespace(run_async=run_async)))
            stack.enter_context(patch.object(agent, "InMemorySessionService", return_value=sessions))
            stack.enter_context(patch.object(agent, "_auto_quote_sql_identifiers", side_effect=lambda sql: sql))
            stack.enter_context(patch.object(agent, "get_sql_execution_trace", return_value=executions or []))
            stack.enter_context(patch.object(agent, "get_correction_events", return_value=[]))
            stack.enter_context(patch.object(agent, "get_final_sql", return_value=declared or {}))
            stack.enter_context(patch.object(agent, "create_model", side_effect=AssertionError("No model construction")))
            result = await service.run_query("question", "unused", "local-test", evidence="evidence", print_output=False)
        self.assertIsNone(service.last_run_diagnostics.get("execution_error"))
        sessions.delete_session.assert_awaited_once()
        return result

    async def test_batched_responses_preserve_query_identity_and_native_submission(self):
        city, school = "SELECT City FROM schools", "SELECT SchoolType FROM schools"
        ledger = [{"sql": city, "state": "accepted", "row_count": 2, "source": "sql_db_query"},
                  {"sql": school, "state": "accepted", "row_count": 18, "source": "sql_db_query"}]
        result = await self.replay([
            event(call("city", city), call("school", school)),
            event(response("city", accepted(2)), response("school", accepted(18))),
        ], executions=ledger, declared={"sql": city, "reasoning": "declared"})
        self.assertEqual([(r["sql"], r["state"], r["row_count"])
                          for r in result["sql_attempt_records"]],
                         [(city, "accepted", 2), (school, "accepted", 18)])
        self.assertEqual([r["tool_call_id"] for r in result["sql_attempt_records"]], ["city", "school"])
        self.assertEqual(result["sql_execution_trace"], ledger)
        self.assertEqual(result["submitted_final_sql"], city)
        self.assertEqual(result["generated_sql_source"], "submit_final_sql")
        self.assertEqual([(r["kind"], r["id"]) for r in result["tool_trace"]],
                         [("call", "city"), ("call", "school"), ("response", "city"), ("response", "school")])

    async def test_reverse_responses_use_ids(self):
        result = await self.replay([
            event(call("a", "SELECT a FROM t"), call("b", "SELECT b FROM t")),
            event(response("b", accepted(18)), response("a", "错误: SQL 执行失败")),
        ])
        self.assertEqual([(r["state"], r["row_count"]) for r in result["sql_attempt_records"]],
                         [("error", 0), ("accepted", 18)])
        self.assertFalse(result["initial_executable"])

    async def test_budget_denial_is_not_attached_to_executed_query(self):
        result = await self.replay([
            event(call("fourth", "SELECT district FROM t"), call("fifth", "SELECT status FROM t")),
            event(response("fourth", accepted(2)), response("fifth", "工具调用次数已达上限")),
        ])
        self.assertEqual([(r["state"], r["row_count"]) for r in result["sql_attempt_records"]],
                         [("accepted", 2), ("rejected", 0)])

    async def test_unmatched_and_duplicate_responses_never_overwrite_records(self):
        result = await self.replay([
            event(call("a", "SELECT a FROM t")), event(response("a", accepted(2))),
            event(response("a", "错误: SQL 执行失败")),
            event(call("b", "SELECT b FROM t")), event(response("unknown", accepted(99))),
        ])
        self.assertEqual([(r["state"], r["row_count"]) for r in result["sql_attempt_records"]],
                         [("accepted", 2), ("pending", 0)])

    async def test_duplicate_call_ids_are_ambiguous(self):
        result = await self.replay([
            event(call("same", "SELECT a FROM t"), call("same", "SELECT b FROM t")),
            event(response("same", accepted(2)), response("same", accepted(18))),
        ])
        self.assertEqual([r["state"] for r in result["sql_attempt_records"]], ["pending", "pending"])

    async def test_legacy_serial_calls_without_ids_remain_unverified(self):
        result = await self.replay([
            event(call(None, "SELECT a FROM t")), event(response(None, accepted(2))),
            event(call(None, "SELECT b FROM t")), event(response(None, accepted(18))),
        ])
        self.assertEqual([r["state"] for r in result["sql_attempt_records"]], ["pending", "pending"])
        self.assertEqual(len(result["tool_trace"]), 4)

    async def test_late_idless_duplicate_cannot_populate_the_next_call(self):
        result = await self.replay([
            event(call(None, "SELECT a FROM t")), event(response(None, accepted(2))),
            event(call(None, "SELECT b FROM t")), event(response(None, accepted(2))),
            event(response(None, accepted(18))),
        ])
        self.assertEqual([(r["state"], r["row_count"]) for r in result["sql_attempt_records"]],
                         [("pending", 0), ("pending", 0)])

    async def test_ambiguous_missing_ids_are_not_guessed(self):
        for calls in [(None, None), ("a", "b"), (None, "b")]:
            with self.subTest(calls=calls):
                result = await self.replay([
                    event(call(calls[0], "SELECT a FROM t"), call(calls[1], "SELECT b FROM t")),
                    event(response(None, accepted(2)), response(None, accepted(18))),
                ])
                self.assertEqual([r["state"] for r in result["sql_attempt_records"]], ["pending", "pending"])


if __name__ == "__main__":
    unittest.main()
