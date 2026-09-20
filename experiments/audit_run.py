"""Audit completed artifacts without gold, scores, SQL execution, or API calls.

Checks declared input/database/index/runtime isolation, raw submission provenance,
bounded attempts, and the arithmetic of recorded usage. Failed/timeout predictions
are valid terminal records. Agent skill/order deviations are observations rather
than damaged experiment data. Missing evidence cannot silently become a pass.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any

from experiments.prepare_dataset import DEFAULT_OUTPUT, sha256_file, write_json

SCHEMA = "aidb_generation_engineering_audit_v1"
GENERATION_KEYS = {"question_id", "question", "evidence", "db_id"}
INPUT_KEYS = GENERATION_KEYS | {"run_id", "attempt", "config"}
CONFIG_KEYS = {
    "max_llm_calls", "question_timeout_seconds", "sql_timeout_seconds", "env_file", "db_root",
    "index_table", "index_version", "temperature", "max_sql_query_calls", "request_timeout_seconds",
    "experiment_profile", "model_name",
}
TERMINAL = {"succeeded", "failed", "timeout"}
ACCEPTED = {"accepted", "accepted_with_warning"}
METRICS = {
    "prompt_tokens": "prompt_token_count", "completion_tokens": "candidates_token_count",
    "total_tokens": "total_token_count", "cached_tokens": "cached_content_token_count",
    "reasoning_tokens": "thoughts_token_count",
}
AGGREGATED = ("llm_calls", *METRICS, "duration_seconds")
FROZEN_KEYS = {"git_commit", "code_sha256", "questions_sha256", "subset", "dataset_manifest_sha256",
               "sqlite_runtime", "index_manifest_sha256", "index_manifest", "config"}


def digest_object(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def normalized_sql(value: str) -> str:
    """Mirror the frozen submission tool's acceptance check, not SQL equivalence."""
    return re.sub(r"\s+", " ", value.strip().rstrip(";").strip())


def _identifier_quote_normalized(sql: str) -> str:
    """Compare query-tool auto-quoting without erasing single-quoted values."""
    parts = []
    index = 0
    while index < len(sql):
        if sql[index] == "'":
            start = index
            index += 1
            while index < len(sql):
                if sql[index] == "'":
                    index += 1
                    if index < len(sql) and sql[index] == "'":
                        index += 1
                        continue
                    break
                index += 1
            parts.append(" literal_" + hashlib.sha256(sql[start:index].encode()).hexdigest() + " ")
        else:
            character = sql[index]
            if character not in ('"', "`", "[", "]"):
                parts.append(character.lower())
            index += 1
    return normalized_sql("".join(parts))


def raw_sql(value: dict[str, Any]) -> str:
    sql = value.get("submitted_final_sql") if "submitted_final_sql" in value else value.get("final_sql", "")
    if sql is None:
        return ""
    if not isinstance(sql, str):
        raise ValueError("Final SQL must be a string")
    return sql


