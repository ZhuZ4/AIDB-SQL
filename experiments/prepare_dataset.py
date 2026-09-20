"""Prepare the frozen mini-dev development set without exposing gold to generation.

Only ``generation/questions.jsonl`` (or smoke_questions.jsonl) may be passed to
the model runner. The sibling evaluation directory is evaluator-only material.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import re
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = PROJECT_ROOT / "experiments/mini_dev_300_manifest.json"
DEFAULT_OUTPUT = PROJECT_ROOT / ".local-services/experiments/dataset"
DEFAULT_DB_ROOT = Path("F:/data/VSCodeproject/minidev/MINIDEV/dev_databases")
SOURCE_SHA256 = "def4b2b43a9b06955193418f24c9be170eb6d83763ab702311790e0cdda8c791"
GENERATION_FIELDS = ("question_id", "db_id", "question", "evidence")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def write_jsonl(path: Path, values: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in values), encoding="utf-8")
    temporary.replace(path)


def database_path(db_root: Path, db_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_]+", db_id):
        raise ValueError(f"Unsafe database ID: {db_id!r}")
    path = db_root.resolve() / db_id / (db_id + ".sqlite")
    if not path.is_file():
        raise FileNotFoundError(path)
    return path


def proportional_quotas(counts: dict[tuple[str, str], int], total: int) -> dict[tuple[str, str], int]:
    denominator = sum(counts.values())
    quotas = {key: count * total // denominator for key, count in counts.items()}
    remainder_order = sorted(counts, key=lambda key: (-(counts[key] * total % denominator), key))
    for key in remainder_order[:total - sum(quotas.values())]:
        quotas[key] += 1
    return quotas


def select_ids(rows: list[dict[str, Any]], total: int, seed: str) -> list[int]:
    strata: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        strata[(row["db_id"], row["difficulty"])].append(row)
    quotas = proportional_quotas({key: len(values) for key, values in strata.items()}, total)
    chosen: set[int] = set()
    for key, values in strata.items():
        ordered = sorted(values, key=lambda row: (hashlib.sha256(f"{seed}:{row['question_id']}".encode()).hexdigest(), row["question_id"]))
        chosen.update(row["question_id"] for row in ordered[:quotas[key]])
    return [row["question_id"] for row in rows if row["question_id"] in chosen]


def validate_source(manifest_path: Path, source_path: Path | None = None) -> tuple[dict[str, Any], list[dict[str, Any]], Path]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    source = source_path or Path(manifest["source_path"])
    if manifest["source_sha256"] != SOURCE_SHA256 or sha256_file(source) != SOURCE_SHA256:
        raise ValueError("Source hash changed: create a new experiment series before proceeding")
    rows = json.loads(source.read_text(encoding="utf-8"))
    ids = [row["question_id"] for row in rows]
    if len(rows) != 500 or len(set(ids)) != 500 or manifest["source_count"] != 500:
        raise ValueError("Expected exactly 500 unique source question IDs")
    selected_ids = manifest["question_ids"]
    if manifest["selected_count"] != 300 or len(selected_ids) != 300 or len(set(selected_ids)) != 300:
        raise ValueError("Manifest must contain exactly 300 unique question IDs")
    if select_ids(rows, 300, manifest["seed"]) != selected_ids:
        raise ValueError("Manifest IDs/order do not match the frozen stratified selection")
    selected = [row for row in rows if row["question_id"] in set(selected_ids)]
    expected_difficulty = {"simple": 89, "moderate": 150, "challenging": 61}
    if dict(Counter(row["difficulty"] for row in selected)) != expected_difficulty:
        raise ValueError("Frozen difficulty distribution changed")
    if len({row["db_id"] for row in selected}) != 11:
        raise ValueError("Frozen set must cover all 11 databases")
    return manifest, selected, source


def audit_original_gold(source: Path) -> dict[str, Any]:
    """Report source-text discrepancies, never use or rewrite the legacy gold."""
    original = source.with_name("mini_dev_sqlite_gold.sql")
    if not original.exists():
        return {"path": str(original), "status": "not_present"}
    rows = json.loads(source.read_text(encoding="utf-8"))
    lines = original.read_text(encoding="utf-8").splitlines()
    malformed, different = [], []
    normalize = lambda sql: " ".join(sql.strip().rstrip(";").split())
    for index, line in enumerate(lines):
        if "\t" not in line:
            malformed.append(index + 1)
            continue
        sql, db_id = line.rsplit("\t", 1)
        if index >= len(rows) or normalize(sql) != normalize(rows[index]["SQL"]) or db_id != rows[index]["db_id"]:
            different.append(index + 1)
    return {"path": str(original), "sha256": sha256_file(original), "line_count": len(lines),
            "malformed_lines": malformed, "text_mismatch_lines": different,
            "source_of_truth": "locked mini_dev_sqlite.json; text mismatches are not semantic claims"}


def prepare_dataset(manifest_path: Path = DEFAULT_MANIFEST, output_dir: Path = DEFAULT_OUTPUT,
                    source_path: Path | None = None, db_root: Path = DEFAULT_DB_ROOT) -> dict[str, Any]:
    manifest, rows, source = validate_source(manifest_path, source_path)
    output_dir = output_dir.resolve()
    smoke_seed = manifest["seed"] + ":smoke-v1"
    smoke_ids = select_ids(rows, 30, smoke_seed)
    smoke = [row for row in rows if row["question_id"] in set(smoke_ids)]
    if len({row["db_id"] for row in smoke}) != 11 or len({row["difficulty"] for row in smoke}) != 3:
        raise ValueError("Smoke selection does not cover all databases/difficulties")
    db_files = {db_id: database_path(db_root, db_id) for db_id in sorted({row["db_id"] for row in rows})}
    database_hashes = {db_id: {"path": str(path), "sha256": sha256_file(path), "size_bytes": path.stat().st_size}
                       for db_id, path in db_files.items()}
    generation = [{key: row[key] for key in GENERATION_FIELDS} for row in rows]
    write_jsonl(output_dir / "generation/questions.jsonl", generation)
    write_jsonl(output_dir / "generation/smoke_questions.jsonl", [row for row in generation if row["question_id"] in set(smoke_ids)])
    for name, selected in (("", rows), ("smoke_", smoke)):
        evaluation_rows = [{"sql_idx": index, "question_id": row["question_id"], "db_id": row["db_id"],
                            "difficulty": row["difficulty"], "gold_sql": row["SQL"]} for index, row in enumerate(selected)]
        if any(any(character in row["gold_sql"] for character in "\r\n\t") for row in evaluation_rows):
            raise ValueError("Gold contains line/tab delimiters; official line export would be ambiguous")
        write_jsonl(output_dir / f"evaluation/{name}records.jsonl", evaluation_rows)
        write_jsonl(output_dir / f"evaluation/{name}difficulty.jsonl", [{key: row[key] for key in ("sql_idx", "question_id", "db_id", "difficulty")} for row in evaluation_rows])
        (output_dir / f"evaluation/{name}gold.sql").write_text("".join(row["gold_sql"] + "\t" + row["db_id"] + "\n" for row in evaluation_rows), encoding="utf-8")
    files = {path.relative_to(output_dir).as_posix(): sha256_file(path)
             for folder in (output_dir / "generation", output_dir / "evaluation") for path in sorted(folder.iterdir()) if path.is_file()}
    result = {"version": 1, "source_path": str(source.resolve()), "source_sha256": SOURCE_SHA256,
              "selection_manifest": str(manifest_path.resolve()), "selection_manifest_sha256": sha256_file(manifest_path),
              "question_ids": manifest["question_ids"], "count": 300, "smoke_question_ids": smoke_ids, "smoke_count": 30,
              "smoke_seed": smoke_seed, "db_root": str(db_root.resolve()),
              "official_db_root": db_root.resolve().as_posix().rstrip("/") + "/",
              "databases": database_hashes, "file_hashes": files, "legacy_gold_audit": audit_original_gold(source),
              "gold_isolation": "Only generation/*.jsonl may enter agent inputs; evaluation/* is evaluator-only"}
    write_json(output_dir / "dataset_manifest.json", result)
    return result


def verify_dataset(dataset_dir: Path, *, verify_databases: bool = True) -> dict[str, Any]:
    manifest = json.loads((dataset_dir / "dataset_manifest.json").read_text(encoding="utf-8"))
    if manifest["source_sha256"] != SOURCE_SHA256 or sha256_file(Path(manifest["source_path"])) != SOURCE_SHA256:
        raise ValueError("Dataset source hash mismatch")
    if sha256_file(Path(manifest["selection_manifest"])) != manifest["selection_manifest_sha256"]:
        raise ValueError("Selection manifest hash mismatch")
    selection, source_records, _ = validate_source(Path(manifest["selection_manifest"]), Path(manifest["source_path"]))
    if manifest["question_ids"] != selection["question_ids"]:
        raise ValueError("Prepared IDs differ from the locked 300-question selection")
    if manifest["smoke_question_ids"] != select_ids(source_records, 30, manifest["smoke_seed"]):
        raise ValueError("Smoke IDs differ from the fixed stratified selection")
    source_by_id = {row["question_id"]: row for row in source_records}
    for relative, expected in manifest["file_hashes"].items():
        if sha256_file(dataset_dir / relative) != expected:
            raise ValueError(f"Prepared dataset artifact hash mismatch: {relative}")
    if verify_databases:
        for db_id, expected in manifest["databases"].items():
            if sha256_file(Path(expected["path"])) != expected["sha256"]:
                raise ValueError(f"SQLite data hash changed: {db_id}")
    for prefix, ids in (("", manifest["question_ids"]), ("smoke_", manifest["smoke_question_ids"])):
        generation = read_jsonl(dataset_dir / f"generation/{prefix}questions.jsonl")
        records = read_jsonl(dataset_dir / f"evaluation/{prefix}records.jsonl")
        difficulty = read_jsonl(dataset_dir / f"evaluation/{prefix}difficulty.jsonl")
        gold = (dataset_dir / f"evaluation/{prefix}gold.sql").read_text(encoding="utf-8").splitlines()
        if not (len(generation) == len(records) == len(difficulty) == len(gold) == len(ids)):
            raise ValueError("Generation/gold/difficulty cardinality mismatch")
        for index, (question, record, diff, line) in enumerate(zip(generation, records, difficulty, gold)):
            if set(question) != set(GENERATION_FIELDS):
                raise ValueError("Generation input contains unexpected (possibly gold) fields")
            if not (question["question_id"] == record["question_id"] == diff["question_id"] == ids[index]):
                raise ValueError("Question-to-scoring index mapping mismatch")
            if not (question["db_id"] == record["db_id"] == diff["db_id"]):
                raise ValueError("Database mapping mismatch")
            if record["sql_idx"] != index or diff["sql_idx"] != index or line != record["gold_sql"] + "\t" + record["db_id"]:
                raise ValueError("Official gold/index export mismatch")
            original = source_by_id[record["question_id"]]
            if record["gold_sql"] != original["SQL"] or record["difficulty"] != original["difficulty"]:
                raise ValueError("Evaluation record differs from the locked source")
            if any(question[key] != original[key] for key in GENERATION_FIELDS):
                raise ValueError("Generation record differs from the locked source")
    if len(manifest["question_ids"]) != 300 or len(set(manifest["question_ids"])) != 300:
        raise ValueError("Evaluation denominator must be exactly 300")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--db-root", type=Path, default=DEFAULT_DB_ROOT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    result = verify_dataset(args.output_dir) if args.verify_only else prepare_dataset(args.manifest, args.output_dir, args.source, args.db_root)
    print(json.dumps({"dataset_dir": str(args.output_dir.resolve()), "count": result["count"], "smoke_count": result["smoke_count"], "source_sha256": result["source_sha256"]}))


if __name__ == "__main__":
    main()
