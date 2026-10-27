"""collect_precedents.py: multi-client loop, time budget, outcomes and step outputs."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'tests'))
import collect_precedents as cp
from test_precedent_d1 import FakeClient, DATE


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def write_manifest(out, status, docs, catalogs_complete=True, stop_reason=None):
    m = {'date': DATE, 'status': status, 'documents': docs,
         'catalogs': {'prec': {'complete': catalogs_complete, 'total': len(docs)}}}
    if stop_reason:
        m['stop_reason'] = stop_reason
    Path(out, 'manifest.json').write_text(json.dumps(m))


class LoopTests(unittest.TestCase):
    def test_fresh_client_per_slice_until_complete(self):
        clock, created, calls = Clock(), [], []

        def factory(remaining):
            created.append(remaining)
            return object()

        def fake_collect(client, out, date, scope, stop=None):
            calls.append(client)
            clock.now += 3000
            if len(calls) < 3:
                write_manifest(out, 'INCOMPLETE', [{'status': 'PENDING'}], stop_reason='TIME_BUDGET')
            else:
                write_manifest(out, 'COMPLETE', [{'status': 'SUCCESS'}])
            return True

        with tempfile.TemporaryDirectory() as t:
            state = cp.run(t, 'all', 18000, factory, clock=clock, collect_fn=fake_collect, date=DATE)
        self.assertEqual(state, 'COMPLETE')
        self.assertEqual(len(set(map(id, calls))), 3)
        self.assertEqual(created, [18000, 15000, 12000])

    def test_overall_budget_pauses_and_stop_callback_fires(self):
        clock, seen = Clock(), []

        def fake_collect(client, out, date, scope, stop=None):
            clock.now += 5000
            seen.append(stop())
            write_manifest(out, 'INCOMPLETE', [{'status': 'PENDING'}], stop_reason='TIME_BUDGET')
            return True

        with tempfile.TemporaryDirectory() as t:
            state = cp.run(t, 'all', 10000, lambda r: object(), clock=clock, collect_fn=fake_collect, date=DATE)
        self.assertEqual(state, 'PAUSED')
        self.assertEqual(seen, [None, 'TIME_BUDGET'])

    def test_catalog_or_cache_failure_is_failed(self):
        def fake_collect(client, out, date, scope, stop=None):
            Path(out, 'attempt.json').write_text('{"status": "FAILED"}')
            return False

        with tempfile.TemporaryDirectory() as t:
            Path(t, 'attempt.json').write_text('{"old": true}')
            self.assertEqual(cp.run(t, 'all', 18000, lambda r: object(), collect_fn=fake_collect, date=DATE), 'FAILED')

    def test_stale_attempt_from_previous_run_is_ignored(self):
        def fake_collect(client, out, date, scope, stop=None):
            write_manifest(out, 'COMPLETE', [{'status': 'SUCCESS'}])
            return True

        with tempfile.TemporaryDirectory() as t:
            Path(t, 'attempt.json').write_text('{"old": true}')
            self.assertEqual(cp.run(t, 'all', 18000, lambda r: object(), collect_fn=fake_collect, date=DATE), 'COMPLETE')

    def test_pass_done_when_some_documents_failed(self):
        with tempfile.TemporaryDirectory() as t:
            write_manifest(t, 'INCOMPLETE', [{'status': 'SUCCESS'}, {'status': 'FAILED'}])
            self.assertEqual(cp.outcome(t), 'PASS_DONE')
            write_manifest(t, 'INCOMPLETE', [{'status': 'SUCCESS'}], catalogs_complete=False)
            self.assertEqual(cp.outcome(t), 'PAUSED')

    def test_real_collector_resumes_across_clients(self):
        with tempfile.TemporaryDirectory() as t:
            clients = [FakeClient(count=3, budget_at=2), FakeClient(count=3)]
            state = cp.run(t, 'all', 18000, lambda r: clients.pop(0), date=DATE)
            self.assertEqual(state, 'COMPLETE')
            self.assertEqual(clients, [])
            m = json.loads(Path(t, 'manifest.json').read_text())
            self.assertTrue(all(d['status'] == 'SUCCESS' for d in m['documents']))
            for p in Path(t, 'summaries').rglob('*.json'):
                self.assertNotIn(b'FULL_JUDGMENT_MUST_NEVER_PERSIST', p.read_bytes())

    def test_step_outputs(self):
        with tempfile.TemporaryDirectory() as t:
            target = Path(t) / 'out.txt'
            with patch.dict(os.environ, {'GITHUB_OUTPUT': str(target)}):
                cp.write_outputs('PASS_DONE')
                cp.write_outputs('PAUSED')
            self.assertEqual(target.read_text(), 'outcome=PASS_DONE\ncomplete=true\noutcome=PAUSED\ncomplete=false\n')


if __name__ == '__main__':
    unittest.main()
