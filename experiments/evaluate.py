"""Read-only, fixed-denominator adapter for the pinned official mini-dev EX.

The official calculate_ex function is loaded directly from its hash-verified AST
to avoid importing unrelated MySQL/PostgreSQL drivers or their connection setup.
Each SQL pair executes in an isolated, time-bounded SQLite process. Result sets
use exactly the official tuple-set equality: order and duplicates are ignored.
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter, defaultdict
import json
import multiprocessing as mp
from pathlib import Path
import subprocess
import time
from typing import Any, Callable

from experiments.prepare_dataset import DEFAULT_OUTPUT, database_path, read_jsonl, sha256_file, verify_dataset, write_json, write_jsonl
from experiments.sqlite_runtime import bootstrap_sqlite_runtime, runtime_metadata

DEFAULT_OFFICIAL_DIR = Path("F:/data/VSCodeproject/mini_dev/evaluation")
OFFICIAL_COMMIT = "abd11b6db92a1c9f809b32f7564c7c71b34d67f0"
OFFICIAL_HASHES = {
    "evaluation_ex.py": "9cb8d82a2f181341d894484895f9b5e7bfabe66eec60688f77689303d5700a0f",
    "evaluation_utils.py": "496d3cf4900a29060a39034dd219fa22c96c972aecea268d3e849d718defa265",
}
# A deliberately invalid SELECT, independently of table names and database values.
# Empty queries and SELECT 1 can accidentally score as correct, so never use them.
MISSING_SQL = "SELECT FROM /* AIDB_MISSING_FINAL_SQL_8bd125 */"
METRIC = "official_mini_dev_EX_tuple_set_equality"


def load_official_calculator(official_dir: Path = DEFAULT_OFFICIAL_DIR, *, verify_commit: bool = True) -> tuple[Callable, dict[str, Any]]:
    for filename, expected in OFFICIAL_HASHES.items():
        if sha256_file(official_dir / filename) != expected:
            raise ValueError(f"Pinned official evaluator file changed: {filename}")
    if verify_commit:
        commit = subprocess.check_output(["git", "-C", str(official_dir.parent), "rev-parse", "HEAD"], text=True).strip()
        if commit != OFFICIAL_COMMIT:
            raise ValueError(f"Official evaluator revision changed: {commit}")
    module = ast.parse((official_dir / "evaluation_ex.py").read_text(encoding="utf-8"))
    functions = [node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == "calculate_ex"]
    if len(functions) != 1:
        raise ValueError("Expected exactly one official calculate_ex function")
    namespace: dict[str, Any] = {}
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(official_dir / "evaluation_ex.py"), "exec"), namespace)
    return namespace["calculate_ex"], {"commit": OFFICIAL_COMMIT, "file_hashes": OFFICIAL_HASHES, "metric": METRIC,
                                      "sqlite_runtime": runtime_metadata()}


def readonly_connection(path: Path, timeout_seconds: float = 30) -> sqlite3.Connection:
    import sqlite3
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=timeout_seconds)
    connection.execute("PRAGMA query_only = ON")
    allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}

    def authorize(action: int, first: str | None, second: str | None, database: str | None, trigger: str | None) -> int:
        if action not in allowed:
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_FUNCTION and (second or first or "").lower() in {"load_extension", "writefile", "readfile"}:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    connection.set_authorizer(authorize)
    deadline = time.monotonic() + timeout_seconds
    connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
    return connection


def execute_pair(predicted_sql: str, gold_sql: str, db_path: Path,
                 timeout_seconds: float = 30, official_dir: Path = DEFAULT_OFFICIAL_DIR) -> dict[str, Any]:
    """Worker body, also useful for small local tests; no result values are exported."""
    import sqlite3
    started = time.monotonic()
    stage = "prediction"
    result: dict[str, Any] = {"ex": 0, "status": "sql_error", "prediction_executable": False}
    connection = None
    try:
        calculate_ex, _ = load_official_calculator(official_dir, verify_commit=False)
        connection = readonly_connection(db_path, timeout_seconds)
        cursor = connection.cursor()
        cursor.execute(predicted_sql)
        if cursor.description is None:
            raise ValueError("Prediction must be a SELECT query with a result schema")
        predicted_columns = len(cursor.description)
        predicted = cursor.fetchall()
        result.update(prediction_executable=True, predicted_row_count=len(predicted), predicted_column_count=predicted_columns)
        stage = "gold"
        cursor.execute(gold_sql)
        gold_columns = len(cursor.description or ())
        gold = cursor.fetchall()
        result.update(ex=int(calculate_ex(predicted, gold)), status="scored", gold_row_count=len(gold), gold_column_count=gold_columns)
        # Auxiliary diagnostics, never substituted for the official EX.
        result["ordered_equal"] = predicted == gold
        result["multiset_equal"] = Counter(predicted) == Counter(gold)
        result["set_overlap_count"] = len(set(predicted) & set(gold))
    except Exception as error:
        timed_out = isinstance(error, sqlite3.OperationalError) and str(error) == "interrupted"
        result.update(status="timeout" if timed_out else ("gold_error" if stage == "gold" else "sql_error"),
                      error_type=type(error).__name__, error=str(error)[:1000], error_stage=stage)
    finally:
        if connection is not None:
            connection.close()
    result["execution_seconds"] = time.monotonic() - started
    return result


def _worker(pipe: Any, predicted_sql: str, gold_sql: str, db_path: str, timeout_seconds: float, official_dir: str) -> None:
    try:
        bootstrap_sqlite_runtime()
        pipe.send(execute_pair(predicted_sql, gold_sql, Path(db_path), timeout_seconds, Path(official_dir)))
    finally:
        pipe.close()


def evaluate_pair(predicted_sql: str, gold_sql: str, db_path: Path,
                  timeout_seconds: float = 30, official_dir: Path = DEFAULT_OFFICIAL_DIR) -> dict[str, Any]:
    """Hard process deadline complements SQLite's opcode-based soft timeout."""
    if timeout_seconds <= 0:
        raise ValueError("SQL timeout must be positive")
    context = mp.get_context("spawn")
    receive, send = context.Pipe(duplex=False)
    process = context.Process(target=_worker, args=(send, predicted_sql, gold_sql, str(db_path), timeout_seconds, str(official_dir)))
    started = time.monotonic()
    process.start()
    send.close()
    # Startup allowance is separate from the official SQL execution budget.
    deadline = started + timeout_seconds + 5
    result: dict[str, Any] | None = None
    try:
        while time.monotonic() < deadline:
            if receive.poll(min(0.1, max(0, deadline - time.monotonic()))):
                try:
                    result = receive.recv()
                except EOFError:
                    break
                break
            if not process.is_alive():
                break
        if result is None:
            result = {"ex": 0, "status": "timeout" if process.is_alive() else "worker_failed",
                      "prediction_executable": False, "error": "SQL worker exceeded deadline" if process.is_alive() else f"SQL worker exited ({process.exitcode})",
                      "execution_seconds": time.monotonic() - started}
    finally:
        if process.is_alive():
            process.join(timeout=0.2 if result is not None else 0)
        if process.is_alive():
            process.terminate()
            process.join(timeout=3)
        if process.is_alive():
            process.kill()
            process.join(timeout=3)
        receive.close()
        process.close()
    return result


