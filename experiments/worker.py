"""One isolated, read-only mini-dev prediction; never loads evaluation answers.

python -m experiments.worker --input question.json --output result.json
Question inputs: question_id, db_id, question, evidence, run_id, attempt, config.
Only question and evidence are sent to the model. Database/config fields control
the local process. Run one process per question: native SQL tools retain globals.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import redirect_stdout
import json
import logging
import os
from pathlib import Path
import re
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

INPUT_KEYS = {"question_id", "db_id", "question", "evidence", "run_id", "attempt", "config"}
CONFIG_KEYS = {
    "max_llm_calls", "question_timeout_seconds", "sql_timeout_seconds", "env_file",
    "db_root", "index_table", "index_version", "temperature", "max_sql_query_calls",
    "request_timeout_seconds", "experiment_profile", "model_name",
}


def classify_error(error: dict | str | None) -> tuple[str, bool]:
    """Separate provider balance exhaustion from retryable HTTP 429 throttling."""
    if not error:
        return "", False
    message = json.dumps(error, ensure_ascii=False, default=str).lower()
    status = error.get("status_code") if isinstance(error, dict) else None
    if any(token in message for token in (
        "insufficient_balance", "insufficient balance", "insufficient_quota",
        "quota exhausted", "quota has been exhausted", "credit balance",
        "credits exhausted", "out of credits", "billing_hard_limit", "余额不足",
        "额度耗尽", "余额已耗尽", "account balance is too low",
    )):
        return "insufficient_balance", False
    if status in (401, 403) or any(token in message for token in (
        "authenticationerror", "invalid_api_key", "invalid api key", "authentication failed",
        "incorrect api key", "permissiondeniederror", "unauthorized",
    )):
        return "authentication", False
    if any(token in message for token in (
        "llmcallbudgetexceeded", "llmcallslimitexceeded", "max_llm_calls",
        "maximum model calls reached", "maximum number of llm calls",
    )):
        return "llm_budget", False
    if "retrieval_service_error" in message:
        return "service_error", True
    if status == 429 or (isinstance(status, int) and status >= 500) or any(token in message for token in (
        "ratelimiterror", "rate limit", "too many requests", "apiconnectionerror",
        "apitimeouterror", "connection reset", "connection refused", "timed out",
        "serviceunavailableerror", "internalservererror", "remoteprotocolerror",
        "server disconnected", "badgatewayerror",
    )):
        return "transient_api", True
    if any(token in message for token in ("timeouterror", "question timeout", "cancelled")):
        return "timeout", False
    if any(token in message for token in ("moduleresponseerror", "modelresponseerror", "badrequesterror")):
        return "model_error", False
    return "service_error", False


def validate_input(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Worker input must be a JSON object")
    unexpected = set(payload) - INPUT_KEYS
    if unexpected:
        raise ValueError(f"Unsupported generation input keys: {sorted(unexpected)}")
    for name in ("question", "db_id"):
        if not isinstance(payload.get(name), str) or not payload[name].strip():
            raise ValueError(f"{name} must be a nonempty string")
    if not isinstance(payload.get("evidence", ""), str):
        raise ValueError("evidence must be a string")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", payload["db_id"]):
        raise ValueError("db_id must be a plain database identifier")
    config = payload.get("config") or {}
    if not isinstance(config, dict) or set(config) - CONFIG_KEYS:
        raise ValueError("Unsupported worker config keys")
    return config


def database_uri(db_root: str | Path, db_id: str) -> str:
    base = Path(db_root).resolve(strict=True)
    path = (base / db_id / f"{db_id}.sqlite").resolve(strict=True)
    if not path.is_relative_to(base) or not path.is_file():
        raise ValueError("SQLite source must be a file inside db_root")
    return f"sqlite:///{path.as_uri()}?mode=ro&uri=true"


def redact(value):
    """Redact locally configured credentials from errors/tool logs before saving."""
    secrets = [val for key, val in os.environ.items()
               if len(val) >= 6 and any(tag in key.upper() for tag in ("API_KEY", "PASSWORD", "SECRET", "TOKEN"))]
    def clean(item):
        if isinstance(item, str):
            for secret in secrets:
                item = item.replace(secret, "[REDACTED]")
            item = re.sub(r"(://[^\s/:@]+:)[^\s/@]+(@)", r"\1[REDACTED]\2", item)
            item = re.sub(r"(?i)(Bearer\s+)[A-Za-z0-9._-]+", r"\1[REDACTED]", item)
            return item
        if isinstance(item, dict):
            return {key: clean(val) for key, val in item.items()}
        if isinstance(item, (list, tuple)):
            return [clean(val) for val in item]
        return item
    return clean(value)


async def predict(payload: dict, usage_checkpoint: Path | None = None) -> dict:
    started = time.monotonic()
    result = {
        "question_id": payload.get("question_id"), "db_id": payload.get("db_id"),
        "run_id": payload.get("run_id"), "attempt": payload.get("attempt", 1),
        "session_id": f"minidev-{uuid.uuid4().hex}",
        "status": "failed", "submitted_final_sql": "", "final_sql": "",
        "final_sql_source": "", "error_category": "", "error": None,
        "retryable": False, "stop_reason": None, "trace": {},
        "llm_calls": 0, "prompt_tokens": None, "completion_tokens": None,
        "usage": {},
    }
    service = None
    tracker = None
    tracker_token = None
    native_tools = None
    try:
        config = validate_input(payload)
        from dotenv import load_dotenv
        env_path = Path(config.get("env_file", ROOT / ".env")).resolve(strict=True)
        load_dotenv(env_path, override=True)
        # Respect this project's user-selected provider and model; never fall back.
        model_name = os.environ.get("LITE_LLM_MODEL_NAME", "")
        if model_name != "deepseek-v4.1-flash":
            raise ValueError("Configured model must remain deepseek-v4.1-flash")
        if config.get("model_name", model_name) != model_name:
            raise ValueError("Experiment model_name does not match the configured model")
        if not os.environ.get("LITE_LLM_API_KEY") or not os.environ.get("LITE_LLM_BASE_URL"):
            raise ValueError("Missing SQL model API credentials/base URL")
        max_calls = int(config.get("max_llm_calls", 40))
        timeout = float(config.get("question_timeout_seconds", 900))
        sql_timeout = float(config.get("sql_timeout_seconds", 30))
        sql_calls = int(config.get("max_sql_query_calls", 4))
        if not 1 <= max_calls <= 40 or not 0 < timeout <= 900 or not 0 < sql_timeout <= 30 or not 1 <= sql_calls <= 4:
            raise ValueError("Question budgets exceed the frozen per-question limits")
        os.environ["LITE_LLM_TEMPERATURE"] = str(float(config.get("temperature", 0)))
        os.environ["SQL_QUERY_TIMEOUT_SECONDS"] = str(sql_timeout)
        os.environ["MAX_SQL_QUERY_CALLS"] = str(sql_calls)
        os.environ["LITE_LLM_REQUEST_TIMEOUT"] = str(float(config.get("request_timeout_seconds", 120)))
        os.environ["BIRD_DEV_DB_ID"] = payload["db_id"]
        os.environ["REQUIRE_HYBRID_RETRIEVAL"] = "1"
        if config.get("index_table"):
            os.environ["BIRD_DEV_COLUMN_TABLE"] = config["index_table"]
            os.environ["BIRD_DEV_INDEX_LAYOUT"] = "column_dual_v1"
        if config.get("index_version"):
            os.environ["BIRD_DEV_INDEX_VERSION"] = str(config["index_version"])
        uri = database_uri(config.get("db_root", os.environ.get("MINIDEV_ROOT", "") + "/dev_databases"), payload["db_id"])
        os.environ["DATABASE_URI"] = uri

        # Pin the same SQLite planner/runtime used by the calibrated evaluator.
        # This must happen before agent/langchain/SQLAlchemy can import sqlite3.
        from experiments.sqlite_runtime import bootstrap_sqlite_runtime
        result["sqlite_runtime"] = bootstrap_sqlite_runtime()
        result["metadata"] = {"sqlite_runtime": result["sqlite_runtime"]}
        from agent import AgentService
        from tools import native_sql_tools
        from utils import ModelUsageTracker, model_usage_tracker
        native_tools = native_sql_tools
        def save_usage(snapshot):
            if usage_checkpoint is None:
                return
            usage_checkpoint.parent.mkdir(parents=True, exist_ok=True)
            temporary = usage_checkpoint.with_name(usage_checkpoint.name + ".tmp")
            document = {"session_id": result["session_id"], "question_id": result["question_id"],
                        "attempt": result["attempt"], "updated_at": time.time(), "usage": snapshot}
            temporary.write_text(json.dumps(document, ensure_ascii=False, default=str), encoding="utf-8")
            os.replace(temporary, usage_checkpoint)
        tracker = ModelUsageTracker(max_calls=max_calls, on_update=save_usage)
        tracker.checkpoint()
        tracker_token = model_usage_tracker.set(tracker)
        service = AgentService(experiment_profile=config.get("experiment_profile", "full"))
        required_skills = {"data-link", "database-query-helper", "correct", "schema-exploration"}
        if required_skills - {s["name"] for s in service._adk.get_available_skills()}:
            raise ValueError("Required Text-to-SQL pipeline skills are missing")
        run = await asyncio.wait_for(service.run_query(
            question=payload["question"], evidence=payload.get("evidence", ""),
            database_uri=uri, session_id=result["session_id"], print_output=False,
            max_llm_calls=max_calls, preserve_context=False,
        ), timeout=timeout)
        result["trace"] = run
        result["error"] = run.get("execution_error")
        result["error_category"], result["retryable"] = classify_error(result["error"])
        # Never promote generated_sql/last_successful_sql heuristics to a prediction.
        declared = native_tools.get_final_sql(result["session_id"])
        submitted = declared.get("sql", "")
        result["submitted_final_sql"] = submitted
        result["final_sql"] = submitted
        if submitted:
            result["final_sql_source"] = "submit_final_sql"
            result["status"] = "succeeded"
        elif not result["error_category"]:
            result["error_category"] = "semantic" if run.get("sql_execution_trace") else "no_submission"
        result["model"] = model_name
        result["index_version"] = config.get("index_version")
    except asyncio.TimeoutError:
        result.update(status="timeout", error_category="timeout", error="Question timeout")
    except Exception as exc:
        result["error"] = {"type": type(exc).__name__, "message": str(exc),
                           "status_code": getattr(exc, "status_code", None), "code": getattr(exc, "code", None)}
        result["error_category"], result["retryable"] = classify_error(result["error"])
    finally:
        if service is not None and not result["trace"]:
            result["trace"] = dict(service.last_run_diagnostics)
        if native_tools is not None:
            sid = result["session_id"]
            result["trace"]["sql_execution_trace"] = native_tools.get_sql_execution_trace(sid)
            result["trace"]["correction_events"] = native_tools.get_correction_events(sid)
            result["trace"]["linked_schema"] = sorted(native_tools.get_linked_schema(sid))
            result["trace"]["linked_schema_snapshot"] = sorted(native_tools.get_linked_schema_snapshot(sid))
            # Preserve accepted submission even if a later natural-language answer times out.
            submitted = native_tools.get_final_sql(sid).get("sql", "")
            result["submitted_final_sql"] = submitted
            result["final_sql"] = submitted
            if submitted:
                result["final_sql_source"] = "submit_final_sql"
        if tracker is not None:
            result["usage"] = tracker.snapshot()
            for key in ("llm_calls", "prompt_tokens", "completion_tokens"):
                result[key] = result["usage"][key]
            model_usage_tracker.reset(tracker_token)
        if result["error_category"] == "insufficient_balance":
            result["stop_reason"] = "stopped_insufficient_balance"
        result["duration_seconds"] = round(time.monotonic() - started, 3)
    return redact(result)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--dispatch-ready", type=Path,
                        help="Wait for the scheduler's durable PID-registration handshake before any API call")
    parser.add_argument("--usage-checkpoint", type=Path,
                        help="Atomically persist call counts before requests and usage after each response")
    args = parser.parse_args()
    if args.dispatch_ready is not None:
        deadline = time.monotonic() + 20
        while not args.dispatch_ready.is_file():
            if time.monotonic() >= deadline:
                print(json.dumps({"status": "failed", "error_category": "dispatch_not_registered"}))
                return 3
            time.sleep(0.1)
    logging.basicConfig(level=logging.ERROR)
    # Third-party SDK banners/logs are local stderr; stdout stays machine-readable.
    with redirect_stdout(sys.stderr):
        payload = json.loads(args.input.read_text(encoding="utf-8-sig"))
        result = asyncio.run(predict(payload, usage_checkpoint=args.usage_checkpoint))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(args.output.name + ".tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    os.replace(temporary, args.output)
    print(json.dumps({"status": result["status"], "error_category": result["error_category"],
                      "output": str(args.output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
