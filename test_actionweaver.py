import copy
from concurrent.futures import ThreadPoolExecutor
import io
import json
from pathlib import Path
import random
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from actionweaver import Ledger, NoRedirect, canonical, parse_json, propose_ollama, validate

SOURCE = Path('example-notes.txt').read_text()
PLAN = parse_json(Path('example-plan.json').read_text())


class ValidationTests(unittest.TestCase):
    def test_valid_plan_topological_order(self):
        self.assertEqual(validate(SOURCE, PLAN), ['t1', 't2', 't3'])

    def test_input_order_independent_topology(self):
        plan = copy.deepcopy(PLAN)
        plan['tasks'].reverse()
        self.assertEqual(validate(SOURCE, plan), ['t1', 't2', 't3'])

    def test_wrong_quote_and_wrong_line(self):
        for changes in ({'line': 1}, {'quote': 'Ava approved a payment'}, {'line': True}, {'line': 0}):
            plan = copy.deepcopy(PLAN)
            plan['tasks'][0]['evidence'][0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate(SOURCE, plan)

    def test_unknown_owner_and_null_owner(self):
        plan = copy.deepcopy(PLAN)
        plan['tasks'][0]['owner'] = 'MadeUp'
        with self.assertRaises(ValueError):
            validate(SOURCE, plan)
        plan['tasks'][0]['owner'] = None
        validate(SOURCE, plan)

    def test_unknown_action_fields_are_rejected(self):
        plan = copy.deepcopy(PLAN)
        plan['tasks'][0]['shell_command'] = 'echo unexpected'
        with self.assertRaises(ValueError):
            validate(SOURCE, plan)

    def test_cycles_and_unknown_dependencies(self):
        for dependencies in (['t3'], ['missing'], ['t1'], ['t2', 't2']):
            plan = copy.deepcopy(PLAN)
            plan['tasks'][0]['depends_on'] = dependencies
            with self.subTest(dependencies=dependencies), self.assertRaises(ValueError):
                validate(SOURCE, plan)

    def test_duplicate_ids(self):
        plan = copy.deepcopy(PLAN)
        plan['tasks'][1]['id'] = 't1'
        with self.assertRaises(ValueError):
            validate(SOURCE, plan)

    def test_task_budget_and_empty_plan(self):
        for plan in ({'tasks': []}, {'tasks': PLAN['tasks']*9}):
            with self.assertRaises(ValueError):
                validate(SOURCE, plan)

    def test_strict_json_duplicate_keys_and_nonfinite(self):
        for raw in ('{"tasks":[],"tasks":[]}', '{"x":NaN}', '{"x":Infinity}'):
            with self.assertRaises(ValueError):
                parse_json(raw)

    def test_source_size_and_control_character_limits(self):
        for source in ('', 'x'*50001, '\x1b'+SOURCE):
            with self.assertRaises(ValueError):
                validate(source, PLAN)

    def test_title_control_characters(self):
        plan = copy.deepcopy(PLAN)
        plan['tasks'][0]['title'] = '\x1b[31mspoof'
        with self.assertRaises(ValueError):
            validate(SOURCE, plan)

    def test_randomized_dag_order(self):
        for seed in range(50):
            rng = random.Random(seed)
            tasks = []
            for i in range(1, 21):
                tasks.append({'id': f't{i}', 'title': 'Document the worker', 'owner': 'Ava',
                              'evidence': PLAN['tasks'][0]['evidence'],
                              'depends_on': [f't{j}' for j in range(1, i) if rng.random() < .15]})
            rng.shuffle(tasks)
            order = validate(SOURCE, {'tasks': tasks})
            position = {id: i for i, id in enumerate(order)}
            self.assertTrue(all(position[d] < position[t['id']] for t in tasks for d in t['depends_on']))


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)/'test.sqlite'
        self.ledger = Ledger(self.path)
        self.addCleanup(self.ledger.db.close)
        self.digest = self.ledger.stage(SOURCE, PLAN, 'fixture')

    def approve(self):
        self.ledger.decide(self.digest, 'test-reviewer')

    def test_pending_cannot_apply(self):
        with self.assertRaises(ValueError):
            self.ledger.apply(self.digest)
        self.assertEqual(self.ledger.board(self.digest)['tasks'], [])

    def test_review_includes_full_source_and_evidence(self):
        review = self.ledger.review(self.digest)
        self.assertEqual(review['source'], SOURCE)
        self.assertEqual(review['plan'], PLAN)

    def test_replay_does_not_duplicate_staging_or_audit(self):
        self.assertEqual(self.ledger.stage(SOURCE, PLAN), self.digest)
        self.assertEqual(len(self.ledger.history(self.digest)), 1)

    def test_changed_source_and_changed_plan_require_new_approval(self):
        changed = self.ledger.stage(SOURCE+'\nNew note', PLAN)
        plan = copy.deepcopy(PLAN)
        plan['tasks'][0]['title'] = 'Updated title'
        second = self.ledger.stage(SOURCE, plan)
        self.approve()
        for digest in (changed, second):
            self.assertNotEqual(digest, self.digest)
            with self.assertRaises(ValueError):
                self.ledger.apply(digest)

    def test_wrong_digest_rejected(self):
        with self.assertRaises(ValueError):
            self.ledger.decide('f'*64, 'reviewer')

    def test_rejected_proposal_cannot_apply_or_approve(self):
        self.ledger.decide(self.digest, 'reviewer', approve=False)
        for action in (lambda: self.ledger.apply(self.digest), self.approve):
            with self.assertRaises(ValueError):
                action()

    def test_apply_retry_is_noop(self):
        self.approve()
        self.assertEqual(self.ledger.apply(self.digest), 3)
        self.assertEqual(self.ledger.apply(self.digest), 0)
        self.assertEqual(len(self.ledger.board(self.digest)['tasks']), 3)
        self.assertEqual([r['action'] for r in self.ledger.history(self.digest)], ['staged', 'approved', 'applied'])

    def test_completion_obeys_dependencies(self):
        self.approve()
        self.ledger.apply(self.digest)
        self.assertEqual([t['status'] for t in self.ledger.board(self.digest)['tasks']], ['ready', 'blocked', 'blocked'])
        with self.assertRaises(ValueError):
            self.ledger.complete(self.digest, 't2', 'reviewer')
        self.ledger.complete(self.digest, 't1', 'reviewer')
        self.ledger.complete(self.digest, 't1', 'reviewer')
        self.assertEqual([t['status'] for t in self.ledger.board(self.digest)['tasks']], ['done', 'ready', 'blocked'])

    def test_failure_mid_apply_rolls_back_all_tasks(self):
        self.approve()
        self.ledger.db.execute("CREATE TRIGGER fail_second BEFORE INSERT ON tasks WHEN NEW.id='t2' BEGIN SELECT RAISE(ABORT,'injected failure'); END")
        with self.assertRaises(sqlite3.Error):
            self.ledger.apply(self.digest)
        self.assertEqual(self.ledger.board(self.digest)['tasks'], [])
        self.assertEqual(self.ledger.review(self.digest)['state'], 'approved')
        self.ledger.db.execute('DROP TRIGGER fail_second')
        self.assertEqual(self.ledger.apply(self.digest), 3)

    def test_reopen_after_commit_and_retry(self):
        self.approve()
        self.ledger.apply(self.digest)
        with Ledger(self.path) as restarted:
            self.assertEqual(restarted.apply(self.digest), 0)
            self.assertEqual(len(restarted.board(self.digest)['tasks']), 3)

    def test_concurrent_apply_creates_one_task_set(self):
        self.approve()
        def worker(_):
            with Ledger(self.path) as ledger:
                return ledger.apply(self.digest)
        with ThreadPoolExecutor(max_workers=8) as pool:
            created = list(pool.map(worker, range(24)))
        self.assertEqual(sum(created), 3)
        self.assertEqual(created.count(3), 1)

    def test_integrity_check_catches_modified_plan(self):
        plan = copy.deepcopy(PLAN)
        plan['tasks'][0]['title'] = 'Tampered title'
        self.ledger.db.execute('UPDATE proposals SET plan=? WHERE digest=?', (canonical(plan), self.digest))
        self.ledger.db.commit()
        with self.assertRaises(ValueError):
            self.ledger.review(self.digest)

    def test_board_read_uses_consistent_snapshot(self):
        self.approve()
        original = self.ledger.review
        def review_then_apply(digest):
            row = original(digest)
            with Ledger(self.path) as writer:
                writer.apply(digest)
            return row
        with patch.object(self.ledger, 'review', review_then_apply):
            board = self.ledger.board(self.digest)
        self.assertEqual(board['state'], 'approved')
        self.assertEqual(board['tasks'], [])
        self.assertEqual(len(self.ledger.board(self.digest)['tasks']), 3)

    def test_approval_replay_is_idempotent(self):
        self.approve()
        self.approve()
        self.assertEqual([r['action'] for r in self.ledger.history(self.digest)], ['staged', 'approved'])

    def test_invalid_proposal_has_no_side_effect(self):
        plan = copy.deepcopy(PLAN)
        plan['tasks'][0]['evidence'][0]['quote'] = 'Not in source'
        with self.assertRaises(ValueError):
            self.ledger.stage(SOURCE, plan)
        self.assertEqual(self.ledger.db.execute('SELECT COUNT(*) FROM proposals').fetchone()[0], 1)


