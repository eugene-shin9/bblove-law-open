"""Bounded producer recovery, synthetic transport only."""
import json
from pathlib import Path
import tempfile
import unittest
from test_precedent_d1 import FakeClient, DATE
from bblove_precedent_collect import collect, verify_cache, save_manifest
from collect_precedents import should_publish, transient_failures


def read(out):
    return json.loads((out/'manifest.json').read_text())


def fixture(out, count=4):
    collect(FakeClient(count=count, nts_rows=[]), out, DATE)
    m = read(out)
    for e in m['documents']:
        e.update(status='FAILED', reason='REQUEST_FAILED_AFTER_RETRIES')
    m['status'] = 'INCOMPLETE'
    save_manifest(out/'manifest.json', m)


class RecoveryTests(unittest.TestCase):
    def test_cap_publish_preservation_and_resume(self):
        with tempfile.TemporaryDirectory() as t:
            out = Path(t); fixture(out)
            old = (out/'summaries/prec/4.json').read_bytes()
            client = FakeClient(count=4, nts_rows=[], budget_at=2)
            collect(client, out, '20261016', incremental=True, transient_limit=2, stop=lambda: None)
            m = read(out)
            self.assertEqual([e['status'] for e in m['documents']], ['SUCCESS', 'FAILED', 'FAILED', 'FAILED'])
            self.assertNotIn('attempts', m['documents'][1])
            self.assertFalse(should_publish(m))
            self.assertEqual(m['incremental']['recovery']['cursor'], 1)
            verify_cache(out, m)
            client = FakeClient(count=4, nts_rows=[])
            collect(client, out, '20261017', incremental=True, transient_limit=2)
            self.assertEqual(client.requested, [('prec', '2')])
            m = read(out)
            self.assertEqual(m['incremental']['round'], 1)
            self.assertEqual(m['incremental']['recovery']['successes'], 2)
            self.assertTrue(should_publish(m))
            self.assertEqual(m['date'], '20261017')
            self.assertEqual((out/'summaries/prec/4.json').read_bytes(), old)
            verify_cache(out, m)

    def test_failure_attempts_freeze_and_consumer_manual_paths(self):
        class Failing(FakeClient):
            def get(self, endpoint, params, **kw):
                if endpoint == 'lawService.do':
                    self.details += 1
                    raise ValueError('DETAIL_NOT_FOUND')
                return super().get(endpoint, params, **kw)
        with tempfile.TemporaryDirectory() as t:
            out = Path(t); fixture(out, 1)
            old = (out/'summaries/prec/1.json').read_bytes()
            for day in (16, 17, 18, 19):
                client = Failing(nts_rows=[])
                collect(client, out, f'202610{day}', incremental=True)
                self.assertEqual(client.details, 0 if day == 19 else 1)
                self.assertEqual((out/'summaries/prec/1.json').read_bytes(), old)
            m = read(out)
            self.assertEqual(m['documents'][0]['attempts'], 3)
            self.assertTrue(m['documents'][0]['frozen'])
            self.assertEqual(m['documents'][0]['reason'], 'REQUEST_FAILED_AFTER_RETRIES')
            self.assertEqual(transient_failures(m), {'REQUEST_FAILED_AFTER_RETRIES': 1})
            self.assertFalse(should_publish(m))
            verify_cache(out, m)
            # Consumer still reuses cached failures as before, ignoring producer frozen hints.
            client = FakeClient(nts_rows=[])
            self.assertTrue(collect(client, out, '20261019'))
            self.assertEqual(client.calls, 0)
            m['documents'][0]['status'] = 'FAILED'
            save_manifest(out/'manifest.json', m)
            client = FakeClient(nts_rows=[])
            self.assertTrue(collect(client, out, '20261019', incremental=True, retry_failed='all'))
            self.assertEqual(client.details, 1)

    def test_permanent_and_future_skipped(self):
        def client():
            c = FakeClient(count=2, nts_rows=[])
            c.rows[1]['선고일자'] = '2026.10.17'
            return c
        with tempfile.TemporaryDirectory() as t:
            out = Path(t)
            collect(client(), out, DATE)
            m = read(out)
            m['documents'][0].update(status='FAILED', reason='CASE_OR_COURT_INVALID')
            save_manifest(out/'manifest.json', m)
            c = client()
            collect(c, out, '20261016', incremental=True)
            self.assertEqual(c.details, 0)
            self.assertEqual(read(out)['documents'][1]['reason'], 'DATE_IN_FUTURE')
            c = client()
            collect(c, out, '20261017', incremental=True)
            self.assertEqual(c.requested, [('prec', '2')])
            verify_cache(out, read(out))
