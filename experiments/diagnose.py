"""Collect evaluation-only evidence after a complete mini-dev run is scored.

This tool never calls an LLM, executes a prediction/gold query, recomputes EX,
or selects an improvement method. SQL is compiled under EXPLAIN with a deny-by-
default SQLite authorizer. SQLITE_READ callbacks identify physical table/column
references across aliases and CTEs. Gold-reference coverage gaps are hypotheses,
not proof of retrieval failure: equivalent SQL can use different references.

Gold enters only through dataset/evaluation/*records.jsonl. Outputs contain IDs,
schema references, hashes, counts, and evidence labels, never SQL text or values.
They are restricted to the run's diagnostics directory, outside generation and
retrieval inputs. Complete predictions and scores must match their saved hashes.

SQLite references:
https://www.sqlite.org/c3ref/set_authorizer.html
https://www.sqlite.org/c3ref/c_alter_table.html
https://www.sqlite.org/lang_explain.html
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import time
from typing import Any

from experiments.prepare_dataset import (
    DEFAULT_OUTPUT, SOURCE_SHA256, database_path, read_jsonl, sha256_file,
    write_json, write_jsonl,
)
from experiments.sqlite_runtime import bootstrap_sqlite_runtime

SCHEMA = "aidb_minidev_diagnosis_v1"
CORRECT = "correct"
TERMINAL_STATUSES = {"succeeded", "failed", "timeout"}
ASCII_FOLD = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
MAX_SQL_BYTES = 1_000_000
DOCS = [
    "https://www.sqlite.org/c3ref/set_authorizer.html",
    "https://www.sqlite.org/c3ref/c_alter_table.html",
    "https://www.sqlite.org/lang_explain.html",
]
LIMITATIONS = [
    "Official EX is copied from scores; diagnostics never change or recompute it.",
    "Gold references are one valid query shape. Missing gold columns are only suspected coverage evidence, never a proven cause.",
    "A final linked-schema snapshot does not establish what was available at initial SQL generation time.",
    "Lookup columns are the visible tool-response union; B0 traces do not expose all per-route candidates, scores, or ranks.",
    "COUNT(*) may emit a table-only SQLITE_READ callback; an empty column name is not a missing column.",
    "View-column callbacks are retained as logical references but excluded from physical-table coverage; the underlying table callbacks supply that coverage.",
    "Compilation can fail or deny a statement; partial references from failed compilation cannot establish complete coverage.",
    "The recorded initial SQL is the first query-call trace where available, not necessarily the unexecuted natural-language draft.",
    "EXPLAIN opcode counts are version-specific auxiliary observations, not semantic correctness judgments.",
]


def identifier_key(value: str) -> str:
    """SQLite's identifier case-insensitivity folds ASCII, not arbitrary Unicode."""
    return value.translate(ASCII_FOLD)


def text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def raw_final_sql(prediction: dict[str, Any]) -> str:
    value = prediction.get("submitted_final_sql") if "submitted_final_sql" in prediction else prediction.get("final_sql", "")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("Final SQL must be the original submitted string")
    return value


def _unquote_identifier(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "`"):
        return value[1:-1].replace(value[0] * 2, value[0])
    if len(value) >= 2 and value[0] == "[" and value[-1] == "]":
        return value[1:-1]
    return value


def _identifier_parts(value: str) -> list[str]:
    """Split schema identifiers on dots outside SQLite quoting styles."""
    parts, start, quote = [], 0, ""
    index = 0
    while index < len(value):
        character = value[index]
        if quote:
            closing = "]" if quote == "[" else quote
            if character == closing:
                if quote != "[" and index + 1 < len(value) and value[index + 1] == closing:
                    index += 1
                else:
                    quote = ""
        elif character in ('"', "`", "["):
            quote = character
        elif character == ".":
            parts.append(_unquote_identifier(value[start:index]))
            start = index + 1
        index += 1
    parts.append(_unquote_identifier(value[start:]))
    return parts if not quote else []


@dataclass
class Catalog:
    tables: dict[str, tuple[str, ...]]
    object_types: dict[str, str]

    def table_name(self, value: str) -> str | None:
        matches = [name for name in self.tables if identifier_key(name) == identifier_key(value)]
        return matches[0] if len(matches) == 1 else None

    def column_name(self, table: str, column: str) -> str | None:
        matches = [name for name in self.tables[table] if identifier_key(name) == identifier_key(column)]
        return matches[0] if len(matches) == 1 else None

    def resolve(self, value: str) -> tuple[str, str] | None:
        parts = _identifier_parts(value)
        candidates = []
        if len(parts) == 3 and identifier_key(parts[0]) in ("main", "temp"):
            parts = parts[1:]
        if len(parts) == 2:
            table = self.table_name(parts[0])
            if table:
                column = self.column_name(table, parts[1])
                if column:
                    candidates.append((table, column))
        # add_schema stores unquoted table.column strings. A real table/column
        # may itself contain dots, so match against the known SQLite identities.
        if not candidates:
            key = identifier_key(value.strip())
            for table, columns in self.tables.items():
                candidates.extend((table, column) for column in columns
                                  if identifier_key(table + "." + column) == key)
        return candidates[0] if len(candidates) == 1 else None


