"""Synthetic fixtures only: tax-law history collection/build without API access."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'scripts'))
import bblove_law_history as H
from build_mcp_assets_official import encode

DATE = '20261015'
LID = '001586'


def body(eff, rate):
    return {'법령': {'기본정보': {'법령ID': LID, '법령명_한글': '국세기본법', '시행일자': eff},
                     '조문': {'조문단위': [
                         {'조문여부': 'Y', '조문번호': '1', '조문제목': '목적', '조문내용': '제1조(목적) 같은 본문'},
                         {'조문여부': 'Y', '조문번호': '47', '조문가지번호': '4', '조문제목': '납부지연가산세',
                          '조문내용': f'제47조의4(납부지연가산세) 1일 {rate}'}]}}}


def row(mst, eff):
    return {'법령ID': LID, '법령명한글': '국세기본법', '법령일련번호': mst, '시행일자': eff, '공포일자': eff,
            '공포번호': '1', '제개정구분명': '일부개정', '법령구분명': '법률'}


ROWS = [row('400', '20280101'), row('300', '20240101'), row('200', '20190101'), row('150', '20120101'),
        row('100', '20100101'), row('50', '20050101')]
BODIES = {'400': body('20280101', 'x'), '300': body('20240101', '10만분의 22'), '200': body('20190101', '10만분의 25'),
          '150': body('20120101', '1만분의 3'), '100': body('20100101', '1만분의 3'), '50': body('20050101', '옛')}


class FakeClient:
    credential = 'secret'

    def __init__(self, bad=()):
        self.calls, self.details, self.bad = 0, 0, set(bad)

    def get(self, endpoint, params, **kw):
        self.calls += 1
        if endpoint == 'lawSearch.do':
            return {'LawSearch': {'totalCnt': str(len(ROWS)), 'law': deepcopy(ROWS)}}, None
        self.details += 1
        doc = deepcopy(BODIES[params['MST']])
        if params['MST'] in self.bad:
            doc['법령']['기본정보']['법령ID'] = '999999'
        return doc, encode(doc)


def assets(root):
    (root / 'groups').mkdir(parents=True)
    meta = {'groups': {name: [f'g{i}', name, '2026-01-01', 0] for i, name in enumerate(H.TARGETS)}}
    (root / 'meta.json').write_bytes(encode(meta))
    for i, name in enumerate(H.TARGETS):
        docs = [{'k': '법률', 't': name, 'id': LID if i == 0 else f'{900000 + i}'}]
        (root / 'groups' / f'g{i}.json').write_bytes(encode(docs))


def built_document(root):
    index = json.loads((root / 'history/index.json').read_bytes())
    parts = index['laws'][LID]['parts']
    assert len(parts) == 1  # these existing small fixtures fit in one part
    return json.loads((root / 'history' / parts[0]['path']).read_bytes())


class HistoryTests(unittest.TestCase):
    def test_select_keeps_window_plus_edition_in_force_at_cutoff_and_drops_future(self):
        chosen = H.select(ROWS, DATE)
        self.assertEqual([r['법령일련번호'] for r in chosen], ['100', '150', '200', '300'])

    def test_collect_resume_build_dedups_bodies(self):
        with tempfile.TemporaryDirectory() as t, patch.object(H, 'TARGETS', H.TARGETS[:1]):
            root = Path(t)
            assets(root / 'assets')
            first = FakeClient()
            m = H.collect(first, root / 'assets', root / 'raw', DATE)
            self.assertTrue(m['complete'])
            self.assertEqual(first.details, 4)
            again = FakeClient()
            H.collect(again, root / 'assets', root / 'raw', DATE)
            self.assertEqual(again.details, 0)
            built = H.build(root / 'raw', root / 'dest', DATE)
            self.assertEqual((built['laws'], built['editions']), (1, 4))
            hist = built_document(root / 'dest')
            self.assertEqual([e['eff'] for e in hist['editions']], ['2010-01-01', '2012-01-01', '2019-01-01', '2024-01-01'])
            self.assertEqual(len(hist['bodies']), 4)  # 제1조 once + 3 distinct versions of 제47조의4
            self.assertNotIn('secret', json.dumps(hist, ensure_ascii=False))

    def test_wrong_law_id_is_failed_not_built(self):
        with tempfile.TemporaryDirectory() as t, patch.object(H, 'TARGETS', H.TARGETS[:1]):
            root = Path(t)
            assets(root / 'assets')
            m = H.collect(FakeClient(bad={'200'}), root / 'assets', root / 'raw', DATE)
            self.assertTrue(m['complete'])
            self.assertEqual(m['counts']['success'], 3)
            bad = [e for e in m['laws'][0]['editions'] if e['status'] != 'SUCCESS']
            self.assertEqual([e['법령일련번호'] for e in bad], ['200'])
            built = H.build(root / 'raw', root / 'dest', DATE)
            self.assertEqual(built['editions'], 4)  # 3 stored + the failed one kept as a gap
            hist = built_document(root / 'dest')
            self.assertEqual([e['eff'] for e in hist['editions'] if e.get('missing')], ['2019-01-01'])



if __name__ == '__main__':
    unittest.main()
