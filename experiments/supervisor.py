"""Persistent engineering-stage supervisor; research decisions stay explicit."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sqlite3
import sys
import time

from experiments.run_batch import LOCAL, ROOT, run_batch
from experiments.state import State, atomic_json, single_instance
from experiments.prepare_dataset import read_jsonl, sha256_file


@contextmanager
def keep_system_awake():
    """Inhibit idle system sleep only while supervised work is alive."""
    if os.name != "nt":
        yield
        return
    import ctypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.SetThreadExecutionState.argtypes = [ctypes.c_uint]
    kernel.SetThreadExecutionState.restype = ctypes.c_uint
    previous = kernel.SetThreadExecutionState(0x80000001)
    if not previous:
        raise OSError("Cannot request the process-local idle sleep inhibition")
    try:
        yield
    finally:
        kernel.SetThreadExecutionState(0x80000000)


def phase(run_id, value, reason=None):
    state = State(LOCAL / "state.sqlite")
    try:
        state.phase(run_id, value, reason)
    finally:
        state.db.close()


def completed_evaluation(args):
    """Validate historical artifacts without opening writable state or current env.

    summary.json is the evaluator's atomic completion boundary. A completed
    marker with missing/broken evidence fails closed instead of overwriting it.
    None means generation still needs ordinary recovery. A bound terminal
    generation without a summary can continue evaluation without regenerating.
    """
    if not args.run_id or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in args.run_id):
        raise ValueError("run_id must be a simple directory name")
    run_dir = LOCAL / "runs" / args.run_id
    action_path = run_dir / "next_action.json"
    action = json.loads(action_path.read_text(encoding="utf-8")) if action_path.exists() else {}
    persisted = None
    state_path = LOCAL / "state.sqlite"
    if state_path.exists():
        db = sqlite3.connect(state_path.resolve().as_uri() + "?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        try:
            row = db.execute("SELECT fingerprint,phase FROM runs WHERE run_id=?", (args.run_id,)).fetchone()
            if row:
                persisted = {**dict(row), "questions": [dict(q) for q in db.execute(
                    "SELECT question_id,status,ordinal,attempt,result_json FROM questions WHERE run_id=? ORDER BY ordinal", (args.run_id,))]}
        finally:
            db.close()
    declared_done = action.get("phase") == "DIAGNOSE" or (persisted or {}).get("phase") == "DIAGNOSE"
    summary_path = run_dir / "summary.json"
    if not summary_path.exists():
        if declared_done:
            raise ValueError("Completed run is missing its evaluation summary; refusing to overwrite artifacts")
        # Only durable, fully terminal generation can bypass run_batch's live
        # code/env checks. Incomplete generation retains its original path.
        if not persisted or not persisted["questions"] or any(q["status"] not in {"succeeded", "failed", "timeout"}
                                                               for q in persisted["questions"]):
            return None
        if not (run_dir / "run_manifest.json").exists() or not (run_dir / "predictions.jsonl").exists():
            return None
    from experiments.evaluate import METRIC, OFFICIAL_COMMIT, OFFICIAL_HASHES, aggregate_scores
    from experiments.compare import validate_scores
    manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
    summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else None
    dataset_path = args.dataset_dir / "dataset_manifest.json"
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    question_path = args.dataset_dir / "generation" / ("smoke_questions.jsonl" if args.subset == "smoke" else "questions.jsonl")
    questions = read_jsonl(question_path)
    predictions_path, scores_path = run_dir / "predictions.jsonl", run_dir / "scores.jsonl"
    predictions = read_jsonl(predictions_path)
    scores = read_jsonl(scores_path) if summary is not None else None
    expected = 30 if args.subset == "smoke" else 300
    def require(condition, message):
        if not condition:
            raise ValueError("Completed evaluation evidence invalid: " + message)
    frozen = {key: value for key, value in manifest.items()
              if key not in ("fingerprint", "run_id", "created_at", "expected_questions")}
    require(hashlib.sha256(json.dumps(frozen, sort_keys=True).encode()).hexdigest() == manifest.get("fingerprint"), "manifest fingerprint")
    require(manifest.get("run_id") == args.run_id and manifest.get("subset") == args.subset
            and manifest.get("expected_questions") == expected, "run/subset identity")
    require(bool(manifest.get("code_sha256")) and bool(manifest.get("index_manifest_sha256")), "frozen code/index provenance")
    require(manifest.get("dataset_manifest_sha256") == sha256_file(dataset_path)
            and manifest.get("questions_sha256") == sha256_file(question_path), "frozen dataset/questions hashes")
    ids = [q["question_id"] for q in questions]
    require(len(ids) == expected and len(set(ids)) == expected
            and ids == dataset["smoke_question_ids" if args.subset == "smoke" else "question_ids"], "dataset IDs/order")
    require([row["question_id"] for row in predictions] == ids, "complete prediction/score IDs/order")
    if scores is not None:
        require([row["question_id"] for row in scores] == ids, "complete prediction/score IDs/order")
        validate_scores(scores, expected)
    for index, (question, prediction) in enumerate(zip(questions, predictions)):
        require(prediction.get("run_id") == args.run_id and prediction.get("status") in {"succeeded", "failed", "timeout"}
                and all(prediction.get(key) == value for key, value in question.items()), "terminal prediction identity")
        sql = prediction.get("submitted_final_sql")
        require(isinstance(sql, str) and (not sql.strip() or prediction.get("final_sql_source") == "submit_final_sql"), "raw submission provenance")
        if scores is None:
            continue
        score = scores[index]
        require(score.get("sql_idx") == index and score.get("db_id") == question["db_id"]
                and score.get("generation_status") == prediction["status"]
                and score.get("submitted") is bool(sql.strip()), "score/prediction metadata")
        require(bool(sql.strip()) or (score["ex"] == 0 and score.get("status") == "missing_sql"
                                     and score.get("prediction_executable") is False), "empty submission scoring")
    if persisted:
        require(persisted["fingerprint"] == manifest["fingerprint"]
                and [q["question_id"] for q in persisted["questions"]] == ids
                and [q["status"] for q in persisted["questions"]] == [p["status"] for p in predictions], "durable generation state")
        for question, prediction in zip(persisted["questions"], predictions):
            result = json.loads(question["result_json"]) if question["result_json"] else {}
            require(question["attempt"] == prediction.get("attempt") and result.get("status") == prediction["status"]
                    and result.get("submitted_final_sql", "") == prediction["submitted_final_sql"], "durable final result")
    if summary is None:
        return {"summary": None, "action": None}
    require(summary.get("subset") == args.subset and summary.get("metric") == METRIC
            and all(summary.get(key) == expected for key in ("completed_generation_records", "scoring_denominator")), "complete evaluator denominator")
    require(summary.get("predictions_sha256") == sha256_file(predictions_path)
            and summary.get("scores_sha256") == sha256_file(scores_path), "evaluation artifact hashes")
    require(all(summary.get(key) == dataset.get(key) for key in ("source_sha256", "selection_manifest_sha256")), "evaluator dataset identity")
    evaluator = summary.get("official_evaluator", {})
    require(evaluator.get("commit") == OFFICIAL_COMMIT and evaluator.get("file_hashes") == OFFICIAL_HASHES
            and evaluator.get("metric") == METRIC, "official evaluator identity")
    runtime = manifest.get("sqlite_runtime", {})
    require(bool(runtime.get("dll_sha256")) and all(evaluator.get("sqlite_runtime", {}).get(key) == runtime.get(key)
            for key in ("version", "dll_sha256", "archive_sha256")), "frozen evaluator runtime")
    require(all(summary.get(key) == value for key, value in aggregate_scores(scores).items()), "score summary arithmetic")
    if action.get("phase") == "DIAGNOSE":
        require(action.get("run_id") == args.run_id and (run_dir / "diagnosis.md").is_file()
                and action.get("next") == ("review_smoke_then_freeze_B0" if args.subset == "smoke"
                                           else "inspect_evidence_then_research_single_hypothesis"), "diagnosis completion marker")
    return {"summary": summary, "action": action if action.get("phase") == "DIAGNOSE" else None}


def diagnose(run_dir):
    scores = [json.loads(line) for line in (run_dir / "scores.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    predictions = {r["question_id"]: r for r in [json.loads(line) for line in (run_dir / "predictions.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]}
    groups = {}
    for score in scores:
        if score["ex"]:
            continue
        prediction = predictions.get(score["question_id"], {})
        category = prediction.get("error_category") or ("execution_error" if score.get("error") else "semantic_unclassified")
        groups.setdefault(category, []).append(score["question_id"])
    lines = ["# Initial error triage", "", "These are engineering categories, not established causes of semantic errors.", "",
             f"Scored records: {len(scores)}. Correct: {sum(r['ex'] for r in scores)}.", ""]
    for category, ids in sorted(groups.items(), key=lambda pair: -len(pair[1])):
        lines.extend([f"- {category}: {len(ids)}; question IDs: {', '.join(map(str, ids))}"])
    lines.extend(["", "Next: inspect retrieval candidates, linked schema, tool traces and evaluator-only gold for the largest group. Only after evidence identifies a cause, read 1–3 primary papers and choose one controlled change. Never feed gold back to generation.", ""])
    if not (run_dir / "diagnosis.md").exists():
        (run_dir / "diagnosis.md").write_text("\n".join(lines), encoding="utf-8")
    return groups


def supervise(args):
    completed = completed_evaluation(args)
    if completed and completed["action"]:
        # This read-only path also works while another run owns supervisor.lock.
        print(json.dumps({**completed["action"], "evaluation_reused": True}), flush=True)
        return 0
    with single_instance(LOCAL / "supervisor.lock"), keep_system_awake():
        completed = completed_evaluation(args)
        if completed and completed["action"]:
            print(json.dumps({**completed["action"], "evaluation_reused": True}), flush=True)
            return 0
        from experiments.sqlite_runtime import runtime_metadata
        calibration = json.loads((LOCAL / "p0_selfcheck_sqlite3401" / "selfcheck.json").read_text(encoding="utf-8"))
        dataset = json.loads((args.dataset_dir / "dataset_manifest.json").read_text(encoding="utf-8"))
        if not calibration.get("passed") or calibration.get("count") != 300:
            raise ValueError("Full 300-question evaluator calibration must pass before supervised generation")
        if calibration.get("source_sha256") != dataset["source_sha256"] or calibration.get("selection_manifest_sha256") != dataset["selection_manifest_sha256"]:
            raise ValueError("Evaluator calibration does not match the frozen dataset")
        if calibration["official_evaluator"]["sqlite_runtime"]["dll_sha256"] != runtime_metadata()["dll_sha256"]:
            raise ValueError("Evaluator calibration runtime mismatch")
        control = LOCAL / "supervisor.json"
        if not completed:
            atomic_json(control, {"pid": os.getpid(), "run_id": args.run_id, "stage": "GENERATE", "updated_at": time.time()})
        recoveries = 0
        while True:
            result = ({"phase": "EVALUATE"} if completed else
                      run_batch(args.dataset_dir, args.run_id, args.config, args.subset))
            if result["phase"] != "waiting_service_recovery" or recoveries >= 2:
                break
            recoveries += 1
            # Existing local service scripts reuse healthy processes; no arbitrary restart.
            restored = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "deploy" / "start-services.ps1")],
                                      cwd=ROOT, capture_output=True, text=True, timeout=120,
                                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            atomic_json(LOCAL / "service_recovery.json", {"attempt": recoveries, "returncode": restored.returncode, "timestamp": time.time()})
            if restored.returncode:
                break
            time.sleep(10 * recoveries)
        run_dir = LOCAL / "runs" / args.run_id
        if result["phase"] != "EVALUATE":
            atomic_json(control, {**result, "pid": os.getpid(), "updated_at": time.time()})
            return 20 if result["phase"] == "stopped_insufficient_balance" else 10
        command = [sys.executable, "-m", "experiments.evaluate", "--dataset-dir", str(args.dataset_dir),
                   "--predictions", str(run_dir / "predictions.jsonl"), "--output-dir", str(run_dir)]
        if args.subset == "smoke":
            command.extend(["--subset", "smoke"])
        evaluation = None if completed and completed["summary"] is not None else subprocess.run(command, cwd=ROOT)
        if evaluation is not None and evaluation.returncode:
            phase(args.run_id, "waiting_evaluation_fix")
            atomic_json(control, {"run_id": args.run_id, "stage": "waiting_evaluation_fix", "updated_at": time.time()})
            return evaluation.returncode
        groups = diagnose(run_dir)
        phase(args.run_id, "DIAGNOSE")
        action = {"phase": "DIAGNOSE", "run_id": args.run_id,
                  "next": "review_smoke_then_freeze_B0" if args.subset == "smoke" else "inspect_evidence_then_research_single_hypothesis",
                  "error_category_counts": {key: len(ids) for key, ids in groups.items()},
                  "requires_user_confirmation": False, "owner": "active Codex Goal"}
        atomic_json(run_dir / "next_action.json", action)
        atomic_json(control, {**action, "pid": os.getpid(), "updated_at": time.time()})
        print(json.dumps(action), flush=True)
        return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--dataset-dir", type=Path, default=LOCAL / "dataset")
    parser.add_argument("--config", type=Path, default=ROOT / "experiments" / "experiment.example.json")
    parser.add_argument("--subset", choices=["all", "smoke"], default="all")
    args = parser.parse_args()
    if not args.run_id or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in args.run_id):
        parser.error("run_id must be a simple directory name")
    try:
        return supervise(args)
    except KeyboardInterrupt:
        phase(args.run_id, "stopped_user", "keyboard interrupt")
        return 130
    except Exception as exc:
        error_path = LOCAL / "supervisor_error.json"
        control_path = LOCAL / "supervisor.json"
        if control_path.exists():
            control = json.loads(control_path.read_text(encoding="utf-8"))
            if control.get("run_id") != args.run_id:
                # A rejected read-only check must not replace another run's
                # global monitoring state, including its current error record.
                error_path = LOCAL / "runs" / args.run_id / "supervisor_error.json"
        atomic_json(error_path, {"run_id": args.run_id, "error_type": type(exc).__name__,
                    "error": str(exc), "updated_at": time.time()})
        raise


if __name__ == "__main__":
    raise SystemExit(main())