def load_catalog(db_path: Path) -> Catalog:
    """Read only schema metadata; never read business row values."""
    import sqlite3
    connection = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}

        def authorize(action, first, second, database, context):
            if action in allowed:
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_PRAGMA and identifier_key(first or "") == "table_xinfo":
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY

        connection.set_authorizer(authorize)
        objects = connection.execute("SELECT name, type FROM sqlite_schema WHERE type IN ('table','view') ORDER BY name").fetchall()
        tables, types = {}, {}
        for name, object_type in objects:
            quoted = '"' + name.replace('"', '""') + '"'
            tables[name] = tuple(row[1] for row in connection.execute("PRAGMA main.table_xinfo(" + quoted + ")"))
            types[name] = object_type
        return Catalog(tables=tables, object_types=types)
    finally:
        connection.close()


def _statement_head(sql: str) -> str:
    """Skip whitespace and leading comments without modifying any SQL."""
    text = sql.lstrip("\ufeff \t\r\n")
    while text.startswith(("--", "/*")):
        if text.startswith("--"):
            _, separator, text = text.partition("\n")
            if not separator:
                return ""
        else:
            _, separator, text = text.partition("*/")
            if not separator:
                return ""
        text = text.lstrip()
    match = re.match(r"[A-Za-z]+", text)
    return match.group(0).upper() if match else ""


def _error_category(error: Exception, denied: bool, timed_out: bool) -> str:
    if timed_out:
        return "compile_timeout"
    if denied:
        return "read_only_authorizer_denied"
    message = str(error).lower()
    for token, category in (("no such column", "unknown_column"), ("no such table", "unknown_table"),
                            ("ambiguous column", "ambiguous_column"), ("bindings", "unbound_parameter"),
                            ("syntax error", "syntax_error"), ("unrecognized token", "syntax_error"),
                            ("one statement", "multiple_statements"), ("too large", "sql_size_limit")):
        if token in message:
            return category
    return "compilation_error"


def compile_references(sql: str, db_path: Path, catalog: Catalog,
                       *, timeout_seconds: float = 5) -> dict[str, Any]:
    """Compile EXPLAIN only; deny every action except read/select/function/CTE."""
    import sqlite3
    started = time.monotonic()
    output: dict[str, Any] = {"status": "missing_sql" if not sql.strip() else "not_compiled",
                              "sql_sha256": text_hash(sql), "tables": [], "columns": [],
                              "table_only_reads": [], "functions": [], "opcode_counts": {},
                              "references_complete": False, "query_executed": False}
    if not sql.strip():
        return output
    if timeout_seconds <= 0:
        raise ValueError("Compile timeout must be positive")
    if len(sql.encode("utf-8")) > MAX_SQL_BYTES:
        output.update(status="rejected", error_category="sql_size_limit")
        return output
    if _statement_head(sql) not in ("SELECT", "WITH"):
        output.update(status="rejected", error_category="non_select_statement")
        return output
    columns: set[tuple[str, str, str]] = set()
    table_reads: set[tuple[str, str]] = set()
    table_only: set[tuple[str, str]] = set()
    functions: set[str] = set()
    contexts: set[str] = set()
    denied_actions: set[int] = set()
    timed_out = False
    connection = None
    try:
        connection = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True,
                                     timeout=timeout_seconds, cached_statements=0)
        connection.execute("PRAGMA query_only=ON")
        connection.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, MAX_SQL_BYTES + 16)
        deadline = time.monotonic() + timeout_seconds
        allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}

        def authorize(action, first, second, database, context):
            nonlocal timed_out
            if time.monotonic() > deadline:
                timed_out = True
                return sqlite3.SQLITE_DENY
            if action not in allowed or (action == sqlite3.SQLITE_FUNCTION and identifier_key(second or first or "") in {"load_extension", "writefile", "readfile"}):
                denied_actions.add(action)
                return sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_READ:
                db = database or "main"
                table = first or ""
                table_reads.add((db, table))
                if second:
                    columns.add((db, table, second))
                else:
                    table_only.add((db, table))
                if context:
                    contexts.add(context)
            elif action == sqlite3.SQLITE_FUNCTION:
                functions.add(identifier_key(second or first or ""))
            return sqlite3.SQLITE_OK

        connection.set_authorizer(authorize)
        def progress():
            nonlocal timed_out
            timed_out = time.monotonic() > deadline
            return int(timed_out)
        connection.set_progress_handler(progress, 1000)
        # EXPLAIN returns the VM program, not the query's data. Do not serialize
        # operands: they may contain gold literals. Only count opcode names.
        opcodes: Counter[str] = Counter()
        cursor = connection.execute("EXPLAIN " + sql)
        for instruction in cursor:
            opcodes[str(instruction[1])] += 1
            if time.monotonic() > deadline:
                timed_out = True
                raise TimeoutError("Compile observation deadline exceeded")
        output.update(status="compiled", references_complete=True, opcode_counts=dict(sorted(opcodes.items())))
    except Exception as error:
        output.update(status="compile_error", error_category=_error_category(error, bool(denied_actions), timed_out),
                      error_type=type(error).__name__, sqlite_errorname=getattr(error, "sqlite_errorname", None),
                      error_message_sha256=text_hash(str(error)))
    finally:
        if connection is not None:
            connection.close()
    # SQLITE_READ includes logical view columns in addition to the underlying
    # table reads. Views (and virtual table-valued functions not in the catalog)
    # must not become missing physical-table coverage requirements.
    output["tables"] = sorted({canonical for database, table in table_reads
                               if database == "main"
                               and (canonical := catalog.table_name(table)) is not None
                               and catalog.object_types.get(canonical) == "table"})
    output["table_only_reads"] = [{"database": database, "table": table} for database, table in sorted(table_only)]
    output["columns"] = []
    for database, table_name, column_name in sorted(columns):
        table = catalog.table_name(table_name)
        column = catalog.column_name(table, column_name) if table else None
        object_type = catalog.object_types.get(table) if table else None
        implicit_rowid = table is not None and column is None and identifier_key(column_name) in {"rowid", "_rowid_", "oid"}
        output["columns"].append({"database": database, "table": table or table_name, "column": column or column_name,
                                   "coverage_eligible": database == "main" and object_type == "table" and column is not None,
                                   "object_type": object_type,
                                   "implicit_rowid": implicit_rowid})
    output["functions"] = sorted(functions)
    output["indirect_read_context_count"] = len(contexts)
    output["denied_authorizer_action_codes"] = sorted(denied_actions)
    output["compile_seconds"] = round(time.monotonic() - started, 6)
    return output


