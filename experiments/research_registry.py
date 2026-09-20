"""Auditable P4/P5 bookkeeping; never generates SQL or chooses research methods.

Examples (run from the project root):
  python -m experiments.research_registry init
  python -m experiments.research_registry register-baseline --scores RUN/scores.jsonl --branch codex/dev_legion --commit FULL_SHA
  python -m experiments.research_registry register-candidate --id C1 --hypothesis "Name-only fields lose an RRF vote" --changed-variable "identifier retention" --paper-notes NOTES.md --branch dev_YYYYMMDD_HHMMSS --commit FULL_SHA
  python -m experiments.research_registry record-decision --id C1 --comparison comparison.json
  python -m experiments.research_registry status

The registry records evidence and decisions only. It never edits Git, predictions,
the frozen runs, or the comparison metric. Unknown monetary costs remain unknown.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re

from experiments.compare import compare_runs, validate_scores
from experiments.evaluate import METRIC, OFFICIAL_COMMIT, OFFICIAL_HASHES
from experiments.prepare_dataset import read_jsonl, sha256_file
from experiments.sqlite_runtime import DLL_SHA256, VERSION as SQLITE_VERSION
from experiments.state import atomic_json, single_instance

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = ROOT / '.local-services/experiments/research_registry.json'
DEFAULT_SELECTION = ROOT / 'experiments/mini_dev_300_manifest.json'


def now():
    return datetime.now(timezone.utc).isoformat()


def load_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def full_commit(value):
    if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', value or ''):
        raise ValueError('Use the complete lowercase Git commit hash')
    return value


def artifact(path):
    path = Path(path).resolve(strict=True)
    return {'path': str(path), 'sha256': sha256_file(path)}


def note(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{name} must be explicitly supplied')
    return value.strip()


def append_event(registry, action, details):
    registry['events'].append({'sequence': len(registry['events']) + 1,
                               'at': now(), 'action': action, 'details': details})
    registry['updated_at'] = now()


def initialize(path=DEFAULT_REGISTRY, *, selection=DEFAULT_SELECTION, required_repeats=2, max_cost_ratio=None):
    path = Path(path)
    if required_repeats < 2:
        raise ValueError('Freeze at least two matched repetitions before candidates')
    if max_cost_ratio is not None and max_cost_ratio <= 0:
        raise ValueError('A monetary cost ratio ceiling must be positive or unset')
    source = load_json(selection)
    ids = source['question_ids']
    if len(ids) != 300 or len(set(ids)) != 300:
        raise ValueError('The registry requires the fixed 300 unique question IDs')
    policy = {'expected_questions': 300, 'required_matched_repeats': required_repeats,
              'max_cost_ratio': max_cost_ratio, 'selection_manifest': artifact(selection),
              'source_sha256': source['source_sha256'], 'question_ids': ids,
              'no_question_level_stitching': True}
    with single_instance(path.with_suffix('.lock')):
        if path.exists():
            previous = load_json(path)
            if previous['policy'] != policy:
                raise ValueError('Research policy is already frozen; use a new registry for a new policy')
            return previous
        registry = {'version': 1, 'policy': policy, 'baseline': None, 'best': None,
                    'candidates': {}, 'events': [], 'consecutive_rejected_hypotheses': [],
                    'review_due': False, 'review_requests': [], 'cycle_reports_due': []}
        append_event(registry, 'initialized', {'baseline_complete': False})
        atomic_json(path, registry)
        return registry


def validate_run(scores_path, policy, expected_commit):
    """Bind complete official scores to one immutable generation run."""
    scores_path = Path(scores_path).resolve(strict=True)
    folder = scores_path.parent
    scores = read_jsonl(scores_path)
    validate_scores(scores, 300)
    if [r['question_id'] for r in scores] != policy['question_ids']:
        raise ValueError('Scores do not match the frozen 300-question IDs/order')
    summary_path, manifest_path = folder / 'summary.json', folder / 'run_manifest.json'
    predictions_path = folder / 'predictions.jsonl'
    summary, manifest = load_json(summary_path), load_json(manifest_path)
    if summary.get('metric') != METRIC or summary.get('subset') != 'all' or manifest.get('subset') != 'all':
        raise ValueError('Only complete official 300-question runs can enter the research registry')
    if summary.get('scores_sha256') != sha256_file(scores_path) or summary.get('predictions_sha256') != sha256_file(predictions_path):
        raise ValueError('Evaluator summary does not bind the supplied scores and predictions')
    if summary.get('source_sha256') != policy['source_sha256'] or summary.get('selection_manifest_sha256') != policy['selection_manifest']['sha256']:
        raise ValueError('Run data/selection provenance differs from the frozen registry')
    evaluator = summary.get('official_evaluator', {})
    if evaluator.get('commit') != OFFICIAL_COMMIT or evaluator.get('file_hashes') != OFFICIAL_HASHES or evaluator.get('metric') != METRIC:
        raise ValueError('Official evaluator identity is missing or different')
    for runtime in (manifest.get('sqlite_runtime'), evaluator.get('sqlite_runtime')):
        if not isinstance(runtime, dict) or runtime.get('version') != SQLITE_VERSION or runtime.get('dll_sha256') != DLL_SHA256:
            raise ValueError('Generation and evaluation must identify the frozen SQLite runtime')
    if any(summary.get(k) != 300 for k in ('completed_generation_records', 'scoring_denominator')):
        raise ValueError('Incomplete generation/scoring records cannot be registered')
    if summary.get('overall', {}).get('count') != 300 or summary['overall'].get('correct') != sum(r['ex'] for r in scores):
        raise ValueError('Evaluator summary does not match the complete score records')
    if manifest.get('git_commit') != full_commit(expected_commit) or manifest.get('expected_questions') != 300:
        raise ValueError('Run manifest does not bind the requested commit and 300 questions')
    created_at = manifest.get('created_at')
    if type(created_at) not in (int, float) or not math.isfinite(created_at):
        raise ValueError('Run manifest must record its creation time for matched repeat ordering')
    frozen = {key: value for key, value in manifest.items()
              if key not in ('fingerprint', 'run_id', 'created_at', 'expected_questions')}
    if hashlib.sha256(json.dumps(frozen, sort_keys=True).encode()).hexdigest() != manifest.get('fingerprint'):
        raise ValueError('Frozen run-manifest fingerprint is invalid')
    if not manifest.get('code_sha256') or not manifest.get('index_manifest_sha256') or not manifest.get('questions_sha256'):
        raise ValueError('Run is missing frozen code, index, or question provenance')
    config = manifest.get('config', {})
    required_rules = ('model', 'max_llm_calls', 'question_timeout_seconds', 'sql_timeout_seconds', 'temperature')
    if any(key not in config for key in required_rules) or not manifest.get('dataset_manifest_sha256'):
        raise ValueError('Run must freeze its model, generation budgets, temperature, and dataset')
    generation_contract = {key: config[key] for key in required_rules}
    # Defaults below are the existing worker/runner defaults, not new budgets.
    generation_contract.update({key: config.get(key, default) for key, default in {
        'max_sql_query_calls': 4, 'request_timeout_seconds': 120,
        'max_transient_retries': 3, 'retry_backoff_seconds': [5, 20, 60], 'concurrency': 1,
        'evidence_policy': 'always_include_question_evidence', 'use_evidence': True, 'include_evidence': True,
        'provider_endpoint_sha256': None, 'max_api_cost': None, 'max_total_api_calls': None,
    }.items()})
    generation_contract['questions_sha256'] = manifest['questions_sha256']
    generation_contract['dataset_manifest_sha256'] = manifest['dataset_manifest_sha256']
    generation_contract['sqlite_runtime'] = {key: value for key, value in manifest.get('sqlite_runtime', {}).items()
                                              if key != 'dll_path'}
    repetitions = config.get('repetition_count')
    if type(repetitions) is not int or repetitions < policy['required_matched_repeats']:
        raise ValueError('Required matched repetitions were not frozen in the run configuration')
    generation_contract['repetition_count'] = repetitions
    if manifest.get('config', {}).get('candidate_cost_ratio_limit') != policy['max_cost_ratio']:
        raise ValueError('Run monetary-cost policy differs from the frozen registry')
    predictions = read_jsonl(predictions_path)
    if len(predictions) != 300 or [r['question_id'] for r in predictions] != policy['question_ids']:
        raise ValueError('Predictions must contain the same complete 300-question sequence')
    for index, (prediction, score) in enumerate(zip(predictions, scores)):
        if prediction.get('run_id') != manifest.get('run_id') or prediction.get('db_id') != score['db_id']:
            raise ValueError('Mixed-run or cross-database prediction assembly is forbidden')
        if prediction.get('status') not in ('succeeded', 'failed', 'timeout'):
            raise ValueError('A pending prediction is not a completed generation record')
        sql = prediction.get('submitted_final_sql')
        if not isinstance(sql, str) or (sql and prediction.get('final_sql_source') != 'submit_final_sql'):
            raise ValueError('Predictions must preserve raw final tool submissions, including empty failures')
        submitted = bool(sql.strip())
        if (score.get('submitted') is not submitted or score.get('generation_status') != prediction['status']
                or score.get('sql_idx') != index):
            raise ValueError('Official score submission metadata disagrees with its generation record')
        if not submitted and (score['ex'] != 0 or score.get('status') != 'missing_sql'
                              or score.get('prediction_executable') is not False):
            raise ValueError('An empty final submission must receive official missing_sql EX=0')
        if score['ex'] == 1 and (not submitted or score.get('status') != 'scored'
                                 or score.get('prediction_executable') is not True):
            raise ValueError('Official EX credit requires an executed final submission')
    identity = dict(evaluator)
    if isinstance(identity.get('sqlite_runtime'), dict):
        identity['sqlite_runtime'] = {k: v for k, v in identity['sqlite_runtime'].items() if k != 'dll_path'}
    reference = {'run_id': manifest['run_id'], 'commit': expected_commit, 'fingerprint': manifest['fingerprint'],
                 'created_at': created_at,
                 'scores': artifact(scores_path), 'summary': artifact(summary_path),
                 'manifest': artifact(manifest_path), 'predictions': artifact(predictions_path),
                 'repetition_count': repetitions, 'official_evaluator': identity,
                 'timeout_seconds': summary['timeout_seconds'], 'generation_contract': generation_contract}
    return reference, scores


def validate_runs(paths, policy, commit):
    result = [validate_run(path, policy, commit) for path in paths]
    refs = [ref for ref, _ in result]
    if not refs or len({r['run_id'] for r in refs}) != len(refs):
        raise ValueError('Matched repeats must be distinct complete runs, not repeated copies')
    if any(r['fingerprint'] != refs[0]['fingerprint'] for r in refs):
        raise ValueError('All repetitions must use one frozen version and configuration')
    if len(refs) > refs[0]['repetition_count']:
        raise ValueError('Complete runs cannot exceed the predeclared repetition count')
    if any(left['created_at'] > right['created_at'] for left, right in zip(refs, refs[1:])):
        raise ValueError('Matched repeats cannot be reordered; use chronological generation order')
    if any((r['official_evaluator'], r['timeout_seconds']) != (refs[0]['official_evaluator'], refs[0]['timeout_seconds']) for r in refs):
        raise ValueError('Evaluation runtime or SQL timeout differs between repetitions')
    return refs, [scores for _, scores in result]


def preserve_registered_runs(previous, current):
    if current[:len(previous)] != previous:
        raise ValueError('Registered run artifacts cannot be changed, omitted, or reordered in later comparisons')


def register_baseline(path, scores, *, branch, commit):
    path = Path(path)
    with single_instance(path.with_suffix('.lock')):
        registry = load_json(path)
        refs, _ = validate_runs(scores, registry['policy'], commit)
        baseline = {'id': 'B0', 'branch': note(branch, 'branch'), 'commit': full_commit(commit), 'runs': refs}
        if registry['baseline']:
            old = registry['baseline']
            if (old['branch'], old['commit']) != (baseline['branch'], baseline['commit']):
                raise ValueError('Baseline is already frozen; it cannot be replaced')
            preserve_registered_runs(old['runs'], refs)
        registry['baseline'] = baseline
        if registry['best'] is None or registry['best']['id'] == 'B0':
            registry['best'] = dict(baseline)
        append_event(registry, 'baseline_registered', {'commit': commit, 'runs': refs})
        atomic_json(path, registry)
        return registry


def register_candidate(path, candidate_id, *, hypothesis, changed_variable, paper_notes, branch, commit):
    path = Path(path)
    if not re.fullmatch(r'[A-Za-z0-9_-]+', candidate_id) or candidate_id == 'B0':
        raise ValueError('Candidate ID must be a unique plain identifier other than B0')
    with single_instance(path.with_suffix('.lock')):
        registry = load_json(path)
        if registry['best'] is None:
            raise ValueError('Register a completed baseline before proposing a candidate')
        if candidate_id in registry['candidates']:
            raise ValueError('Candidate ID already exists; its hypothesis and commit are immutable')
        if full_commit(commit) == registry['best']['commit']:
            raise ValueError('A candidate must identify its own changed commit')
        candidate = {'id': candidate_id, 'hypothesis': note(hypothesis, 'single hypothesis'),
                     'changed_variable': note(changed_variable, 'one changed variable'),
                     'paper_notes': artifact(paper_notes), 'branch': note(branch, 'branch'), 'commit': commit,
                     'base_id': registry['best']['id'], 'base_commit': registry['best']['commit'],
                     'base_runs': registry['best']['runs'], 'registered_at': now(),
                     'status': 'pending', 'decisions': []}
        registry['candidates'][candidate_id] = candidate
        append_event(registry, 'candidate_registered', candidate.copy())
        atomic_json(path, registry)
        return registry


def verified_comparison(comparison_path, candidate, registry):
    comparison = load_json(comparison_path)
    inputs = comparison.get('input_artifacts', {})
    paths = {}
    for side in ('baseline', 'candidate'):
        entries = inputs.get(side, [])
        if not entries or any(artifact(entry['path'])['sha256'] != entry.get('sha256') for entry in entries):
            raise ValueError('Comparison input artifacts are missing or their hashes have changed')
        paths[side] = [entry['path'] for entry in entries]
    before, baseline_scores = validate_runs(paths['baseline'], registry['policy'], candidate['base_commit'])
    after, candidate_scores = validate_runs(paths['candidate'], registry['policy'], candidate['commit'])
    preserve_registered_runs(candidate['base_runs'], before)
    if registry['best']['commit'] == candidate['base_commit']:
        preserve_registered_runs(registry['best']['runs'], before)
    preserve_registered_runs(candidate.get('runs', []), after)
    if {r['run_id'] for r in before} & {r['run_id'] for r in after}:
        raise ValueError('Baseline and candidate require distinct generation run IDs')
    if (before[0]['official_evaluator'], before[0]['timeout_seconds']) != (after[0]['official_evaluator'], after[0]['timeout_seconds']):
        raise ValueError('Baseline and candidate scoring provenance differs')
    if before[0]['generation_contract'] != after[0]['generation_contract']:
        raise ValueError('Baseline and candidate model/budgets/temperature/evidence/data contract differs')
    if comparison.get('provenance_verified') is not True:
        raise ValueError('Use compare.py with evaluator summaries to verify comparison provenance')
    samples = comparison.get('bootstrap_samples', 0)
    if not isinstance(samples, int) or not 100 <= samples <= 100000:
        raise ValueError('Comparison bootstrap sample count is invalid')
    recomputed = compare_runs(baseline_scores, candidate_scores, expected_count=300,
                             bootstrap_samples=samples, seed=comparison['bootstrap_seed'],
                             checks_passed=comparison.get('engineering_checks_passed') is True,
                             max_cost_ratio=comparison.get('max_cost_ratio'))
    if any(comparison.get(key) != value for key, value in recomputed.items()):
        raise ValueError('Comparison output disagrees with its score artifacts and compare.py')
    return comparison, before, after


def record_decision(path, candidate_id, comparison_path, *, decision='auto', reason=''):
    if decision not in ('auto', 'keep', 'reject'):
        raise ValueError('Decision must be auto, keep, or reject')
    path = Path(path)
    with single_instance(path.with_suffix('.lock')):
        registry = load_json(path)
        candidate = registry['candidates'][candidate_id]
        proof = artifact(comparison_path)
        if candidate['status'] in ('kept', 'rejected'):
            if candidate['decisions'][-1]['comparison'] == proof:
                verified_comparison(comparison_path, candidate, registry)
                return registry
            raise ValueError('A finalized candidate cannot be reselected using another comparison')
        comparison, before, after = verified_comparison(comparison_path, candidate, registry)
        pending, rejected = [], []
        required = max(registry['policy']['required_matched_repeats'],
                       *(r['repetition_count'] for r in before + after))
        if comparison['matched_repeats'] < required:
            pending.append('predeclared_matched_repetitions_incomplete')
        if comparison['mean_net_correct'] <= 0:
            rejected.append('no_positive_net_gain')
        if comparison['engineering_checks_passed'] is not True:
            rejected.append('engineering_checks_not_passed')
        if comparison['all_matched_repeats_improved'] is not True:
            rejected.append('repeat_improvement_direction_not_consistent')
        if comparison['max_cost_ratio'] != registry['policy']['max_cost_ratio']:
            pending.append('comparison_cost_policy_differs_from_frozen_policy')
        monetary_known = all(comparison['cost'][side]['cost_usd']['missing_questions'] == 0 for side in ('baseline', 'candidate'))
        if comparison['cost_check_passed'] is not True:
            (rejected if monetary_known else pending).append('cost_ceiling_exceeded' if monetary_known else 'monetary_cost_unknown_with_explicit_ceiling')
        if registry['best']['commit'] != candidate['base_commit']:
            pending.append('best_version_changed_since_candidate_registration')
        if comparison['adoption_recommendation'] not in ('eligible_after_matched_repeats', 'provisional_requires_matched_repeats') and not rejected and not pending:
            pending.append('comparison_does_not_recommend_adoption')
        status = 'rejected' if decision == 'reject' or rejected else ('pending' if pending else 'kept')
        record = {'at': now(), 'requested_decision': decision, 'status': status,
                  'reason': reason, 'rejection_reasons': rejected, 'pending_reasons': pending,
                  'comparison': proof, 'matched_repeats': comparison['matched_repeats'],
                  'mean_net_correct': comparison['mean_net_correct'], 'mean_delta_ex': comparison['mean_delta_ex'],
                  'milestone_net_six_reached': comparison['milestone_net_six_reached'],
                  'small_gain_provisional': status == 'kept' and 0 < comparison['mean_net_correct'] < 6,
                  'paired_bootstrap_95_ci_delta_ex': comparison['paired_bootstrap_95_ci_delta_ex'],
                  'interpretation': comparison['interpretation'],
                  'monetary_cost_status': 'verified' if monetary_known else 'unknown',
                  'cost': comparison['cost'], 'cost_ratio': comparison['cost_ratio'],
                  'required_matched_repeats': required, 'runs': {'baseline': before, 'candidate': after}}
        candidate['decisions'].append(record)
        candidate['status'] = status
        candidate['runs'] = after
        if status == 'kept':
            registry['best'] = {key: candidate[key] for key in ('id', 'branch', 'commit', 'runs')}
            registry['consecutive_rejected_hypotheses'] = []
        elif status == 'rejected':
            sequence = registry['consecutive_rejected_hypotheses']
            hypothesis = candidate['hypothesis'].casefold().strip()
            if hypothesis not in [item['hypothesis'] for item in sequence]:
                sequence.append({'candidate_id': candidate_id, 'hypothesis': hypothesis})
            if len(sequence) >= 3 and not registry['review_due']:
                registry['review_due'] = True
                registry['review_requests'].append({'at': now(), 'candidate_ids': [item['candidate_id'] for item in sequence],
                                                    'action': 'recheck_error_taxonomy_and_experiment_design', 'status': 'due'})
        if status != 'pending':
            completed = [event['details']['candidate_id'] for event in registry['events']
                         if event['action'] == 'candidate_decision' and event['details']['status'] in ('kept', 'rejected')]
            completed.append(candidate_id)
            if len(completed) % 5 == 0:
                registry['cycle_reports_due'].append({'cycle': len(completed) // 5, 'at': now(),
                                                       'candidate_ids': completed[-5:], 'status': 'due'})
        append_event(registry, 'candidate_decision', {'candidate_id': candidate_id, **record})
        atomic_json(path, registry)
        return registry


def status(registry):
    return {'baseline_complete': registry['baseline'] is not None,
            'best_commit': registry['best']['commit'] if registry['best'] else None,
            'best_candidate': registry['best']['id'] if registry['best'] else None,
            'candidates': {key: value['status'] for key, value in registry['candidates'].items()},
            'required_matched_repeats': registry['policy']['required_matched_repeats'],
            'monetary_cost_ratio_limit': registry['policy']['max_cost_ratio'],
            'review_due': registry['review_due'], 'cycle_reports_due': registry['cycle_reports_due'],
            'event_count': len(registry['events'])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--registry', type=Path, default=DEFAULT_REGISTRY)
    commands = parser.add_subparsers(dest='command', required=True)
    init = commands.add_parser('init')
    init.add_argument('--selection-manifest', type=Path, default=DEFAULT_SELECTION)
    init.add_argument('--required-repeats', type=int, default=2)
    init.add_argument('--max-cost-ratio', type=float)
    baseline = commands.add_parser('register-baseline')
    baseline.add_argument('--scores', type=Path, nargs='+', required=True)
    baseline.add_argument('--branch', required=True)
    baseline.add_argument('--commit', required=True)
    candidate = commands.add_parser('register-candidate')
    candidate.add_argument('--id', required=True)
    candidate.add_argument('--hypothesis', required=True)
    candidate.add_argument('--changed-variable', required=True)
    candidate.add_argument('--paper-notes', type=Path, required=True)
    candidate.add_argument('--branch', required=True)
    candidate.add_argument('--commit', required=True)
    decide = commands.add_parser('record-decision')
    decide.add_argument('--id', required=True)
    decide.add_argument('--comparison', type=Path, required=True)
    decide.add_argument('--decision', choices=('auto', 'keep', 'reject'), default='auto')
    decide.add_argument('--reason', default='')
    commands.add_parser('status')
    args = parser.parse_args()
    if args.command == 'init':
        registry = initialize(args.registry, selection=args.selection_manifest,
                              required_repeats=args.required_repeats, max_cost_ratio=args.max_cost_ratio)
    elif args.command == 'register-baseline':
        registry = register_baseline(args.registry, args.scores, branch=args.branch, commit=args.commit)
    elif args.command == 'register-candidate':
        registry = register_candidate(args.registry, args.id, hypothesis=args.hypothesis, changed_variable=args.changed_variable,
                                      paper_notes=args.paper_notes, branch=args.branch, commit=args.commit)
    elif args.command == 'record-decision':
        registry = record_decision(args.registry, args.id, args.comparison, decision=args.decision, reason=args.reason)
    else:
        registry = load_json(args.registry)
    print(json.dumps(status(registry), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
