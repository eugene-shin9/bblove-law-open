"""Synthetic administrative decision regressions: no network or real records."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from bblove_precedent_collect import collect, identifier, summary_bytes, validated, verify_cache, DEFAULT_TARGETS

ROW={'행정심판재결례일련번호':'900001','사건명':'합성 행정심판','사건번호':'2099경기행심1','재결청':'합성 지방위원회',
     '처분청':'합성 기관','처분일자':'','의결일자':'2026.01.01','재결구분명':'합성','재결구분코드':'S',
     '행정심판례상세링크':'https://www.law.go.kr/DRF/lawService.do?target=decc&ID=900001&type=HTML&OC=test'}
DETAIL={**{k:v for k,v in ROW.items() if k not in ('행정심판례상세링크','행정심판재결례일련번호')},
        '행정심판례일련번호':'900001','의결일자':'20260101','재결요지':'','주문':'합성 주문','청구취지':'합성 청구',
        '이유':'FULL_REASON_NEVER_STORED','재결례유형명':'합성','재결례유형코드':'S'}

class Client:
    credential='test'
    def __init__(self): self.calls=0; self.requests=[]
    def get(self,endpoint,params,**kwargs):
        self.calls+=1; self.requests.append((endpoint,params['target']))
        if endpoint=='lawSearch.do':
            rows=[ROW] if params['target']=='decc' else []
            return {'Decc':{'totalCnt':str(len(rows)),params['target']:rows}},None
        doc={'PrecService':DETAIL}
        return doc,json.dumps(doc,ensure_ascii=False).encode()

class CommitteeTests(unittest.TestCase):
    def test_validation_and_projection(self):
        raw=summary_bytes(json.dumps({'PrecService':DETAIL}).encode(),'decc')
        self.assertNotIn(b'FULL_REASON',raw)
        row=validated(ROW,json.loads(raw),'decc','detail')
        self.assertEqual(row['summary_status'],'EMPTY_AT_SOURCE')
        self.assertIsNone(row['disposed'])
        self.assertEqual(row['decided'],'2026-01-01')
        self.assertNotIn('OC=',row['official_url'])
        self.assertEqual((row['order'],row['claim']),('합성 주문','합성 청구'))
        for key in ('사건명','사건번호','재결청','의결일자','행정심판례일련번호'):
            with self.assertRaises(ValueError): validated(ROW,{**DETAIL,key:'wrong'},'decc','detail')
        with self.assertRaises(ValueError): identifier({**ROW,'행정심판례일련번호':'900002'},'decc')

    def test_explicit_scope_only(self):
        for scope in ('all','tax','committee'):
            with tempfile.TemporaryDirectory() as t:
                c=Client(); self.assertTrue(collect(c,Path(t),'20261103',scope=scope))
                if scope!='committee': self.assertEqual([x[1] for x in c.requests],list(DEFAULT_TARGETS))
                else:
                    self.assertEqual([x[1] for x in c.requests[:3]],list(DEFAULT_TARGETS))
                    m=json.loads((Path(t)/'manifest.json').read_text()); verify_cache(Path(t),m)
                    raw=(Path(t)/'summaries/decc/900001.json').read_text()
                    self.assertIn('EMPTY_AT_SOURCE',raw); self.assertNotIn('FULL_REASON',raw)
                    self.assertNotIn('OC=test',json.dumps(m))
                    # Resuming with ordinary scope must not request committee records.
                    later=Client(); self.assertTrue(collect(later,Path(t),'20261103',scope='all')); self.assertEqual(later.calls,0)

    def test_budget_prevents_committee_requests(self):
        with tempfile.TemporaryDirectory() as t:
            c=Client(); collect(c,Path(t),'20261103',scope='committee',stop=lambda:'ROW_BUDGET' if c.calls>=3 else None)
            self.assertNotIn('decc',[x[1] for x in c.requests])