def column_pairs(reference: dict[str, Any]) -> set[tuple[str, str]]:
    return {(row["table"], row["column"]) for row in reference.get("columns", []) if row.get("coverage_eligible")}


def serialized_pairs(values: set[tuple[str, str]]) -> list[dict[str, str]]:
    return [{"table": table, "column": column} for table, column in sorted(values)]


def _response_text(response: Any) -> str | None:
    if isinstance(response, str):
        return response
    if isinstance(response, dict) and isinstance(response.get("result"), str):
        return response["result"]
    return None


def _foreign_key_target(line: str, catalog: Catalog, db_id: str | None) -> tuple[str, str] | None:
    """Read only the native lookup's structural FK row, never SQL/reference text."""
    match = re.fullmatch(r"\*\*(.+?)\*\*\s*\|\s*主键/外键\s*\|\s*(?:主键\s*\+\s*)?外键\s*→\s*(.+)", line)
    if not match:
        return None
    source_parts = _identifier_parts(match.group(1))
    if len(source_parts) == 3 and identifier_key(source_parts[0]) == "temp":
        return None
    target = match.group(2).strip()
    # The native suffix starts with a Chinese comma. Ignore commas inside
    # quoted identifiers, including SQLite's doubled quote escaping.
    quote, index = "", 0
    while index < len(target):
        character = target[index]
        if quote:
            closing = "]" if quote == "[" else quote
            if character == closing:
                if quote != "[" and index + 1 < len(target) and target[index + 1] == closing:
                    index += 1
                else:
                    quote = ""
        elif character in ('"', "`", "["):
            quote = character
        elif character == "，":
            if not re.fullmatch(r"，\s*用于\s+JOIN\s+.+", target[index:]):
                return None
            target = target[:index].strip()
            break
        index += 1
    parts = _identifier_parts(target)
    if len(parts) == 3 and identifier_key(parts[0]) in {"main", identifier_key(db_id or "")}:
        # Resolve only this database's explicit qualifier; never discard an
        # arbitrary database prefix merely because its table/column exists.
        target = ".".join('"' + part.replace('"', '""') + '"' for part in parts[1:])
    elif len(parts) == 3 and identifier_key(parts[0]) == "temp":
        return None
    return catalog.resolve(target)


