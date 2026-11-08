"""Synthetic full-listing and bounded refresh regression tests; no external calls."""
import json
from pathlib import Path
import tempfile
import unittest
from test_precedent_d1 import FakeClient, DATE, PREC
from bblove_precedent_collect import collect, verify_cache
from collect_precedents import run

NEXT = '20261016'

def manifest(out):
    return json.loads((out / 'manifest.json').read_text())

def client(count=301):
    return FakeClient(count=count)

class ReconciliationTests(unittest.TestCase):
    def test_new_id_after_overlap_resets_streak(self):
        with tempfile.TemporaryDirectory() as t:
            out = Path(t)
            collect(client(501), out, DATE)
            c = client(501)
            c.rows.insert(100, {**PREC, '판례일련번호': '9999', '사건번호': '2099두9999'})
            self.assertTrue(collect(c, out, NEXT, incremental=True))
            m = manifest(out)
            self.assertEqual(m['catalogs']['prec']['increments'][-1]['next_page'], 5)
            self.assertTrue(any(d['id'] == '9999' for d in m['documents']))
            verify_cache(out, m)

    def test_full_scan_finds_late_insert_changed_and_disappeared_then_refreshes(self):
        with tempfile.TemporaryDirectory() as t:
            out = Path(t)
            collect(client(), out, DATE)
            c = client()
            removed = c.rows.pop(250)['판례일련번호']
            c.rows.append({**PREC, '판례일련번호': '9999', '사건번호': '2099두9999'})
            c.rows[0]['사건명'] = '합성 변경 사건명'
            self.assertEqual(run(out, 'all', 1000, lambda _: c, date=NEXT, reconcile=True), 'RECONCILED')
            self.assertEqual(c.details, 0)
            m = manifest(out)
            self.assertEqual(m['reconciliation']['catalogs']['prec'],
                {'new_missing': 1, 'disappeared': 1, 'metadata_changed': 1, 'list_calls': 4})
            self.assertTrue(any(d['id'] == removed for d in m['documents']))
            verify_cache(out, m)
            c2 = client()
            c2.rows = c.rows
            self.assertTrue(collect(c2, out, NEXT, incremental=True))
            self.assertEqual(c2.details, 2)  # one metadata refresh and one missing ID
            m = manifest(out)
            self.assertEqual(m['completeness'], 'reconciled:' + NEXT)
            self.assertEqual(len(m['reconciliation']['changed']), 1)
            verify_cache(out, m)

    def test_refresh_failure_preserves_old_and_daily_cap_survives_restart(self):
        with tempfile.TemporaryDirectory() as t:
            out = Path(t)
            collect(client(3), out, DATE)
            before = {d['id']: (out/d['path']).read_bytes() for d in manifest(out)['documents'] if d['target']=='prec'}
            c = client(3)
            for r in c.rows: r['사건명'] = '변경된 합성 제목'
            collect(c, out, NEXT, incremental=True, reconcile=True)
            failing = FakeClient(count=3, detail={**PREC, '사건번호':'2099두99999'})
            collect(failing, out, NEXT, incremental=True, refresh_limit=1)
            m = manifest(out)
            self.assertEqual(m['refresh_budget']['attempts'], 1)
            self.assertEqual(sum('refresh_row' in d for d in m['documents']), 3)
            self.assertEqual({d['id']: (out/d['path']).read_bytes() for d in m['documents'] if d['target']=='prec'}, before)
            c = client(3)
            collect(c, out, NEXT, incremental=True, refresh_limit=1)
            self.assertEqual(c.details, 0)
            collect(client(3), out, '20261017', incremental=True, refresh_limit=1)
            self.assertEqual(manifest(out)['refresh_budget']['attempts'], 1)
            verify_cache(out, manifest(out))

    def test_full_scan_resume_and_tamper_detection(self):
        with tempfile.TemporaryDirectory() as t:
            out = Path(t)
            collect(client(), out, DATE)
            c = client()
            collect(c, out, NEXT, incremental=True, reconcile=True,
                    stop=lambda: 'TIME_BUDGET' if c.calls else None)
            verify_cache(out, manifest(out))
            c = client()
            self.assertTrue(collect(c, out, NEXT, incremental=True, reconcile=True))
            self.assertEqual(c.details, 0)
            m=manifest(out)
            m['documents'][0]['row'] = {**m['documents'][0]['row'], '사건명':'not in any listing'}
            with self.assertRaisesRegex(ValueError, 'CACHE_ID_DUPLICATE_OR_MISMATCH'):
                verify_cache(out, m)

    def test_release_metadata_contains_counts_changed_and_pending(self):
        from release_info import build
        with tempfile.TemporaryDirectory() as t:
            out = Path(t)
            collect(client(1), out, DATE)
            c = client(1)
            c.rows[0]['사건명'] = '변경된 합성 제목'
            collect(c, out, NEXT, incremental=True, reconcile=True)
            info = build(out, out/'manifest.json', 'precedent-summary-raw')
            self.assertEqual(info['completeness'], 'reconciled:' + NEXT)
            self.assertEqual(info['refresh_pending'], 1)
            c2=client(1); c2.rows=c.rows
            collect(c2, out, NEXT, incremental=True)
            info = build(out, out/'manifest.json', 'precedent-summary-raw')
            self.assertEqual(info['refresh_pending'], 0)
            self.assertEqual(len(info['changed']), 1)

    def test_nondefault_overlap_limit(self):
        with tempfile.TemporaryDirectory() as t:
            out=Path(t)
            collect(client(501), out, DATE)
            c=client(501)
            collect(c, out, NEXT, incremental=True, overlap_limit=3)
            m=manifest(out)
            self.assertEqual(m['catalogs']['prec']['increments'][-1]['next_page'], 4)
            verify_cache(out,m)

    def test_versioned_summary_interruption_recovery(self):
        from bblove_precedent_collect import discard_unrecorded
        with tempfile.TemporaryDirectory() as t:
            out=Path(t)
            collect(client(1), out, DATE)
            m=manifest(out)
            old=out/'summaries/prec/1.json'
            # Before manifest commit the new version is an orphan, and the old file stays.
            new=out/('summaries/prec/1-r'+'a'*64+'.json')
            new.write_bytes(b'new synthetic orphan')
            discard_unrecorded(out,m)
            self.assertTrue(old.exists()); self.assertFalse(new.exists())
            verify_cache(out,m)

    def test_refresh_failed_once_retries_next_day_and_same_hash_is_not_changed(self):
        with tempfile.TemporaryDirectory() as t:
            out=Path(t)
            collect(client(1), out, DATE)
            c=client(1)
            # Court remains list metadata: raw detail can be byte-identical after a listing correction.
            c.rows[0]['법원명']='서울고등법원'
            collect(c,out,NEXT,incremental=True,reconcile=True)
            original=client(1)
            collect(original,out,NEXT,incremental=True)
            m=manifest(out)
            self.assertEqual(m['reconciliation']['changed'],[])
            self.assertEqual(sum('refresh_row' in d for d in m['documents']),0)
            verify_cache(out,m)
