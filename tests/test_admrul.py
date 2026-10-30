"""Synthetic admrul contracts. No DRF calls, deployments or real credentials."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
import bblove_admrul as A

DATE = '20261016'


def row(i=1, org='국세청', title='조사사무처리규정', kind='훈령'):
    return {'행정규칙ID':str(100+i),'행정규칙일련번호':str(2100000000000+i),
        '행정규칙명':title,'행정규칙종류':kind,'소관부처명':org,
        '발령일자':'20250101','시행일자':'20250201','현행연혁구분':'현행'}


def detail(r, body='제1조(목적) 합성 세무조사의 목적.\n제2조(절차) 합성 납세자 권익 보호.'):
    basic={k:v for k,v in r.items() if k!='현행연혁구분'}
    basic.update(현행여부='Y',소관부처코드=A.ORG_NAMES[r['소관부처명']])
    return {'AdmRulService':{'행정규칙기본정보':basic,'조문내용':body}}


class Fake:
    credential='fixture-credential-only'

    def __init__(self, rows=None, details=None, stop_after=None):
        self.rows=rows if rows is not None else [row(),row(2,'기획재정부','소득세 납세절차','예규')]
        self.details=details or {}
        self.calls=0
        self.requests=[]
        self.stop_after=stop_after

    def get(self, endpoint, params, original=False):
        self.calls+=1
        self.requests.append((endpoint,params,original))
        if self.stop_after is not None and self.calls>self.stop_after:
            raise A.PilotBlocked('COLLECTION_BUDGET_EXHAUSTED')
        if endpoint=='lawSearch.do':
            rows=[r for r in self.rows if A.ORG_NAMES.get(r['소관부처명'])==params['org']]
            start=(params['page']-1)*100
            doc={'AdmRulSearch':{'totalCnt':str(len(rows)),'page':str(params['page']),
                                'admrul':rows[start:start+100]}}
        else:
            r=next(r for r in self.rows if r['행정규칙일련번호']==params['ID'])
            doc=self.details.get(params['ID'],detail(r))
        raw=A.encode(doc)
        return doc,raw


def both_orgs(rows):
    """Unselected personnel rule keeps each required catalog nonempty."""
    present = {A.ORG_NAMES.get(r['소관부처명']) for r in rows}
    return rows + [row(900 + i, names[0], '인사관리규정')
                   for i, (code, names) in enumerate(A.ORG.items()) if code not in present]


def meta(root):
    (root/'groups').mkdir()
    law={'id':'001586','k':'법률','t':'국세기본법','eff':'2025-01-01',
         'pub':'2024-12-01','st':'시행','url':'https://www.law.go.kr/법령/국세기본법',
         'a':[['제1조','목적','기존 법률 본문']]}
    (root/'groups/law.json').write_bytes(A.encode([law]))
    m={'groups':{'국세기본법':['law','국세기본법','2025-01-01',0]},
       'alias':{'국기법':'국세기본법','국세기본법':'국세기본법'},'titles':{},'routes':{},
       'history':{'schema':2,'laws':{}},'source_commit':'law.go.kr DRF 2026-10-15'}
    (root/'meta.json').write_bytes(A.encode(m))
    return m


NODE=r"""
import fs from 'node:fs';
const p=JSON.parse(fs.readFileSync(0,'utf8'));
const {default:worker}=await import('data:text/javascript;base64,'+Buffer.from(fs.readFileSync(p.worker,'utf8')).toString('base64'));
let live=0;const paths=[];
globalThis.fetch=async()=>{live++;throw Error('LIVE_FORBIDDEN');};
const env={ASSETS:{fetch:async u=>{
 const path=new URL(u).pathname;paths.push(path);
 try{return new Response(fs.readFileSync(p.root+path),{headers:{'content-type':'application/json'}});}
 catch{return new Response('',{status:404});}
}}};
const results=[];
for(const [name,args] of p.requests){
 const res=await worker.fetch(new Request('https://test.invalid/mcp',{method:'POST',body:JSON.stringify({jsonrpc:'2.0',id:1,method:'tools/call',params:{name,arguments:args}})}),env);
 results.push(await res.json());
}
console.log(JSON.stringify({results,live,paths}));
"""


def rpc(root, requests):
    return json.loads(subprocess.run(['node','--input-type=module','-e',NODE],
        input=json.dumps({'root':str(root),'worker':str(ROOT/'mcp/src/index.js'),'requests':requests}),
        text=True,capture_output=True,check=True,timeout=30).stdout)


class AdminRules(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.raw=self.root/'raw'
        self.dest=self.root/'assets'

    def collected(self, client=None):
        client=client or Fake()
        self.assertTrue(A.collect(client,self.raw,DATE))
        return client,json.loads((self.raw/'manifest.json').read_bytes())

    def assets(self, client=None):
        self.collected(client)
        stats=A.build(self.raw,self.dest)
        meta(self.dest)
        A.verify(self.dest,merge_meta=True)
        return stats

    def test_org_kind_keyword_and_customs_filters(self):
        rows=[row(),row(2,'기획재정부','법인 과세 기준','고시'),
              row(3,'국세청','인사관리규정'),row(4,'기획재정부','관세 징수 규정'),
              row(5,'국세청','소득세 지침','지침'),row(6,'법무부','조세 조사 규정')]
        client,m=self.collected(Fake(rows))
        self.assertEqual(len(m['documents']),2)
        self.assertEqual({e['row']['소관부처명'] for e in m['documents']},{'국세청','기획재정부'})
        self.assertEqual({e['reason'] for e in m['excluded']},{'NO_TAX_KEYWORD','CUSTOMS_EXCLUDED','OUTSIDE_KINDS'})
        self.assertEqual(A.exclusion(row(6,'법무부')), 'OUTSIDE_ORGANIZATIONS')
        lists=[p for ep,p,_ in client.requests if ep=='lawSearch.do']
        self.assertEqual({p['org'] for p in lists},set(A.ORG))
        self.assertTrue(all(p['target']=='admrul' and p['nw']=='1' for p in lists))

    def test_resume_zero_calls_and_exact_raw_bytes(self):
        client,m=self.collected()
        self.assertEqual(client.calls,4)
        originals={e['path']:(self.raw/e['path']).read_bytes() for e in m['documents']}
        again,m=self.collected(Fake(stop_after=0))
        self.assertEqual(again.calls,0)
        self.assertTrue(all(e['reused'] for e in m['documents']))
        for path,raw in originals.items():
            self.assertEqual(raw,(self.raw/path).read_bytes())
            self.assertEqual(json.loads(raw),detail(next(r for r in client.rows if r['행정규칙일련번호'] in path)))

    def test_corrupt_hash_refetches_one_body(self):
        _,m=self.collected()
        (self.raw/m['documents'][0]['path']).write_bytes(b'{}')
        again,_=self.collected()
        self.assertEqual(again.calls,1)

    def test_budget_keeps_receipts_for_not_yet_processed_cached_documents(self):
        _,m=self.collected()
        (self.raw/m['documents'][0]['path']).write_bytes(b'{}')
        self.assertFalse(A.collect(Fake(stop_after=0),self.raw,DATE))
        interrupted=json.loads((self.raw/'manifest.json').read_bytes())
        self.assertEqual(interrupted['documents'][1]['sha256'],m['documents'][1]['sha256'])
        again,_=self.collected()
        self.assertEqual(again.calls,1)

    def test_noncanonical_json_original_is_not_reencoded(self):
        class Pretty(Fake):
            def get(self,*args,**kwargs):
                d,_=super().get(*args,**kwargs)
                return d,(' \n'+json.dumps(d,ensure_ascii=False,indent=4)+'\n').encode()
        _,m=self.collected(Pretty())
        for e in m['documents']:
            raw=(self.raw/e['path']).read_bytes()
            self.assertTrue(raw.startswith(b' \n{\n'))
            self.assertNotEqual(raw,A.encode(json.loads(raw)))

    def test_next_day_refreshes_list_but_reuses_unchanged_details(self):
        self.collected()
        client=Fake()
        self.assertTrue(A.collect(client,self.raw,'20261017'))
        self.assertEqual(client.calls,2)

    def test_mismatch_empty_unknown_body_and_failure_publication(self):
        for damage in ('id','mst','title','kind','org','eff','pub','current','code','empty','shape','image'):
            with self.subTest(damage=damage),tempfile.TemporaryDirectory() as tmp:
                r=row();d=detail(r);b=d['AdmRulService']['행정규칙기본정보']
                fields={'id':'행정규칙ID','mst':'행정규칙일련번호','title':'행정규칙명','kind':'행정규칙종류','org':'소관부처명','eff':'시행일자','pub':'발령일자','current':'현행여부','code':'소관부처코드'}
                if damage in fields:b[fields[damage]]='20250102' if damage in {'eff','pub'} else 'wrong'
                else:d['AdmRulService']['조문내용']={'empty':'','shape':{'unknown':'text'},'image':'<img src="x">'}[damage]
                out=Path(tmp);c=Fake(both_orgs([r,row(2)]),{r['행정규칙일련번호']:d})
                self.assertTrue(A.collect(c,out,DATE))
                failures=json.loads((out/'collection-failures.json').read_bytes())
                self.assertEqual(len(failures),1)
                self.assertEqual(failures[0]['id'],r['행정규칙ID'])
                A.build(out,out/'assets')
                idx=json.loads((out/'assets/admrul/index.json').read_bytes())
                self.assertEqual(len(idx['documents']),1)
                self.assertEqual(len(idx['failures']),1)
                replay=Fake(c.rows,c.details,stop_after=0)
                self.assertTrue(A.collect(replay,out,DATE))
                self.assertEqual(replay.calls,0)

    def test_all_failed_cannot_publish_assets(self):
        r=row();self.collected(Fake(both_orgs([r]),{r['행정규칙일련번호']:detail(r,'')}))
        with self.assertRaisesRegex(ValueError,'NO_SUCCESSFUL'):
            A.build(self.raw,self.dest)

    def test_credentials_never_persisted_even_in_failure(self):
        for payload in ('fixture-credential-only','https://example.invalid/?OC=fixture-secret','https://example.invalid/?%4f%43=fixture-credential-only'):
            with self.subTest(payload=payload),tempfile.TemporaryDirectory() as tmp:
                r=row();d=detail(r,payload);out=Path(tmp)
                self.assertTrue(A.collect(Fake(both_orgs([r]),{r['행정규칙일련번호']:d}),out,DATE))
                contents=b''.join(p.read_bytes() for p in out.rglob('*') if p.is_file())
                self.assertNotIn(payload.encode(),contents)
                self.assertNotIn(b'fixture-credential-only',contents)
                self.assertFalse(list(out.glob('raw/**/*.json')))

    def test_catalog_echo_is_sanitized_selection_evidence(self):
        class Echo(Fake):
            def get(self,*args,**kwargs):
                d,raw=super().get(*args,**kwargs)
                if args[0]=='lawSearch.do':
                    d['echo']='https://test.invalid/?OC='+self.credential
                return d,A.encode(d)
        self.assertTrue(A.collect(Echo(),self.raw,DATE))
        checkpoint=json.loads((self.raw/'catalog.json').read_bytes())
        self.assertTrue(all(p['role']=='sanitized_selection_evidence' for p in checkpoint['pages']))
        self.assertIn('REDACTED',(self.raw/checkpoint['pages'][0]['path']).read_text())
        all_bytes=b''.join(p.read_bytes() for p in self.raw.rglob('*') if p.is_file())
        self.assertNotIn(Echo.credential.encode(),all_bytes)
        replay=Echo(stop_after=0)
        self.assertTrue(A.collect(replay,self.raw,DATE));self.assertEqual(replay.calls,0)

    def test_catalog_unknown_envelope_bad_page_other_org_duplicate_and_date_fail(self):
        for damage in ('envelope','page','org','duplicate','date'):
            with self.subTest(damage=damage):
                d={'AdmRulSearch':{'totalCnt':'1','page':'1','admrul':[row()]}}
                if damage=='envelope':d={'unknown':d['AdmRulSearch']}
                elif damage=='page':d['AdmRulSearch']['page']='2'
                elif damage=='org':d['AdmRulSearch']['admrul'][0]['소관부처명']='법무부'
                elif damage=='date':d['AdmRulSearch']['admrul'][0]['시행일자']='20250230'
                else:d['AdmRulSearch']['admrul']='bad'
                with self.assertRaises(ValueError):A.parse_page(d,'1210000',1,DATE)
        with self.assertRaisesRegex(ValueError,'DUPLICATE_ID'):
            A.catalog(Fake([row(),row()]),self.raw,DATE)

    def test_multiple_catalog_pages_and_complete_resume(self):
        rows=both_orgs([row(i,title=f'세무조사 규정 {i}') for i in range(1,102)])
        c=Fake(rows)
        self.assertEqual(len(A.catalog(c,self.raw,DATE)),102)
        self.assertEqual(c.calls,3) # two NTS pages, one nonempty MOEF page
        c=Fake(rows,stop_after=0)
        self.assertEqual(len(A.catalog(c,self.raw,DATE)),102)
        self.assertEqual(c.calls,0)

    def test_partial_budget_resumes_and_catalog_failure_invalidates_complete(self):
        c=Fake(stop_after=3)
        self.assertFalse(A.collect(c,self.raw,DATE))
        self.assertTrue(A.only_budget(self.raw))
        self.assertEqual(json.loads((self.raw/'manifest.json').read_bytes())['status'],'INCOMPLETE')
        with self.assertRaisesRegex(ValueError,'INCOMPLETE'):A.build(self.raw,self.dest)
        c,_=self.collected()
        self.assertEqual(c.calls,1)
        c=Fake(stop_after=0)
        self.assertFalse(A.collect(c,self.raw,'20261017'))
        with self.assertRaisesRegex(ValueError,'INCOMPLETE'):A.build(self.raw,self.dest)

    def test_plain_paragraphs_html_and_supplement_preserve_text(self):
        self.assertEqual(A.article_rows('첫 문단\n\n둘째 문단'),[['문단1','','첫 문단'],['문단2','','둘째 문단']])
        arts=A.article_rows('<p>제1조(목적) 원문 &amp; 합성</p><p>제2조(표) 첫 칸</p><table><tr><td>10</td><td>20</td></tr></table>')
        self.assertEqual(arts[0][0],'제1조')
        self.assertIn('원문 & 합성',arts[0][2])
        self.assertIn('10\n20',arts[1][2])
        d=detail(row());d['AdmRulService']['부칙']={'부칙내용':'이 규정은 합성 시행일부터 적용한다.'}
        self.assertEqual(A.validate_raw(A.encode(d),row(),DATE)[1][-1][0],'부칙')
        with self.assertRaisesRegex(ValueError,'DUPLICATE_ARTICLE'):A.article_rows('제1조(목적) 하나\n제1조(목적) 둘')


    def test_asset_verifier_rejects_tampering_extras_oversize_and_meta_is_atomic(self):
        self.assets()
        path=next((self.dest/'admrul/docs').glob('*.json'))
        original=path.read_bytes();path.write_bytes(original+b' ')
        with self.assertRaisesRegex(ValueError,'HASH'):A.verify(self.dest)
        path.write_bytes(original)
        (self.dest/'admrul/stale.json').write_text('{}')
        with self.assertRaisesRegex(ValueError,'UNLISTED'):A.verify(self.dest)
        (self.dest/'admrul/stale.json').unlink()
        raw=(self.dest/'meta.json').read_bytes()
        with patch.object(A,'META_LIMIT',1):
            with self.assertRaisesRegex(ValueError,'META_TOO_LARGE'):A.verify(self.dest,True)
        self.assertEqual((self.dest/'meta.json').read_bytes(),raw)
        with patch.object(A,'FILE_LIMIT',1):
            with self.assertRaises(ValueError):A.verify(self.dest)



    def test_cli_partial_only_for_budget_and_step_output(self):
        with patch.dict(os.environ,{'LAW_OC':'fixture-credential-only','GITHUB_OUTPUT':str(self.root/'output')}), \
             patch.object(sys,'argv',['admrul','collect','--out',str(self.raw),'--partial-ok']), \
             patch.object(A,'Client',return_value=Fake(stop_after=2)):
            self.assertEqual(A.main(),0)
        self.assertTrue((self.root/'output').read_text().endswith('complete=false\n'))
        with patch.dict(os.environ,{'LAW_OC':'fixture-credential-only','GITHUB_OUTPUT':str(self.root/'output')}), \
             patch.object(sys,'argv',['admrul','collect','--out',str(self.raw),'--partial-ok']), \
             patch.object(A,'Client',return_value=Fake()):
            self.assertEqual(A.main(),0)
        self.assertTrue((self.root/'output').read_text().endswith('complete=true\n'))
        class BrokenCatalog(Fake):
            def get(self,*args,**kwargs):
                self.calls+=1
                raise RuntimeError('https://test.invalid/?OC=fixture-credential-only')
        with patch.dict(os.environ,{'LAW_OC':'fixture-credential-only','GITHUB_OUTPUT':str(self.root/'output')}), \
             patch.object(sys,'argv',['admrul','collect','--out',str(self.root/'failed'),'--partial-ok']), \
             patch.object(A,'Client',return_value=BrokenCatalog()):
            self.assertEqual(A.main(),1)
        self.assertTrue((self.root/'output').read_text().endswith('complete=false\n'))
        self.assertNotIn('fixture-credential-only',(self.root/'failed/attempt.json').read_text())



if __name__=='__main__':unittest.main()