def trace_schema_observations(trace: dict[str, Any], catalog: Catalog, *, db_id: str | None = None) -> dict[str, Any]:
    linked: set[tuple[str, str]] = set()
    snapshot: set[tuple[str, str]] = set()
    invalid = Counter()
    linked_available = isinstance(trace.get("linked_schema"), list)
    snapshot_available = isinstance(trace.get("linked_schema_snapshot"), list)
    for key, destination in (("linked_schema", linked), ("linked_schema_snapshot", snapshot)):
        for value in trace.get(key, []) if isinstance(trace.get(key), list) else []:
            resolved = catalog.resolve(value) if isinstance(value, str) else None
            if resolved:
                destination.add(resolved)
            else:
                invalid[key] += 1
    candidates: set[tuple[str, str]] = set()
    structural: set[tuple[str, str]] = set()
    locations: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    lookup_calls = lookup_responses = recognized_responses = 0
    for event in trace.get("tool_trace", []) if isinstance(trace.get("tool_trace"), list) else []:
        if not isinstance(event, dict) or event.get("name") != "sql_db_value_lookup":
            continue
        if event.get("kind") == "call":
            lookup_calls += 1
            continue
        if event.get("kind") != "response":
            continue
        lookup_responses += 1
        text = _response_text(event.get("response"))
        if text is None:
            continue
        response = event.get("response")
        declared = response.get("db_id") if isinstance(response, dict) else None
        headers = re.findall(r"^【DB_ID】\s*([^\r\n]+)", text, flags=re.MULTILINE)
        if db_id is not None and any(value != db_id for value in ([declared] if declared is not None else []) + headers):
            invalid["lookup_database"] += 1
            continue
        section = "visible_lookup_columns"
        recognized = False
        rank = 0
        for line in text.splitlines():
            if line.startswith("###"):
                if "结构键" in line:
                    section = "structural_keys"
                    recognized = True
                elif "架构元素" in line:
                    section = "visible_lookup_columns"
                    recognized = True
                else:
                    section = "other"
            # The real tc_values-only fallback emits a bold identifier without
            # the vector result's metadata pipe. Both are schema-column rows.
            match = re.match(r"^\*\*(.+?)\*\*(?:\s*\||\s*$)", line)
            if not match or section not in {"visible_lookup_columns", "structural_keys"}:
                continue
            resolved = catalog.resolve(match.group(1))
            if resolved is None:
                invalid["lookup_response"] += 1
                continue
            recognized = True
            rank += 1
            (candidates if section == "visible_lookup_columns" else structural).add(resolved)
            locations[resolved].append({"lookup_response_index": lookup_responses, "visible_position": rank, "section": section})
            if section == "structural_keys":
                target = _foreign_key_target(line, catalog, db_id)
                if target:
                    structural.add(target)
                    locations[target].append({"lookup_response_index": lookup_responses, "visible_position": rank,
                                              "section": section, "source": "foreign_key_target",
                                              "source_table": resolved[0], "source_column": resolved[1]})
        if recognized:
            recognized_responses += 1
    return {"linked_available": linked_available, "snapshot_available": snapshot_available,
            "lookup_available": recognized_responses > 0,
            "lookup_calls": lookup_calls, "lookup_responses": lookup_responses,
            "recognized_lookup_responses": recognized_responses,
            "linked": linked, "snapshot": snapshot, "candidates": candidates, "structural": structural,
            "locations": locations, "unresolved_identifier_counts": dict(invalid),
            "per_route_candidates_available": False}


def initial_sql_from_trace(trace: dict[str, Any]) -> tuple[str, str]:
    value = trace.get("initial_sql")
    if isinstance(value, str) and value.strip():
        return value, "trace.initial_sql_first_query_call"
    for event in trace.get("tool_trace", []) if isinstance(trace.get("tool_trace"), list) else []:
        if isinstance(event, dict) and event.get("kind") == "call" and event.get("name") == "sql_db_query":
            args = event.get("args") or {}
            value = args.get("query", args.get("sql")) if isinstance(args, dict) else None
            if isinstance(value, str) and value.strip():
                return value, "first_sql_db_query_call"
    for event in trace.get("sql_execution_trace", []) if isinstance(trace.get("sql_execution_trace"), list) else []:
        if isinstance(event, dict) and not event.get("is_probe") and isinstance(event.get("sql"), str) and event["sql"].strip():
            return event["sql"], "first_non_probe_execution"
    return "", "unavailable"


def outcome_from_score(score: dict[str, Any], prediction: dict[str, Any]) -> str:
    if score["ex"] == 1:
        return CORRECT
    status = score.get("status")
    if status == "gold_error":
        return "evaluation_gold_error"
    if status == "worker_failed":
        return "evaluation_worker_failure"
    if status == "timeout":
        return "evaluation_timeout"
    if not raw_final_sql(prediction).strip():
        if prediction.get("status") == "timeout":
            return "generation_timeout"
        if prediction.get("error_category") not in (None, "", "no_submission", "semantic"):
            return "generation_error_without_submission"
        return "no_final_sql"
    if status == "sql_error":
        return "prediction_execution_error"
    if status == "scored" and score.get("prediction_executable"):
        return "executable_result_mismatch"
    return "unclassified_failure"