class ProviderTests(unittest.TestCase):
    def call(self, envelope):
        with patch('actionweaver.urllib.request.build_opener') as build:
            build.return_value.open.return_value = io.BytesIO(envelope)
            result = propose_ollama(SOURCE, 'test-model')
            request = build.return_value.open.call_args.args[0]
            payload = json.loads(request.data)
            self.assertEqual(request.full_url, 'http://127.0.0.1:11434/api/chat')
            self.assertFalse(payload['stream'])
            self.assertEqual(payload['format']['type'], 'object')
            self.assertEqual(len(payload['messages']), 2)
            self.assertNotIn('tools', payload)
            return result

    def test_schema_constrained_request_and_valid_response(self):
        self.assertEqual(self.call(json.dumps({'done': True, 'message': {'content': canonical(PLAN)}}).encode()), PLAN)

    def test_truncated_or_malformed_envelope(self):
        for envelope in ({'done': False}, [], {'done': True, 'message': []}):
            with self.assertRaises(ValueError):
                self.call(json.dumps(envelope).encode())

    def test_fabricated_citation_is_rejected(self):
        plan = copy.deepcopy(PLAN)
        plan['tasks'][0]['evidence'][0]['quote'] = 'Invented'
        with self.assertRaises(ValueError):
            self.call(json.dumps({'done': True, 'message': {'content': canonical(plan)}}).encode())

    def test_oversized_response_is_rejected(self):
        with self.assertRaises(ValueError):
            self.call(b'x'*1_000_001)

    def test_redirects_rejected(self):
        with self.assertRaises(ValueError):
            NoRedirect().redirect_request(None, None, None, None, None, None)


if __name__ == '__main__':
    unittest.main()
