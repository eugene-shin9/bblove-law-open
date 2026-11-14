"""Synthetic publication and scheduling contracts; no private sample text."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unicodedata
import unittest
from unittest.mock import patch
sys.path[:0]=[str(Path(__file__).parents[1]/'src'),str(Path(__file__).parents[1]/'scripts')]
from test_committee_collect import Client, ROW, DETAIL
from bblove_precedent_collect import collect, validated
import bblove_committee_release as committee
from bblove_committee_release import gate, export, verify, FIELDS, pii_text_dash, _DASHES_WIDE
from committee_release import needs_publication
import collect_precedents as wrapper
from workflow_contract import load_workflow

# Digit groups of a synthetic account; every account-shaped string is joined at runtime for the repository guard.
ACCT=('110','123','456789')


class Hundred(Client):
    """100 synthetic decc records; 재결요지 of record i is texts.get(i), otherwise a neutral summary."""
    def __init__(self, texts): super().__init__(); self.texts=texts
    def get(self, endpoint, params, **kwargs):
        self.calls+=1
        if endpoint=='lawSearch.do':
            rows=[]
            for i in range(100):
                sid=str(900001+i)
                rows.append(dict(ROW, **{'행정심판재결례일련번호':sid,'사건번호':f'2099행심{i+1}',
                    '행정심판례상세링크':f'https://www.law.go.kr/DRF/lawService.do?target=decc&ID={sid}'}))
            return {'Decc':{'totalCnt':'100','decc':rows}},None
        i=int(params['ID'])-900001
        d=dict(DETAIL, **{'행정심판례일련번호':params['ID'],'사건번호':f'2099행심{i+1}',
            '재결요지':self.texts.get(i,'합성 요지')})
        doc={'PrecService':d}; return doc,json.dumps(doc).encode()


class CommitteeReleaseTests(unittest.TestCase):
    def test_target_only_incremental_and_summary_untouched(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t); summary=root/'summary'; committee=root/'committee'
            collect(Client(),summary,'20261103')
            before={str(p):p.read_bytes() for p in summary.rglob('*') if p.is_file()}
            c=Client(); self.assertTrue(collect(c,committee,'20261103',targets=('decc',),incremental=True))
            self.assertTrue(all(target=='decc' for _,target in c.requests))
            self.assertEqual(before,{str(p):p.read_bytes() for p in summary.rglob('*') if p.is_file()})
            c=Client(); collect(c,committee,'20261104',targets=('decc',),incremental=True)
            m=json.loads((committee/'manifest.json').read_text())
            self.assertTrue(m['incremental']['complete'])
            self.assertEqual(set(m['catalogs']),{'decc'})
            exported=root/'export'; export(committee,exported)
            rows=verify(exported)
            self.assertEqual(set(rows[0]),set(FIELDS))
            self.assertNotIn('이유',(exported/'records.jsonl').read_text())
            self.assertEqual(set(p.name for p in exported.iterdir()),{'records.jsonl','committee-manifest.json'})
            from release_info import sha256
            self.assertFalse(needs_publication(exported,sha256(exported/'committee-manifest.json')))
            self.assertTrue(needs_publication(exported,None))
            (exported/'records.jsonl').write_bytes(b'{}\n')
            with self.assertRaises(ValueError): verify(exported)

    def test_gate_boundaries_and_fail_closed(self):
        row={k:validated(ROW,DETAIL,'decc','detail')[k] for k in FIELDS}
        # Synthetic identifiers are assembled at runtime so the public repository guard
        # never sees a literal resident-number or phone pattern in tracked source.
        rrn=lambda last7: '900101'+'-'+last7
        phone='-'.join(('010','1234','5678'))
        for text in (rrn('1234567'),rrn('5234567'),phone,'02-123-4567','person@example.invalid','123-456-789012','계좌 123456789012'):
            self.assertTrue(gate(dict(row,holding=text)),text)
        for text in ('2026-01-01','2026-12345','2099경기행심1','제12조 제3항','1,234,567원'):
            self.assertFalse(gate(dict(row,holding=text)),text)
        with tempfile.TemporaryDirectory() as t:
            class Bad(Client):
                def get(self,*a,**kw):
                    doc,raw=super().get(*a,**kw)
                    if raw:
                        doc=json.loads(raw)
                        doc['PrecService']['재결요지']='-'.join(('010','1234','5678'))
                        raw=json.dumps(doc).encode()
                    return doc,raw
            collect(Bad(),Path(t)/'s','20261103',targets=('decc',))
            # Deliberate failure: capture the ::error:: line so it never becomes a red annotation in Actions.
            with contextlib.redirect_stdout(io.StringIO()) as log, self.assertRaisesRegex(ValueError,'EXCLUSION_RATE'): export(Path(t)/'s',Path(t)/'out')
            self.assertIn('::error::COMMITTEE_EXCLUSION_RATE_EXCEEDED',log.getvalue())
            self.assertFalse((Path(t)/'out').exists())

    def test_gate_normalizes_width_dashes_and_invisible_characters(self):
        wide=str.maketrans('0123456789-','０１２３４５６７８９－')  # assembled at runtime for the repository guard
        phone='-'.join(('010','1234','5678')); rrn='900101'+'-'+'1234567'
        for text in (phone.translate(wide), phone.replace('-','\u2013'), phone.replace('-','\u200b-'), rrn.translate(wide), rrn.replace('-','\u2212')):
            self.assertTrue(gate({'holding':text}),text.encode('unicode_escape'))
        for text in ('２０２６－０１－０１','제１２조 제３항','１，２３４，５６７원'):
            self.assertFalse(gate({'holding':text}),text)

    def test_halt_percent_boundary(self):
        for bad in (5,6):
            with tempfile.TemporaryDirectory() as t:
                root=Path(t); collect(Hundred({i:'person@example.invalid' for i in range(bad)}),root/'s','20261103',targets=('decc',))
                if bad==5:
                    m=export(root/'s',root/'out')
                    self.assertEqual((m['accepted'],m['excluded']),(95,5))
                    self.assertEqual(len(verify(root/'out')),95)
                else:
                    with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(ValueError,'EXCLUSION_RATE'): export(root/'s',root/'out')

    def test_gate_dash_copy_prefixed_and_split_accounts(self):
        a=ACCT; dashed='-'.join(a)
        newly={'\u00ad'.join(a):'ACCOUNT', '\u3161'.join(a):'ACCOUNT', '국민은행-'+dashed+'.':'ACCOUNT_PREFIXED',
               '신한-'+dashed:'ACCOUNT_PREFIXED', '계좌번호 '+' '.join(a):'ACCOUNT_CONTEXT_SPLIT',
               '계좌 '+'.'.join(a):'ACCOUNT_CONTEXT_SPLIT', 'account: '+' '.join(a):'ACCOUNT_CONTEXT_SPLIT'}
        for text,code in newly.items():
            self.assertEqual(gate({'holding':text}),[code],text.encode('unicode_escape'))
        for text in ('입금 계좌 '+dashed+'.', '\u2010'.join(a), '계좌 '+'3333'+'012345678'+'.'):
            self.assertTrue(gate({'holding':text}),text.encode('unicode_escape'))
        # Decided: a word, a hyphen and three groups cannot be told apart from an account, so it is excluded.
        for text in ('처분청-'+'-'.join(('2019','123','4567')), '산-'+'-'.join(('106','19','123')), '\u00ad'.join(('106','19','123'))):
            self.assertTrue(gate({'holding':text}),text.encode('unicode_escape'))
        for text in ('서울특별시 ○○구 ○○동 106-19','1997-02540','2026.01.01','계좌 2019.03.05 100만원',
                     '계좌번호 1997 5 3 12345','제12조-제3항','국민은행-2026-01-01'):
            self.assertFalse(gate({'holding':text}),text)

    def test_dash_copy_table_lists_each_nfkc_form(self):
        for ch in '\u00ad\u2043\u02d7\u2e3a\u2e3b\u3161\uffda':
            normalized=unicodedata.normalize('NFKC',ch)
            self.assertEqual(len(normalized),1,hex(ord(ch)))
            self.assertEqual(_DASHES_WIDE.get(ord(normalized)),'-',hex(ord(ch)))
            self.assertEqual(pii_text_dash(ch),'-',hex(ord(ch)))

    def test_normalized_copies_once_per_value(self):
        with patch.object(committee,'pii_text',wraps=committee.pii_text) as plain, \
                patch.object(committee,'pii_text_dash',wraps=committee.pii_text_dash) as dashed:
            committee.gate({'id':'1','title':'합성 제목','holding':'합성 요지','order':'합성 주문'})
        self.assertEqual((plain.call_count,dashed.call_count),(3,3))

    def test_gate_time_is_linear(self):
        # The former ACCOUNT_CONTEXT needed over 20 s on the first input (cubic in the blank run);
        # the order makes a regression fail on it before the 60,000-blank input.
        for text in ('계좌'+' '*2000+'x', '계좌'+' '*60000+'x', ('계좌'+' '*200)*300, '계좌 '+'1 '*30000,
                     '계좌 '+'1'*60000, '계좌 '+'12.'*30000, '가-12-34-'*10000, ('\u3161'+'1')*30000,
                     '\u00ad1'*30000, '가'*200000):
            start=time.perf_counter(); gate({'holding':text})
            self.assertLess(time.perf_counter()-start,1.0,text[:12])

    def test_new_codes_pass_verify(self):
        a=ACCT
        with tempfile.TemporaryDirectory() as t:
            root=Path(t)
            collect(Hundred({0:'신한-'+'-'.join(a), 1:'계좌번호 '+' '.join(a), 2:'\u00ad'.join(a)}),root/'s','20261103',targets=('decc',))
            m=export(root/'s',root/'out')
            self.assertEqual(m['exclusion_reasons'],{'ACCOUNT':1,'ACCOUNT_CONTEXT_SPLIT':1,'ACCOUNT_PREFIXED':1})
            self.assertEqual(len(verify(root/'out')),97)

    def test_total_change_reset_uses_explicit_targets(self):
        with tempfile.TemporaryDirectory() as t:
            out=Path(t)
            m={'catalogs':{'decc':{'complete':False}},'documents':[], 'pages':[]}
            (out/'manifest.json').write_text(json.dumps(m))
            self.assertIsNone(wrapper.reset_catalog(out))
            self.assertEqual(wrapper.reset_catalog(out,('decc',)),'decc')
            self.assertEqual(json.loads((out/'manifest.json').read_text())['catalogs']['decc'],{'next_page':1,'complete':False})

    def test_wrapper_isolates_failure(self):
        with tempfile.TemporaryDirectory() as t, patch.dict(os.environ,{'GITHUB_OUTPUT':t+'/outputs'}):
            wrapper.write_outputs('COMPLETE')
            with patch.object(wrapper,'run',side_effect=RuntimeError('synthetic')) as run:
                self.assertEqual(wrapper.run_committee('PAUSED',Path(t),100,lambda _:None),'SKIPPED')
                run.assert_not_called()
                with contextlib.redirect_stdout(io.StringIO()) as log:
                    self.assertEqual(wrapper.run_committee('COMPLETE',Path(t),100,lambda _:None),'FAILED')
                self.assertIn('::warning::COMMITTEE_COLLECTION_FAILED',log.getvalue())
            output=Path(t+'/outputs').read_text()
            self.assertTrue(output.startswith('outcome=COMPLETE\ncomplete=true\n'))
            self.assertIn('committee_outcome=FAILED',output)

    def test_workflow_separate_release_and_scan(self):
        path=Path(__file__).parents[1]/'.github/workflows/collect-precedents.yml'
        if not path.exists(): self.skipTest('public producer workflow only')
        wf=load_workflow(path); job=wf['jobs']['collect']; steps=job['steps']
        self.assertEqual(job['timeout-minutes'],355)
        self.assertIn('--committee',next(s['run'] for s in steps if s.get('id')=='collect'))
        for step in steps:
            if step.get('id')=='committee_attest':
                self.assertEqual(step['with']['subject-path'],'release-decc/*')
                self.assertIn('always()',step['if'])
        self.assertEqual(wrapper.published_targets([dict(tagName='decc-20261103',isDraft=False)]),set())
