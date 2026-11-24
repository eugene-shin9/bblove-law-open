"""Offline: current-edition fetch (eflaw+MST+efYd), binding, receipt reuse and dropped addenda."""
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'scripts'))
from test_full_law_collection import DOC, ROW, DATE, FakeClient, encode
import bblove_law_all
from bblove_law_all import FETCH, collect, now
from bblove_tax_p2_collect import PilotBlocked
from diagnose_law_collection import diagnose

SECOND = {**ROW, '법령ID': '000002', '법령일련번호': '222222', '법령명한글': '둘째법',
          '시행일자': '20260301', '공포일자': '20250201', '공포번호': '20002'}


class Recording(FakeClient):
    """Answers each MST with a body that matches its catalog row (optionally altered)."""
    def __init__(self, rows=None, alter=None, addenda=None):
        super().__init__(rows=rows)
        self.seen, self.alter, self.addenda = [], alter, addenda

    def get(self, endpoint, params, **kw):
        if endpoint != 'lawService.do':
            return super().get(endpoint, params, **kw)
        self.calls += 1
        self.details += 1
        self.seen.append((dict(params), kw.get('original')))
        row = next(r for r in self.rows if r['법령일련번호'] == params['MST'])
        doc = deepcopy(DOC)
        basic = doc['법령']['기본정보']
        basic.update({k: row[k] for k in ('법령ID', '시행일자', '공포일자', '공포번호')},
                     법령명_한글=row['법령명한글'])
        if self.addenda is not None:
            doc['법령']['부칙'] = {'부칙단위': [{'부칙공포일자': d, '부칙공포번호': n, '부칙내용': '합성 부칙'}
                                               for d, n in self.addenda]}
        if self.alter:
            self.alter(doc)
        return doc, encode(doc)


def manifest(path):
    return json.loads((path / 'manifest.json').read_bytes())


def age_receipts(path, days, **changes):
    m = manifest(path)
    for e in m['documents']:
        e['collected_at'] = (now() - timedelta(days=days)).isoformat()
        e.update(changes)
    (path / 'manifest.json').write_bytes(encode(m))


