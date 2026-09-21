"""Repair cumulative usage metadata without regenerating or changing any SQL.

Default: write a separate reviewed copy. Publishing is allowed only before any
evaluation, after the entire frozen run is terminal; original bytes are retained.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sqlite3
from datetime import datetime, timezone

from experiments.prepare_dataset import DEFAULT_OUTPUT, read_jsonl, sha256_file
from experiments.state import State, TERMINAL, atomic_json, single_instance

TOKENS = ('prompt_tokens', 'completion_tokens', 'total_tokens', 'cached_tokens', 'reasoning_tokens')
ALLOWED = {'usage_unknown', *TOKENS, *(key + '_known' for key in TOKENS)}


def repair_export(run_dir: Path, dataset_dir: Path, *, output_dir: Path | None = None,
                  publish: bool = False):
    run_dir, dataset_dir = run_dir.resolve(), dataset_dir.resolve()
    local = run_dir.parent.parent
    source = run_dir / 'predictions.jsonl'
    manifest_path = run_dir / 'run_manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    run_id = manifest['run_id']
    if run_dir.name != run_id:
        raise ValueError('Run directory identity mismatch')
    questions_path = dataset_dir / 'generation' / (
        'smoke_questions.jsonl' if manifest['subset'] == 'smoke' else 'questions.jsonl')
    if sha256_file(questions_path) != manifest['questions_sha256']:
        raise ValueError('Frozen question input hash mismatch')
    expected = 30 if manifest['subset'] == 'smoke' else 300
    if manifest['expected_questions'] != expected:
        raise ValueError('Frozen denominator mismatch')
    original_hash = sha256_file(source)
    output_dir = (output_dir or run_dir / ('accounting_repair_' + original_hash[:12])).resolve()
    if run_dir not in output_dir.parents or output_dir.exists():
        raise ValueError('Output must be a new directory inside this run')
    with single_instance(local / 'supervisor.lock'), single_instance(local / 'generation.lock'):
        state = State.__new__(State)
        state.db = sqlite3.connect((local / 'state.sqlite').resolve().as_uri() + '?mode=ro', uri=True)
        state.db.row_factory = sqlite3.Row
        try:
            state.db.execute('BEGIN')
            persisted = state.db.execute('SELECT fingerprint FROM runs WHERE run_id=?', (run_id,)).fetchone()
            if not persisted or persisted['fingerprint'] != manifest['fingerprint']:
                raise ValueError('Frozen state fingerprint mismatch')
            active = state.db.execute("SELECT COUNT(*) FROM questions WHERE status='running' OR worker_pid IS NOT NULL OR lease_until IS NOT NULL").fetchone()[0]
            if active:
                raise ValueError('Active or unrecovered worker blocks metadata repair')
            questions = read_jsonl(questions_path)
            durable = state.rows(run_id)
            if len(questions) != expected or [r['question_id'] for r in durable] != [r['question_id'] for r in questions]:
                raise ValueError('Frozen question order mismatch')
            terminal = [r for r in durable if r['status'] in TERMINAL]
            if publish:
                if len(terminal) != expected:
                    raise ValueError('Cannot publish a partial run')
                if any((run_dir / name).exists() for name in ('scores.jsonl', 'summary.json', 'engineering_audit.json', 'predictions_official.json')):
                    raise ValueError('Cannot alter already evaluated or audited artifacts')
            original = read_jsonl(source)
            if [r['question_id'] for r in original] != [r['question_id'] for r in terminal]:
                raise ValueError('Prediction IDs differ from terminal state')
            # Bind every attempt used in the export to the immutable worker result.
            trace_hashes = {}
            for row in terminal:
                attempts = state.db.execute('SELECT attempt,result_json FROM attempts WHERE run_id=? AND question_id=? ORDER BY attempt',
                                            (run_id, row['question_id'])).fetchall()
                if len(attempts) != row['attempt']:
                    raise ValueError('Missing attempt records')
                for attempt in attempts:
                    path = run_dir / 'traces' / f"{row['question_id']}.attempt{attempt['attempt']}.result.json"
                    raw = json.loads(path.read_text(encoding='utf-8'))
                    if raw != json.loads(attempt['result_json']):
                        raise ValueError('State and worker attempt result differ')
                    trace_hashes[str(path)] = sha256_file(path)
            output_dir.mkdir()
            corrected_path = output_dir / 'predictions.accounting_corrected.jsonl'
            corrected = state.export(run_id, questions, corrected_path)
            changed = []
            for before, after in zip(original, corrected):
                missing = object()
                keys = {k for k in before.keys() | after.keys() if before.get(k, missing) != after.get(k, missing)}
                if keys - ALLOWED:
                    raise ValueError('Repair would change SQL, identity, call/time counts, or other protected fields')
                if keys:
                    changed.append({'question_id': before['question_id'], 'fields': sorted(keys)})
            if len(corrected) != len(original) or sha256_file(source) != original_hash:
                raise ValueError('Source predictions changed during repair')
            for path, digest in trace_hashes.items():
                if sha256_file(Path(path)) != digest:
                    raise ValueError('Worker result changed during repair')
            backup = output_dir / 'predictions.original.jsonl'
            shutil.copy2(source, backup)
            report = {
                'run_id': run_id, 'created_at_utc': datetime.now(timezone.utc).isoformat(),
                'mode': 'publish_before_evaluation' if publish else 'separate_copy_only',
                'published': False, 'record_count': len(corrected), 'expected_count': expected,
                'original_sha256': original_hash, 'corrected_sha256': sha256_file(corrected_path),
                'original_backup': str(backup), 'corrected_copy': str(corrected_path),
                'changed_questions': changed, 'raw_sql_and_all_non_usage_fields_unchanged': True,
                'state_opened_read_only': True, 'model_requests': 0, 'sql_evaluation_performed': False,
                'manifest_sha256': sha256_file(manifest_path), 'worker_result_sha256': trace_hashes,
            }
            atomic_json(output_dir / 'repair_report.json', report)
            if publish:
                temporary = output_dir / 'publish.tmp'
                shutil.copyfile(corrected_path, temporary)
                os.replace(temporary, source)
                report['published'] = True
                atomic_json(output_dir / 'repair_report.json', report)
            return report
        finally:
            state.db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--dataset-dir', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--publish-before-evaluation', action='store_true')
    args = parser.parse_args()
    report = repair_export(args.run_dir, args.dataset_dir, output_dir=args.output_dir,
                           publish=args.publish_before_evaluation)
    print(json.dumps({key: report[key] for key in ('run_id', 'mode', 'published', 'record_count', 'changed_questions')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
