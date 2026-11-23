"""Offline synthetic coverage: the repeal-only shape no longer needs a second request.

The collector used to fetch target=law and, when that answered only a repeal
declaration, ask target=eflaw&MST&efYd as a fallback. The body is now fetched
with that same eflaw request, so the outcome for that shape is unchanged (the
eflaw body is kept) while the duplicate request is gone.
"""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from test_full_law_collection import DOC, ROW, DATE, FakeClient, collect, encode

REPEAL = deepcopy(DOC)
REPEAL['법령']['조문']['조문단위'] = [{'조문여부': '전문', '조문내용': '테스트법은 이를 폐지한다.'}]


class FallbackTests(unittest.TestCase):
    def run_case(self, eflaw=None, law=REPEAL):
        """target=law would answer `law` (repeal-only by default); target=eflaw answers `eflaw`."""
        good = deepcopy(DOC)
        class Client(FakeClient):
            def get(self, endpoint, params, **kw):
                if endpoint == 'lawService.do':
                    self.calls += 1
                    self.details += 1
                    self.seen.append(dict(params))
                    doc = (eflaw if eflaw is not None else good) if params.get('target') == 'eflaw' else law
                    return doc, encode(doc)
                return super().get(endpoint, params, **kw)
            seen = []
        Client.seen = []
        with tempfile.TemporaryDirectory() as t:
            path = Path(t)
            client = Client()
            collect(client, path, DATE)
            entry = json.loads((path / 'manifest.json').read_bytes())['documents'][0]
            file = path / entry['path']
            raw = file.read_bytes() if file.exists() else None
            if entry['status'] == 'SUCCESS':
                resumed = Client()
                collect(resumed, path, DATE)
                self.assertEqual(resumed.details, 0)  # reused within the window, same fetch
            return client, entry, raw, good

    def test_success_is_the_eflaw_body_with_one_request(self):
        c, e, raw, good = self.run_case()
        self.assertEqual(c.details, 1)
        self.assertEqual(c.seen, [{'target': 'eflaw', 'MST': ROW['법령일련번호'], 'efYd': ROW['시행일자']}])
        self.assertEqual(e['status'], 'SUCCESS')
        self.assertEqual(e['fetch'], 'eflaw-mst-efyd-v1')
        self.assertNotIn('detail_source', e)
        self.assertEqual(raw, encode(good))

    def test_empty(self):
        c, e, _, _ = self.run_case(eflaw={})
        self.assertEqual(c.details, 1)
        self.assertEqual(e['reason'], 'DETAIL_VALIDATION_FAILED')

    def test_repeal_only_from_eflaw_fails_without_a_second_request(self):
        c, e, raw, _ = self.run_case(eflaw=REPEAL)
        self.assertEqual(c.details, 1)
        self.assertEqual(e['reason'], 'DETAIL_VALIDATION_FAILED')
        self.assertEqual(raw, encode(REPEAL))

    def test_mismatched_mst(self):
        bad = deepcopy(DOC)
        bad['법령']['법령일련번호'] = '999999'
        _, e, raw, _ = self.run_case(eflaw=bad)
        self.assertEqual(e['reason'], 'CURRENT_EDITION_MISMATCH')
        self.assertIsNone(raw)

    def test_mismatched_id(self):
        bad = deepcopy(DOC)
        bad['법령']['기본정보']['법령ID'] = '999999'
        bad['법령']['법령일련번호'] = ROW['법령일련번호']
        _, e, raw, _ = self.run_case(eflaw=bad)
        self.assertEqual(e['reason'], 'CURRENT_EDITION_MISMATCH')
        self.assertIsNone(raw)

    def test_non_repeal_preamble(self):
        doc = deepcopy(DOC)
        doc['법령']['조문']['조문단위'] = [{'조문여부': '전문', '조문내용': '일반 전문'}]
        c, e, raw, _ = self.run_case(eflaw=doc)
        self.assertEqual(c.details, 1)
        self.assertEqual(e['reason'], 'DETAIL_VALIDATION_FAILED')
        self.assertEqual(raw, encode(doc))


if __name__ == '__main__':
    unittest.main()
