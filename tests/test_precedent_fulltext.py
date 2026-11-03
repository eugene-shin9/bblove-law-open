"""Synthetic fulltext only; no private sample content or network."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).parents[1]/'src'))
from test_precedent_d1 import FakeClient, DATE, PREC
from bblove_precedent_collect import (fulltext_object, store_fulltext, collect_fulltext,
    collect, CollectionPaused, summary_bytes)
from bblove_storage import digest

class FulltextTests(unittest.TestCase):
    def value(self, body='<p>합성 문단</p><br/>제1조\n끝'):
        return fulltext_object(PREC, json.dumps({'PrecService': dict(PREC, 판례내용=body)}).encode(), 'synthetic-time')

    def test_raw_html_offsets_and_summary_unchanged(self):
        body='<p>합성 문단</p><br/>제1조\n끝'
        value=self.value(body)
        self.assertEqual(value['body'],body)
        self.assertEqual(value['body_sha256'],digest(body.encode()))
        self.assertEqual(''.join(body[a:b] for a,b in value['paragraphs']),body)
        self.assertEqual(value['transform_version'],'raw-1')
        before=json.dumps(PREC).encode()
        after=json.dumps(dict(PREC, 판례내용=body)).encode()
        self.assertEqual(summary_bytes(before,'prec'),summary_bytes(after,'prec'))
        self.assertNotIn('secret',self.value('<a href="?OC=secret">링크</a>')['body'])

    def test_versions_budget_and_noop(self):
        with tempfile.TemporaryDirectory() as t:
            out=Path(t); m={'documents':{}}
            first=self.value('첫째'); second=self.value('둘째'); third=self.value('셋째')
            self.assertTrue(store_fulltext(out,first,m))
            files={str(p):p.read_bytes() for p in out.rglob('*.json')}
            self.assertFalse(store_fulltext(out,first,m))
            self.assertEqual(files,{str(p):p.read_bytes() for p in out.rglob('*.json')})
            with self.assertRaises(CollectionPaused): store_fulltext(out,second,m,budget=1)
            self.assertEqual(files,{str(p):p.read_bytes() for p in out.rglob('*.json')})
            store_fulltext(out,second,m); store_fulltext(out,third,m)
            prior=list((out/'prec'/'versions'/first['id']).glob('*.json'))
            self.assertEqual([p.stem for p in prior],[second['body_sha256']])
            self.assertEqual(len(list(out.rglob('*.json'))),2)

    def test_summary_first_and_deadline(self):
        with tempfile.TemporaryDirectory() as t:
            summary=Path(t)/'summary'; out=Path(t)/'fulltext'
            collect(FakeClient(),summary,DATE)
            calls=[]
            def factory(seconds):
                calls.append(seconds)
                class FullClient(FakeClient):
                    def get(self, *args, **kwargs):
                        doc, raw = super().get(*args, **kwargs)
                        doc = {'PrecService': doc['Service']}
                        return doc, json.dumps(doc).encode()
                return FullClient(detail=dict(PREC, 판례내용='<p>합성</p>'))
            self.assertEqual(collect_fulltext(factory,summary,out,10,DATE,clock=lambda:0),'PAUSED')
            self.assertEqual(calls,[])
            self.assertEqual(collect_fulltext(factory,summary,out,100,DATE,clock=lambda:0),'COMPLETE')
            m=json.loads((out/'fulltext-manifest.json').read_text())
            self.assertEqual(m['kind'],'precedent-fulltext-raw')
            self.assertEqual(len(m['documents']),1)


class FulltextProgressTests(unittest.TestCase):
    def factory(self):
        class FullClient(FakeClient):
            def get(self, *args, **kwargs):
                doc, raw = super().get(*args, **kwargs)
                doc = {'PrecService': doc['Service']}
                return doc, json.dumps(doc).encode()
        return lambda seconds: FullClient(count=5)

    def test_next_day_continues_with_unstored_ids_instead_of_restarting(self):
        from datetime import datetime, timedelta
        with tempfile.TemporaryDirectory() as t:
            summary = Path(t)/'summary'; out = Path(t)/'fulltext'
            collect(FakeClient(count=5), summary, DATE)
            ids = [e['id'] for e in json.loads((summary/'manifest.json').read_text())['documents']
                   if e['target'] == 'prec' and e.get('kind') == 'detail' and e['status'] == 'SUCCESS']
            self.assertGreaterEqual(len(ids), 3)
            ticks = iter(range(0, 10 ** 6, 40))
            # A short day-1 budget stores some, not all, IDs.
            day1 = collect_fulltext(self.factory(), summary, out, 3 * 40 + 34, DATE, clock=lambda: next(ticks))
            self.assertEqual(day1, 'PAUSED')
            first = set(json.loads((out/'fulltext-manifest.json').read_text())['documents'])
            self.assertTrue(1 <= len(first) < len(ids))
            next_day = (datetime.strptime(DATE, '%Y%m%d') + timedelta(days=1)).strftime('%Y%m%d')
            self.assertEqual(collect_fulltext(self.factory(), summary, out, 10 ** 6, next_day, clock=lambda: 0), 'COMPLETE')
            m = json.loads((out/'fulltext-manifest.json').read_text())['documents']
            self.assertEqual(set(m), set(ids))
            # Day-1 bodies were not refetched on day 2 (recheck waits for the recheck age).
            self.assertTrue(all(m[i]['checked_date'] == DATE for i in first))

    def test_usage_counter_matches_disk(self):
        from bblove_precedent_collect import fulltext_usage
        with tempfile.TemporaryDirectory() as t:
            out = Path(t)
            m = dict(kind='precedent-fulltext-raw', format_version=1, date=DATE, documents={})
            usage = {'bytes': fulltext_usage(out)}
            make = lambda body: fulltext_object(PREC, json.dumps({'PrecService': dict(PREC, 판례내용=body)}).encode(), 'x')
            for body in ('<p>하나</p>', '<p>둘 더 긴 본문</p>', '<p>셋</p>', '<p>셋</p>'):
                store_fulltext(out, make(body), m, usage=usage)
                self.assertEqual(usage['bytes'], fulltext_usage(out))
