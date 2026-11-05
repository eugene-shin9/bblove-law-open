"""Empty source summaries and one-time legacy migration; synthetic transport only."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from test_precedent_d1 import FakeClient, DATE, PREC
from bblove_precedent_collect import collect, verify_cache, save_manifest
from bblove_storage import digest
from build_precedent_d1 import build

EMPTY = {**PREC, '판시사항': '<p>&nbsp;</p>', '판결요지': '<br/>'}

class EmptySourceTests(unittest.TestCase):
    def test_empty_success_cache_and_sql(self):
        with tempfile.TemporaryDirectory() as t:
            out = Path(t)/'source'
            self.assertTrue(collect(FakeClient(detail=EMPTY), out, DATE))
            m = json.loads((out/'manifest.json').read_text())
            self.assertEqual(m['documents'][0]['status'], 'SUCCESS')
            data = json.loads((out/m['documents'][0]['path']).read_text())
            self.assertEqual((data['issues'], data['holding'], data['summary_status']), ('', '', 'EMPTY_AT_SOURCE'))
            verify_cache(out, m)
            sql = Path(t)/'sql'
            report = build(out, sql)
            db = sqlite3.connect(':memory:')
            db.executescript((sql/'schema.sql').read_text())
            for b in report['batches']:
                db.executescript((sql/b['path']).read_text())
            self.assertEqual(db.execute('SELECT case_no, issues, holding FROM precedents').fetchone(), ('2099두1', '', ''))
            self.assertEqual(db.execute('SELECT count(*) FROM precedents_fts').fetchone()[0], 1)
            db.close()

    def test_legacy_failure_refetched_once_and_other_failure_preserved(self):
        for cached in (True, False):
            with self.subTest(cached=cached), tempfile.TemporaryDirectory() as t:
                out = Path(t)
                collect(FakeClient(count=2), out, DATE)
                m = json.loads((out/'manifest.json').read_text())
                m.pop('rule_version', None)
                e = m['documents'][0]
                e.update(status='FAILED', reason='SUMMARY_EMPTY')
                file = out/e['path']
                raw = json.dumps(EMPTY).encode()
                file.write_bytes(raw)
                e['sha256'] = digest(raw)
                if not cached:
                    file.unlink()
                    e.pop('sha256'); e.pop('path')
                other = m['documents'][1]
                other.update(status='FAILED', reason='CASE_OR_COURT_INVALID')
                before = dict(other)
                save_manifest(out/'manifest.json', m)
                client = FakeClient(count=2, detail=EMPTY)
                self.assertFalse(collect(client, out, DATE, incremental=True))
                self.assertEqual(client.requested, [('prec', '1')])
                m = json.loads((out/'manifest.json').read_text())
                self.assertEqual(m['documents'][1], before)
                self.assertEqual(m['documents'][0]['status'], 'SUCCESS')
                verify_cache(out, m)
                client = FakeClient(count=2, detail=EMPTY)
                collect(client, out, DATE, incremental=True)
                self.assertEqual(client.calls, 0)

    def test_consumer_never_refetches_legacy_summary_empty(self):
        # A consumer (incremental=False, e.g. the private D1 loader) keeps a received collection as it is:
        # old SUMMARY_EMPTY failures are permanent there and wait for the producer's next release.
        with tempfile.TemporaryDirectory() as t:
            out = Path(t)
            collect(FakeClient(count=2), out, DATE)
            m = json.loads((out/'manifest.json').read_text())
            m.pop('rule_version', None)
            e = m['documents'][0]
            e.update(status='FAILED', reason='SUMMARY_EMPTY')
            (out/e['path']).unlink()
            e.pop('sha256'); e.pop('path')
            save_manifest(out/'manifest.json', m)
            client, skipped = FakeClient(count=2, detail=EMPTY), {}
            self.assertFalse(collect(client, out, DATE, skipped_failures=skipped))  # a FAILED entry remains
            self.assertEqual(client.calls, 0)
            self.assertEqual(skipped, {'SUMMARY_EMPTY': 1})
            m = json.loads((out/'manifest.json').read_text())
            self.assertEqual(m['documents'][0]['status'], 'FAILED')
            self.assertNotIn('rule_version', m)

    def test_migration_reset_survives_budget_stop(self):
        with tempfile.TemporaryDirectory() as t:
            out = Path(t)
            collect(FakeClient(), out, DATE)
            m = json.loads((out/'manifest.json').read_text())
            m.pop('rule_version', None)
            m['documents'][0].update(status='FAILED', reason='SUMMARY_EMPTY')
            save_manifest(out/'manifest.json', m)
            client = FakeClient(detail=EMPTY)
            collect(client, out, DATE, incremental=True, stop=lambda: 'TIME_BUDGET')
            self.assertEqual(client.calls, 0)
            m = json.loads((out/'manifest.json').read_text())
            self.assertGreaterEqual(m['rule_version'], 2)  # later rules (v3) may run in the same pass
            verify_cache(out, m)
            self.assertTrue(collect(client, out, DATE, incremental=True))
            self.assertEqual(client.details, 1)
            verify_cache(out, json.loads((out/'manifest.json').read_text()))