def diagnose_question(record: dict[str, Any], prediction: dict[str, Any], score: dict[str, Any],
                      trace: dict[str, Any], db_path: Path, catalog: Catalog,
                      *, compile_timeout_seconds: float = 5) -> dict[str, Any]:
    """Build evidence without emitting either SQL, literal values, or questions."""
    predicted_sql = raw_final_sql(prediction)
    initial_sql, initial_source = initial_sql_from_trace(trace)
    gold = compile_references(record["gold_sql"], db_path, catalog, timeout_seconds=compile_timeout_seconds)
    final = compile_references(predicted_sql, db_path, catalog, timeout_seconds=compile_timeout_seconds)
    initial = (dict(final) if initial_sql == predicted_sql else compile_references(initial_sql, db_path, catalog, timeout_seconds=compile_timeout_seconds))
    observed = trace_schema_observations(trace, catalog, db_id=record["db_id"])
    gold_columns, final_columns, initial_columns = column_pairs(gold), column_pairs(final), column_pairs(initial)
    observed_union = observed["candidates"] | observed["structural"]
    complete_gold = gold["references_complete"]
    linked_comparable = complete_gold and observed["linked_available"]
    lookup_comparable = complete_gold and observed["lookup_available"]
    missing_linked = gold_columns - observed["linked"] if linked_comparable else set()
    missing_lookup = gold_columns - observed_union if lookup_comparable else set()
    candidate_not_linked = (gold_columns & observed_union) - observed["linked"] if linked_comparable and lookup_comparable else set()
    missing_linked_tables = (set(gold["tables"]) - {table for table, column in observed["linked"]}) if linked_comparable else set()
    missing_lookup_tables = (set(gold["tables"]) - {table for table, column in observed_union}) if lookup_comparable else set()
    outcome = outcome_from_score(score, prediction)
    flags = []
    if outcome != CORRECT:
        if missing_linked or missing_linked_tables:
            flags.append("suspected_linked_schema_coverage_gap")
        if missing_lookup or missing_lookup_tables:
            flags.append("suspected_visible_candidate_coverage_gap")
        if candidate_not_linked:
            flags.append("reference_columns_visible_but_not_linked")
    execution_events = [row for row in (trace.get("sql_execution_trace") or []) if isinstance(row, dict)]
    main_events = [row for row in execution_events if not row.get("is_probe")]
    correction_events = [row for row in (trace.get("correction_events") or []) if isinstance(row, dict)]
    edits_comparable = initial["references_complete"] and final["references_complete"]
    evidence = {
        "schema": SCHEMA, "question_id": record["question_id"], "db_id": record["db_id"], "difficulty": record["difficulty"],
        "evaluation_only": True, "ex": score["ex"], "outcome": outcome,
        "score_status": score.get("status"), "generation_status": prediction.get("status"),
        "generation_error_category": prediction.get("error_category") or None,
        "submitted": bool(predicted_sql.strip()), "suspected_causes": flags,
        "references": {"gold": gold, "prediction": final, "initial": initial},
        "coverage": {
            "linked_comparison_available": linked_comparable, "lookup_comparison_available": lookup_comparable,
            "reference_column_count": len(gold_columns) if complete_gold else None,
            "linked_columns": serialized_pairs(observed["linked"]), "snapshot_columns": serialized_pairs(observed["snapshot"]),
            "visible_lookup_columns": serialized_pairs(observed["candidates"]),
            "structural_key_columns": serialized_pairs(observed["structural"]),
            "missing_from_linked_schema": serialized_pairs(missing_linked) if linked_comparable else None,
            "missing_from_visible_lookup_union": serialized_pairs(missing_lookup) if lookup_comparable else None,
            "visible_candidates_not_linked": serialized_pairs(candidate_not_linked) if linked_comparable and lookup_comparable else None,
            "reference_tables_absent_from_linked_schema": sorted(missing_linked_tables) if linked_comparable else None,
            "reference_tables_absent_from_visible_lookup_union": sorted(missing_lookup_tables) if lookup_comparable else None,
            "prediction_reference_columns_absent_from_linked_schema": serialized_pairs(final_columns - observed["linked"]) if final["references_complete"] and observed["linked_available"] else None,
            "reference_columns_absent_from_prediction": serialized_pairs(gold_columns - final_columns) if complete_gold and final["references_complete"] else None,
            "reference_candidate_locations": [{"table": table, "column": column, "observations": observed["locations"][(table, column)]}
                                              for table, column in sorted(gold_columns & set(observed["locations"]))],
            "gold_shape_is_not_unique": True,
        },
        "trace_observability": {key: observed[key] for key in (
            "linked_available", "snapshot_available", "lookup_available", "lookup_calls", "lookup_responses",
            "recognized_lookup_responses", "unresolved_identifier_counts", "per_route_candidates_available")},
        "initial_to_submission": {
            "initial_source": initial_source, "both_present": bool(initial_sql.strip() and predicted_sql.strip()),
            "raw_text_changed": initial_sql != predicted_sql if initial_sql.strip() and predicted_sql.strip() else None,
            "reference_changes_comparable": edits_comparable,
            "columns_added": serialized_pairs(final_columns - initial_columns) if edits_comparable else None,
            "columns_removed": serialized_pairs(initial_columns - final_columns) if edits_comparable else None,
            "tables_added": sorted(set(final["tables"]) - set(initial["tables"])) if edits_comparable else None,
            "tables_removed": sorted(set(initial["tables"]) - set(final["tables"])) if edits_comparable else None,
            "functions_added": sorted(set(final["functions"]) - set(initial["functions"])) if edits_comparable else None,
            "functions_removed": sorted(set(initial["functions"]) - set(final["functions"])) if edits_comparable else None,
            "execution_attempt_count": len(main_events), "probe_execution_count": len(execution_events) - len(main_events),
            "execution_state_counts": dict(Counter(str(row.get("state", "unknown")) for row in main_events)),
            "correction_event_count": len(correction_events),
            "correction_kind_counts": dict(Counter(str(row.get("kind", "unknown")) for row in correction_events)),
            "semantic_rejection_recorded": bool(trace.get("semantic_reject_reason")),
        },
    }
    return evidence


