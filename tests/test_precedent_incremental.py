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
            collect(FakeClient(detail={**PREC, '판시사항': '', '판결요지': ''}), out, DATE)
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