class CurrentEditionTests(unittest.TestCase):
    def test_every_document_uses_eflaw_mst_efyd_once(self):
        with tempfile.TemporaryDirectory() as t:
            client = Recording(rows=[deepcopy(ROW), deepcopy(SECOND)])
            self.assertTrue(collect(client, Path(t), DATE))
            self.assertEqual(sorted(p['MST'] for p, _ in client.seen), ['123456', '222222'])
            for params, original in client.seen:
                row = ROW if params['MST'] == '123456' else SECOND
                self.assertEqual(params, {'target': 'eflaw', 'MST': row['법령일련번호'], 'efYd': row['시행일자']})
                self.assertTrue(original)
            docs = manifest(Path(t))['documents']
            self.assertEqual({e['status'] for e in docs}, {'SUCCESS'})
            self.assertEqual({e['fetch'] for e in docs}, {FETCH})
            self.assertEqual(FETCH, 'eflaw-mst-efyd-v1')

    def test_binding_failures_are_not_written(self):
        def mst(doc): doc['법령']['법령일련번호'] = '999999'
        def ident(doc): doc['법령']['기본정보']['법령ID'] = '000009'
        def effective(doc): doc['법령']['기본정보']['시행일자'] = '20251231'
        def number(doc): doc['법령']['기본정보']['공포번호'] = '20009'
        def no_number(doc): doc['법령']['기본정보'].pop('공포번호')
        for name, alter in (('mst', mst), ('id', ident), ('effective', effective),
                            ('number', number), ('number', no_number)):
            with self.subTest(alter=alter.__name__), tempfile.TemporaryDirectory() as t:
                path = Path(t)
                # A stale body from an earlier run must not survive a mismatch either.
                self.assertTrue(collect(Recording(), path, DATE))
                entry = manifest(path)['documents'][0]
                self.assertTrue((path / entry['path']).exists())
                age_receipts(path, 0, fetch='target-law-mst')
                self.assertTrue(collect(Recording(alter=alter), path, DATE))
                entry = manifest(path)['documents'][0]
                self.assertEqual((entry['status'], entry['reason']), ('FAILED', 'CURRENT_EDITION_MISMATCH'))
                self.assertTrue(entry['detail'].startswith(name + ':'), entry['detail'])
                self.assertFalse((path / entry['path']).exists())
                for key in ('sha256', 'raw_status', 'fetch', 'warnings'):
                    self.assertNotIn(key, entry)
                # Not reusable: the next run asks again.
                retry = Recording()
                self.assertTrue(collect(retry, path, DATE))
                self.assertEqual(retry.details, 1)
                self.assertEqual(manifest(path)['documents'][0]['status'], 'SUCCESS')

    def test_mst_binds_with_matching_or_absent_number(self):
        for outer, inner in ((ROW['법령일련번호'], ''), ('', ROW['법령일련번호']),
                             (ROW['법령일련번호'], ROW['법령일련번호'])):
            for number in ('020001', None):
                with self.subTest(outer=outer, inner=inner, number=number):
                    doc = self.edition_body(outer, inner, number)
                    bblove_law_all.bind_current(encode(doc), {'row': ROW})
        bblove_law_all.bind_current(encode(self.edition_body('', '', '020001')), {'row': ROW})

    def test_mst_cannot_override_wrong_promulgation_number(self):
        doc = self.edition_body(ROW['법령일련번호'], '', '99999')
        with self.assertRaisesRegex(bblove_law_all.EditionMismatch, '^number:'):
            bblove_law_all.bind_current(encode(doc), {'row': ROW})

    def test_outer_and_basic_mst_must_agree(self):
        doc = self.edition_body(ROW['법령일련번호'], '999999', '020001')
        with self.assertRaisesRegex(bblove_law_all.EditionMismatch, 'outer=123456 basic=999999'):
            bblove_law_all.bind_current(encode(doc), {'row': ROW})

    @staticmethod
    def edition_body(outer, inner, number):
        doc = deepcopy(DOC)
        law = doc['법령']
        law['법령일련번호'] = outer
        law['기본정보']['법령일련번호'] = inner
        law['기본정보']['공포번호'] = number
        return doc

    def test_receipt_from_another_fetch_is_not_reused(self):
        for changes in ({'fetch': 'target-law-mst'}, {'fetch': None}):
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as t:
                path = Path(t)
                self.assertTrue(collect(Recording(), path, DATE))
                age_receipts(path, 0, **changes)
                again = Recording()
                self.assertTrue(collect(again, path, DATE))
                self.assertEqual(again.details, 1)
                entry = manifest(path)['documents'][0]
                self.assertEqual((entry['fetch'], entry['reused']), (FETCH, False))

    def test_receipt_age_window(self):
        for days, refetched in ((5, 0), (7, 1)):
            with self.subTest(days=days), tempfile.TemporaryDirectory() as t:
                path = Path(t)
                self.assertTrue(collect(Recording(), path, DATE))
                age_receipts(path, days)
                again = Recording()
                self.assertTrue(collect(again, path, DATE))
                self.assertEqual(again.details, refetched)
                self.assertEqual(manifest(path)['documents'][0]['reused'], not refetched)
        self.assertFalse(bblove_law_all.recent('not-a-date'))
        self.assertFalse(bblove_law_all.recent('2026-11-20T00:00:00'))  # no zone: not trusted
        self.assertFalse(bblove_law_all.recent((now() + timedelta(days=1)).isoformat()))

    def test_catalog_date_change_under_same_mst_refetches(self):
        with tempfile.TemporaryDirectory() as t:
            path = Path(t)
            self.assertTrue(collect(Recording(), path, DATE))
            moved = Recording(rows=[{**ROW, '시행일자': '20260701'}])
            self.assertTrue(collect(moved, path, DATE))
            self.assertEqual(moved.details, 1)
            self.assertEqual(moved.seen[0][0]['efYd'], '20260701')

    def test_pending_receipt_is_kept_only_when_reusable(self):
        class Budget(Recording):
            def get(self, endpoint, params, **kw):
                if endpoint == 'lawService.do':
                    raise PilotBlocked('COLLECTION_BUDGET_EXHAUSTED')
                return super().get(endpoint, params, **kw)
        for days, kept in ((1, True), (7, False)):
            with self.subTest(days=days), tempfile.TemporaryDirectory() as t:
                path = Path(t)
                self.assertTrue(collect(Recording(), path, DATE))
                age_receipts(path, days)
                entry = manifest(path)['documents'][0]
                (path / entry['path']).write_bytes(b'{}')  # force a request, which runs out of budget
                self.assertFalse(collect(Budget(), path, DATE))
                entry = manifest(path)['documents'][0]
                self.assertEqual(entry['reason'], 'COLLECTION_BUDGET_EXHAUSTED')
                self.assertEqual('sha256' in entry and entry.get('fetch') == FETCH, kept)
                self.assertEqual(entry.get('raw_status') == 'SUCCESS', kept)

    def test_dropped_addenda_recorded_and_diagnosed(self):
        with tempfile.TemporaryDirectory() as t:
            path = Path(t)
            self.assertTrue(collect(Recording(addenda=[('20090130', '09346'), ('20201229', '17761')]), path, DATE))
            self.assertNotIn('dropped_addenda', manifest(path)['documents'][0])  # no earlier RAW
            age_receipts(path, 7)
            newer = Recording(addenda=[('20201229', '17761'), ('20251223', '21219')])
            self.assertTrue(collect(newer, path, DATE))
            self.assertEqual(newer.details, 1)
            entry = manifest(path)['documents'][0]
            self.assertEqual(entry['dropped_addenda'], ['20090130/09346'])
            # Reuse within the window keeps the record without another request.
            again = Recording()
            self.assertTrue(collect(again, path, DATE))
            self.assertEqual(again.details, 0)
            self.assertEqual(manifest(path)['documents'][0]['dropped_addenda'], ['20090130/09346'])
            report = diagnose(path, DATE)
            self.assertEqual(report['fetch_counts'], {FETCH: 1})
            self.assertEqual(report['current_edition_mismatch'], 0)
            self.assertEqual(report['dropped_addenda'], {'documents': 1, 'addenda': 1, 'examples': [
                {'title': '테스트법', 'mst': '123456', 'addendum': '20090130/09346'}]})

    def test_dropped_addenda_skips_a_different_law(self):
        with tempfile.TemporaryDirectory() as t:
            path = Path(t)
            self.assertTrue(collect(Recording(addenda=[('20090130', '09346')]), path, DATE))
            entry = manifest(path)['documents'][0]
            other = deepcopy(DOC); other['법령']['기본정보']['법령ID'] = '000777'
            (path / entry['path']).write_bytes(encode(other))
            self.assertTrue(collect(Recording(addenda=[]), path, DATE))
            self.assertNotIn('dropped_addenda', manifest(path)['documents'][0])

    def test_old_raw_from_another_fetch_is_removed_when_refetch_fails(self):
        class Down(Recording):
            def get(self, endpoint, params, **kw):
                if endpoint == 'lawService.do':
                    raise PilotBlocked('REQUEST_FAILED_AFTER_RETRIES')
                return super().get(endpoint, params, **kw)
        for changes, kept in (({'fetch': 'target-law-mst'}, False), ({}, True)):
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as t:
                path = Path(t)
                self.assertTrue(collect(Recording(), path, DATE))
                age_receipts(path, 0, **changes)
                entry = manifest(path)['documents'][0]
                if kept:  # a reusable receipt whose file no longer matches: earlier behaviour, kept
                    (path / entry['path']).write_bytes(b'{}')
                self.assertTrue(collect(Down(), path, DATE))
                entry = manifest(path)['documents'][0]
                self.assertEqual(entry['reason'], 'DETAIL_FETCH_FAILED')
                self.assertEqual((path / entry['path']).exists(), kept)
                self.assertEqual('sha256' in entry, kept)

    def test_build_refuses_many_edition_mismatches(self):
        from build_mcp_assets_official import MAX_EDITION_MISMATCH, edition_mismatches, build
        docs = [{'reason': 'CURRENT_EDITION_MISMATCH'}] * (MAX_EDITION_MISMATCH + 1) + [{'status': 'SUCCESS'}]
        self.assertEqual(edition_mismatches(docs), 21)
        with tempfile.TemporaryDirectory() as t:
            path = Path(t) / 'raw'
            self.assertTrue(collect(Recording(rows=[deepcopy(ROW), deepcopy(SECOND)]), path, DATE))
            m = manifest(path)
            m['documents'][1].update(status='FAILED', reason='CURRENT_EDITION_MISMATCH')
            (path / 'manifest.json').write_bytes(encode(m))
            with self.assertRaisesRegex(ValueError, '"current_edition_mismatch": 1'):
                build(path, ROOT, Path(t) / 'assets', DATE)

    def test_diagnose_counts_mismatch_and_fetch(self):
        def ident(doc): doc['법령']['기본정보']['법령ID'] = '000009'
        with tempfile.TemporaryDirectory() as t:
            path = Path(t)
            self.assertTrue(collect(Recording(alter=ident), path, DATE))
            report = diagnose(path, DATE)
            self.assertEqual(report['current_edition_mismatch'], 1)
            self.assertEqual(report['fetch_counts'], {'unrecorded': 1})
            self.assertEqual(report['dropped_addenda']['documents'], 0)


if __name__ == '__main__':
    unittest.main()