def summarize_diagnostics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    def summarize(group):
        failures = [row for row in group if row["ex"] == 0]
        correct = [row for row in group if row["ex"] == 1]
        flags = Counter(flag for row in failures for flag in row["suspected_causes"])
        return {"count": len(group), "correct": len(correct), "incorrect": len(failures),
                "official_ex": len(correct) / len(group) if group else 0,
                "outcome_counts": dict(Counter(row["outcome"] for row in group)),
                "failure_question_ids": [row["question_id"] for row in failures],
                "suspected_evidence_counts_among_failures": dict(sorted(flags.items())),
                "failed_with_linked_comparison": sum(row["coverage"]["linked_comparison_available"] for row in failures),
                "failed_with_lookup_comparison": sum(row["coverage"]["lookup_comparison_available"] for row in failures),
                "correct_with_reference_coverage_gaps": sum(bool(row["coverage"]["missing_from_linked_schema"] or row["coverage"]["reference_tables_absent_from_linked_schema"]) for row in correct),
                "gold_compile_failures": sum(not row["references"]["gold"]["references_complete"] for row in group),
                "initial_to_final_text_changes": sum(row["initial_to_submission"]["raw_text_changed"] is True for row in group)}
    output = {"schema": SCHEMA, "evaluation_only": True, "overall": summarize(rows),
              "limitations": LIMITATIONS, "method_sources": DOCS}
    for field in ("db_id", "difficulty"):
        output["by_" + field] = {value: summarize([row for row in rows if row[field] == value]) for value in sorted({row[field] for row in rows})}
    output["failure_evidence_question_ids"] = {
        label: [row["question_id"] for row in rows if row["ex"] == 0 and label in row["suspected_causes"]]
        for label in sorted({label for row in rows if row["ex"] == 0 for label in row["suspected_causes"]})}
    return output


def checked_output_directory(run_dir: Path, output_dir: Path) -> Path:
    run = run_dir.resolve()
    output = output_dir.resolve()
    allowed = (run / "diagnostics").resolve()
    if not allowed.is_relative_to(run) or not output.is_relative_to(allowed):
        raise ValueError("Diagnosis outputs must stay under <run-dir>/diagnostics; generation/retrieval writes are forbidden")
    if any(identifier_key(part) in {"generation", "retrieval"} for part in output.parts):
        raise ValueError("Diagnosis cannot write into generation or retrieval directories")
    return output


