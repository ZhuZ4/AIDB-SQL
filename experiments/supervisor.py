"""Persistent engineering-stage supervisor; research decisions stay explicit."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from experiments.run_batch import LOCAL, ROOT, run_batch
from experiments.state import State, atomic_json, single_instance


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
    (run_dir / "diagnosis.md").write_text("\n".join(lines), encoding="utf-8")
    return groups


def supervise(args):
    with single_instance(LOCAL / "supervisor.lock"), keep_system_awake():
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
        atomic_json(control, {"pid": os.getpid(), "run_id": args.run_id, "stage": "GENERATE", "updated_at": time.time()})
        recoveries = 0
        while True:
            result = run_batch(args.dataset_dir, args.run_id, args.config, args.subset)
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
        evaluation = subprocess.run(command, cwd=ROOT)
        if evaluation.returncode:
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
    try:
        return supervise(args)
    except KeyboardInterrupt:
        phase(args.run_id, "stopped_user", "keyboard interrupt")
        return 130
    except Exception as exc:
        atomic_json(LOCAL / "supervisor_error.json", {"run_id": args.run_id, "error_type": type(exc).__name__,
                    "error": str(exc), "updated_at": time.time()})
        raise


if __name__ == "__main__":
    raise SystemExit(main())
