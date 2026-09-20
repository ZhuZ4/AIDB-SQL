"""Synthetic artifact contracts for the registry; no APIs or Git mutations."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from experiments.compare import compare_runs
from experiments.evaluate import METRIC, OFFICIAL_COMMIT, OFFICIAL_HASHES
from experiments.prepare_dataset import sha256_file, write_jsonl
from experiments.research_registry import (
    artifact, initialize, load_json, record_decision, register_baseline, register_candidate, status,
)
from experiments.state import atomic_json


class ResearchRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / 'registry.json'
        self.selection = self.root / 'selection.json'
        self.source_hash = '0' * 64
        atomic_json(self.selection, {'question_ids': list(range(300)), 'source_sha256': self.source_hash})
        initialize(self.path, selection=self.selection)
        self.notes = self.root / 'paper_notes.md'
        self.notes.write_text('Synthetic offline fixture: one hypothesis and one controlled change.\n', encoding='utf-8')
        self.a = 'a' * 40
        self.b = 'b' * 40
        self.baseline = [self.make_run('b0_1', self.a, 100), self.make_run('b0_2', self.a, 101)]

    def make_run(self, run_id, commit, correct, *, cost_limit=None, repetitions=2):
        folder = self.root / run_id
        folder.mkdir(exist_ok=True)
        scores = [{'question_id': i, 'db_id': 'example', 'difficulty': 'simple',
                   'ex': int(i < correct), 'status': 'scored', 'llm_calls': 3,
                   'duration_seconds': 2, 'prompt_tokens': None, 'completion_tokens': None} for i in range(300)]
        predictions = [{'question_id': i, 'db_id': 'example', 'run_id': run_id, 'status': 'succeeded',
                        'submitted_final_sql': 'SELECT 1', 'final_sql_source': 'submit_final_sql'} for i in range(300)]
        write_jsonl(folder / 'scores.jsonl', scores)
        write_jsonl(folder / 'predictions.jsonl', predictions)
        frozen = {'git_commit': commit, 'code_sha256': {'agent.py': commit + '0' * 24},
                  'questions_sha256': '1' * 64, 'dataset_manifest_sha256': '2' * 64,
                  'index_manifest_sha256': '3' * 64, 'subset': 'all',
                  'config': {'repetition_count': repetitions, 'candidate_cost_ratio_limit': cost_limit,
                             'model': 'deepseek-v4.1-flash', 'max_llm_calls': 40,
                             'question_timeout_seconds': 900, 'sql_timeout_seconds': 30, 'temperature': 0}}
        manifest = {**frozen, 'fingerprint': hashlib.sha256(json.dumps(frozen, sort_keys=True).encode()).hexdigest(),
                    'run_id': run_id, 'created_at': 1, 'expected_questions': 300}
        atomic_json(folder / 'run_manifest.json', manifest)
        summary = {'metric': METRIC, 'subset': 'all', 'completed_generation_records': 300,
                   'scoring_denominator': 300, 'overall': {'count': 300, 'correct': correct},
                   'source_sha256': self.source_hash, 'selection_manifest_sha256': sha256_file(self.selection),
                   'official_evaluator': {'commit': OFFICIAL_COMMIT, 'file_hashes': OFFICIAL_HASHES, 'metric': METRIC},
                   'timeout_seconds': 30, 'scores_sha256': sha256_file(folder / 'scores.jsonl'),
                   'predictions_sha256': sha256_file(folder / 'predictions.jsonl')}
        atomic_json(folder / 'summary.json', summary)
        return folder / 'scores.jsonl'

    def register_base(self, paths=None):
        return register_baseline(self.path, paths or self.baseline, branch='codex/dev_legion', commit=self.a)

    def candidate(self, candidate_id='C1', commit=None, hypothesis=None):
        return register_candidate(self.path, candidate_id, hypothesis=hypothesis or f'Hypothesis {candidate_id}',
                                  changed_variable='one retrieval variable', paper_notes=self.notes,
                                  branch=f'dev_{candidate_id}', commit=commit or self.b)

    def comparison(self, candidates, *, baseline=None, checks=True, max_cost_ratio=None, name='comparison'):
        before = baseline or self.baseline
        read = lambda path: [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
        value = compare_runs([read(p) for p in before], [read(p) for p in candidates],
                             bootstrap_samples=100, checks_passed=checks, max_cost_ratio=max_cost_ratio)
        value['input_artifacts'] = {'baseline': [artifact(p) for p in before], 'candidate': [artifact(p) for p in candidates]}
        value['provenance_verified'] = True
        path = self.root / f'{name}.json'
        atomic_json(path, value)
        return path

    def test_init_is_empty_and_freezes_policy(self):
        registry = initialize(self.path, selection=self.selection)
        self.assertFalse(status(registry)['baseline_complete'])
        self.assertIsNone(status(registry)['best_commit'])
        with self.assertRaisesRegex(ValueError, 'already frozen'):
            initialize(self.path, selection=self.selection, required_repeats=3)
        with self.assertRaisesRegex(ValueError, 'completed baseline'):
            self.candidate()

    def test_incomplete_or_duplicate_scores_cannot_register_baseline(self):
        path = self.baseline[0]
        original = path.read_text(encoding='utf-8').splitlines()
        for bad in (original[:-1], original[:-1] + [original[0]]):
            path.write_text('\n'.join(bad) + '\n', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, '300 unique'):
                self.register_base([path])
            self.assertIsNone(load_json(self.path)['baseline'])

    def test_score_or_manifest_tampering_is_rejected(self):
        path = self.baseline[0]
        path.write_text(path.read_text(encoding='utf-8') + '\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'does not bind'):
            self.register_base([path])
        self.baseline[0] = self.make_run('b0_1', self.a, 100)
        manifest_path = path.parent / 'run_manifest.json'
        manifest = load_json(manifest_path)
        manifest['code_sha256']['agent.py'] = 'changed'
        atomic_json(manifest_path, manifest)
        with self.assertRaisesRegex(ValueError, 'fingerprint'):
            self.register_base([path])

    def test_mixed_run_prediction_assembly_is_forbidden(self):
        path = self.baseline[0]
        predictions_path = path.parent / 'predictions.jsonl'
        predictions = [json.loads(line) for line in predictions_path.read_text(encoding='utf-8').splitlines()]
        predictions[17]['run_id'] = 'another_run'
        write_jsonl(predictions_path, predictions)
        summary_path = path.parent / 'summary.json'
        summary = load_json(summary_path)
        summary['predictions_sha256'] = sha256_file(predictions_path)
        atomic_json(summary_path, summary)
        with self.assertRaisesRegex(ValueError, 'Mixed-run'):
            self.register_base([path])

    def test_kept_requires_two_improved_repeats_and_preserves_unknown_cost(self):
        self.register_base()
        self.candidate()
        candidates = [self.make_run('c1_1', self.b, 107), self.make_run('c1_2', self.b, 108)]
        proof = self.comparison(candidates)
        result = record_decision(self.path, 'C1', proof)
        self.assertEqual(result['best']['commit'], self.b)
        self.assertEqual(result['candidates']['C1']['status'], 'kept')
        decision = result['candidates']['C1']['decisions'][-1]
        self.assertEqual(decision['matched_repeats'], 2)
        self.assertEqual(decision['monetary_cost_status'], 'unknown')
        self.assertIsNone(decision['cost']['candidate']['cost_usd']['total'])
        self.assertIsNone(decision['cost_ratio'])
        self.assertEqual(record_decision(self.path, 'C1', proof), result)

    def test_one_repeat_remains_pending_then_accepts_full_matched_repeats(self):
        self.register_base([self.baseline[0]])
        self.candidate()
        candidates = [self.make_run('c1_1', self.b, 107), self.make_run('c1_2', self.b, 108)]
        single = self.comparison(candidates[:1], baseline=self.baseline[:1], name='single')
        pending = record_decision(self.path, 'C1', single, decision='keep')
        self.assertEqual(pending['candidates']['C1']['status'], 'pending')
        self.assertEqual(pending['best']['commit'], self.a)
        complete = record_decision(self.path, 'C1', self.comparison(candidates))
        self.assertEqual(complete['candidates']['C1']['status'], 'kept')
        self.assertEqual(len(complete['candidates']['C1']['decisions']), 2)

    def test_repeating_same_run_is_not_a_matched_repetition(self):
        self.register_base([self.baseline[0]])
        self.candidate()
        candidate = self.make_run('c1_1', self.b, 107)
        proof = self.comparison([candidate, candidate], baseline=[self.baseline[0], self.baseline[0]])
        with self.assertRaisesRegex(ValueError, 'distinct complete runs'):
            record_decision(self.path, 'C1', proof)
        self.assertEqual(load_json(self.path)['best']['commit'], self.a)

    def test_missing_engineering_checks_rejects_positive_gain(self):
        self.register_base()
        self.candidate()
        candidates = [self.make_run('c1_1', self.b, 107), self.make_run('c1_2', self.b, 108)]
        rejected = record_decision(self.path, 'C1', self.comparison(candidates, checks=False))
        self.assertEqual(rejected['candidates']['C1']['status'], 'rejected')
        self.assertEqual(rejected['best']['commit'], self.a)

    def test_unknown_money_cannot_pass_a_frozen_monetary_ceiling(self):
        self.path = self.root / 'capped_registry.json'
        initialize(self.path, selection=self.selection, max_cost_ratio=1.5)
        self.baseline = [self.make_run('cap_b0_1', self.a, 100, cost_limit=1.5),
                         self.make_run('cap_b0_2', self.a, 101, cost_limit=1.5)]
        self.register_base()
        self.candidate()
        candidates = [self.make_run('cap_c1_1', self.b, 107, cost_limit=1.5),
                      self.make_run('cap_c1_2', self.b, 108, cost_limit=1.5)]
        result = record_decision(self.path, 'C1', self.comparison(candidates, max_cost_ratio=1.5))
        self.assertEqual(result['candidates']['C1']['status'], 'pending')
        self.assertEqual(result['best']['commit'], self.a)
        self.assertIn('monetary_cost_unknown_with_explicit_ceiling', result['candidates']['C1']['decisions'][-1]['pending_reasons'])

    def test_comparison_cannot_forge_metrics(self):
        self.register_base()
        self.candidate()
        candidates = [self.make_run('c1_1', self.b, 97), self.make_run('c1_2', self.b, 98)]
        proof = self.comparison(candidates)
        forged = load_json(proof)
        forged['mean_net_correct'] = 10
        atomic_json(proof, forged)
        with self.assertRaisesRegex(ValueError, 'disagrees'):
            record_decision(self.path, 'C1', proof)
        self.assertEqual(load_json(self.path)['best']['commit'], self.a)

    def test_registered_baseline_artifacts_cannot_be_replaced(self):
        self.register_base()
        self.make_run('b0_1', self.a, 99)
        with self.assertRaisesRegex(ValueError, 'cannot be changed'):
            self.register_base()

    def test_model_budget_or_evidence_changes_cannot_be_adopted_as_one_method(self):
        self.register_base()
        self.candidate()
        candidates = [self.make_run('c1_1', self.b, 107), self.make_run('c1_2', self.b, 108)]
        originals = [load_json(path.parent / 'run_manifest.json') for path in candidates]
        mutations = [('model', 'different-larger-model'), ('max_llm_calls', 80),
                     ('question_timeout_seconds', 1800), ('sql_timeout_seconds', 60),
                     ('temperature', 1), ('use_evidence', False)]
        for field, changed in mutations:
            for path, original in zip(candidates, originals):
                manifest = copy.deepcopy(original)
                manifest['config'][field] = changed
                frozen = {key: value for key, value in manifest.items()
                          if key not in ('fingerprint', 'run_id', 'created_at', 'expected_questions')}
                manifest['fingerprint'] = hashlib.sha256(json.dumps(frozen, sort_keys=True).encode()).hexdigest()
                atomic_json(path.parent / 'run_manifest.json', manifest)
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'contract differs'):
                record_decision(self.path, 'C1', self.comparison(candidates))
            self.assertEqual(load_json(self.path)['best']['commit'], self.a)

    def test_three_failed_hypotheses_request_review_and_five_mark_cycle(self):
        self.register_base()
        for index in range(1, 6):
            candidate_id = f'C{index}'
            commit = f'{index:040x}'
            self.candidate(candidate_id, commit=commit)
            candidates = [self.make_run(f'c{index}_1', commit, 99), self.make_run(f'c{index}_2', commit, 100)]
            result = record_decision(self.path, candidate_id, self.comparison(candidates, name=f'comparison_{index}'))
            self.assertEqual(result['candidates'][candidate_id]['status'], 'rejected')
            self.assertEqual(result['best']['commit'], self.a)
            self.assertEqual(result['review_due'], index >= 3)
        self.assertEqual(len(result['review_requests']), 1)
        self.assertEqual(result['cycle_reports_due'][0]['candidate_ids'], ['C1', 'C2', 'C3', 'C4', 'C5'])
        self.assertEqual(result['cycle_reports_due'][0]['cycle'], 1)


if __name__ == '__main__':
    unittest.main()
