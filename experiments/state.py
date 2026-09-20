"""Durable run/attempt bookkeeping and OS-released single-instance locks."""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
import json
import os
from pathlib import Path
import sqlite3
import time

TERMINAL = {"succeeded", "failed", "timeout"}


def checkpoint_usage(path: Path):
    if not path.exists():
        return {}
    record = json.loads(path.read_text(encoding="utf-8"))
    usage = record.get("usage", record)
    return {"usage": usage, "llm_calls": usage.get("llm_calls"),
            "prompt_tokens": usage.get("prompt_tokens"), "completion_tokens": usage.get("completion_tokens"),
            "usage_unknown": not usage.get("usage_complete", False)}


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def process_identity(pid: int | None) -> str | None:
    """Use process creation time to avoid treating a reused PID as our worker."""
    if not pid:
        return None
    if os.name == "nt":
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:
                return None
            values = [wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(handle, *[ctypes.byref(v) for v in values]):
                raise OSError("Cannot read worker process creation time")
            return str((values[0].dwHighDateTime << 32) | values[0].dwLowDateTime)
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        stat = Path(f"/proc/{pid}/stat")
        return stat.read_text().split(")", 1)[1].split()[19] if stat.exists() else str(pid)
    except ProcessLookupError:
        return None


@contextmanager
def single_instance(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b")
    acquired = False
    try:
        if path.stat().st_size == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        acquired = True
        yield
    finally:
        if acquired:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_UN)
        stream.close()


class State:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY, experiment_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL, phase TEXT NOT NULL,
                reason TEXT, created_at REAL NOT NULL, heartbeat REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS questions (
                run_id TEXT NOT NULL, question_id INTEGER NOT NULL,
                ordinal INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                attempt INTEGER NOT NULL DEFAULT 0, worker_pid INTEGER,
                worker_identity TEXT, lease_until REAL, result_json TEXT,
                PRIMARY KEY(run_id, question_id)
            );
            CREATE TABLE IF NOT EXISTS attempts (
                run_id TEXT NOT NULL, question_id INTEGER NOT NULL,
                attempt INTEGER NOT NULL, started_at REAL NOT NULL,
                ended_at REAL, status TEXT NOT NULL, result_json TEXT,
                PRIMARY KEY(run_id, question_id, attempt)
            );
        """)

    def initialize(self, run_id, experiment_id, fingerprint, questions):
        old = self.db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if old and old["fingerprint"] != fingerprint:
            raise ValueError("Run configuration/code/data changed; create a new run_id")
        ids = [q["question_id"] for q in questions]
        if len(set(ids)) != len(ids):
            raise ValueError("Duplicate question IDs")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO runs VALUES (?,?,?,?,?,?,?)",
                            (run_id, experiment_id, fingerprint, "PREFLIGHT", None, time.time(), time.time()))
            self.db.executemany("INSERT OR IGNORE INTO questions(run_id,question_id,ordinal) VALUES (?,?,?)",
                                [(run_id, qid, i) for i, qid in enumerate(ids)])
        actual = [r["question_id"] for r in self.rows(run_id)]
        if actual != ids:
            raise ValueError("Run question order changed")

    def phase(self, run_id, phase, reason=None):
        if reason is not None and not isinstance(reason, str):
            reason = json.dumps(reason, ensure_ascii=False, default=str)
        with self.db:
            self.db.execute("UPDATE runs SET phase=?,reason=?,heartbeat=? WHERE run_id=?",
                            (phase, reason, time.time(), run_id))

    def rows(self, run_id):
        return self.db.execute("SELECT * FROM questions WHERE run_id=? ORDER BY ordinal", (run_id,)).fetchall()

    def start(self, run_id, qid, timeout):
        with self.db:
            old = self.db.execute("SELECT * FROM questions WHERE run_id=? AND question_id=?", (run_id, qid)).fetchone()
            if old["status"] in TERMINAL or old["status"] == "running":
                raise ValueError("Question already completed or running")
            attempt = old["attempt"] + 1
            self.db.execute("UPDATE questions SET status='running',attempt=?,worker_pid=NULL,worker_identity=NULL,lease_until=? WHERE run_id=? AND question_id=?",
                            (attempt, time.time() + timeout + 60, run_id, qid))
            self.db.execute("INSERT INTO attempts VALUES (?,?,?,?,?,?,?)", (run_id, qid, attempt, time.time(), None, "running", None))
        return attempt

    def attach_worker(self, run_id, qid, pid):
        identity = process_identity(pid)
        with self.db:
            self.db.execute("UPDATE questions SET worker_pid=?,worker_identity=? WHERE run_id=? AND question_id=?", (pid, identity, run_id, qid))

    def heartbeat(self, run_id, qid, timeout):
        with self.db:
            self.db.execute("UPDATE runs SET heartbeat=? WHERE run_id=?", (time.time(), run_id))
            self.db.execute("UPDATE questions SET lease_until=? WHERE run_id=? AND question_id=?", (time.time() + timeout + 60, run_id, qid))

    def finish(self, run_id, qid, result, pending=False):
        status = "pending" if pending else result["status"]
        if status not in TERMINAL | {"pending"}:
            raise ValueError(f"Invalid terminal state: {status}")
        blob = json.dumps(result, ensure_ascii=False)
        with self.db:
            row = self.db.execute("SELECT attempt FROM questions WHERE run_id=? AND question_id=?", (run_id, qid)).fetchone()
            self.db.execute("UPDATE attempts SET ended_at=?,status=?,result_json=? WHERE run_id=? AND question_id=? AND attempt=?",
                            (time.time(), result["status"], blob, run_id, qid, row["attempt"]))
            self.db.execute("UPDATE questions SET status=?,result_json=?,worker_pid=NULL,worker_identity=NULL,lease_until=NULL WHERE run_id=? AND question_id=?",
                            (status, None if pending else blob, run_id, qid))

    def recover(self, run_id, run_dir):
        stop_category = None
        for row in self.rows(run_id):
            if row["status"] != "running":
                continue
            alive = process_identity(row["worker_pid"])
            if alive and (not row["worker_identity"] or alive == row["worker_identity"]):
                raise RuntimeError(f"Existing worker {row['worker_pid']} is still active; do not start another")
            output = run_dir / "traces" / f"{row['question_id']}.attempt{row['attempt']}.result.json"
            if output.exists():
                result = json.loads(output.read_text(encoding="utf-8"))
                result["question_id"] = row["question_id"]
                self.finish(run_id, row["question_id"], result,
                            pending=result.get("error_category") in {"insufficient_balance", "authentication"} and not result.get("submitted_final_sql"))
                if result.get("error_category") in {"insufficient_balance", "authentication"}:
                    stop_category = result["error_category"]
            else:
                # An interrupted model call may have been billed. Retain the attempt
                # and explicitly label its usage unknown, never erase its history.
                checkpoint = checkpoint_usage(run_dir / "traces" / f"{row['question_id']}.attempt{row['attempt']}.usage.json")
                elapsed = self.db.execute("SELECT started_at FROM attempts WHERE run_id=? AND question_id=? AND attempt=?",
                                          (run_id, row["question_id"], row["attempt"])).fetchone()[0]
                self.finish(run_id, row["question_id"], {"status": "failed", "submitted_final_sql": "",
                            "error_category": "interrupted", "usage_unknown": True,
                            "reserved_llm_calls": 0 if checkpoint else 40,
                            "duration_seconds": max(0, time.time() - elapsed), **checkpoint}, pending=True)
        return stop_category

    def export(self, run_id, questions, path):
        inputs = {q["question_id"]: q for q in questions}
        rows = []
        for row in self.rows(run_id):
            if row["status"] not in TERMINAL:
                continue
            value = {**inputs[row["question_id"]], **json.loads(row["result_json"])}
            value.update(question_id=row["question_id"], attempt=row["attempt"], run_id=run_id)
            attempts = self.db.execute("SELECT result_json FROM attempts WHERE run_id=? AND question_id=? AND result_json IS NOT NULL", (run_id, row["question_id"])).fetchall()
            value["attempt_count"] = len(attempts)
            for metric in ("llm_calls", "prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens", "reasoning_tokens", "duration_seconds"):
                values = [json.loads(a[0]).get(metric, json.loads(a[0]).get("usage", {}).get(metric)) for a in attempts]
                known_total = sum(v for v in values if v is not None)
                value[metric] = known_total if all(v is not None for v in values) else None
                value[metric + "_known"] = known_total
            value["usage_unknown"] = any(json.loads(a[0]).get("usage_unknown", False) for a in attempts)
            rows.append(value)
        temp = path.with_suffix(".tmp")
        with temp.open("w", encoding="utf-8", newline="\n") as stream:
            for value in rows:
                stream.write(json.dumps(value, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        return rows
