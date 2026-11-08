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
    basic.update(현행여부='Y',소관부처코드=A.ORG[A.current_org(r['소관부처명'])])
    return {'AdmRulService':{'행정규칙기본정보':basic,'조문내용':body}}


class Fake:
    credential='fixture-credential-only'

    def __init__(self, rows=None, details=None, stop_after=None):
        self.rows=rows if rows is not None else [row(),row(2,'재정경제부','소득세 납세절차','예규')]
        self.rows=both_orgs(self.rows)
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
            org=next(n for n,c in A.ORG.items() if c==params['org'])
            rows=[r for r in self.rows if A.current_org(r['소관부처명'])==org]
            start=(params['page']-1)*100
            doc={'AdmRulSearch':{'totalCnt':str(len(rows)),'page':str(params['page']),
                                'admrul':rows[start:start+100]}}
        else:
            r=next(r for r in self.rows if r['행정규칙일련번호']==params['ID'])
            doc=self.details.get(params['ID'],detail(r))
        raw=A.encode(doc)
        return doc,raw


def both_orgs(rows):
    """Pad valid synthetic catalogs to ten with excluded personnel rules."""
    rows = list(rows)
    for i, org in enumerate(A.ORG):
        count = sum(A.current_org(r['소관부처명']) == org for r in rows)
        rows += [row(900 + i * 10 + j, org, '인사관리규정')
                 for j in range(max(0, 10 - count))]
    return rows


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
        # Former ministry name (기획재정부) is accepted as 재정경제부; others stay excluded.
        self.assertEqual({A.current_org(e['row']['소관부처명']) for e in m['documents']},set(A.ORG))
        self.assertEqual({e['reason'] for e in m['excluded']},{'NO_TAX_KEYWORD','CUSTOMS_EXCLUDED','OUTSIDE_KINDS'})
        self.assertEqual(A.exclusion(row(6,'법무부')), 'OUTSIDE_ORGANIZATIONS')
        lists=[p for ep,p,_ in client.requests if ep=='lawSearch.do']
        self.assertEqual({p['org'] for p in lists},{'1210000', '1053000'})
        self.assertTrue(all(p['target']=='admrul' and p['nw']=='1' for p in lists))

    def test_renamed_ministry_and_foreign_ministry_in_filtered_catalog(self):
        client,m=self.collected(Fake([row(),row(2,'재정경제부','부가가치세 사무처리','고시'),
                                      row(3,'기획재정부','소득세 납세절차','예규')]))
        self.assertEqual(len(m['documents']),3)

        class SubAgency(Fake):
            def get(self,endpoint,params,original=False):
                doc,raw=super().get(endpoint,params,original)
                if endpoint=='lawSearch.do' and params['org']==A.ORG['국세청']:
                    doc['AdmRulSearch']['admrul'][0]['소관부처명']='국세상담센터'
                return doc,A.encode(doc)
        # A sub-agency under the requested org code is excluded and listed, not a filter failure.
        out=self.root/'suborg'
        self.assertTrue(A.collect(SubAgency(both_orgs([row(),row(2,'국세청','세무조사 운영 지침','훈령')])),out,DATE))
        m=json.loads((out/'manifest.json').read_text())
        self.assertEqual(sorted(m['org_observed']),['국세상담센터','국세청','재정경제부'])
        self.assertIn('OUTSIDE_ORGANIZATIONS',{e['reason'] for e in m['excluded']})
        self.assertTrue(all(e['row']['소관부처명']!='국세상담센터' for e in m['documents']))

        class WrongCode(Fake):
            def get(self,endpoint,params,original=False):
                doc,raw=super().get(endpoint,params,original)
                if endpoint=='lawService.do':
                    doc['AdmRulService']['행정규칙기본정보']['소관부처코드']='9999999'
                return doc,A.encode(doc)
        out=self.root/'wrongcode'
        A.collect(WrongCode(both_orgs([row()])),out,DATE)
        m=json.loads((out/'manifest.json').read_text())
        self.assertEqual({e['reason'] for e in m['documents'] if e['status']=='FAILED'},{'ADMRUL_ORG_CODE_MISMATCH'})

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
        # 'org' no longer aborts: a sub-agency name under the requested code is excluded (see sub-agency test).
        for damage in ('envelope','page','duplicate','date'):
            with self.subTest(damage=damage):
                d={'AdmRulSearch':{'totalCnt':'1','page':'1','admrul':[row()]}}
                if damage=='envelope':d={'unknown':d['AdmRulSearch']}
                elif damage=='page':d['AdmRulSearch']['page']='2'
                elif damage=='org':d['AdmRulSearch']['admrul'][0]['소관부처명']='법무부'
                elif damage=='date':d['AdmRulSearch']['admrul'][0]['시행일자']='20250230'
                else:d['AdmRulSearch']['admrul']='bad'
                with self.assertRaises(ValueError):A.parse_page(d,'국세청',1,DATE)
        with self.assertRaisesRegex(ValueError,'DUPLICATE_ID'):
            A.catalog(Fake([row(),row()]),self.raw,DATE)

    def test_small_catalog_blocks_each_org_and_invalidates_previous_complete(self):
        for org in A.ORG:
            for count in (0, 1, 9):
                with self.subTest(org=org, count=count), tempfile.TemporaryDirectory() as tmp:
                    out = Path(tmp)
                    self.assertTrue(A.collect(Fake(), out, DATE))
                    client = Fake()
                    client.rows = [r for r in client.rows if A.current_org(r['소관부처명']) != org]
                    client.rows += [row(800+i, org) for i in range(count)]
                    self.assertFalse(A.collect(client, out, '20261017'))
                    manifest = json.loads((out/'manifest.json').read_text())
                    self.assertEqual(manifest['catalog_failure'], 'ADMRUL_ORG_CATALOG_TOO_SMALL')
                    self.assertEqual(manifest['status'], 'INCOMPLETE')
                    with self.assertRaisesRegex(ValueError, 'INCOMPLETE'):
                        A.build(out, self.dest)

    def test_cached_small_catalog_is_rejected(self):
        self.collected()
        checkpoint = json.loads((self.raw/'catalog.json').read_text())
        page = checkpoint['pages'][-1]
        path = self.raw/page['path']
        doc = json.loads(path.read_text())
        doc['AdmRulSearch']['totalCnt'] = '1'
        doc['AdmRulSearch']['admrul'] = doc['AdmRulSearch']['admrul'][:1]
        path.write_bytes(A.encode(doc))
        page['sha256'] = A.hashlib.sha256(path.read_bytes()).hexdigest()
        (self.raw/'catalog.json').write_bytes(A.encode(checkpoint))
        client = Fake(stop_after=0)
        self.assertFalse(A.collect(client, self.raw, DATE))
        self.assertEqual(client.calls, 0)
        self.assertEqual(json.loads((self.raw/'attempt.json').read_text())['reason'],
                         'ADMRUL_ORG_CATALOG_TOO_SMALL')

    def test_multiple_catalog_pages_and_complete_resume(self):
        rows=both_orgs([row(i,title=f'세무조사 규정 {i}') for i in range(1,102)])
        c=Fake(rows)
        self.assertEqual(len(A.catalog(c,self.raw,DATE)),111)
        self.assertEqual(c.calls,3) # two NTS pages, one nonempty MOEF page
        c=Fake(rows,stop_after=0)
        self.assertEqual(len(A.catalog(c,self.raw,DATE)),111)
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


    def test_mixed_html_succeeds_with_image_markers_and_removal_receipts(self):
        r = row()
        body = ('<p>제1조(목적) 합성 본문<img src="x" alt="합성 도표">'
                '<img src="y"/></p><script>hidden_script()</script>'
                '<style>.hidden_style { color: red; }</style><p>제2조(절차) 합성 절차</p>')
        d = detail(r, body)
        d['AdmRulService']['부칙'] = {'부칙내용':
            '<p>합성 부칙<img alt="부칙 도표"></p><script>hidden_supplement()</script>'}
        client, manifest = self.collected(Fake(details={r['행정규칙일련번호']: d}))
        self.assertEqual(manifest['schema'], 2)
        entry = manifest['documents'][0]
        self.assertEqual((entry['status'], entry['images_removed'], entry['scripts_removed']),
                         ('SUCCESS', 3, 2))
        self.assertEqual((self.raw / entry['path']).read_bytes(), A.encode(d))
        A.build(self.raw, self.dest)
        asset = json.loads((self.dest / f"admrul/docs/{entry['mst']}.json").read_bytes())[0]
        rendered = '\n'.join(a[2] for a in asset['a'])
        self.assertIn('[이미지: 합성 도표][이미지]', rendered)
        self.assertIn('[이미지: 부칙 도표]', rendered)
        self.assertNotIn('hidden_', rendered)
        self.assertNotIn('<', rendered)
        # A v1 checkpoint has no counters; replay must calculate them from exact originals.
        manifest['schema'] = 1
        for e in manifest['documents']:
            e.pop('images_removed')
            e.pop('scripts_removed')
        A.save(self.raw / 'manifest.json', manifest)
        replay, updated = self.collected(Fake(stop_after=0))
        self.assertEqual(replay.calls, 0)
        self.assertEqual(updated['documents'][0]['images_removed'], 3)
        self.assertEqual(updated['documents'][0]['scripts_removed'], 2)
        self.assertEqual(updated['documents'][1]['scripts_removed'], 0)

    def test_script_only_body_fails_with_existing_code_and_counts(self):
        r = row()
        d = detail(r, '<script>synthetic_only()</script><style>p{display:none}</style>')
        _, manifest = self.collected(Fake(details={r['행정규칙일련번호']: d}))
        entry = manifest['documents'][0]
        self.assertEqual(entry['status'], 'FAILED')
        self.assertEqual(entry['reason'], 'ADMRUL_BODY_MISSING')
        self.assertEqual((entry['images_removed'], entry['scripts_removed']), (0, 1))
        failures = json.loads((self.raw / 'collection-failures.json').read_bytes())
        self.assertEqual(failures[0]['reason'], 'ADMRUL_BODY_MISSING')
        for body in ('<img alt="합성 도표">', '<style>hidden</style>', '  '):
            with self.subTest(body=body), self.assertRaisesRegex(ValueError, 'ADMRUL_BODY_MISSING'):
                A.body_text(body)

    def test_link_and_event_attributes_never_become_body(self):
        body = ('<p onclick="hidden_event()"><a href="javascript:hidden_link()" '
                'onmouseover="hidden_hover()">합성 링크 본문</a>'
                '<img src="javascript:hidden_image()" onerror="hidden_error()" alt="합성 도표"></p>')
        self.assertEqual(A.body_text(body), '합성 링크 본문[이미지: 합성 도표]')
        # Entity-encoded/mixed-case attributes remain attributes, never text.
        self.assertEqual(A.body_text('<A HREF="java&#115;cript:hidden()" ONCLICK="hidden()">합성</A>'),
                         '합성')
        self.assertEqual(A.body_text('가<img alt="javascript:hidden()">나'), '가[이미지]나')

    def test_embedded_subtrees_nested_self_closing_and_unclosed(self):
        for hidden in (
                '<iframe>hidden<iframe>nested</iframe>hidden</iframe>',
                '<object><p>hidden</p><object>nested</object></object>',
                '<object><script>hidden()</script><img alt="hidden"></object>',
                '<script/>', '<style/>', '<iframe/>', '<object/>'):
            with self.subTest(hidden=hidden):
                stats = {}
                rendered = A.body_text('합성 앞' + hidden + '합성 뒤', stats)
                self.assertEqual(' '.join(rendered.split()), '합성 앞 합성 뒤')
                self.assertEqual(stats['scripts_removed'], int('script' in hidden))
                self.assertEqual(stats['images_removed'], int('<img' in hidden))
        self.assertEqual(A.body_text('합성 앞<object>hidden'), '합성 앞')
        # A mismatched close must not expose content of an unclosed nested subtree.
        self.assertEqual(A.body_text('합성 앞<object><iframe>hidden</object>hidden'), '합성 앞')
        self.assertEqual(A.body_text(['<p>가</p>', '<p>나</p>']), '가\n\n\n나')

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
