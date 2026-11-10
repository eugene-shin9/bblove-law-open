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
        for damage in ('envelope','page','duplicate','date','publication'):
            with self.subTest(damage=damage):
                d={'AdmRulSearch':{'totalCnt':'1','page':'1','admrul':[row()]}}
                if damage=='envelope':d={'unknown':d['AdmRulSearch']}
                elif damage=='page':d['AdmRulSearch']['page']='2'
                elif damage=='org':d['AdmRulSearch']['admrul'][0]['소관부처명']='법무부'
                elif damage=='date':d['AdmRulSearch']['admrul'][0]['시행일자']='20250230'
                elif damage=='publication':d['AdmRulSearch']['admrul'][0]['발령일자']='20261017'
                else:d['AdmRulSearch']['admrul']='bad'
                code={'date':'^ADMRUL_DATE_INVALID$','publication':'^ADMRUL_PUBLICATION_FUTURE$'}.get(damage,'')
                with self.assertRaisesRegex(ValueError,code):A.parse_page(d,'국세청',1,DATE)
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




def source_line(module, needle):
    """Line number of the unique source line containing needle (keeps tests line-shift proof)."""
    lines = Path(module.__file__).read_text(encoding='utf-8').splitlines()
    found = [i for i, l in enumerate(lines, 1) if needle in l]
    assert len(found) == 1, found
    return found[0]


class CatalogFailureDiagnostics(unittest.TestCase):
    """Unexpected catalog errors record class and location only, never message/body/URL/OC."""
    SECRET = 'fixture-credential-only'

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.raw = Path(self.temp.name) / 'raw'

    def attempt(self):
        text = (self.raw / 'attempt.json').read_text(encoding='utf-8')
        for leaked in (self.SECRET, 'http', 'OC=', 'future-effective', 'admrul effective', '조사'):
            self.assertNotIn(leaked, text)
        return json.loads(text)

    def test_bad_list_dates_are_coded_failures_without_exception_fields(self):
        # Before: a not-yet-effective row failed the whole list as ADMRUL_CATALOG_FAILED (11/10).
        for field, value, reason in (('시행일자', '20250230', 'ADMRUL_DATE_INVALID'),
                                     ('시행일자', '2025.02.01', 'ADMRUL_DATE_INVALID'),
                                     ('발령일자', '20261017', 'ADMRUL_PUBLICATION_FUTURE')):
            with self.subTest(field=field, value=value):
                bad = row(1, title='세무조사 사무처리규정')
                bad[field] = value
                client = Fake([bad, row(2, '재정경제부', '소득세 납세절차', '예규')])
                self.assertFalse(A.collect(client, self.raw, DATE))
                self.assertEqual(client.calls, 1)
                self.assertEqual(self.attempt(), {'status': 'FAILED', 'reason': reason, 'api_calls': 1})
                manifest = json.loads((self.raw / 'manifest.json').read_text())
                self.assertEqual(manifest['catalog_failure'], reason)

    def test_unexpected_error_message_with_url_and_credential_is_not_recorded(self):
        class Broken(Fake):
            def get(self, *args, **kwargs):
                self.calls += 1
                raise RuntimeError(f'https://test.invalid/DRF/lawSearch.do?OC={self.credential} body')
        self.assertFalse(A.collect(Broken(), self.raw, DATE))
        att = self.attempt()
        self.assertEqual(set(att), {'status', 'reason', 'api_calls', 'exception', 'where', 'admrul_where'})
        self.assertEqual(att['exception'], 'RuntimeError')
        self.assertRegex(att['where'], r'^test_admrul\.py:get:\d+$')
        self.assertRegex(att['admrul_where'], r'^bblove_admrul\.py:catalog:\d+$')

    def test_coded_failure_adds_no_exception_fields(self):
        self.assertFalse(A.collect(Fake(stop_after=0), self.raw, DATE))
        self.assertEqual(self.attempt(), {'status': 'INCOMPLETE', 'reason': 'COLLECTION_BUDGET_EXHAUSTED',
                                          'api_calls': 1})

    def test_unusual_frame_names_are_dropped(self):
        frame = A.traceback.FrameSummary('/x/evil name?OC=1.py', 3, 'f', lookup_line=False)
        self.assertIsNone(A._site(frame))
        frame = A.traceback.FrameSummary('/x/ok.py', 3, 'f(x)', lookup_line=False)
        self.assertIsNone(A._site(frame))
        self.assertEqual(A._site(A.traceback.FrameSummary('/x/ok.py', 3, '<genexpr>', lookup_line=False)), 'ok.py:<genexpr>:3')

    def test_stale_attempt_from_restored_cache_is_cleared_on_success(self):
        self.raw.mkdir()
        (self.raw / 'attempt.json').write_text('{"status": "FAILED", "reason": "ADMRUL_CATALOG_FAILED"}')
        self.assertTrue(A.collect(Fake(), self.raw, DATE))
        self.assertFalse((self.raw / 'attempt.json').exists())

    def test_workflow_progress_reports_only_filtered_class_and_location(self):
        import yaml
        flow = yaml.safe_load((ROOT / '.github/workflows/collect-admrul.yml').read_text(encoding='utf-8'))
        step = next(s for s in flow['jobs']['collect']['steps'] if s.get('name', '').startswith('Progress'))
        script = step['run'].split("<<'PY'\n", 1)[1].rsplit('PY', 1)[0]
        work = Path(self.temp.name) / 'work'
        (work / 'auto/admrul').mkdir(parents=True)
        (work / 'auto/admrul/manifest.json').write_text('{"date": "20261110", "status": "INCOMPLETE", "documents": []}')
        (work / 'auto/admrul/attempt.json').write_text(json.dumps({
            'status': 'FAILED', 'reason': 'ADMRUL_CATALOG_FAILED', 'exception': 'ValueError',
            'where': 'bblove_v107_pilot.py:check_date:57',
            'admrul_where': f'https://x.invalid/?OC={self.SECRET}'}))
        summary = work / 'summary.md'
        env = {k: v for k, v in os.environ.items() if k != 'GITHUB_STEP_SUMMARY'}
        env['GITHUB_STEP_SUMMARY'] = str(summary)
        out = subprocess.run([sys.executable, '-I', '-c', script], cwd=work, env=env,
                             capture_output=True, text=True, check=True).stdout
        for text in (out, summary.read_text(encoding='utf-8')):
            self.assertIn('exception=ValueError where=bblove_v107_pilot.py:check_date:57 admrul_where=None', text)
            self.assertNotIn(self.SECRET, text)
            self.assertNotIn('http', text)



