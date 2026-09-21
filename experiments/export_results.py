"""Export one audited, officially scored 300-question run without reading Gold.

Only generation metadata/questions, predictions, scores, the passing engineering
audit, and its allowlisted trace artifacts are read. No model, database, state,
registry selection, or evaluation is invoked.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any

from experiments import audit_run as engineering
from experiments.evaluate import aggregate_scores
from experiments.prepare_dataset import DEFAULT_MANIFEST, DEFAULT_OUTPUT, GENERATION_FIELDS, SOURCE_SHA256, sha256_file
from experiments.research_registry import validate_run

SCHEMA = "aidb_complete_question_sql_scores_v1"
TRACE_NAME = re.compile(r"(\d+)\.attempt([1-9]\d*)\.(input|result|usage)\.json")
SHA256 = re.compile(r"[0-9a-f]{64}")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def plain_file(path: Path) -> Path:
    # Ancestor worktree junctions are supported; individual input aliases are
    # unnecessary and could redirect a named generation artifact into Gold.
    require(not path.is_symlink(), f"Source file aliases are not allowed: {path}")
    resolved = path.resolve(strict=True)
    require(resolved.is_file(), f"Expected a source file: {path}")
    return resolved


class SourceSnapshot:
    """Hash and modification-time binding for every file actually consumed."""

    def __init__(self) -> None:
        self.files: dict[str, dict[str, Any]] = {}

    def read(self, path: Path) -> bytes:
        path = plain_file(path)
        before = path.stat()
        raw = path.read_bytes()
        after = path.stat()
        require((before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns),
                f"Source changed while being read: {path}")
        identity = {"sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw),
                    "mtime_ns": after.st_mtime_ns}
        previous = self.files.setdefault(str(path), identity)
        require(previous == identity, f"Source changed during export: {path}")
        return raw

    def document(self, path: Path, *, jsonl: bool = False) -> Any:
        text = self.read(path).decode("utf-8-sig")
        return [json.loads(line) for line in text.splitlines() if line.strip()] if jsonl else json.loads(text)

    def hash(self, path: Path) -> str:
        self.read(path)
        return self.files[str(path.resolve())]["sha256"]

    def verify(self) -> None:
        for name in list(self.files):
            self.read(Path(name))


def _destination(output: Path, run_dir: Path, dataset_dir: Path) -> Path:
    require(not os.path.lexists(output), "Output directory already exists; use a new directory")
    target = output.resolve()
    require(not target.is_relative_to(run_dir) and not target.is_relative_to(dataset_dir),
            "Output directory must be outside the source run and dataset")
    require(not run_dir.is_relative_to(target) and not dataset_dir.is_relative_to(target),
            "Output directory overlaps a source directory")
    return target


def _audit_bindings(audit: dict, predictions: list[dict], run_dir: Path,
                    dataset_dir: Path, snapshot: SourceSnapshot) -> dict[Path, str]:
    require(audit.get("schema") == engineering.SCHEMA and audit.get("passed") is True
            and audit.get("result") == "passed" and audit.get("subset") == "all",
            "A passing full-run engineering audit is required")
    require(audit.get("engineering_findings") == [] and audit.get("unverified_evidence") == [],
            "Engineering audit contains errors or unverified evidence")
    summary = audit.get("summary") or {}
    require(all(summary.get(key) == 300 for key in ("expected_count", "record_count", "terminal_count"))
            and summary.get("engineering_error_count") == 0 and summary.get("unverified_evidence_count") == 0,
            "Engineering audit is not a complete 300-question pass")
    # Do not grandfather an audit produced by a validator with known missing
    # checks. Re-auditing is artifact-only and never regenerates or rescores.
    require(audit.get("audit_code_sha256") == snapshot.hash(Path(engineering.__file__)),
            "Engineering audit was produced by different audit code; run the current audit first")
    details = audit.get("questions") or []
    require([row.get("question_id") for row in details] == [row["question_id"] for row in predictions],
            "Audit question IDs/order differ from predictions")
    for detail, prediction in zip(details, predictions):
        require(all(detail.get(key) == prediction.get(key) for key in
                    ("question_id", "db_id", "status", "attempt_count"))
                and detail.get("submitted") is bool(prediction["submitted_final_sql"].strip()),
                "Audit question metadata differs from predictions")
    require(summary.get("status_counts") == dict(Counter(row["status"] for row in predictions))
            and summary.get("submitted_count") == sum(bool(row["submitted_final_sql"].strip()) for row in predictions),
            "Engineering audit summary differs from predictions")

    allowed = {plain_file(path) for path in (run_dir / "run_manifest.json", run_dir / "predictions.jsonl",
               dataset_dir / "dataset_manifest.json", dataset_dir / "generation/questions.jsonl")}
    attempt_limits = {row["question_id"]: row["attempt"] for row in predictions}
    for path in (run_dir / "traces").iterdir():
        if not any(path.name.endswith(suffix) for suffix in (".input.json", ".result.json", ".usage.json")):
            continue
        match = TRACE_NAME.fullmatch(path.name)
        require(match is not None and int(match[1]) in attempt_limits
                and int(match[2]) <= attempt_limits[int(match[1])], "Unexpected worker trace artifact")
        resolved = plain_file(path)
        require(resolved.parent == (run_dir / "traces").resolve(), "Trace path escapes the source run")
        allowed.add(resolved)

    hashes = audit.get("source_artifact_sha256")
    require(isinstance(hashes, dict) and bool(hashes), "Engineering audit has no source hash bindings")
    bindings: dict[Path, str] = {}
    # Validate the entire path list before opening even the first audit target.
    for name, digest in hashes.items():
        require(isinstance(name, str) and Path(name).is_absolute(), "Audit source paths must be absolute")
        path = Path(name)
        require(not path.is_symlink(), "Audit source file aliases are forbidden")
        path = path.resolve()
        require(path in allowed and path not in bindings, "Audit source path is outside the generation allowlist")
        require(isinstance(digest, str) and SHA256.fullmatch(digest) is not None, "Invalid audit source hash")
        bindings[path] = digest
    require(set(bindings) == allowed, "Audit source bindings are missing or stale worker artifacts exist")
    for path, digest in bindings.items():
        require(snapshot.hash(path) == digest, f"Engineering audit source hash mismatch: {path.name}")
    return bindings


def export_results(run_dir: Path, output_dir: Path, *, dataset_dir: Path = DEFAULT_OUTPUT,
                   audit_path: Path | None = None) -> dict[str, Any]:
    run_dir, dataset_dir = Path(run_dir).resolve(strict=True), Path(dataset_dir).resolve(strict=True)
    target = _destination(Path(output_dir), run_dir, dataset_dir)
    # Resolve the selected roots once, then reject aliases below them (including
    # Windows junctions). Otherwise generation/ could redirect a safe filename
    # into evaluation/ before its JSON field guard has a chance to reject Gold.
    for path in (dataset_dir / "generation", dataset_dir / "generation/questions.jsonl",
                 dataset_dir / "dataset_manifest.json", run_dir / "traces",
                 *(run_dir / name for name in ("run_manifest.json", "predictions.jsonl", "scores.jsonl", "summary.json"))):
        require(path.resolve() == path, f"Source path aliases are not allowed: {path}")
    audit_path = Path(audit_path) if audit_path is not None else run_dir / "engineering_audit.json"
    require(audit_path.resolve().is_relative_to(run_dir)
            and not audit_path.resolve().is_relative_to(run_dir / "traces")
            and audit_path.suffix == ".json" and "gold" not in audit_path.name.lower(),
            "Audit must be a JSON report in the selected run, outside traces")
    snapshot = SourceSnapshot()
    selection = snapshot.document(DEFAULT_MANIFEST)
    ids = selection.get("question_ids") or []
    require(len(ids) == len(set(ids)) == 300 and all(type(qid) is int for qid in ids)
            and selection.get("source_sha256") == SOURCE_SHA256, "Invalid frozen 300-question selection")
    dataset = snapshot.document(dataset_dir / "dataset_manifest.json")
    questions = snapshot.document(dataset_dir / "generation/questions.jsonl", jsonl=True)
    manifest = snapshot.document(run_dir / "run_manifest.json")
    # Reject smoke/partial manifests before inspecting any detailed trace files.
    require(manifest.get("subset") == "all" and manifest.get("expected_questions") == 300,
            "Only a complete 300-question run can be exported; smoke is unsupported")
    require(dataset.get("question_ids") == ids and dataset.get("count") == 300
            and dataset.get("source_sha256") == SOURCE_SHA256
            and dataset.get("selection_manifest_sha256") == snapshot.hash(DEFAULT_MANIFEST),
            "Dataset metadata does not match the frozen selection")
    require([q.get("question_id") for q in questions] == ids
            and all(set(q) == set(GENERATION_FIELDS) and type(q["question_id"]) is int
                    and all(isinstance(q[key], str) for key in ("db_id", "question", "evidence")) for q in questions),
            "Generation questions, fields, or order differ from the fixed 300 inputs")
    question_hash = snapshot.hash(dataset_dir / "generation/questions.jsonl")
    require(manifest.get("questions_sha256") == question_hash
            and dataset.get("file_hashes", {}).get("generation/questions.jsonl") == question_hash
            and manifest.get("dataset_manifest_sha256") == snapshot.hash(dataset_dir / "dataset_manifest.json"),
            "Frozen generation/dataset hashes differ")

    predictions = snapshot.document(run_dir / "predictions.jsonl", jsonl=True)
    scores = snapshot.document(run_dir / "scores.jsonl", jsonl=True)
    summary = snapshot.document(run_dir / "summary.json")
    require([p.get("question_id") for p in predictions] == ids
            and all(type(p.get("question_id")) is int for p in predictions),
            "Predictions must contain the complete fixed 300 IDs/order")
    policy = {"question_ids": ids, "source_sha256": SOURCE_SHA256,
              "selection_manifest": {"sha256": snapshot.hash(DEFAULT_MANIFEST)},
              "required_matched_repeats": 2, "max_cost_ratio": manifest.get("config", {}).get("candidate_cost_ratio_limit")}
    reference, validated_scores = validate_run(run_dir / "scores.jsonl", policy, manifest.get("git_commit"))
    require(validated_scores == scores, "Scores changed during validation")
    require(all(summary.get(key) == value for key, value in aggregate_scores(scores).items()),
            "Official summary totals or groups disagree with score records")
    require(summary.get("timeout_seconds") == manifest["config"]["sql_timeout_seconds"],
            "Scoring timeout differs from the frozen run")
    for question, prediction, score in zip(questions, predictions, scores):
        require(all(prediction.get(key) == question[key] for key in GENERATION_FIELDS)
                and score.get("db_id") == question["db_id"], "Question, evidence, or database identity changed")
        require(type(prediction.get("attempt")) is int and prediction["attempt"] >= 1
                and prediction.get("attempt_count") == prediction["attempt"], "Invalid terminal attempt identity")
        sql = prediction["submitted_final_sql"]
        require("final_sql" not in prediction or prediction["final_sql"] == sql,
                "Final SQL alias differs from the original submission")

    audit = snapshot.document(audit_path)
    require(audit.get("run_id") == manifest.get("run_id"), "Engineering audit belongs to another run")
    _audit_bindings(audit, predictions, run_dir, dataset_dir, snapshot)
    trace_audit = engineering.Audit()
    for prediction in predictions:
        qid, attempt = prediction["question_id"], prediction["attempt"]
        result_path = run_dir / "traces" / f"{qid}.attempt{attempt}.result.json"
        if not result_path.exists():
            synthetic = any(note.get("code") == "scheduler_budget_terminal_without_worker_dispatch"
                            and note.get("question_id") == qid and note.get("attempt") == attempt
                            for note in audit.get("observations", []))
            require(synthetic and prediction.get("error_category") in {"call_budget_exhausted", "timeout"}
                    and prediction["status"] in {"failed", "timeout"}
                    and not prediction["submitted_final_sql"], "Final worker result is missing")
            continue
        result = snapshot.document(result_path)
        require(all(result.get(key) == prediction.get(key) for key in
                    ("question_id", "db_id", "run_id", "attempt", "status", "session_id",
                     "submitted_final_sql", "final_sql_source", "trace")),
                "Prediction differs from its original terminal worker result")
        engineering.audit_tool_trace(trace_audit, result, question_id=qid, attempt=attempt,
                                     expected_db=prediction["db_id"],
                                     max_sql_calls=manifest["config"].get("max_sql_query_calls", 4))
    require(not any(note["severity"] in {"error", "unverified"} for note in trace_audit.findings),
            "Original tool submission/accepted execution provenance is invalid")

    rows = [{**{key: prediction[key] for key in GENERATION_FIELDS},
             "run_id": manifest["run_id"], "status": prediction["status"],
             "submitted_final_sql": prediction["submitted_final_sql"],
             "final_sql_source": prediction.get("final_sql_source", ""),
             "error_category": prediction.get("error_category", ""), "attempt_count": prediction["attempt_count"],
             "official_ex": score["ex"], "evaluation_status": score["status"],
             "difficulty": score["difficulty"], "prediction_executable": score["prediction_executable"]}
            for prediction, score in zip(predictions, scores)]
    data = ("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)).encode("utf-8")
    snapshot.verify()
    output_manifest = {"schema": SCHEMA, "created_at_utc": datetime.now(timezone.utc).isoformat(),
                       "run_id": manifest["run_id"], "git_commit": reference["commit"],
                       "run_fingerprint": manifest["fingerprint"], "record_count": 300,
                       "official_metric": summary["metric"], "official_summary": aggregate_scores(scores),
                       "official_evaluator": summary["official_evaluator"],
                       "raw_submitted_sql_preserved_exactly": True, "no_gold_sql_in_export": True,
                       "no_model_requests_or_scoring": True, "source_bytes_and_mtime_unchanged": True,
                       "engineering_audit": {"path": str(audit_path.resolve()),
                                             "sha256": snapshot.files[str(audit_path.resolve())]["sha256"]},
                       "source_artifacts": dict(sorted(snapshot.files.items())),
                       "exporter_code_sha256": sha256_file(Path(__file__)),
                       "files": {"question_sql_scores.jsonl": {"sha256": hashlib.sha256(data).hexdigest(),
                                                               "size_bytes": len(data), "records": 300}}}
    target.parent.mkdir(parents=True, exist_ok=True)
    target.mkdir(exist_ok=False)
    created: list[Path] = []
    try:
        for name, content in (("question_sql_scores.jsonl", data),
                              ("manifest.json", (json.dumps(output_manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))):
            path = target / name
            with path.open("xb") as stream:
                created.append(path)
                stream.write(content)
        snapshot.verify()
    except BaseException:
        # Only the two files created by this invocation, never recursive cleanup.
        for path in created:
            path.unlink()
        target.rmdir()
        raise
    return output_manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--dataset-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--audit", type=Path, help="Passing current-code engineering audit inside the selected run")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    result = export_results(args.run_dir, args.output_dir, dataset_dir=args.dataset_dir, audit_path=args.audit)
    print(json.dumps({"run_id": result["run_id"], "record_count": result["record_count"],
                      "output_dir": str(args.output_dir.resolve()), "files": result["files"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
