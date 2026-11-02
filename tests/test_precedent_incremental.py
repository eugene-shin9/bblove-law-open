"""Offline incremental catalog regression checks using synthetic rows."""
import json
from pathlib import Path
import tempfile
import unittest
from test_precedent_d1 import FakeClient, DATE, PREC
from bblove_precedent_collect import collect, verify_cache
from collect_precedents import outcome, should_publish

NEXT = '20261016'


def grown():
    client = FakeClient(count=301)
    client.rows = [{**PREC, '판례일련번호': str(n), '사건번호': f'2099두{n}'} for n in range(302, 305)] + client.rows
    return client


class IncrementalTests(unittest.TestCase):
    def test_new_only_resume_integrity_and_second_round(self):
        with tempfile.TemporaryDirectory() as t:
            out = Path(t)
            self.assertTrue(collect(FakeClient(count=301), out, DATE))
            before = {p: p.read_bytes() for p in (out/'summaries').rglob('*.json')}
            client = grown()
            collect(client, out, NEXT, incremental=True, stop=lambda: 'TIME_BUDGET' if client.calls >= 1 else None)
            self.assertEqual(outcome(out), 'PAUSED')
            m = json.loads((out/'manifest.json').read_text())
            verify_cache(out, m)
            client = grown()
            self.assertTrue(collect(client, out, NEXT, incremental=True))
            self.assertEqual(client.details, 3)
            self.assertEqual({p: p.read_bytes() for p in before}, before)
            m = json.loads((out/'manifest.json').read_text())
            self.assertEqual(m['incremental']['new_documents'], 3)
            self.assertTrue(should_publish(m))
            verify_cache(out, m)
            client = grown()
            self.assertTrue(collect(client, out, '20261017', incremental=True))
            self.assertEqual(client.details, 0)
            self.assertEqual(client.calls, 4)  # prec 2; each short catalog 1
            m = json.loads((out/'manifest.json').read_text())
            self.assertFalse(should_publish(m))
            verify_cache(out, m)
            # Tampered round total must fail even with valid file hashes.
            m['catalogs']['prec']['increments'][0]['total'] += 1
            with self.assertRaisesRegex(ValueError, 'CACHE_CATALOG_TOTAL_MISMATCH'):
                verify_cache(out, m)

    def test_min_days_and_failed_snapshot_preserved(self):
        with tempfile.TemporaryDirectory() as t:
            out = Path(t)
            collect(FakeClient(detail={**PREC, '사건번호': '2099두999'}), out, DATE)
            client = FakeClient()
            collect(client, out, NEXT, incremental=True, min_days=2)
            self.assertEqual(client.calls, 0)
            client = FakeClient()
            collect(client, out, '20261017', incremental=True)
            self.assertEqual(client.details, 0)
            self.assertEqual(outcome(out), 'PASS_DONE')

    def test_detail_budget_resumes_and_catalog_count_change_fails_closed(self):
        with tempfile.TemporaryDirectory() as t:
            out = Path(t)
            collect(FakeClient(count=301), out, DATE)
            client = grown()
            client.budget_at = 2
            collect(client, out, NEXT, incremental=True, stop=lambda: None)
            self.assertEqual(outcome(out), 'PAUSED')
            client = grown()
            self.assertTrue(collect(client, out, NEXT, incremental=True))
            self.assertEqual(client.details, 2)
            verify_cache(out, json.loads((out/'manifest.json').read_text()))
        with tempfile.TemporaryDirectory() as t:
            out = Path(t)
            collect(FakeClient(count=301), out, DATE)
            client = grown()
            collect(client, out, NEXT, incremental=True, stop=lambda: 'TIME_BUDGET' if client.calls else None)
            client.rows.append({**PREC, '판례일련번호': '305', '사건번호': '2099두305'})
            self.assertFalse(collect(client, out, NEXT, incremental=True))
            self.assertEqual(json.loads((out/'attempt.json').read_text())['reason'], 'CATALOG_COUNT_CHANGED')
            verify_cache(out, json.loads((out/'manifest.json').read_text()))

class IncrementalResetTests(unittest.TestCase):
    def test_run_recovers_count_change_and_preserves_previous_rounds(self):
        from collect_precedents import run
        from test_collect_precedents import Clock
        with tempfile.TemporaryDirectory() as t:
            out = Path(t)
            collect(FakeClient(count=301), out, DATE)
            collect(grown(), out, NEXT, incremental=True)
            before = {p: p.read_bytes() for folder in ('pages', 'summaries')
                      for p in (out / folder).rglob('*.json')}
            old = json.loads((out / 'manifest.json').read_text())
            clock = Clock()
            class Pausing(FakeClient):
                def get(self, endpoint, params, **kw):
                    result = super().get(endpoint, params, **kw)
                    clock.now = 1000
                    return result
            client = Pausing(count=301)
            client.rows = grown().rows
            self.assertEqual(run(out, 'all', 1000, lambda r: client, clock=clock,
                                 date='20261017'), 'PAUSED')
            client = grown()
            client.rows.insert(0, {**PREC, '판례일련번호': '305', '사건번호': '2099두305'})
            self.assertEqual(run(out, 'all', 1000, lambda r: client, date='20261017'), 'COMPLETE')
            m = json.loads((out / 'manifest.json').read_text())
            verify_cache(out, m)
            self.assertTrue(any(d['id'] == '305' and d['status'] == 'SUCCESS' for d in m['documents']))
            self.assertEqual(m['incremental']['new_documents'], 1)
            self.assertEqual({p: p.read_bytes() for p in before}, before)
            for target, catalog in old['catalogs'].items():
                self.assertEqual({k: v for k, v in m['catalogs'][target].items() if k != 'increments'},
                                 {k: v for k, v in catalog.items() if k != 'increments'})
                self.assertEqual(m['catalogs'][target]['increments'][:-1], catalog['increments'])

    def test_incremental_reset_limit(self):
        import collect_precedents as cp
        calls = []
        def failing(client, out, date, scope, stop=None):
            calls.append(1)
            m = {'catalogs': {'prec': {'complete': True, 'increments': [
                {'complete': False, 'next_page': 1}]}}, 'pages': [], 'documents': [],
                'incremental': {'round': 1, 'complete': False, 'new_documents': 0,
                                'per_target': {'prec': 0}}}
            Path(out, 'manifest.json').write_text(json.dumps(m))
            Path(out, 'attempt.json').write_text(json.dumps({'reason': 'CATALOG_COUNT_CHANGED'}))
        with tempfile.TemporaryDirectory() as t:
            self.assertEqual(cp.run(t, 'all', 1000, lambda r: None, collect_fn=failing), 'FAILED')
        self.assertEqual(len(calls), cp.MAX_RESETS + 1)

    def test_fetched_active_detail_prevents_destructive_reset(self):
        from collect_precedents import reset_catalog
        with tempfile.TemporaryDirectory() as t:
            out = Path(t)
            collect(FakeClient(count=301), out, DATE)
            client = grown()
            collect(client, out, NEXT, incremental=True,
                    stop=lambda: 'TIME_BUDGET' if client.calls else None)
            m = json.loads((out / 'manifest.json').read_text())
            entry = next(d for d in m['documents'] if d.get('round') == 1)
            entry.update(sha256='cached', path='summaries/prec/cached.json')
            (out / entry['path']).write_bytes(b'cached summary')
            (out / 'manifest.json').write_text(json.dumps(m))
            before = {p: p.read_bytes() for p in out.rglob('*.json')}
            self.assertIsNone(reset_catalog(out))
            self.assertEqual({p: p.read_bytes() for p in before}, before)