LATER = '20261110'


class NotYetEffective(unittest.TestCase):
    """A list row whose 시행일자 is after the collection date is excluded, never published."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.raw = self.root / 'raw'

    def rows(self, effective=None, history='현행', mst=None):
        target = row(1, title='세무조사 사무처리규정')
        if effective:
            target['시행일자'] = effective
            target['현행연혁구분'] = history
        if mst:
            target['행정규칙일련번호'] = mst
        return [target, row(2, title='국세 징수 사무처리규정'),
                row(3, '재정경제부', '소득세 납세절차', '예규')]

    def manifest(self):
        return json.loads((self.raw / 'manifest.json').read_text(encoding='utf-8'))

    def pending(self, manifest):
        return [e for e in manifest['excluded'] if e['reason'] == 'NOT_YET_EFFECTIVE']

    def details_requested(self, client):
        return [p['ID'] for e, p, _ in client.requests if e == 'lawService.do']

    def test_pending_row_is_excluded_and_its_body_is_never_requested(self):
        client = Fake(self.rows('20270101'))
        self.assertTrue(A.collect(client, self.raw, LATER))
        m = self.manifest()
        self.assertEqual(m['status'], 'COMPLETE')
        [p] = self.pending(m)
        self.assertEqual((p['id'], p['mst'], p['title'], p['effective'], p['history_class']),
                         ('101', '2100000000001', '세무조사 사무처리규정', '20270101', '현행'))
        self.assertNotIn('previous_edition_dropped', p)  # no previous manifest: exclusion only
        self.assertNotIn('2100000000001', self.details_requested(client))
        self.assertFalse((self.raw / p['path']).exists())
        self.assertEqual({e['id'] for e in m['documents']}, {'102', '103'})
        self.assertTrue(all(e['status'] == 'SUCCESS' for e in m['documents']))

    def test_previous_edition_is_marked_dropped_not_kept(self):
        self.assertTrue(A.collect(Fake(self.rows()), self.raw, DATE))
        old_mst = '2100000000001'
        self.assertIn('101', {e['id'] for e in self.manifest()['documents']})
        # New edition of the same rule (same 행정규칙ID), not yet in force.
        client = Fake(self.rows('20270101', mst='2100000000777'))
        self.assertTrue(A.collect(client, self.raw, LATER))
        m = self.manifest()
        [p] = self.pending(m)
        self.assertEqual((p['previous_edition_dropped'], p['previous_mst'], p['mst']),
                         (True, old_mst, '2100000000777'))
        self.assertNotIn('101', {e['id'] for e in m['documents']})
        dest = self.root / 'assets'
        A.build(self.raw, dest)
        index = json.loads((dest / 'admrul/index.json').read_text(encoding='utf-8'))
        self.assertNotIn('101', {e['id'] for e in index['documents'].values()})
        self.assertIn({'id': '101', 'title': '세무조사 사무처리규정', 'reason': 'NOT_YET_EFFECTIVE'},
                      index['excluded'])
        # The mark stays on later runs (and through a failed list) until the edition takes effect.
        self.assertTrue(A.collect(Fake(client.rows), self.raw, '20261111'))
        self.assertTrue(self.pending(self.manifest())[0]['previous_edition_dropped'])
        broken = Fake(client.rows)
        broken.rows = broken.rows[:1]
        self.assertFalse(A.collect(broken, self.raw, '20261112'))
        self.assertTrue(A.collect(Fake(client.rows), self.raw, '20261113'))
        self.assertEqual(self.pending(self.manifest())[0]['previous_mst'], old_mst)

    def test_rule_is_collected_once_effective(self):
        rows = self.rows('20261111')
        self.assertTrue(A.collect(Fake(rows), self.raw, LATER))
        self.assertEqual(len(self.pending(self.manifest())), 1)
        client = Fake(rows)
        self.assertTrue(A.collect(client, self.raw, '20261111'))
        m = self.manifest()
        self.assertEqual(self.pending(m), [])
        self.assertIn('2100000000001', self.details_requested(client))
        entry = next(e for e in m['documents'] if e['id'] == '101')
        self.assertEqual(entry['status'], 'SUCCESS')
        self.assertNotIn(A.NOT_YET_EFFECTIVE, entry['row'])

    def test_pending_rows_still_count_for_pages_totals_and_duplicates(self):
        rows = both_orgs(self.rows('20270101'))
        with self.assertRaisesRegex(ValueError, '^ADMRUL_DUPLICATE_ID$'):
            A.catalog(Fake(rows + [dict(rows[0])]), self.root / 'dup', LATER)
        class Short(Fake):
            def get(self, endpoint, params, original=False):
                doc, raw = super().get(endpoint, params, original)
                if endpoint == 'lawSearch.do':
                    doc['AdmRulSearch']['totalCnt'] = str(int(doc['AdmRulSearch']['totalCnt']) + 1)
                    raw = A.encode(doc)
                return doc, raw
        with self.assertRaisesRegex(ValueError, '^ADMRUL_CATALOG_INCOMPLETE$'):
            A.catalog(Short(rows), self.root / 'short', LATER)
        page = {'AdmRulSearch': {'totalCnt': '1', 'page': '2', 'admrul': [rows[0]]}}
        with self.assertRaisesRegex(ValueError, '^ADMRUL_CATALOG_COUNT_OR_PAGE$'):
            A.parse_page(page, '국세청', 1, LATER)
        # A ministry below ten stays blocked even when one of its rows is pending.
        small = [r for r in rows if A.current_org(r['소관부처명']) == '국세청'][:9]
        small += [r for r in rows if A.current_org(r['소관부처명']) == '재정경제부']
        client = Fake(small)
        client.rows = small
        self.assertFalse(A.collect(client, self.raw, LATER))
        self.assertEqual(json.loads((self.raw / 'attempt.json').read_text())['reason'],
                         'ADMRUL_ORG_CATALOG_TOO_SMALL')

    def test_history_class_check_skipped_only_for_pending_rows(self):
        page = lambda r: {'AdmRulSearch': {'totalCnt': '1', 'page': '1', 'admrul': [r]}}
        pending = self.rows('20270101', history='연혁')[0]
        [item], _ = A.parse_page(page(pending), '국세청', 1, LATER)
        self.assertEqual((item[A.NOT_YET_EFFECTIVE], item['현행연혁구분']), (True, '연혁'))
        self.assertTrue(A.collect(Fake(self.rows('20270101', history='연혁')), self.raw, LATER))
        self.assertEqual(self.pending(self.manifest())[0]['history_class'], '연혁')
        in_force = dict(self.rows()[0], 현행연혁구분='연혁')
        with self.assertRaisesRegex(ValueError, '^ADMRUL_NOT_CURRENT$'):
            A.parse_page(page(in_force), '국세청', 1, LATER)

    def test_publication_gates_still_reject_future_effective_bodies(self):
        r = row(1, title='세무조사 사무처리규정')
        future = dict(r, 시행일자='20270101')
        with self.assertRaisesRegex(ValueError, 'future-effective'):
            A.validate_raw(A.encode(detail(future)), future, LATER)
        with self.assertRaisesRegex(ValueError, 'future-effective'):
            A.checked_row(future, LATER)
        # A details response whose 시행일자 is future fails that document, never publishes it.
        client = Fake(self.rows(), {r['행정규칙일련번호']: detail(future)})
        self.assertTrue(A.collect(client, self.raw, LATER))
        entry = next(e for e in self.manifest()['documents'] if e['id'] == '101')
        self.assertEqual((entry['status'], entry['reason']), ('FAILED', 'ADMRUL_DETAIL_FAILED'))
        # build: a manifest row that is not yet effective is refused.
        self.raw2 = self.root / 'raw2'
        self.assertTrue(A.collect(Fake(self.rows()), self.raw2, LATER))
        m = json.loads((self.raw2 / 'manifest.json').read_text(encoding='utf-8'))
        m['documents'][0]['row']['시행일자'] = '20270101'
        (self.raw2 / 'manifest.json').write_bytes(A.encode(m))
        with self.assertRaisesRegex(ValueError, 'future-effective'):
            A.build(self.raw2, self.root / 'assets-x')
        # verify: an index entry that is not yet effective is refused.
        dest = self.root / 'assets'
        self.assertTrue(A.collect(Fake(self.rows()), self.root / 'raw3', LATER))
        A.build(self.root / 'raw3', dest)
        index = json.loads((dest / 'admrul/index.json').read_text(encoding='utf-8'))
        next(iter(index['documents'].values()))['eff'] = '2027-01-01'
        (dest / 'admrul/index.json').write_bytes(A.encode(index))
        with self.assertRaisesRegex(ValueError, 'future-effective'):
            A.verify(dest)

    def workflow_script(self, step_prefix, marker="<<'PY'\n"):
        import yaml
        flow = yaml.safe_load((ROOT / '.github/workflows/collect-admrul.yml').read_text(encoding='utf-8'))
        step = next(s for s in flow['jobs']['collect']['steps'] if s.get('name', '').startswith(step_prefix))
        return step['run'].split(marker, 1)[1].split('\nPY\n', 1)[0]

    def run_script(self, script, cwd):
        env = {k: v for k, v in os.environ.items() if k != 'GITHUB_STEP_SUMMARY'}
        env['GITHUB_STEP_SUMMARY'] = str(cwd / 'summary.md')
        out = subprocess.run([sys.executable, '-I', '-c', script], cwd=cwd, env=env,
                             capture_output=True, text=True, check=True).stdout
        summary = cwd / 'summary.md'
        return out, summary.read_text(encoding='utf-8') if summary.exists() else ''

    def test_build_verify_package_and_reports_with_pending_rows(self):
        work = self.root / 'work'
        self.raw = work / 'auto/admrul'
        self.assertTrue(A.collect(Fake(self.rows()), self.raw, DATE))
        client = Fake(self.rows('20270101', history='현행', mst='2100000000777'))
        client.credential = 'fixture-credential-only'
        self.assertTrue(A.collect(client, self.raw, LATER))
        dest = work / 'admrul-assets'
        A.build(self.raw, dest)
        meta(dest)
        A.verify(dest, merge_meta=True)
        # Package as the workflow does and write release-info.
        release = work / 'release'
        release.mkdir()
        (release / 'admrul-manifest.json').write_bytes((self.raw / 'manifest.json').read_bytes())
        subprocess.run([sys.executable, '-I', str(ROOT / 'scripts/release_info.py'), '--release', str(release),
                        '--manifest', str(self.raw / 'manifest.json'), '--kind', 'admrul'],
                       check=True, capture_output=True)
        info = json.loads((release / 'release-info.json').read_text(encoding='utf-8'))
        self.assertEqual((info['collection_status'], info['collected_date']), ('COMPLETE', LATER))
        env = {**os.environ, 'LAW_OC': 'fixture-credential-only'}
        for target in (self.raw, dest, release):
            scan = subprocess.run([sys.executable, '-I', str(ROOT / 'scripts/secret_scan.py'), str(target)],
                                  env=env, capture_output=True, text=True)
            self.assertEqual(scan.returncode, 0, scan.stdout + scan.stderr)
        # Progress notice and Job Summary: counts, history values and titles only.
        out, summary = self.run_script(self.workflow_script('Progress'), work)
        for text in (out, summary):
            self.assertIn('not_yet_effective=1 previous_edition_dropped=1 history_class=["현행"]', text)
            self.assertIn('세무조사 사무처리규정 (시행 20270101, 옛 판 빠짐)', text)
            for leaked in ('fixture-credential-only', 'http', 'OC=', '제1조'):
                self.assertNotIn(leaked, text)
        # Release notes carry counts only, never titles.
        notes, _ = self.run_script(self.workflow_script('Publish', "pending=$(python3 -I - <<'PY'\n"), work)
        self.assertEqual(notes.strip(), '시행 전 제외 1건(옛 판 빠짐 1건).')
        self.assertNotIn('세무조사', notes)


if __name__=='__main__':unittest.main()