def load_completed_inputs(dataset_dir: Path, run_dir: Path, subset: str) -> tuple[dict[str, Any], list, list, list, dict]:
    """Verify lineage using hashes, reading gold text only from evaluation files."""
    dataset_path = dataset_dir / "dataset_manifest.json"
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    run_manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    if run_manifest.get("dataset_manifest_sha256") != sha256_file(dataset_path):
        raise ValueError("Run does not match this dataset manifest")
    if dataset["source_sha256"] != SOURCE_SHA256 or sha256_file(Path(dataset["source_path"])) != SOURCE_SHA256:
        raise ValueError("Frozen source hash changed")
    if sha256_file(Path(dataset["selection_manifest"])) != dataset["selection_manifest_sha256"]:
        raise ValueError("Frozen selection manifest changed")
    selection = json.loads(Path(dataset["selection_manifest"]).read_text(encoding="utf-8"))
    if dataset["question_ids"] != selection["question_ids"] or len(set(dataset["question_ids"])) != 300:
        raise ValueError("Dataset does not contain the frozen 300-question IDs")
    if summary.get("subset") != subset or run_manifest.get("subset") != subset:
        raise ValueError("Scored run subset differs from requested diagnosis subset")
    if summary.get("source_sha256") != SOURCE_SHA256 or summary.get("selection_manifest_sha256") != dataset["selection_manifest_sha256"]:
        raise ValueError("Scoring and dataset provenance differ")
    predictions_path, scores_path = run_dir / "predictions.jsonl", run_dir / "scores.jsonl"
    for path, key in ((predictions_path, "predictions_sha256"), (scores_path, "scores_sha256")):
        if sha256_file(path) != summary.get(key):
            raise ValueError(f"Completed scoring input was changed: {path.name}")
    prefix = "smoke_" if subset == "smoke" else ""
    relative = f"evaluation/{prefix}records.jsonl"
    if sha256_file(dataset_dir / relative) != dataset["file_hashes"].get(relative):
        raise ValueError("Evaluation-only records changed")
    records, predictions, scores = read_jsonl(dataset_dir / relative), read_jsonl(predictions_path), read_jsonl(scores_path)
    ids = dataset["smoke_question_ids"] if subset == "smoke" else dataset["question_ids"]
    count = 30 if subset == "smoke" else 300
    if len(ids) != count or len(set(ids)) != count:
        raise ValueError("Invalid fixed scoring IDs")
    for name, rows in (("evaluation records", records), ("predictions", predictions), ("scores", scores)):
        if len(rows) != count or [row["question_id"] for row in rows] != ids:
            raise ValueError(f"Need complete {count}-question {name} in the frozen order before diagnosis")
    for record, prediction, score in zip(records, predictions, scores):
        if not (record["db_id"] == prediction["db_id"] == score["db_id"] and record["difficulty"] == score["difficulty"]):
            raise ValueError("Cross-database/difficulty diagnostic pairing")
        if prediction.get("status") not in TERMINAL_STATUSES or score.get("ex") not in (0, 1):
            raise ValueError("Diagnosis requires terminal predictions and valid official EX values")
        if score["ex"] == 1 and not raw_final_sql(prediction).strip():
            raise ValueError("A correct score cannot correspond to a missing final SQL")
        if "submitted" in score and score["submitted"] != bool(raw_final_sql(prediction).strip()):
            raise ValueError("Scored submission flag differs from the original prediction")
    if summary.get("overall", {}).get("count") != count or summary["overall"].get("correct") != sum(row["ex"] for row in scores):
        raise ValueError("Official summary and per-question scores disagree")
    for db_id, info in dataset["databases"].items():
        if sha256_file(database_path(Path(dataset["db_root"]), db_id)) != info["sha256"]:
            raise ValueError(f"Business SQLite changed: {db_id}")
    return dataset, records, predictions, scores, summary


def read_prediction_trace(prediction: dict[str, Any], run_dir: Path) -> tuple[dict[str, Any], str]:
    if isinstance(prediction.get("trace"), dict) and prediction["trace"]:
        return prediction["trace"], "predictions.jsonl"
    question_id, attempt = prediction.get("question_id"), prediction.get("attempt")
    if not isinstance(question_id, int) or not isinstance(attempt, int) or attempt < 1:
        return {}, "unavailable"
    path = run_dir / "traces" / f"{question_id}.attempt{attempt}.result.json"
    if not path.exists():
        return {}, "unavailable"
    result = json.loads(path.read_text(encoding="utf-8"))
    if result.get("question_id") != question_id or result.get("db_id") != prediction.get("db_id") or raw_final_sql(result) != raw_final_sql(prediction):
        raise ValueError("Attempt trace does not match the scored prediction")
    if prediction.get("session_id") and result.get("session_id") != prediction["session_id"]:
        raise ValueError("Attempt trace session does not match the scored prediction")
    return result.get("trace", {}) if isinstance(result.get("trace"), dict) else {}, path.name


