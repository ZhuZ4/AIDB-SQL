"""Serial, process-isolated mini-dev generation with durable resumption."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from experiments.state import State, TERMINAL, atomic_json, checkpoint_usage, single_instance
from experiments.process_control import identity_alive, terminate_worker_tree, wait_worker_hello
from experiments.worker import CONFIG_KEYS

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / ".local-services" / "experiments"
TRANSIENT = {"transient_api", "service_error", "connection_error", "rate_limit"}
STOP = {"insufficient_balance": "stopped_insufficient_balance", "authentication": "waiting_authentication"}


def sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def code_snapshot():
    paths = [ROOT / "agent.py", ROOT / "utils.py", ROOT / "AGENTS.md"]
    for folder, pattern in (("tools", "*.py"), ("skills", "*"), ("experiments", "*.py"), ("deploy", "*.py")):
        paths.extend(p for p in (ROOT / folder).rglob(pattern) if p.is_file() and "__pycache__" not in p.parts)
    return {str(p.relative_to(ROOT)).replace("\\", "/"): sha(p) for p in sorted(set(paths))}


def load_config(path, frozen_index=None):
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    from dotenv import dotenv_values
    env = dotenv_values(value.get("env_file", ROOT / ".env"))
    if value.get("model") != "deepseek-v4.1-flash" or env.get("LITE_LLM_MODEL_NAME") != value["model"]:
        raise ValueError("Experiment must use the explicitly configured deepseek-v4.1-flash model")
    if value.get("concurrency", 1) != 1:
        raise ValueError("Initial experiments require serial process isolation")
    frozen_index = frozen_index or {}
    value["index_table"] = value.get("index_table") or frozen_index.get("index_table") or env.get("BIRD_DEV_COLUMN_TABLE")
    value["index_version"] = value.get("index_version") or frozen_index.get("index_version") or env.get("BIRD_DEV_INDEX_VERSION")
    value["provider_endpoint_sha256"] = hashlib.sha256(str(env.get("LITE_LLM_BASE_URL", "")).encode()).hexdigest()
    # Hash service configuration without exposing connection credentials. Index
    # table/version are explicit per-worker overrides and deliberately excluded.
    service_keys = ("LITE_LLM_BASE_URL", "LITE_LLM_MODEL_NAME", "EMBEDDING_API_URL", "EMBEDDING_MODEL",
                    "EMBEDDING_DIM", "BIRD_DEV_PG_URI", "BIRD_DEV_SCHEMA")
    value["services_sha256"] = hashlib.sha256(json.dumps({k: env.get(k) for k in service_keys}, sort_keys=True).encode()).hexdigest()
    if not value["index_version"] or value["index_table"] == "column_embeddings":
        raise ValueError("A complete, versioned 798-column index is required before generation")
    value["max_api_cost"] = None
    value["max_total_api_calls"] = None
    value["stop_on_insufficient_balance"] = True
    return value


def safe_configuration(config):
    sensitive = {"api_key", "password", "token", "secret", "base_url"}
    return {k: v for k, v in config.items() if not any(s in k.lower() for s in sensitive)}


def manifest_for(config, questions_path, subset):
    from experiments.sqlite_runtime import runtime_metadata
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    index_manifest_path = ROOT / ".local-services" / "column-index" / config["index_version"] / "manifest.json"
    index_manifest = json.loads(index_manifest_path.read_text(encoding="utf-8"))
    if index_manifest.get("counts", {}).get("columns") != 798 or index_manifest.get("table") != config["index_table"]:
        raise ValueError("Index manifest is not the complete frozen column index")
    return {"git_commit": commit, "code_sha256": code_snapshot(),
            "questions_sha256": sha(questions_path), "subset": subset,
            "dataset_manifest_sha256": sha(questions_path.parents[1] / "dataset_manifest.json"),
            "sqlite_runtime": runtime_metadata(),
            "index_manifest_sha256": sha(index_manifest_path), "index_manifest": index_manifest,
            "config": safe_configuration(config)}


def run_worker(payload, run_dir, timeout, state, run_id, qid, attempt):
    prefix = run_dir / "traces" / f"{qid}.attempt{attempt}"
    source = Path(str(prefix) + ".input.json")
    target = Path(str(prefix) + ".result.json")
    ready = Path(str(prefix) + ".ready.json")
    hello = Path(str(prefix) + ".hello.json")
    usage_checkpoint = Path(str(prefix) + ".usage.json")
    atomic_json(source, payload)
    env = os.environ.copy()
    env.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PYTHONUNBUFFERED="1")
    start = time.monotonic()
    worker_identity = None
    with Path(str(prefix) + ".log").open("w", encoding="utf-8") as log:
        proc = subprocess.Popen([sys.executable, "-m", "experiments.worker", "--input", str(source), "--output", str(target),
                                 "--dispatch-ready", str(ready), "--worker-hello", str(hello),
                                 "--usage-checkpoint", str(usage_checkpoint)],
                                cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                                start_new_session=os.name != "nt",
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        timed_out = False
        try:
            # Windows venv python.exe may be a redirector. The interpreter
            # publishes its actual PID before it is allowed to call the API.
            worker_identity = wait_worker_hello(hello, min(20, timeout))
            state.attach_worker(run_id, qid, worker_identity["worker_pid"])
            atomic_json(ready, {**worker_identity, "run_id": run_id, "question_id": qid, "attempt": attempt})
            next_heartbeat = 0.0
            while identity_alive(worker_identity):
                remaining = timeout - (time.monotonic() - start)
                if remaining <= 0:
                    timed_out = True
                    terminate_worker_tree(proc, worker_identity)
                    break
                if time.monotonic() >= next_heartbeat:
                    state.heartbeat(run_id, qid, timeout)
                    next_heartbeat = time.monotonic() + 2
                time.sleep(min(0.2, remaining))
            if proc.poll() is None:
                # A dead interpreter cannot make further API calls; reap any
                # remaining redirector without interpreting it as the worker.
                terminate_worker_tree(proc, worker_identity)
        except BaseException:
            terminate_worker_tree(proc, worker_identity)
            raise
    if target.exists() and not timed_out:
        result = json.loads(target.read_text(encoding="utf-8-sig"))
    else:
        result = {"status": "timeout" if timed_out else "failed", "submitted_final_sql": "",
                  "error_category": "timeout" if timed_out else "worker_crash",
                  "error": f"Worker exit code: {proc.returncode}", "usage_unknown": True}
        result.update(checkpoint_usage(usage_checkpoint))
    if result.get("status") not in TERMINAL:
        raise ValueError("Worker returned an invalid terminal status")
    result["duration_seconds"] = time.monotonic() - start
    result["question_id"] = qid
    atomic_json(target, result)
    return result


def run_batch(dataset_dir, run_id, config_path, subset="all", max_questions=None):
    if not run_id or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in run_id):
        raise ValueError("run_id must be a simple directory name")
    questions_path = dataset_dir / "generation" / ("smoke_questions.jsonl" if subset == "smoke" else "questions.jsonl")
    questions = [json.loads(line) for line in questions_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(questions) != (30 if subset == "smoke" else 300):
        raise ValueError("Incorrect fixed-set question count")
    for q in questions:
        if set(q) - {"question_id", "db_id", "question", "evidence"}:
            raise ValueError("Unexpected generation fields; gold leakage guard")
    run_dir = LOCAL / "runs" / run_id
    saved_manifest = run_dir / "run_manifest.json"
    previous_config = json.loads(saved_manifest.read_text(encoding="utf-8"))["config"] if saved_manifest.exists() else None
    config = load_config(config_path, previous_config)
    dataset = json.loads((dataset_dir / "dataset_manifest.json").read_text(encoding="utf-8"))
    relative_input = questions_path.relative_to(dataset_dir).as_posix()
    if sha(questions_path) != dataset["file_hashes"][relative_input]:
        raise ValueError("Generation input no longer matches the prepared dataset")
    expected_ids = dataset["smoke_question_ids" if subset == "smoke" else "question_ids"]
    if [q["question_id"] for q in questions] != expected_ids:
        raise ValueError("Generation input IDs/order changed")
    if Path(config["db_root"]).resolve() != Path(dataset["db_root"]).resolve():
        raise ValueError("Generation and evaluation database roots must match")
    for info in dataset["databases"].values():
        if sha(Path(info["path"])) != info["sha256"]:
            raise ValueError("Business SQLite file changed; new dataset series required")
    source = ROOT.parent / "minidev" / "MINIDEV" / "mini_dev_sqlite.json"
    expected = json.loads((ROOT / "experiments" / "mini_dev_300_manifest.json").read_text(encoding="utf-8"))["source_sha256"]
    if sha(source) != expected:
        raise ValueError("Source data version changed; new experiment series required")
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "traces").mkdir(exist_ok=True)
    frozen = manifest_for(config, questions_path, subset)
    fingerprint = hashlib.sha256(json.dumps(frozen, sort_keys=True).encode()).hexdigest()
    with single_instance(LOCAL / "generation.lock"):
        state = State(LOCAL / "state.sqlite")
        try:
            state.initialize(run_id, config["experiment_id"], fingerprint, questions)
            existing = run_dir / "run_manifest.json"
            if existing.exists():
                if json.loads(existing.read_text(encoding="utf-8"))["fingerprint"] != fingerprint:
                    raise ValueError("Frozen run manifest changed")
            else:
                atomic_json(existing, {**frozen, "fingerprint": fingerprint, "run_id": run_id,
                                      "created_at": time.time(), "expected_questions": len(questions)})
            recovered_stop = state.recover(run_id, run_dir)
            if recovered_stop:
                state.phase(run_id, STOP[recovered_stop])
                state.export(run_id, questions, run_dir / "predictions.jsonl")
                return {"run_id": run_id, "phase": STOP[recovered_stop]}
            state.phase(run_id, "GENERATE")
            completed_now = 0
            for row in state.rows(run_id):
                if row["status"] in TERMINAL:
                    continue
                if (LOCAL / "STOP").exists():
                    state.phase(run_id, "stopped_user")
                    return {"run_id": run_id, "phase": "stopped_user"}
                if max_questions is not None and completed_now >= max_questions:
                    state.phase(run_id, "GENERATE", "bounded diagnostic invocation")
                    return {"run_id": run_id, "phase": "GENERATE"}
                if shutil.disk_usage(LOCAL).free < 512 * 1024 * 1024:
                    state.phase(run_id, "waiting_disk_space")
                    return {"run_id": run_id, "phase": "waiting_disk_space"}
                # Enforce the frozen code manifest at every dispatch, including resume.
                if code_snapshot() != frozen["code_sha256"]:
                    state.phase(run_id, "waiting_code_changed")
                    return {"run_id": run_id, "phase": "waiting_code_changed"}
                if load_config(config_path, config) != config:
                    state.phase(run_id, "waiting_configuration_changed")
                    return {"run_id": run_id, "phase": "waiting_configuration_changed"}
                question = questions[row["ordinal"]]
                previous_attempts = state.db.execute("SELECT result_json FROM attempts WHERE run_id=? AND question_id=? AND result_json IS NOT NULL", (run_id, question["question_id"])).fetchall()
                previous_results = [json.loads(a[0]) for a in previous_attempts]
                retry = sum(r.get("error_category") in TRANSIENT for r in previous_results)
                used_calls = sum((r.get("llm_calls") or 0) + r.get("reserved_llm_calls", 0) for r in previous_results)
                used_seconds = sum(r.get("duration_seconds") or 0 for r in previous_results)
                while True:
                    attempt = state.start(run_id, question["question_id"], config["question_timeout_seconds"])
                    remaining_calls = config["max_llm_calls"] - used_calls
                    remaining_seconds = config["question_timeout_seconds"] - used_seconds
                    if remaining_calls <= 0 or remaining_seconds <= 0:
                        result = {"status": "failed" if remaining_calls <= 0 else "timeout", "submitted_final_sql": "",
                                  "error_category": "call_budget_exhausted" if remaining_calls <= 0 else "timeout", "llm_calls": 0}
                        state.finish(run_id, question["question_id"], result)
                        break
                    worker_config = {k: v for k, v in config.items() if k in CONFIG_KEYS}
                    worker_config.update(max_llm_calls=remaining_calls, model_name=config["model"], question_timeout_seconds=remaining_seconds)
                    payload = {**question, "run_id": run_id, "attempt": attempt, "config": worker_config}
                    result = run_worker(payload, run_dir, remaining_seconds, state,
                                        run_id, question["question_id"], attempt)
                    category = result.get("error_category")
                    used_calls += result.get("llm_calls") or 0
                    used_seconds += result.get("duration_seconds") or 0
                    if category in STOP:
                        state.finish(run_id, question["question_id"], result, pending=not bool(result.get("submitted_final_sql")))
                        phase = STOP[category]
                        state.phase(run_id, phase, result.get("error", ""))
                        state.export(run_id, questions, run_dir / "predictions.jsonl")
                        atomic_json(run_dir / "next_action.json", {"phase": phase, "resume_run_id": run_id,
                                    "question_id": question["question_id"], "reason": result.get("error")})
                        return {"run_id": run_id, "phase": phase}
                    if category in TRANSIENT and not result.get("submitted_final_sql") and result.get("retryable", False) and retry < config["max_transient_retries"]:
                        state.finish(run_id, question["question_id"], result, pending=True)
                        delay = config["retry_backoff_seconds"][min(retry, len(config["retry_backoff_seconds"]) - 1)]
                        retry += 1
                        time.sleep(delay)
                        used_seconds += delay
                        continue
                    state.finish(run_id, question["question_id"], result)
                    break
                completed_now += 1
                exported = state.export(run_id, questions, run_dir / "predictions.jsonl")
                atomic_json(run_dir / "progress.json", {"run_id": run_id, "completed": len(exported),
                            "total": len(questions), "last_question_id": question["question_id"],
                            "last_status": result["status"], "updated_at": time.time()})
                if len(exported) % 10 == 0 or len(exported) == 1:
                    print(json.dumps({"run_id": run_id, "completed": len(exported), "total": len(questions), "last_status": result["status"]}), flush=True)
                # Three consecutive infrastructure failures mean a real service issue.
                if len(exported) >= 3 and all(r.get("error_category") in TRANSIENT for r in exported[-3:]):
                    state.phase(run_id, "waiting_service_recovery")
                    return {"run_id": run_id, "phase": "waiting_service_recovery"}
            state.phase(run_id, "EVALUATE")
            return {"run_id": run_id, "phase": "EVALUATE", "completed": len(questions)}
        finally:
            state.db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-dir", type=Path, default=LOCAL / "dataset")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--config", type=Path, default=ROOT / "experiments" / "experiment.example.json")
    parser.add_argument("--subset", choices=["all", "smoke"], default="all")
    parser.add_argument("--max-questions", type=int, help="Bounded engineering diagnostic, never a complete run")
    args = parser.parse_args()
    result = run_batch(args.dataset_dir, args.run_id, args.config, args.subset, args.max_questions)
    print(json.dumps(result), flush=True)
    return 0 if result["phase"] == "EVALUATE" else 20 if result["phase"] == "stopped_insufficient_balance" else 10


if __name__ == "__main__":
    raise SystemExit(main())
