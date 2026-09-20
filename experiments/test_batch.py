"""Offline parent-process integration tests for dispatch and durable recovery.

Only worker execution and Git identity are substituted. Dataset verification,
configuration freezing, file locks, manifests, SQLite state, retry decisions,
and prediction exports run against isolated temporary files. No API is called.
"""
from contextlib import ExitStack, closing, redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from experiments import run_batch as batch
from experiments.state import atomic_json
from experiments.worker import CONFIG_KEYS, classify_error, validate_input


class BatchBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / 'project'
        self.local = self.root / '.local-services' / 'experiments'
        self.dataset = self.local / 'dataset'
        self.generation = self.dataset / 'generation' / 'smoke_questions.jsonl'
        self.generation.parent.mkdir(parents=True)
        for name in ('agent.py', 'utils.py', 'AGENTS.md'):
            (self.root / name).write_text('frozen fixture\n', encoding='utf-8')
        self.database_root = Path(self.temporary.name) / 'business'
        database = self.database_root / 'example' / 'example.sqlite'
        database.parent.mkdir(parents=True)
        with closing(sqlite3.connect(database)) as connection:
            connection.execute('CREATE TABLE sample(value INTEGER)')
            connection.commit()
        self.questions = [{'question_id': qid, 'db_id': 'example',
                           'question': f'Question {qid}', 'evidence': 'Fixture evidence'} for qid in range(30)]
        self.generation.write_text(''.join(json.dumps(q) + '\n' for q in self.questions), encoding='utf-8')
        atomic_json(self.dataset / 'dataset_manifest.json', {
            'file_hashes': {'generation/smoke_questions.jsonl': batch.sha(self.generation)},
            'smoke_question_ids': list(range(30)), 'question_ids': list(range(30)),
            'db_root': str(self.database_root),
            'databases': {'example': {'path': str(database), 'sha256': batch.sha(database)}},
        })
        source = self.root.parent / 'minidev' / 'MINIDEV' / 'mini_dev_sqlite.json'
        atomic_json(source, self.questions)
        atomic_json(self.root / 'experiments' / 'mini_dev_300_manifest.json', {'source_sha256': batch.sha(source)})
        self.env_path = self.root / '.env'
        self.env_path.write_text('\n'.join([
            'LITE_LLM_MODEL_NAME=deepseek-v4.1-flash',
            'LITE_LLM_BASE_URL=http://offline.invalid/v1',
            'BIRD_DEV_COLUMN_TABLE=columns_test', 'BIRD_DEV_INDEX_VERSION=column_dual_v1_test',
            'BIRD_DEV_SCHEMA=bird_dev_emb_v2', 'EMBEDDING_DIM=1024',
        ]) + '\n', encoding='utf-8')
        self.config_path = self.root / 'experiment.json'
        self.config = {
            'experiment_id': 'offline_boundaries', 'model': 'deepseek-v4.1-flash',
            'env_file': str(self.env_path), 'db_root': str(self.database_root),
            'max_llm_calls': 40, 'question_timeout_seconds': 900, 'sql_timeout_seconds': 30,
            'max_transient_retries': 3, 'retry_backoff_seconds': [5, 20, 60],
            'concurrency': 1, 'temperature': 0, 'audit_only': 'must not reach worker',
        }
        atomic_json(self.config_path, self.config)
        atomic_json(self.root / '.local-services' / 'column-index' / 'column_dual_v1_test' / 'manifest.json',
                    {'counts': {'columns': 798}, 'table': 'columns_test', 'index_version': 'column_dual_v1_test'})
        self.patches = self.enterContext(ExitStack())
        self.patches.enter_context(patch.object(batch, 'ROOT', self.root))
        self.patches.enter_context(patch.object(batch, 'LOCAL', self.local))
        self.patches.enter_context(patch.object(batch.subprocess, 'check_output', return_value='offline-commit\n'))
        self.sleep = self.patches.enter_context(patch.object(batch.time, 'sleep'))
        self.dispatched = []

    @staticmethod
    def success(calls=2, seconds=1):
        return {'status': 'succeeded', 'submitted_final_sql': 'SELECT 1', 'error_category': '',
                'llm_calls': calls, 'prompt_tokens': 17, 'completion_tokens': 3,
                'duration_seconds': seconds, 'usage_unknown': False}

    @staticmethod
    def throttled(calls, seconds, **extra):
        error = {'status_code': 429, 'message': 'rate limited'}
        category, retryable = classify_error(error)
        return {'status': 'failed', 'submitted_final_sql': '', 'error_category': category,
                'retryable': retryable, 'error': error, 'llm_calls': calls,
                'prompt_tokens': None, 'completion_tokens': None,
                'duration_seconds': seconds, **extra}

    def invoke(self, worker, max_questions=None):
        def dispatch(payload, run_dir, timeout, state, run_id, qid, attempt):
            self.dispatched.append({'payload': payload, 'timeout': timeout, 'attempt': attempt})
            return worker(payload, run_dir, timeout, state, run_id, qid, attempt)

        with patch.object(batch, 'run_worker', side_effect=dispatch), redirect_stdout(io.StringIO()):
            return batch.run_batch(self.dataset, 'offline', self.config_path, subset='smoke', max_questions=max_questions)

    def query(self, sql):
        with closing(sqlite3.connect(self.local / 'state.sqlite')) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute(sql)]

    def predictions(self):
        path = self.local / 'runs' / 'offline' / 'predictions.jsonl'
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]

    def test_structured_balance_error_stops_globally_and_preserves_pending(self):
        error = {'type': 'APIError', 'message': 'insufficient_balance', 'status_code': 429}
        category, retryable = classify_error(error)
        response = {'status': 'failed', 'submitted_final_sql': '', 'error_category': category,
                    'retryable': retryable, 'error': error, 'llm_calls': 1, 'duration_seconds': 2}
        result = self.invoke(lambda *args: response)
        self.assertEqual(result['phase'], 'stopped_insufficient_balance')
        self.assertEqual(len(self.dispatched), 1)
        rows = self.query('SELECT status,attempt FROM questions ORDER BY ordinal')
        self.assertEqual(rows[0], {'status': 'pending', 'attempt': 1})
        self.assertTrue(all(row == {'status': 'pending', 'attempt': 0} for row in rows[1:]))
        run = self.query('SELECT phase,reason FROM runs')[0]
        self.assertEqual(run['phase'], 'stopped_insufficient_balance')
        self.assertEqual(json.loads(run['reason']), error)
        self.assertEqual(self.predictions(), [])
        self.assertEqual(len(self.query('SELECT * FROM attempts')), 1)

    def test_429_retries_cannot_exceed_cumulative_call_budget(self):
        responses = iter([self.throttled(25, 100), self.throttled(15, 120)])
        self.invoke(lambda *args: next(responses), max_questions=1)
        self.assertEqual([d['payload']['config']['max_llm_calls'] for d in self.dispatched], [40, 15])
        self.assertEqual([d['timeout'] for d in self.dispatched], [900, 795])
        prediction = self.predictions()[0]
        self.assertEqual(prediction['error_category'], 'call_budget_exhausted')
        self.assertEqual(prediction['llm_calls'], 40)
        self.assertEqual(prediction['submitted_final_sql'], '')
        self.assertEqual(len(self.dispatched), 2)

    def test_429_retry_gets_only_remaining_question_time(self):
        responses = iter([self.throttled(10, 890),
                          {'status': 'timeout', 'error_category': 'timeout', 'submitted_final_sql': '',
                           'llm_calls': 1, 'duration_seconds': 5}])
        self.invoke(lambda *args: next(responses), max_questions=1)
        self.assertEqual([d['timeout'] for d in self.dispatched], [900, 5])
        self.assertEqual([d['payload']['config']['question_timeout_seconds'] for d in self.dispatched], [900, 5])
        self.assertEqual(self.predictions()[0]['status'], 'timeout')
        self.assertEqual(len(self.dispatched), 2)

    def test_resume_never_redispatches_completed_questions(self):
        self.invoke(lambda *args: self.success(), max_questions=2)
        self.invoke(lambda *args: self.success(), max_questions=1)
        self.assertEqual([d['payload']['question_id'] for d in self.dispatched], [0, 1, 2])
        self.assertEqual([p['question_id'] for p in self.predictions()], [0, 1, 2])
        self.assertTrue(all(p['attempt'] == 1 for p in self.predictions()))

    def test_data_link_policy_reaches_worker_and_frozen_manifest(self):
        self.config['data_link_policy'] = 'explicit_projection_v1'
        atomic_json(self.config_path, self.config)
        self.invoke(lambda *args: self.success(), max_questions=1)
        payload = self.dispatched[0]['payload']
        self.assertEqual(validate_input(payload)['data_link_policy'], 'explicit_projection_v1')
        manifest = json.loads((self.local / 'runs/offline/run_manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['config']['data_link_policy'], 'explicit_projection_v1')

    def test_unknown_data_link_policy_cannot_dispatch(self):
        self.config['data_link_policy'] = 'unknown-policy'
        atomic_json(self.config_path, self.config)
        with self.assertRaisesRegex(ValueError, 'Unknown data_link_policy'):
            self.invoke(lambda *args: self.success(), max_questions=1)
        self.assertEqual(self.dispatched, [])

    def test_orphan_final_result_recovers_without_rebilling(self):
        def completed_before_parent_crash(payload, run_dir, timeout, state, run_id, qid, attempt):
            atomic_json(run_dir / 'traces' / f'{qid}.attempt{attempt}.result.json', self.success())
            raise KeyboardInterrupt('simulate parent crash before State.finish')

        with self.assertRaises(KeyboardInterrupt):
            self.invoke(completed_before_parent_crash)
        self.assertEqual(self.query('SELECT status FROM questions WHERE question_id=0')[0]['status'], 'running')
        self.invoke(lambda *args: self.success(), max_questions=1)
        self.assertEqual([d['payload']['question_id'] for d in self.dispatched], [0, 1])
        self.assertEqual(self.query('SELECT attempt FROM questions WHERE question_id=0')[0]['attempt'], 1)
        self.assertEqual([p['question_id'] for p in self.predictions()], [0, 1])

    def test_unknown_usage_survives_a_successful_retry(self):
        responses = iter([self.throttled(1, 3, usage_unknown=True), self.success(calls=3)])
        self.invoke(lambda *args: next(responses), max_questions=1)
        prediction = self.predictions()[0]
        self.assertTrue(prediction['usage_unknown'])
        self.assertEqual(prediction['llm_calls'], 4)
        self.assertIsNone(prediction['prompt_tokens'])
        self.assertIsNone(prediction['completion_tokens'])
        self.assertEqual(prediction['prompt_tokens_known'], 17)
        self.assertEqual(prediction['completion_tokens_known'], 3)
        self.assertEqual(prediction['attempt_count'], 2)

    def test_worker_payload_contains_only_whitelisted_generation_config(self):
        self.invoke(lambda *args: self.success(), max_questions=1)
        payload = self.dispatched[0]['payload']
        config = validate_input(payload)
        self.assertLessEqual(set(config), CONFIG_KEYS)
        self.assertNotIn('audit_only', config)
        self.assertNotIn('provider_endpoint_sha256', config)
        self.assertNotIn('services_sha256', config)
        self.assertNotIn('SQL', payload)
        self.assertEqual(config['index_version'], 'column_dual_v1_test')
        self.assertEqual(config['index_table'], 'columns_test')
        self.assertEqual(payload['evidence'], 'Fixture evidence')

    def test_code_drift_stops_before_the_next_dispatch(self):
        def changed_code(*args):
            (self.root / 'agent.py').write_text('changed code\n', encoding='utf-8')
            return self.success()

        result = self.invoke(changed_code)
        self.assertEqual(result['phase'], 'waiting_code_changed')
        self.assertEqual(len(self.dispatched), 1)
        self.assertEqual(len(self.predictions()), 1)
        self.assertEqual(self.query('SELECT COUNT(*) AS n FROM questions WHERE status="pending"')[0]['n'], 29)

    def test_service_configuration_drift_stops_before_next_dispatch(self):
        def changed_service(*args):
            with self.env_path.open('a', encoding='utf-8') as stream:
                stream.write('EMBEDDING_API_URL=http://changed.invalid/v1\n')
            return self.success()

        result = self.invoke(changed_service)
        self.assertEqual(result['phase'], 'waiting_configuration_changed')
        self.assertEqual(len(self.dispatched), 1)


if __name__ == '__main__':
    unittest.main()