def markdown_report(summary: dict[str, Any]) -> str:
    overall = summary["overall"]
    lines = ["# mini-dev evaluation-only diagnostic evidence", "",
             f"Scored records: {overall['count']}; official correct: {overall['correct']}; failed: {overall['incorrect']}.",
             "EX is copied unchanged from the official score artifact. No gold/prediction query was executed by this diagnostic.", "",
             "## Observed outcomes", "", "| Outcome | Count |", "|---|---:|"]
    lines.extend(f"| {key} | {value} |" for key, value in sorted(overall["outcome_counts"].items()))
    lines.extend(["", "## Suspected schema-coverage evidence among failures", "",
                  "These are observations against one gold query shape, not causal conclusions or an improvement recommendation.", "",
                  "| Evidence | Count | Question IDs |", "|---|---:|---|"])
    for label, ids in summary["failure_evidence_question_ids"].items():
        lines.append(f"| {label} | {len(ids)} | {', '.join(map(str, ids))} |")
    if not summary["failure_evidence_question_ids"]:
        lines.append("| No supported coverage flags | 0 | |")
    lines.extend(["", f"Correct questions with a gold-reference coverage gap: {overall['correct_with_reference_coverage_gaps']}. These remain correct and are excluded from failure-cause counts.", "",
                  "## Coverage by database", "", "| Database | Count | Correct | Failed |", "|---|---:|---:|---:|"])
    lines.extend(f"| {db_id} | {row['count']} | {row['correct']} | {row['incorrect']} |" for db_id, row in summary["by_db_id"].items())
    lines.extend(["", "## Limits and interpretation", ""])
    lines.extend("- " + item for item in LIMITATIONS)
    lines.extend(["", "The compiler authorizer reports table and column reads, including table-only reads such as COUNT(*). [SQLite authorizer documentation](https://www.sqlite.org/c3ref/set_authorizer.html).",
                  "Read action arguments identify the table and column. [SQLite action codes](https://www.sqlite.org/c3ref/c_alter_table.html).",
                  "EXPLAIN returns a VM description; authorization still applies, so all PRAGMA and write actions are denied. [SQLite EXPLAIN documentation](https://www.sqlite.org/lang_explain.html).", ""])
    return "\n".join(lines)


def run_diagnosis(dataset_dir: Path, run_dir: Path, output_dir: Path | None = None,
                  *, subset: str = "all", compile_timeout_seconds: float = 5) -> dict[str, Any]:
    if subset not in ("all", "smoke"):
        raise ValueError("subset must be all or smoke")
    destination = checked_output_directory(run_dir, output_dir or run_dir / "diagnostics")
    runtime = bootstrap_sqlite_runtime()
    dataset, records, predictions, scores, official_summary = load_completed_inputs(dataset_dir, run_dir, subset)
    scored_runtime = official_summary.get("official_evaluator", {}).get("sqlite_runtime", {})
    if any(scored_runtime.get(key) != runtime[key] for key in ("version", "dll_sha256")):
        raise ValueError("Diagnostic and scored SQLite runtimes differ")
    catalogs = {}
    output = []
    trace_artifacts = {}
    for index, (record, prediction, score) in enumerate(zip(records, predictions, scores)):
        db_id = record["db_id"]
        db_path = database_path(Path(dataset["db_root"]), db_id)
        if db_id not in catalogs:
            catalogs[db_id] = load_catalog(db_path)
        trace, trace_source = read_prediction_trace(prediction, run_dir)
        diagnostic = diagnose_question(record, prediction, score, trace, db_path, catalogs[db_id],
                                       compile_timeout_seconds=compile_timeout_seconds)
        diagnostic["trace_source"] = trace_source
        if trace_source not in ("predictions.jsonl", "unavailable"):
            trace_artifacts[trace_source] = sha256_file(run_dir / "traces" / trace_source)
        output.append(diagnostic)
        if (index + 1) % 50 == 0:
            print(json.dumps({"diagnosed": index + 1, "total": len(records)}), flush=True)
    summary = summarize_diagnostics(output)
    summary["provenance"] = {
        "predictions_sha256": sha256_file(run_dir / "predictions.jsonl"), "scores_sha256": sha256_file(run_dir / "scores.jsonl"),
        "run_manifest_sha256": sha256_file(run_dir / "run_manifest.json"), "dataset_manifest_sha256": sha256_file(dataset_dir / "dataset_manifest.json"),
        "diagnostic_code_sha256": sha256_file(Path(__file__)), "source_sha256": dataset["source_sha256"],
        "sqlite_runtime": runtime, "trace_artifact_hashes": trace_artifacts,
        "compile_timeout_seconds": compile_timeout_seconds, "subset": subset,
    }
    destination.mkdir(parents=True, exist_ok=True)
    write_jsonl(destination / "diagnostics.jsonl", output)
    write_json(destination / "summary.json", summary)
    report = destination / "diagnosis.md"
    temporary = report.with_suffix(".md.tmp")
    temporary.write_text(markdown_report(summary), encoding="utf-8")
    temporary.replace(report)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--subset", choices=("all", "smoke"), default="all")
    parser.add_argument("--compile-timeout", type=float, default=5)
    args = parser.parse_args()
    summary = run_diagnosis(args.dataset_dir, args.run_dir, args.output_dir, subset=args.subset,
                            compile_timeout_seconds=args.compile_timeout)
    print(json.dumps(summary["overall"], ensure_ascii=False))


if __name__ == "__main__":
    main()
