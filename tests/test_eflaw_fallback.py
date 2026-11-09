"""Offline synthetic fallback and cache regression coverage."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from test_full_law_collection import DOC, ROW, DATE, FakeClient, collect, encode


class FallbackTests(unittest.TestCase):
    def run_case(self, candidate=None, original=None):
        repeal = deepcopy(DOC)
        repeal['법령']['조문']['조문단위'] = [
            {'조문여부': '전문', '조문내용': '테스트법은 이를 폐지한다.'}]
        good = deepcopy(DOC)
        good['법령']['법령키'] = ROW['법령ID'] + ROW['공포일자'] + '00000'
        repeal['법령']['법령키'] = good['법령']['법령키']
        class Client(FakeClient):
            def get(self, endpoint, params, **kw):
                if params.get('target') == 'eflaw':
                    self.calls += 1
                    self.details += 1
                    self.params = params
                    assert kw['attempts'] == 1
                    doc = candidate if candidate is not None else good
                    return doc, encode(doc)
                return super().get(endpoint, params, **kw)
        with tempfile.TemporaryDirectory() as t:
            path = Path(t)
            client = Client(doc=original if original is not None else repeal)
            collect(client, path, DATE)
            entry = json.loads((path / 'manifest.json').read_bytes())['documents'][0]
            raw = (path / entry['path']).read_bytes()
            if entry.get('detail_source'):
                resumed = Client()
                collect(resumed, path, DATE)
                self.assertEqual(resumed.details, 0)
                self.assertEqual(json.loads((path / 'manifest.json').read_bytes())['documents'][0]['detail_source'], 'eflaw-fallback')
            return client, entry, raw, good

    def test_success(self):
        c, e, raw, good = self.run_case()
        self.assertEqual(c.details, 2)
        self.assertEqual(c.params, {'target': 'eflaw', 'MST': ROW['법령일련번호'], 'efYd': ROW['시행일자']})
        self.assertEqual(e['status'], 'SUCCESS')
        self.assertEqual(e['fallback_reason'], 'LAW_TARGET_REPEAL_ONLY')
        self.assertEqual(raw, encode(good))

    def test_empty(self):
        c, e, _, _ = self.run_case(candidate={})
        self.assertEqual(c.details, 2)
        self.assertEqual(e['reason'], 'DETAIL_VALIDATION_FAILED')
        self.assertNotIn('detail_source', e)

    def test_normal_unchanged(self):
        c, e, raw, _ = self.run_case(original=DOC)
        self.assertEqual(c.details, 1)
        self.assertEqual(raw, encode(DOC))
        self.assertNotIn('detail_source', e)

    def test_mismatched_mst(self):
        bad = deepcopy(DOC)
        bad['법령']['법령일련번호'] = '999999'
        _, e, _, _ = self.run_case(candidate=bad)
        self.assertEqual(e['reason'], 'DETAIL_VALIDATION_FAILED')

    def test_mismatched_id(self):
        bad = deepcopy(DOC)
        bad['법령']['기본정보']['법령ID'] = '999999'
        bad['법령']['법령일련번호'] = ROW['법령일련번호']
        _, e, _, _ = self.run_case(candidate=bad)
        self.assertEqual(e['reason'], 'DETAIL_VALIDATION_FAILED')

    def test_non_repeal_preamble_does_not_fetch(self):
        doc = deepcopy(DOC)
        doc['법령']['조문']['조문단위'] = [
            {'조문여부': '전문', '조문내용': '일반 전문'}]
        c, e, raw, _ = self.run_case(original=doc)
        self.assertEqual(c.details, 1)
        self.assertEqual(e['reason'], 'DETAIL_VALIDATION_FAILED')
        self.assertEqual(raw, encode(doc))