def final_sql(record: dict[str, Any]) -> str:
    # Presence of submitted_final_sql, including an empty value, is authoritative.
    value = record.get("submitted_final_sql") if "submitted_final_sql" in record else record.get("final_sql", "")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("Final SQL must be the original submitted string")
    return value


def align_predictions(predictions: list[dict[str, Any]], records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    expected = {row["question_id"] for row in records}
    by_id: dict[int, dict[str, Any]] = {}
    for prediction in predictions:
        question_id = prediction.get("question_id")
        if question_id in by_id:
            raise ValueError(f"Duplicate prediction ID: {question_id}")
        if question_id not in expected:
            raise ValueError(f"Prediction ID outside the frozen scoring set: {question_id}")
        by_id[question_id] = prediction
    aligned = []
    for record in records:
        prediction = dict(by_id.get(record["question_id"], {"question_id": record["question_id"], "db_id": record["db_id"], "submitted_final_sql": "", "status": "missing_prediction"}))
        if prediction.get("db_id", record["db_id"]) != record["db_id"]:
            raise ValueError(f"Cross-database prediction for ID {record['question_id']}")
        prediction["db_id"] = record["db_id"]
        prediction["submitted_final_sql"] = final_sql(prediction)
        aligned.append(prediction)
    return aligned


def aggregate_scores(scores: list[dict[str, Any]]) -> dict[str, Any]:
    def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
        total = len(rows)
        correct = sum(row["ex"] for row in rows)
        submitted = sum(row.get("submitted", False) for row in rows)
        return {"count": total, "correct": correct, "ex": correct / total if total else 0,
                "ex_percent": 100 * correct / total if total else 0, "submitted": submitted,
                "submission_rate": submitted / total if total else 0,
                "executable": sum(row.get("prediction_executable", False) for row in rows),
                "timeout_count": sum(row.get("status") == "timeout" or row.get("generation_status") == "timeout" for row in rows),
                "status_counts": dict(Counter(row["status"] for row in rows)),
                "generation_status_counts": dict(Counter(row.get("generation_status", "unknown") for row in rows)),
                "evaluation_seconds": sum(row.get("execution_seconds", 0) for row in rows)}
    summary: dict[str, Any] = {"metric": METRIC, "overall": summarize(scores)}
    for field in ("db_id", "difficulty"):
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in scores:
            grouped[row[field]].append(row)
        summary["by_" + field] = {key: summarize(rows) for key, rows in sorted(grouped.items())}
    return summary


def evaluate_predictions(dataset_dir: Path, predictions_path: Path, output_dir: Path,
                         *, subset: str = "all", timeout_seconds: float = 30,
                         official_dir: Path = DEFAULT_OFFICIAL_DIR) -> dict[str, Any]:
    bootstrap_sqlite_runtime()
    dataset = verify_dataset(dataset_dir)
    _, provenance = load_official_calculator(official_dir)
    prefix = "smoke_" if subset == "smoke" else ""
    records = read_jsonl(dataset_dir / f"evaluation/{prefix}records.jsonl")
    predictions = align_predictions(read_jsonl(predictions_path), records)
    if len(predictions) != (30 if subset == "smoke" else 300):
        raise ValueError("Unexpected evaluation denominator")
    output_dir.mkdir(parents=True, exist_ok=True)
    official = {str(index): (final_sql(prediction) if final_sql(prediction).strip() else MISSING_SQL) + "\t----- bird -----\t" + record["db_id"]
                for index, (prediction, record) in enumerate(zip(predictions, records))}
    write_json(output_dir / "predictions_official.json", official)
    write_jsonl(output_dir / "scoring_id_map.jsonl", [{"sql_idx": index, "question_id": row["question_id"], "db_id": row["db_id"]} for index, row in enumerate(records)])
    # Export the exact aligned official inputs; never rely on the official zip truncation.
    for original, exported in ((f"{prefix}gold.sql", "gold.sql"), (f"{prefix}difficulty.jsonl", "difficulty.jsonl")):
        (output_dir / exported).write_bytes((dataset_dir / "evaluation" / original).read_bytes())
    scores = []
    scores_path = output_dir / "scores.jsonl"
    temporary = scores_path.with_suffix(".jsonl.partial")
    with temporary.open("w", encoding="utf-8") as stream:
        for index, (prediction, record) in enumerate(zip(predictions, records)):
            submitted = bool(final_sql(prediction).strip())
            if submitted:
                result = evaluate_pair(final_sql(prediction), record["gold_sql"], database_path(Path(dataset["db_root"]), record["db_id"]), timeout_seconds, official_dir)
            else:
                result = {"ex": 0, "status": "missing_sql", "prediction_executable": False, "execution_seconds": 0,
                          "error": "No raw final SQL was submitted; retained in the denominator"}
            row = {"sql_idx": index, "question_id": record["question_id"], "db_id": record["db_id"], "difficulty": record["difficulty"],
                   "submitted": submitted, "generation_status": prediction.get("status", "unknown"), **result}
            for field in ("duration_seconds", "elapsed_seconds", "latency_seconds", "llm_calls", "api_calls", "prompt_tokens", "completion_tokens", "input_tokens", "output_tokens", "total_tokens", "cached_tokens", "reasoning_tokens", "attempt_count", "usage_unknown", "cost", "cost_usd", "usage_estimated"):
                if field in prediction:
                    row[field] = prediction[field]
            if isinstance(prediction.get("usage"), dict):
                row["usage"] = prediction["usage"]
            scores.append(row)
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            stream.flush()
            if (index + 1) % 10 == 0:
                print(json.dumps({"evaluated": index + 1, "total": len(records), "correct_so_far": sum(value["ex"] for value in scores)}), flush=True)
    temporary.replace(scores_path)
    summary = aggregate_scores(scores)
    summary.update({"source_sha256": dataset["source_sha256"], "selection_manifest_sha256": dataset["selection_manifest_sha256"],
                    "official_evaluator": provenance, "subset": subset, "timeout_seconds": timeout_seconds,
                    "predictions_sha256": sha256_file(predictions_path), "scores_sha256": sha256_file(scores_path),
                    "completed_generation_records": len(read_jsonl(predictions_path)), "scoring_denominator": len(scores)})
    write_json(output_dir / "summary.json", summary)
    return summary


def selfcheck(dataset_dir: Path, output_dir: Path, *, timeout_seconds: float = 30,
              official_dir: Path = DEFAULT_OFFICIAL_DIR) -> dict[str, Any]:
    """Calibrate all 300 golds, valid wrong SQLs, and missing predictions."""
    bootstrap_sqlite_runtime()
    dataset = verify_dataset(dataset_dir)
    calculate_ex, provenance = load_official_calculator(official_dir)
    assert calculate_ex([(1,), (1,)], [(1,)]) == 1
    assert calculate_ex([(1,), (2,)], [(2,), (1,)]) == 1
    assert calculate_ex([(1,)], [(2,)]) == 0
    records = read_jsonl(dataset_dir / "evaluation/records.jsonl")
    output_dir.mkdir(parents=True, exist_ok=True)
    checks = []
    placeholder_checks = {}
    for db_id in dataset["databases"]:
        result = execute_pair(MISSING_SQL, "SELECT 1", database_path(Path(dataset["db_root"]), db_id), timeout_seconds, official_dir)
        placeholder_checks[db_id] = result["ex"] == 0 and result["status"] == "sql_error"
    for index, record in enumerate(records):
        db_path = database_path(Path(dataset["db_root"]), record["db_id"])
        gold = evaluate_pair(record["gold_sql"], record["gold_sql"], db_path, timeout_seconds, official_dir)
        # One extra result column guarantees inequality, including empty golds.
        wrong_sql = "SELECT " + ", ".join("'AIDB_DELIBERATELY_WRONG'" for _ in range(gold.get("gold_column_count", 1) + 1))
        wrong = evaluate_pair(wrong_sql, record["gold_sql"], db_path, timeout_seconds, official_dir)
        checks.append({"question_id": record["question_id"], "db_id": record["db_id"], "gold_ex": gold["ex"],
                       "gold_status": gold["status"], "gold_error": gold.get("error"), "wrong_ex": wrong["ex"],
                       "wrong_status": wrong["status"], "wrong_prediction_executable": wrong.get("prediction_executable", False)})
        if (index + 1) % 10 == 0:
            write_jsonl(output_dir / "selfcheck_scores.jsonl", checks)
            print(json.dumps({"selfcheck_completed": index + 1, "total": 300, "gold_correct": sum(row["gold_ex"] for row in checks)}), flush=True)
    missing_path = output_dir / "missing_predictions.jsonl"
    write_jsonl(missing_path, [])
    missing_summary = evaluate_predictions(dataset_dir, missing_path, output_dir / "missing", timeout_seconds=timeout_seconds, official_dir=official_dir)
    all_gold_pass = len(checks) == 300 and all(row["gold_ex"] == 1 for row in checks)
    all_wrong_pass = all(row["wrong_ex"] == 0 and row["wrong_prediction_executable"] and row["wrong_status"] == "scored" for row in checks)
    report = {"passed": all_gold_pass and all_wrong_pass and all(placeholder_checks.values()) and missing_summary["overall"]["count"] == 300 and missing_summary["overall"]["correct"] == 0,
              "count": 300, "gold_self_correct": sum(row["gold_ex"] for row in checks),
              "valid_wrong_rejected": sum(row["wrong_ex"] == 0 and row["wrong_prediction_executable"] and row["wrong_status"] == "scored" for row in checks),
              "missing_denominator": missing_summary["overall"]["count"], "missing_correct": missing_summary["overall"]["correct"],
              "placeholder_checks": placeholder_checks, "official_evaluator": provenance, "timeout_seconds": timeout_seconds,
              "source_sha256": dataset["source_sha256"], "selection_manifest_sha256": dataset["selection_manifest_sha256"],
              "database_sha256": {key: value["sha256"] for key, value in dataset["databases"].items()}}
    write_jsonl(output_dir / "selfcheck_scores.jsonl", checks)
    write_json(output_dir / "selfcheck.json", report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--predictions", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--subset", choices=("all", "smoke"), default="all")
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--official-dir", type=Path, default=DEFAULT_OFFICIAL_DIR)
    parser.add_argument("--selfcheck", action="store_true")
    args = parser.parse_args()
    if args.selfcheck:
        summary = selfcheck(args.dataset_dir, args.output_dir, timeout_seconds=args.timeout, official_dir=args.official_dir)
        print(json.dumps(summary, ensure_ascii=False), flush=True)
        raise SystemExit(0 if summary["passed"] else 1)
    if args.predictions is None:
        parser.error("--predictions is required without --selfcheck")
    summary = evaluate_predictions(args.dataset_dir, args.predictions, args.output_dir, subset=args.subset,
                                   timeout_seconds=args.timeout, official_dir=args.official_dir)
    print(json.dumps(summary["overall"], ensure_ascii=False), flush=True)


if __name__ == "__main__":
    mp.freeze_support()
    main()
