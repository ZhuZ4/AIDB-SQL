import json
from pathlib import Path
import tempfile
import unittest

from experiments.prepare_dataset import read_jsonl, sha256_file, write_json, write_jsonl
from experiments.repair_usage_export import repair_export
from experiments.state import State


class UsageRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.dataset = self.root / 'dataset'
        self.run = self.root / 'runs/fixture'
        self.questions = [{'question_id': i, 'db_id': 'fixture', 'question': 'Count', 'evidence': ''} for i in range(30)]
        self.input = self.dataset / 'generation/smoke_questions.jsonl'
        write_jsonl(self.input, self.questions)
        write_json(self.run / 'run_manifest.json', {'run_id': 'fixture', 'subset': 'smoke',
                   'fingerprint': 'frozen', 'questions_sha256': sha256_file(self.input), 'expected_questions': 30})
        self.state = State(self.root / 'state.sqlite')
        self.state.initialize('fixture', 'B0', 'frozen', self.questions)

    def tearDown(self):
        self.state.db.close()
        self.temp.cleanup()

    def prepare(self, *, complete=True):
        for i in range(30 if complete else 29):
            for unknown in ([True, False] if i == 0 else [False]):
                attempt = self.state.start('fixture', i, 900)
                result = {'question_id': i, 'run_id': 'fixture', 'attempt': attempt,
                          'status': 'failed' if unknown else 'succeeded',
                          'submitted_final_sql': '' if unknown else 'SELECT 1',
                          'final_sql_source': '' if unknown else 'submit_final_sql',
                          'llm_calls': 2 if unknown else 1, 'duration_seconds': 1.0,
                          'prompt_tokens': 10, 'completion_tokens': 2,
                          'usage': {'usage_complete': not unknown, 'calls_without_usage': int(unknown),
                                    'total_tokens': 12, 'cached_tokens': 5, 'reasoning_tokens': 0}}
                self.state.finish('fixture', i, result, pending=unknown)
                write_json(self.run / 'traces' / f'{i}.attempt{attempt}.result.json', result)
        path = self.run / 'predictions.jsonl'
        self.state.export('fixture', self.questions, path)
        rows = read_jsonl(path)
        # Reproduce the historic exporter without depending on its implementation.
        rows[0]['usage_unknown'] = False
        for metric in ('prompt_tokens', 'completion_tokens', 'total_tokens', 'cached_tokens', 'reasoning_tokens'):
            rows[0][metric] = rows[0][metric + '_known']
        write_jsonl(path, rows)
        return path

    def test_preview_preserves_source_and_repairs_retry_unknown(self):
        path = self.prepare()
        before = path.read_bytes()
        report = repair_export(self.run, self.dataset)
        self.assertFalse(report['published'])
        self.assertEqual(path.read_bytes(), before)
        corrected = read_jsonl(Path(report['corrected_copy']))[0]
        self.assertTrue(corrected['usage_unknown'])
        self.assertIsNone(corrected['total_tokens'])
        self.assertEqual(corrected['total_tokens_known'], 24)
        self.assertEqual(corrected['llm_calls'], 3)
        self.assertEqual(corrected['duration_seconds'], 2)
        self.assertEqual(corrected['submitted_final_sql'], 'SELECT 1')

    def test_publish_preserves_original_and_changes_only_usage(self):
        path = self.prepare()
        original = path.read_bytes()
        report = repair_export(self.run, self.dataset, publish=True)
        self.assertTrue(report['published'])
        self.assertEqual(Path(report['original_backup']).read_bytes(), original)
        self.assertEqual(sha256_file(path), report['corrected_sha256'])
        self.assertEqual(report['changed_questions'][0]['question_id'], 0)

    def test_sql_drift_is_rejected(self):
        path = self.prepare()
        rows = read_jsonl(path)
        rows[0]['submitted_final_sql'] = 'SELECT 2'
        write_jsonl(path, rows)
        before = path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'protected fields'):
            repair_export(self.run, self.dataset, publish=True)
        self.assertEqual(path.read_bytes(), before)

    def test_partial_publish_is_rejected(self):
        path = self.prepare(complete=False)
        before = path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'partial'):
            repair_export(self.run, self.dataset, publish=True)
        self.assertEqual(path.read_bytes(), before)

    def test_already_scored_publish_is_rejected(self):
        path = self.prepare()
        before = path.read_bytes()
        (self.run / 'scores.jsonl').write_text('{}\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'already evaluated'):
            repair_export(self.run, self.dataset, publish=True)
        self.assertEqual(path.read_bytes(), before)

    def test_active_worker_blocks_preview(self):
        self.prepare(complete=False)
        self.state.start('fixture', 29, 900)
        with self.assertRaisesRegex(ValueError, 'Active'):
            repair_export(self.run, self.dataset)


if __name__ == '__main__':
    unittest.main()