def number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def equal_number(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return left is right
    return number(left) and number(right) and math.isclose(left, right, rel_tol=1e-10, abs_tol=1e-6)


def runtime_identity(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {key: value.get(key) for key in ("version", "dll_sha256", "archive_sha256")}


class Audit:
    def __init__(self):
        self.findings: list[dict[str, Any]] = []
        self.hashes: dict[str, str] = {}
        self.documents: dict[str, Any] = {}

    def note(self, severity: str, code: str, *, question_id=None, attempt=None, **details):
        item = {"severity": severity, "code": code}
        if question_id is not None:
            item["question_id"] = question_id
        if attempt is not None:
            item["attempt"] = attempt
        item.update(details)
        self.findings.append(item)

    def check(self, condition: bool, code: str, **details) -> bool:
        if not condition:
            self.note("error", code, **details)
        return condition

    def read(self, path: Path, *, jsonl: bool = False) -> Any:
        """Only callers with explicit generation/trace/manifest paths use this."""
        identity = str(path.resolve())
        if identity in self.documents:
            return self.documents[identity]
        raw = path.read_bytes()
        self.hashes[identity] = hashlib.sha256(raw).hexdigest()
        text = raw.decode("utf-8-sig")
        document = [json.loads(line) for line in text.splitlines() if line.strip()] if jsonl else json.loads(text)
        self.documents[identity] = document
        return document

    def bind_hash(self, path: Path) -> str:
        value = sha256_file(path)
        self.hashes[str(path.resolve())] = value
        return value


def audit_usage(audit: Audit, result: dict, checkpoint: dict | None, *, question_id: int,
                attempt: int, expected_model: str) -> dict[str, Any]:
    context = {"question_id": question_id, "attempt": attempt}
    usage = result.get("usage")
    if not isinstance(usage, dict) or not isinstance(usage.get("calls"), list):
        if result.get("llm_calls") not in (None, 0):
            audit.note("unverified", "per_call_usage_missing", **context)
        return {"calls": result.get("llm_calls"), "usage_complete": False, "per_call_verified": False}
    calls = usage["calls"]
    audit.check(all(isinstance(call, dict) for call in calls), "invalid_call_ledger", **context)
    if not all(isinstance(call, dict) for call in calls):
        return {"calls": None, "usage_complete": False, "per_call_verified": False}
    audit.check([call.get("call") for call in calls] == list(range(1, len(calls) + 1)), "call_sequence_not_contiguous", **context)
    audit.check(usage.get("llm_calls") == len(calls), "usage_call_count_mismatch", **context)
    if result.get("llm_calls") is not None:
        audit.check(result["llm_calls"] == len(calls), "worker_call_count_mismatch", **context)
    reported = [call for call in calls if isinstance(call.get("usage"), dict)]
    missing = len(calls) - len(reported)
    audit.check(usage.get("calls_without_usage") == missing, "missing_usage_count_mismatch", **context)
    audit.check(usage.get("usage_complete") == (missing == 0), "usage_completeness_mismatch", **context)
    for metric, provider_key in METRICS.items():
        values = [call["usage"].get(provider_key) for call in reported]
        audit.check(all(value is None or number(value) for value in values), "invalid_token_value", metric=metric, **context)
        expected = sum(value for value in values if number(value)) if values else None
        audit.check(equal_number(usage.get(metric), expected), "usage_token_sum_mismatch", metric=metric, **context)
        if metric in result:
            audit.check(equal_number(result[metric], expected), "worker_token_sum_mismatch", metric=metric, **context)
    missing_fields = Counter()
    for call in calls:
        model = call.get("model", "")
        audit.check(model in (expected_model, "openai/" + expected_model), "call_model_changed", **context)
        if call.get("duration_seconds") is not None:
            audit.check(number(call["duration_seconds"]), "invalid_call_duration", **context)
        details = call.get("usage")
        if isinstance(details, dict):
            for key in ("prompt_token_count", "candidates_token_count", "total_token_count"):
                if details.get(key) is None:
                    missing_fields[key] += 1
        if call.get("status") == "running":
            audit.note("observation", "interrupted_call_intent_retained", **context)
    if missing or missing_fields:
        audit.note("observation", "provider_usage_partly_unreported", missing_calls=missing,
                   missing_field_counts=dict(missing_fields), **context)
    if calls and checkpoint is None:
        audit.note("unverified", "usage_checkpoint_missing", **context)
    if checkpoint is not None:
        audit.check(checkpoint.get("question_id") == question_id and checkpoint.get("attempt") == attempt,
                    "checkpoint_question_or_attempt_mismatch", **context)
        audit.check(checkpoint.get("usage") == usage, "checkpoint_and_worker_usage_disagree", **context)
        if result.get("session_id"):
            audit.check(checkpoint.get("session_id") == result["session_id"], "checkpoint_session_mismatch", **context)
    return {"calls": len(calls), "usage_complete": missing == 0 and not missing_fields, "per_call_verified": True}


def _response_payload(response: Any) -> Any:
    if isinstance(response, dict) and set(response) == {"result"}:
        return response["result"]
    return response


def _accepted_query_response(response: Any) -> bool:
    if isinstance(response, dict):
        return response.get("state") in ACCEPTED
    if not isinstance(response, str):
        return False
    # These prefixes are emitted by the frozen native query tool, including
    # accepted_with_warning; ordinary empty-result rejection has another prefix.
    return response.lstrip().startswith(("✅ 查询成功", "⚠️ 查询已执行，但结果未通过语义过滤。"))


def audit_tool_trace(audit: Audit, result: dict, *, question_id: int, attempt: int,
                     expected_db: str, max_sql_calls: int) -> dict[str, Any]:
    context = {"question_id": question_id, "attempt": attempt}
    trace = result.get("trace") if isinstance(result.get("trace"), dict) else {}
    tools = trace.get("tool_trace") if isinstance(trace.get("tool_trace"), list) else []
    executions = trace.get("sql_execution_trace") if isinstance(trace.get("sql_execution_trace"), list) else []
    direct = [row for row in executions if isinstance(row, dict) and row.get("source") == "sql_db_query" and not row.get("is_probe")]
    audit.check(len(direct) <= max_sql_calls, "physical_sql_execution_budget_exceeded", **context)
    accepted = [row for row in direct if row.get("state") in ACCEPTED and isinstance(row.get("sql"), str)]
    accepted_by_sql = {normalized_sql(row["sql"]): row for row in accepted}
    stats = trace.get("tool_stats") or {}
    if isinstance(stats, dict) and stats.get("session_id"):
        audit.check(stats["session_id"] == result.get("session_id"), "tool_stats_session_mismatch", **context)
    if isinstance(stats, dict) and isinstance(stats.get("tool_call_counts"), dict):
        recorded_query_calls = stats["tool_call_counts"].get("sql_db_query", 0)
        audit.check(number(recorded_query_calls) and len(direct) <= recorded_query_calls <= max_sql_calls,
                    "native_sql_query_counter_outside_budget_or_ledger", **context)
    loaded = []
    pending: dict[str, tuple[int, dict]] = {}
    successful_submissions = []
    completed_queries = []
    preceding_accepted = []
    accepted_response_events = []
    sql_call_count = 0
    for event_index, event in enumerate(tools):
        if not isinstance(event, dict):
            audit.note("error", "malformed_tool_event", event_index=event_index, **context)
            continue
        kind, name, call_id = event.get("kind"), event.get("name"), event.get("id")
        if kind == "call":
            args = event.get("args") if isinstance(event.get("args"), dict) else {}
            if name == "load_skill":
                skill = args.get("skill_name")
                loaded.append(skill)
                if skill in {"data-link", "database-query-helper", "correct"} and loaded.count(skill) > 1:
                    audit.note("observation", "agent_reloaded_pipeline_skill", skill=skill, **context)
            if name == "sql_db_query":
                sql_call_count += 1
                if "correct" not in loaded:
                    audit.note("observation", "agent_query_before_correct_skill", event_index=event_index, **context)
            if name == "sql_db_value_lookup" and "correct" in loaded:
                audit.note("observation", "agent_retrieval_after_correct_skill", event_index=event_index, **context)
            if name in {"data-link", "database-query-helper", "correct"}:
                audit.note("observation", "agent_called_skill_as_tool", event_index=event_index, **context)
            if name == "submit_final_sql":
                sql = args.get("sql")
                if isinstance(sql, str) and not any(normalized_sql(sql) == normalized_sql(value) for value in completed_queries):
                    # A tool may reject this model action; that alone is not damaged data.
                    audit.note("observation", "agent_submission_without_prior_matching_query_call", event_index=event_index, **context)
            key = str(call_id) if call_id is not None else "unnamed:" + str(name)
            if key in pending:
                audit.note("unverified", "overlapping_tool_call_identity", event_index=event_index, **context)
            pending[key] = (event_index, event)
        elif kind == "response":
            key = str(call_id) if call_id is not None else "unnamed:" + str(name)
            pair = pending.pop(key, None)
            if pair is None:
                audit.note("unverified", "tool_response_without_call", event_index=event_index, **context)
                continue
            call_index, call = pair
            audit.check(call.get("name") == name, "tool_response_name_mismatch", event_index=event_index, **context)
            args = call.get("args") if isinstance(call.get("args"), dict) else {}
            response = _response_payload(event.get("response"))
            if name == "sql_db_query":
                sql = args.get("query", args.get("sql"))
                if isinstance(sql, str):
                    # The separate execution ledger supplies accepted status. The
                    # tool event pair supplies the before-submission ordering.
                    completed_queries.append(sql)
                if _accepted_query_response(response):
                    position = len(preceding_accepted)
                    if audit.check(position < len(accepted), "accepted_query_response_missing_from_execution_ledger", **context):
                        executed_sql = accepted[position]["sql"]
                        preceding_accepted.append(executed_sql)
                        accepted_response_events.append((event_index, executed_sql))
                        if isinstance(sql, str) and normalized_sql(sql) != normalized_sql(executed_sql):
                            if audit.check(_identifier_quote_normalized(sql) == _identifier_quote_normalized(executed_sql),
                                           "executed_sql_differs_beyond_identifier_quoting", event_index=event_index, **context):
                                audit.note("observation", "query_tool_transformed_execution_sql", event_index=event_index, **context)
            elif name == "build_linked_mschema" and isinstance(response, str):
                match = re.search(r"【DB_ID】\s*([^\r\n]+)", response)
                if match:
                    audit.check(match.group(1).strip() == expected_db, "linked_mschema_database_mismatch", **context)
                else:
                    audit.note("observation", "mschema_database_header_unobserved", **context)
            elif name == "submit_final_sql" and isinstance(response, dict) and response.get("status") == "success":
                sql = args.get("sql")
                audit.check(isinstance(sql, str) and response.get("final_sql") == sql, "tool_submission_raw_text_changed", **context)
                if not isinstance(sql, str):
                    continue
                accepted_row = accepted_by_sql.get(normalized_sql(sql))
                audit.check(accepted_row is not None, "successful_submission_not_in_accepted_execution_ledger", **context)
                preceding_match = any(normalized_sql(sql) == normalized_sql(value) for value in preceding_accepted)
                audit.check(preceding_match, "successful_submission_not_preceded_by_accepted_execution", **context)
                if preceding_match and not any(index < call_index and normalized_sql(sql) == normalized_sql(value)
                                               for index, value in accepted_response_events):
                    # ADK may announce a batch of calls before returning its
                    # responses. Response order alone cannot prove whether a
                    # concurrently announced submission started after execution.
                    audit.note("observation", "agent_submission_requested_before_query_feedback", **context)
                if preceding_accepted and normalized_sql(sql) != normalized_sql(preceding_accepted[-1]):
                    audit.note("observation", "agent_submitted_earlier_accepted_query", event_index=event_index, **context)
                if accepted_row and accepted_row["sql"] != sql:
                    audit.note("observation", "accepted_execution_matches_frozen_text_normalization_only", **context)
                successful_submissions.append(sql)
    final = raw_sql(result)
    if final.strip():
        audit.check(bool(successful_submissions), "final_sql_without_successful_tool_submission", **context)
        audit.check(bool(successful_submissions) and successful_submissions[-1] == final,
                    "final_sql_differs_from_last_raw_tool_submission", **context)
        audit.check(result.get("final_sql_source") == "submit_final_sql", "non_explicit_final_sql_source", **context)
        if "submitted_final_sql" in trace:
            audit.check(trace["submitted_final_sql"] == final, "trace_final_sql_mismatch", **context)
    elif successful_submissions:
        audit.note("error", "accepted_submission_lost_from_worker_result", **context)
    audit.check(result.get("status") != "succeeded" or bool(final.strip()), "successful_status_without_submission", **context)
    core = [skill for skill in loaded if skill in {"data-link", "database-query-helper", "correct"}]
    if tools and core != ["data-link", "database-query-helper", "correct"]:
        audit.note("observation", "agent_pipeline_skill_order_or_completion_deviation", core_skill_sequence=core, **context)
    if sql_call_count > len(direct):
        audit.note("observation", "sql_calls_denied_or_not_physically_executed", count=sql_call_count - len(direct), **context)
    if pending:
        audit.note("observation", "unfinished_tool_calls_in_terminal_worker", count=len(pending), **context)
    if len(preceding_accepted) < len(accepted):
        audit.note("observation", "accepted_execution_response_absent_from_terminal_trace", count=len(accepted)-len(preceding_accepted), **context)
    return {"physical_sql_execution_count": len(direct), "sql_tool_call_count": sql_call_count,
            "probe_execution_count": sum(bool(row.get("is_probe")) for row in executions if isinstance(row, dict)),
            "successful_submission_count": len(successful_submissions), "core_skill_sequence": core}


def _attempt_metric(result: dict, metric: str):
    usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
    return result.get(metric, usage.get(metric))


def _valid_output(run_dir: Path, output: Path) -> Path:
    root, target = run_dir.resolve(), output.resolve()
    if not target.is_relative_to(root) or target.suffix.lower() != ".json":
        raise ValueError("Audit output must be a JSON file inside the audited run directory")
    if any(part.lower() in {"traces", "generation", "retrieval", "evaluation"} for part in target.relative_to(root).parts):
        raise ValueError("Audit output must not overwrite generation, retrieval, trace, or evaluation inputs")
    if target.exists():
        raise FileExistsError("Audit output already exists; use --output with a fresh filename")
    return target


def audit_run(dataset_dir: Path, run_dir: Path, *, subset: str = "all",
              output: Path | None = None, timing_tolerance_seconds: float = 5) -> dict[str, Any]:
    if subset not in {"all", "smoke"}:
        raise ValueError("subset must be all or smoke")
    if not number(timing_tolerance_seconds) or timing_tolerance_seconds > 30:
        raise ValueError("Timing tolerance must be between 0 and 30 seconds")
    destination = _valid_output(run_dir, output or run_dir / "engineering_audit.json")
    audit = Audit()
    count = 30 if subset == "smoke" else 300
    questions_path = dataset_dir / "generation" / ("smoke_questions.jsonl" if subset == "smoke" else "questions.jsonl")
    manifest = audit.read(run_dir / "run_manifest.json")
    questions = audit.read(questions_path, jsonl=True)
    predictions = audit.read(run_dir / "predictions.jsonl", jsonl=True)
    frozen = {key: manifest[key] for key in FROZEN_KEYS if key in manifest}
    audit.check(set(frozen) == FROZEN_KEYS and digest_object(frozen) == manifest.get("fingerprint"), "frozen_manifest_fingerprint_mismatch")
    audit.check(manifest.get("subset") == subset and manifest.get("expected_questions") == count, "run_subset_or_expected_count_mismatch")
    audit.check(audit.hashes[str(questions_path.resolve())] == manifest.get("questions_sha256"), "generation_input_hash_mismatch")
    audit.check(audit.bind_hash(dataset_dir / "dataset_manifest.json") == manifest.get("dataset_manifest_sha256"), "dataset_metadata_hash_mismatch")
    ids = [row.get("question_id") for row in questions]
    prediction_ids = [row.get("question_id") for row in predictions]
    complete = len(questions) == count and len(predictions) == count and ids == prediction_ids and len(set(ids)) == count
    audit.check(complete, "run_incomplete_or_question_ids_mismatch", expected=count, generation_count=len(questions), prediction_count=len(predictions))
    audit.check(all(set(row) == GENERATION_KEYS and isinstance(row.get("question"), str) and isinstance(row.get("evidence"), str)
                    for row in questions), "generation_contains_answer_or_unexpected_fields")
    audit.check(all(isinstance(value, int) and not isinstance(value, bool) for value in ids), "invalid_question_id_type")
    config = manifest.get("config") or {}
    index = manifest.get("index_manifest") or {}
    expected_runtime = runtime_identity(manifest.get("sqlite_runtime"))
    audit.check(bool(expected_runtime.get("version")) and bool(expected_runtime.get("dll_sha256")), "frozen_runtime_identity_missing")
    audit.check(index.get("table") == config.get("index_table") and index.get("index_version") == config.get("index_version"), "frozen_index_identity_mismatch")
    max_calls = config.get("max_llm_calls", 40)
    max_seconds = config.get("question_timeout_seconds", 900)
    max_sql_calls = config.get("max_sql_query_calls", 4)
    audit.check(number(max_calls) and 0 < max_calls <= 40 and number(max_seconds) and 0 < max_seconds <= 900
                and number(max_sql_calls) and 0 < max_sql_calls <= 4, "frozen_budget_outside_plan")
    by_id = {row.get("question_id"): row for row in questions}
    sessions: dict[str, tuple[int, int]] = {}
    details = []
    terminal_count = 0
    observed_trace_files = set()
    for prediction in predictions:
        qid = prediction.get("question_id")
        if qid not in by_id or not isinstance(qid, int):
            continue
        context = {"question_id": qid}
        question = by_id[qid]
        audit.check(all(prediction.get(key) == question[key] for key in GENERATION_KEYS), "exported_question_or_database_changed", **context)
        audit.check(prediction.get("run_id") == manifest.get("run_id"), "prediction_run_id_mismatch", **context)
        terminal = prediction.get("status") in TERMINAL
        terminal_count += int(terminal)
        audit.check(terminal, "prediction_is_not_terminal", **context)
        last_attempt = prediction.get("attempt")
        if not audit.check(isinstance(last_attempt, int) and not isinstance(last_attempt, bool) and last_attempt >= 1, "invalid_attempt_number", **context):
            continue
        audit.check(prediction.get("attempt_count") == last_attempt, "attempt_count_mismatch", **context)
        attempts = []
        usage_checks = []
        tool_checks = []
        used_calls = used_seconds = 0
        for attempt in range(1, last_attempt + 1):
            attempt_context = {**context, "attempt": attempt}
            prefix = run_dir / "traces" / f"{qid}.attempt{attempt}"
            input_path, result_path, checkpoint_path = (Path(str(prefix) + suffix) for suffix in (".input.json", ".result.json", ".usage.json"))
            synthetic = (attempt == last_attempt and not input_path.exists() and not result_path.exists()
                         and prediction.get("error_category") in {"call_budget_exhausted", "timeout"}
                         and not raw_sql(prediction).strip() and (used_calls >= max_calls or used_seconds >= max_seconds))
            if synthetic:
                attempts.append({"status": prediction["status"], "submitted_final_sql": "", "llm_calls": 0})
                audit.note("observation", "scheduler_budget_terminal_without_worker_dispatch", **attempt_context)
                continue
            if not input_path.exists() or not result_path.exists():
                audit.note("unverified", "attempt_input_or_result_missing", input_present=input_path.exists(), result_present=result_path.exists(), **attempt_context)
                continue
            observed_trace_files.update((input_path.name, result_path.name))
            payload, result = audit.read(input_path), audit.read(result_path)
            checkpoint = audit.read(checkpoint_path) if checkpoint_path.exists() else None
            if checkpoint is not None:
                observed_trace_files.add(checkpoint_path.name)
            attempts.append(result)
            audit.check(set(payload) == INPUT_KEYS and isinstance(payload.get("config"), dict)
                        and not (set(payload.get("config", {})) - CONFIG_KEYS), "worker_input_contains_answer_or_unexpected_fields", **attempt_context)
            audit.check(all(payload.get(key) == question[key] for key in GENERATION_KEYS), "worker_input_question_or_database_changed", **attempt_context)
            audit.check(payload.get("run_id") == manifest.get("run_id") and payload.get("attempt") == attempt, "worker_input_run_or_attempt_mismatch", **attempt_context)
            audit.check(result.get("question_id") == qid and result.get("status") in TERMINAL, "worker_result_identity_or_status_mismatch", **attempt_context)
            if result.get("db_id") is not None:
                audit.check(result["db_id"] == question["db_id"], "worker_result_database_mismatch", **attempt_context)
            if result.get("attempt") is not None:
                audit.check(result["attempt"] == attempt, "worker_result_attempt_mismatch", **attempt_context)
            if result.get("run_id") is not None:
                audit.check(result["run_id"] == manifest.get("run_id"), "worker_result_run_id_mismatch", **attempt_context)
            worker_config = payload.get("config") or {}
            for key, default in (("index_table", None), ("index_version", None), ("temperature", 0),
                                 ("sql_timeout_seconds", 30), ("max_sql_query_calls", 4), ("experiment_profile", "full"),
                                 ("request_timeout_seconds", 120)):
                audit.check(worker_config.get(key, default) == config.get(key, default), "worker_frozen_config_changed", field=key, **attempt_context)
            for key in ("db_root", "env_file"):
                audit.check(bool(worker_config.get(key)) and bool(config.get(key)) and Path(worker_config[key]).resolve() == Path(config[key]).resolve(),
                            "worker_frozen_path_changed", field=key, **attempt_context)
            audit.check(worker_config.get("model_name") == config.get("model"), "worker_model_config_changed", **attempt_context)
            budget = worker_config.get("max_llm_calls")
            seconds = worker_config.get("question_timeout_seconds")
            audit.check(number(budget) and 0 < budget <= max_calls - used_calls, "retry_call_budget_not_reduced", **attempt_context)
            audit.check(number(seconds) and 0 < seconds <= max_seconds - used_seconds + timing_tolerance_seconds, "retry_time_budget_not_reduced", **attempt_context)
            for runtime in (result.get("sqlite_runtime"), (result.get("metadata") or {}).get("sqlite_runtime")):
                if runtime is not None:
                    audit.check(runtime_identity(runtime) == expected_runtime, "worker_sqlite_runtime_changed", **attempt_context)
            if not result.get("sqlite_runtime") and (result.get("llm_calls") or 0) > 0:
                audit.note("unverified", "worker_runtime_not_recorded", **attempt_context)
            for key, expected in (("index_version", config.get("index_version")), ("model", config.get("model"))):
                if result.get(key) is not None:
                    audit.check(result[key] == expected, "worker_reported_identity_changed", field=key, **attempt_context)
            session = result.get("session_id") or (checkpoint or {}).get("session_id")
            if session:
                identity = (qid, attempt)
                audit.check(session not in sessions or sessions[session] == identity, "session_reused_across_workers", **attempt_context)
                sessions[session] = identity
            elif (result.get("llm_calls") or 0) > 0:
                audit.note("unverified", "session_identity_unobserved_after_model_calls", **attempt_context)
            usage = audit_usage(audit, result, checkpoint, expected_model=config.get("model", ""), **attempt_context)
            usage_checks.append(usage)
            calls = usage["calls"]
            if calls is not None:
                audit.check(number(calls) and (not number(budget) or calls <= budget), "attempt_llm_budget_exceeded", **attempt_context)
                used_calls += calls
            else:
                audit.note("unverified", "attempt_call_count_unknown", **attempt_context)
            reserved = result.get("reserved_llm_calls", 0)
            audit.check(number(reserved), "invalid_reserved_call_count", **attempt_context)
            used_calls += reserved if number(reserved) else 0
            duration = result.get("duration_seconds")
            if duration is not None:
                audit.check(number(duration), "invalid_worker_duration", **attempt_context)
                if number(duration):
                    used_seconds += duration
                    audit.check(not number(seconds) or duration <= seconds + timing_tolerance_seconds, "attempt_wall_time_budget_exceeded", **attempt_context)
                    if number(seconds) and duration > seconds:
                        audit.note("observation", "worker_teardown_time_exceeds_query_budget", overage_seconds=duration-seconds, **attempt_context)
            else:
                audit.note("unverified", "worker_duration_unreported", **attempt_context)
            tool_checks.append(audit_tool_trace(audit, result, expected_db=question["db_id"], max_sql_calls=max_sql_calls, **attempt_context))
            if attempt == last_attempt:
                for key in ("status", "error_category", "session_id", "final_sql_source", "trace", "usage", "sqlite_runtime", "model", "index_version"):
                    audit.check(prediction.get(key) == result.get(key), "exported_final_worker_record_changed", field=key, **attempt_context)
                audit.check(raw_sql(prediction) == raw_sql(result), "exported_raw_submission_changed", **attempt_context)
                if "final_sql" in prediction:
                    audit.check(prediction["final_sql"] == raw_sql(prediction), "final_sql_alias_differs_from_raw_submission", **attempt_context)
            else:
                audit.check(not raw_sql(result).strip(), "retry_after_an_accepted_submission", **attempt_context)
                external_stops = {"insufficient_balance", "authentication"}
                allowed_retry = {"transient_api", "service_error", "connection_error", "rate_limit", "interrupted"} | external_stops
                audit.check(result.get("error_category") in allowed_retry, "retry_after_non_transport_failure", **attempt_context)
                if result.get("error_category") in external_stops:
                    audit.note("observation", "observed_resume_after_external_stop", stop_category=result["error_category"], **attempt_context)
        if len(attempts) == last_attempt:
            for metric in AGGREGATED:
                values = [_attempt_metric(result, metric) for result in attempts]
                audit.check(all(value is None or number(value) for value in values), "invalid_aggregate_metric", metric=metric, **context)
                known = sum(value for value in values if number(value))
                expected = known if all(value is not None for value in values) else None
                audit.check(equal_number(prediction.get(metric), expected), "exported_attempt_total_mismatch", metric=metric, **context)
                audit.check(equal_number(prediction.get(metric + "_known"), known), "exported_known_total_mismatch", metric=metric, **context)
            audit.check(prediction.get("usage_unknown", False) == any(result.get("usage_unknown", False) for result in attempts), "exported_usage_unknown_flag_mismatch", **context)
        audit.check(used_calls <= max_calls, "question_llm_budget_exceeded", **context)
        audit.check(used_seconds <= max_seconds + timing_tolerance_seconds * max(1, last_attempt), "question_wall_time_budget_exceeded", **context)
        details.append({"question_id": qid, "db_id": question["db_id"], "status": prediction.get("status"),
                        "submitted": bool(raw_sql(prediction).strip()), "attempt_count": last_attempt,
                        "verified_attempt_artifacts": len(attempts), "calls_including_reservations": used_calls,
                        "observed_worker_seconds": used_seconds, "usage": usage_checks, "tool_observations": tool_checks})
    audit.check(terminal_count == count, "run_not_fully_terminal", terminal_count=terminal_count, expected=count)
    retryable_failures = {"transient_api", "service_error", "connection_error", "rate_limit"}
    # Each question may have bounded transient retries; interrupted process
    # recovery remains constrained by its cumulative call and time budgets.
    for row in details:
        prior = []
        for attempt in range(1, row["attempt_count"]):
            path = run_dir / "traces" / f"{row['question_id']}.attempt{attempt}.result.json"
            if path.exists():
                prior.append(audit.read(path).get("error_category"))
        audit.check(sum(category in retryable_failures for category in prior) <= config.get("max_transient_retries", 3),
                    "transient_retry_budget_exceeded", question_id=row["question_id"])
    for suffix in ("*.input.json", "*.result.json", "*.usage.json"):
        for path in (run_dir / "traces").glob(suffix):
            if path.name not in observed_trace_files:
                audit.note("error", "unmatched_worker_trace_artifact", filename=path.name)
    # Snapshot consistency matters if a caller races an in-progress writer.
    for path, digest in list(audit.hashes.items()):
        audit.check(Path(path).is_file() and sha256_file(Path(path)) == digest, "artifact_changed_during_audit", filename=Path(path).name)
    errors = [row for row in audit.findings if row["severity"] == "error"]
    unverified = [row for row in audit.findings if row["severity"] == "unverified"]
    observations = [row for row in audit.findings if row["severity"] == "observation"]
    result = "failed" if errors else "incomplete_evidence" if unverified else "passed"
    report = {
        "schema": SCHEMA, "run_id": manifest.get("run_id"), "subset": subset,
        "audited_at_utc": datetime.now(timezone.utc).isoformat(), "result": result, "passed": result == "passed",
        "scope": "Generation inputs, frozen manifest, worker result/tool traces, and usage checkpoints only. No gold, scores, business database reads, SQL execution, or API calls.",
        "summary": {"expected_count": count, "record_count": len(predictions), "terminal_count": terminal_count,
                    "unique_sessions_observed": len(sessions), "submitted_count": sum(row["submitted"] for row in details),
                    "status_counts": dict(Counter(row["status"] for row in details)),
                    "database_count": len({row["db_id"] for row in details}),
                    "engineering_error_count": len(errors), "unverified_evidence_count": len(unverified),
                    "agent_and_runtime_observation_count": len(observations),
                    "all_recorded_usage_complete": all(usage["usage_complete"] for row in details for usage in row["usage"]),
                    "sqlite_runtime": expected_runtime, "index_version": config.get("index_version"), "index_table": config.get("index_table")},
        "engineering_findings": errors, "unverified_evidence": unverified, "observations": observations, "questions": details,
        "source_artifact_sha256": dict(sorted(audit.hashes.items())), "audit_code_sha256": sha256_file(Path(__file__)),
        "timing_tolerance_seconds": timing_tolerance_seconds,
        "limitations": [
            "Declared database and index identities are checked against the frozen inputs/results. Hidden retrieval-route candidates and physical connection state are not independently observed.",
            "No business schema or gold is read, so unqualified candidate/linked-column names are not independently checked against a database catalog.",
            "Usage arithmetic checks provider-reported fields; missing usage and an interrupted call intent are not invented as zero actual billing.",
            "Time-budget arithmetic covers recorded worker durations and dispatched remaining budgets; pauses/backoff outside worker records are not independently timed.",
            "SQL execution budget checks use direct non-probe sql_db_query records and the native query counter where present; separate diagnostic probes are counted as observations.",
            "A successful submission must match a recorded accepted execution under the frozen tool's whitespace/semicolon normalization; this is not a semantic-equivalence claim.",
            "Skill order, repeated skill loads, denied query calls, and earlier accepted submissions are agent behavior observations, not automatic engineering failures.",
            "Hashes bind this audit to the observed artifacts; they do not constitute a signature from an external trusted recorder.",
        ],
    }
    write_json(destination, report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--subset", choices=("smoke", "all"), default="all")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--timing-tolerance-seconds", type=float, default=5)
    args = parser.parse_args()
    report = audit_run(args.dataset_dir, args.run_dir, subset=args.subset, output=args.output,
                       timing_tolerance_seconds=args.timing_tolerance_seconds)
    print(json.dumps({"result": report["result"], **report["summary"]}, ensure_ascii=False))
    raise SystemExit(0 if report["passed"] else 3 if report["result"] == "incomplete_evidence" else 2)


if __name__ == "__main__":
    main()
