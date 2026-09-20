"""Paired, full-denominator comparison of frozen mini-dev experiment runs.

Pass matched repeat score paths in chronological order. All repeats are retained;
the tool never selects the best repeat or stitches question-level predictions.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import random
import statistics
from typing import Any

from experiments.prepare_dataset import read_jsonl, sha256_file, write_json


def validate_scores(rows: list[dict[str, Any]], expected_count: int = 300) -> None:
    if len(rows) != expected_count or len({row["question_id"] for row in rows}) != expected_count:
        raise ValueError(f"Every run must contain exactly {expected_count} unique scored IDs, including failures")
    for row in rows:
        if row.get("ex") not in (0, 1):
            raise ValueError(f"Invalid EX for question {row['question_id']}")
        if not row.get("db_id") or not row.get("difficulty"):
            raise ValueError("Scores must preserve db_id and difficulty")


def mcnemar_exact(wrong_to_right: int, right_to_wrong: int) -> float:
    discordant = wrong_to_right + right_to_wrong
    if not discordant:
        return 1.0
    tail = sum(math.comb(discordant, value) for value in range(min(wrong_to_right, right_to_wrong) + 1))
    return min(1.0, 2.0 * tail / (2 ** discordant))


def paired_metrics(baseline: list[dict[str, Any]], candidate: list[dict[str, Any]]) -> dict[str, Any]:
    if [row["question_id"] for row in baseline] != [row["question_id"] for row in candidate]:
        raise ValueError("Paired runs must have identical frozen IDs and order")
    for before, after in zip(baseline, candidate):
        if (before["db_id"], before["difficulty"]) != (after["db_id"], after["difficulty"]):
            raise ValueError(f"Pair metadata differs for question {before['question_id']}")
    improved = [after["question_id"] for before, after in zip(baseline, candidate) if before["ex"] == 0 and after["ex"] == 1]
    regressed = [after["question_id"] for before, after in zip(baseline, candidate) if before["ex"] == 1 and after["ex"] == 0]
    count = len(baseline)
    before_correct = sum(row["ex"] for row in baseline)
    after_correct = sum(row["ex"] for row in candidate)
    return {"count": count, "baseline_correct": before_correct, "candidate_correct": after_correct,
            "baseline_ex": before_correct / count if count else 0, "candidate_ex": after_correct / count if count else 0,
            "wrong_to_right": len(improved), "right_to_wrong": len(regressed), "net_correct": len(improved) - len(regressed),
            "delta_ex": (after_correct - before_correct) / count if count else 0,
            "delta_ex_percentage_points": 100 * (after_correct - before_correct) / count if count else 0,
            "improved_question_ids": improved, "regressed_question_ids": regressed,
            "mcnemar_exact_two_sided_p": mcnemar_exact(len(improved), len(regressed))}


def bootstrap_interval(differences: list[float], *, iterations: int = 10000, seed: int = 20260921) -> list[float]:
    if iterations < 100:
        raise ValueError("Use at least 100 paired bootstrap samples")
    if not differences:
        raise ValueError("Cannot bootstrap an empty paired set")
    rng = random.Random(seed)
    count = len(differences)
    means = sorted(sum(rng.choices(differences, k=count)) / count for _ in range(iterations))
    return [means[int((iterations - 1) * 0.025)], means[int((iterations - 1) * 0.975)]]


def cost_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    aliases = {
        "generation_seconds": ("duration_seconds", "elapsed_seconds", "latency_seconds"),
        "llm_calls": ("llm_calls", "api_calls"),
        "prompt_tokens": ("prompt_tokens", "input_tokens"),
        "completion_tokens": ("completion_tokens", "output_tokens"),
        "total_tokens": ("total_tokens",),
        "cost_usd": ("cost_usd", "cost"),
    }
    output: dict[str, Any] = {}
    for metric, names in aliases.items():
        values = []
        for row in rows:
            usage = row.get("usage", {}) if isinstance(row.get("usage"), dict) else {}
            value = next((row.get(name, usage.get(name)) for name in names if isinstance(row.get(name, usage.get(name)), (int, float))), None)
            if value is not None:
                values.append(value)
        output[metric] = {"total": sum(values) if values else None, "mean": statistics.mean(values) if values else None,
                          "reported_questions": len(values), "missing_questions": len(rows) - len(values)}
    output["estimated_usage_questions"] = sum(bool(row.get("usage_estimated") or
                                                   (row["usage"].get("estimated", False) if isinstance(row.get("usage"), dict) else False)) for row in rows)
    return output


def compare_runs(baseline_runs: list[list[dict[str, Any]]], candidate_runs: list[list[dict[str, Any]]],
                 *, expected_count: int = 300, bootstrap_samples: int = 10000, seed: int = 20260921,
                 checks_passed: bool = False, max_cost_ratio: float | None = None) -> dict[str, Any]:
    if not baseline_runs or len(baseline_runs) != len(candidate_runs):
        raise ValueError("Provide an equal, nonzero number of matched baseline/candidate repeats")
    if max_cost_ratio is not None and max_cost_ratio <= 0:
        raise ValueError("Cost ratio limit must be positive")
    repeated = []
    reference_ids = [row["question_id"] for row in baseline_runs[0]]
    for index, (baseline, candidate) in enumerate(zip(baseline_runs, candidate_runs)):
        validate_scores(baseline, expected_count)
        validate_scores(candidate, expected_count)
        if [row["question_id"] for row in baseline] != reference_ids:
            raise ValueError("Repeat IDs/order differ from the frozen set")
        metrics = paired_metrics(baseline, candidate)
        metrics["repeat"] = index + 1
        metrics["cost"] = {"baseline": cost_summary(baseline), "candidate": cost_summary(candidate)}
        for field in ("db_id", "difficulty"):
            groups = sorted({row[field] for row in baseline})
            metrics["by_" + field] = {group: paired_metrics([row for row in baseline if row[field] == group],
                                                            [row for row in candidate if row[field] == group]) for group in groups}
        repeated.append(metrics)
    count_repeats = len(repeated)
    differences = [statistics.mean(candidate[index]["ex"] - baseline[index]["ex"] for baseline, candidate in zip(baseline_runs, candidate_runs))
                   for index in range(expected_count)]
    delta_mean = statistics.mean(row["delta_ex"] for row in repeated)
    before_all = [row for run in baseline_runs for row in run]
    after_all = [row for run in candidate_runs for row in run]
    costs = {"baseline": cost_summary(before_all), "candidate": cost_summary(after_all)}
    before_cost = costs["baseline"]["cost_usd"]
    after_cost = costs["candidate"]["cost_usd"]
    complete_costs = before_cost["missing_questions"] == after_cost["missing_questions"] == 0
    ratio = after_cost["total"] / before_cost["total"] if complete_costs and before_cost["total"] else None
    cost_check = max_cost_ratio is None or (ratio is not None and ratio <= max_cost_ratio)
    score_errors = any(row.get("status") in ("gold_error", "worker_failed") for row in before_all + after_all)
    consistent_direction = all(row["net_correct"] > 0 for row in repeated)
    eligible = delta_mean > 0 and checks_passed and cost_check and not score_errors
    recommendation = "reject_or_diagnose"
    if eligible:
        recommendation = "eligible_after_matched_repeats" if count_repeats >= 2 and consistent_direction else "provisional_requires_matched_repeats"
    output: dict[str, Any] = {
        "metric": "official_mini_dev_EX_tuple_set_equality", "paired_question_count": expected_count, "matched_repeats": count_repeats,
        "mean_baseline_ex": statistics.mean(row["baseline_ex"] for row in repeated),
        "mean_candidate_ex": statistics.mean(row["candidate_ex"] for row in repeated),
        "mean_net_correct": statistics.mean(row["net_correct"] for row in repeated),
        "mean_delta_ex": delta_mean, "mean_delta_ex_percentage_points": 100 * delta_mean,
        "paired_bootstrap_95_ci_delta_ex": bootstrap_interval(differences, iterations=bootstrap_samples, seed=seed),
        "bootstrap_samples": bootstrap_samples, "bootstrap_seed": seed,
        "bootstrap_unit": "question ID; matched repeat differences averaged before resampling",
        "per_repeat": repeated, "cost": costs, "cost_ratio": ratio, "max_cost_ratio": max_cost_ratio,
        "engineering_checks_passed": checks_passed, "cost_check_passed": cost_check,
        "all_matched_repeats_improved": consistent_direction, "adoption_recommendation": recommendation,
        "milestone_net_six_reached": statistics.mean(row["net_correct"] for row in repeated) >= 6,
        "interpretation": "Development-set evidence only. Method search and reused questions invalidate claims of unseen-test generalization; no best-of-repeat selection.",
    }
    if count_repeats == 1:
        output.update({key: value for key, value in repeated[0].items() if key != "cost"})
    else:
        for field in ("db_id", "difficulty"):
            key = "by_" + field
            output[key] = {group: {"count_per_repeat": repeated[0][key][group]["count"],
                                   "mean_baseline_ex": statistics.mean(row[key][group]["baseline_ex"] for row in repeated),
                                   "mean_candidate_ex": statistics.mean(row[key][group]["candidate_ex"] for row in repeated),
                                   "mean_wrong_to_right": statistics.mean(row[key][group]["wrong_to_right"] for row in repeated),
                                   "mean_right_to_wrong": statistics.mean(row[key][group]["right_to_wrong"] for row in repeated),
                                   "mean_net_correct": statistics.mean(row[key][group]["net_correct"] for row in repeated)}
                           for group in repeated[0][key]}
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, nargs="+", required=True)
    parser.add_argument("--candidate", type=Path, nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-count", type=int, choices=(30, 300), default=300)
    parser.add_argument("--bootstrap-samples", type=int, default=10000)
    parser.add_argument("--seed", type=int, default=20260921)
    parser.add_argument("--checks-passed", action="store_true")
    parser.add_argument("--max-cost-ratio", type=float)
    args = parser.parse_args()
    # Guard against comparing runs with changed data, scoring timeout, or SQLite.
    # Score-only imports remain supported but are marked as unverified provenance.
    summaries = []
    for path in args.baseline + args.candidate:
        summary_path = path.parent / "summary.json"
        if summary_path.exists():
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            if summary.get("scores_sha256") != sha256_file(path):
                raise ValueError(f"Scores differ from their evaluator summary: {path}")
            summaries.append(summary)
    provenance_verified = len(summaries) == len(args.baseline) + len(args.candidate)
    if summaries:
        for field in ("source_sha256", "selection_manifest_sha256", "subset", "timeout_seconds"):
            if any(summary.get(field) != summaries[0].get(field) for summary in summaries[1:]):
                raise ValueError(f"Scoring provenance differs across paired runs: {field}")
        def evaluator_identity(summary):
            identity = dict(summary.get("official_evaluator", {}))
            if isinstance(identity.get("sqlite_runtime"), dict):
                runtime = dict(identity["sqlite_runtime"])
                runtime.pop("dll_path", None)  # Worktrees may locate the same bytes differently.
                identity["sqlite_runtime"] = runtime
            return identity
        if any(evaluator_identity(summary) != evaluator_identity(summaries[0]) for summary in summaries[1:]):
            raise ValueError("Scoring provenance differs across paired runs: official_evaluator")
    result = compare_runs([read_jsonl(path) for path in args.baseline], [read_jsonl(path) for path in args.candidate],
                          expected_count=args.expected_count, bootstrap_samples=args.bootstrap_samples,
                          seed=args.seed, checks_passed=args.checks_passed, max_cost_ratio=args.max_cost_ratio)
    result["input_artifacts"] = {"baseline": [{"path": str(path.resolve()), "sha256": sha256_file(path)} for path in args.baseline],
                                 "candidate": [{"path": str(path.resolve()), "sha256": sha256_file(path)} for path in args.candidate]}
    result["provenance_verified"] = provenance_verified
    if not provenance_verified and result["adoption_recommendation"] != "reject_or_diagnose":
        result["adoption_recommendation"] = "provisional_requires_provenance_verification"
    write_json(args.output, result)
    print(json.dumps({key: result[key] for key in ("paired_question_count", "matched_repeats", "mean_net_correct", "mean_delta_ex_percentage_points", "adoption_recommendation")}))


if __name__ == "__main__":
    main()
