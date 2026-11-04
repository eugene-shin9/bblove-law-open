"""Synthetic v2 fulltext metadata/deltas; no source samples or live requests."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path[:0]=[str(Path(__file__).parents[1]/'src'),str(Path(__file__).parents[1]/'scripts')]
from test_precedent_d1 import FakeClient, DATE, PREC
from bblove_precedent_collect import (fulltext_object,store_fulltext,collect_fulltext,collect,
                                     CollectionPaused,summary_bytes,fulltext_usage)
from bblove_fulltext_delta import new_manifest,gate,queue,verify_delta,committed_snapshot
from bblove_law_all import save
from bblove_storage import digest
from fulltext_release import prepare,finalize,recover
TAG='prec-fulltext-'+DATE+'-r1-a1'


def value(body='합성 전문',fetched='synthetic-time'):
    return fulltext_object(PREC,json.dumps({'PrecService':dict(PREC,판례내용=body)}).encode(),fetched)


def publish_metadata(m,sid):
    m['documents'][sid].update(status='PUBLISHED',release=TAG)


class FulltextTests(unittest.TestCase):
    def test_raw_html_offsets_and_summary_unchanged(self):
        body='<p>합성 문단</p><br/>제1조\n끝';v=value(body)
        self.assertEqual(v['body'],body);self.assertEqual(v['body_sha256'],digest(body.encode()))
        self.assertEqual(''.join(body[a:b] for a,b in v['paragraphs']),body)
        self.assertEqual(summary_bytes(json.dumps(PREC).encode(),'prec'),summary_bytes(json.dumps(dict(PREC,판례내용=body)).encode(),'prec'))
        self.assertNotIn('secret',value('<a href="?OC=secret">링크</a>')['body'])

    def test_manifest_only_noop_previous_and_budget(self):
        with tempfile.TemporaryDirectory() as t:
            out=Path(t);m=new_manifest(DATE);usage={'bytes':0}
            first=value();store_fulltext(out,first,m,usage=usage);publish_metadata(m,first['id'])
            (out/'prec'/f"{first['id']}.json").unlink()
            self.assertFalse(store_fulltext(out,value(fetched='different-time'),m,usage=usage))
            self.assertEqual(list((out/'prec').iterdir()),[])
            with self.assertRaises(CollectionPaused):store_fulltext(out,value('changed'),m,budget=1,usage=usage)
            store_fulltext(out,value('changed'),m,usage=usage)
            self.assertEqual(m['documents'][first['id']]['prev_body_sha256'],first['body_sha256'])
            self.assertEqual(usage['bytes'],fulltext_usage(m))
            self.assertEqual(len(list(out.rglob('*.json'))),1)
            self.assertFalse((out/'prec/versions').exists())

    def test_gate_allowlist_and_credential_variants(self):
        phone='010'+'-'+'1234'+'-'+'5678'
        hexadecimal='0'*6+'1'*7+'a'*51
        self.assertEqual(gate(dict(value(),body_sha256=hexadecimal,id=phone,official_url=phone)),[])
        self.assertIn('PHONE',gate(value(phone)))
        from urllib.parse import quote,quote_plus
        secret='Synthetic /+ Credential'
        for variant in (secret,quote(secret,safe=''),quote_plus(secret,safe='')):
            self.assertIn('CREDENTIAL',gate(value(variant.upper()),secret))

    def test_queue_priority_attempts_and_180_days(self):
        m=new_manifest('20261103');entries=[{'id':str(i)} for i in range(1,7)]
        m['documents']={'1':{'status':'FAILED','attempts':1,'checked_date':'20261102'},
                        '2':{'status':'PUBLISHED','checked_date':'20250101'},
                        '3':{'status':'STAGED'},'4':{'status':'FAILED','attempts':3},
                        '5':{'status':'PUBLISHED','checked_date':'20260101'}}
        self.assertEqual([e['id'] for e in queue(entries,m,'20261103')],['3','6','1','2','5'])

    def run_collection(self,root,bodies=None,count=5,clock=lambda:0,deadline=10000):
        summary=root/'summary';out=root/'cache'
        if not (summary/'manifest.json').exists():collect(FakeClient(count=count),summary,DATE)
        calls=[]
        class FullClient(FakeClient):
            def get(self,endpoint,params,**kwargs):
                calls.append(params['ID'])
                doc,_=super().get(endpoint,params,**kwargs)
                detail=doc['Service']; body=(bodies or {}).get(params['ID'],'합성 전문')
                if body is None:raise ValueError('synthetic failure')
                detail['판례내용']=body
                return {'PrecService':detail},json.dumps({'PrecService':detail}).encode()
        state=collect_fulltext(lambda seconds:FullClient(count=count),summary,out,deadline,DATE,clock=clock)
        return state,calls,out,root/'fulltext-delta/fulltext-delta'

    def test_finalize_recovery_and_resume_without_bodies(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);state,calls,out,stage=self.run_collection(root)
            self.assertEqual(state,'COMPLETE');self.assertEqual(len(calls),5)
            tag=prepare(stage,out/'fulltext-manifest.json',root/'release','1','1')
            before=(out/'fulltext-manifest.json').read_bytes()
            self.assertEqual(finalize(out/'fulltext-manifest.json',stage,'failure',5,state,tag),'FAILED')
            self.assertEqual((out/'fulltext-manifest.json').read_bytes(),before)
            self.assertEqual(finalize(out/'fulltext-manifest.json',stage,'success',5,state,tag),'PUBLISHED')
            snapshot=(out/'fulltext-manifest.json').read_bytes();(out/'fulltext-manifest.json').unlink()
            operations=[]
            def execute(args):
                operations.append(args)
                if args[0]=='api':return json.dumps([[dict(tag_name=tag,draft=False,prerelease=False,published_at='2026')]])
                if args[:2]==['release','download']:
                    name=args[args.index('--pattern')+1];dest=Path(args[args.index('--dir')+1]);(dest/name).write_bytes((root/'release'/name).read_bytes())
                return ''
            self.assertEqual(recover(out/'fulltext-manifest.json',execute),'RELEASE')
            self.assertEqual(json.loads(snapshot),json.loads((out/'fulltext-manifest.json').read_text()))
            self.assertEqual(sum(a[0]=='attestation' for a in operations),2)
            state,calls,out,stage=self.run_collection(root)
            self.assertEqual(calls,[])
            self.assertEqual(finalize(out/'fulltext-manifest.json',stage,'skipped',0,state,''),'NO_DELTA')

    def test_exclusions_threshold_withdrawals_and_failed_retry(self):
        from bblove_fulltext_delta import delta_manifest
        self.assertEqual(delta_manifest(new_manifest(DATE),[],[],4,3)['status'],'READY')
        self.assertEqual(delta_manifest(new_manifest(DATE),[],[],4,4)['status'],'HALTED')
        phone='010'+'-'+'1234'+'-'+'5678'
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);state,calls,out,stage=self.run_collection(root,{'1':phone,'2':None})
            m=json.loads((out/'fulltext-manifest.json').read_text())
            self.assertEqual(m['documents']['1']['status'],'EXCLUDED');self.assertEqual(m['documents']['2']['status'],'FAILED')
            self.assertFalse((stage/'prec/1.json').exists())
            for sid in ('3','4','5'):publish_metadata(m,sid)
            m['documents']['3']['checked_date']='20200101';save(out/'fulltext-manifest.json',m)
            state,calls,out,stage=self.run_collection(root,{'3':phone})
            self.assertEqual(calls,['2','3'])
            d=verify_delta(stage);self.assertEqual(d['withdrawn'][0]['id'],'3')
            self.assertNotIn(phone,json.dumps(d))

    def test_v1_reset_deadline_and_checkpoint_batch(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);out=root/'cache';save(out/'fulltext-manifest.json',{'format_version':1})
            state,calls,_,_=self.run_collection(root,deadline=10)
            self.assertEqual(state,'PAUSED');self.assertEqual(calls,[])
            self.assertEqual(json.loads((out/'fulltext-manifest.json').read_text())['format_version'],2)
            with patch('bblove_precedent_collect.save',wraps=save) as saving:
                self.run_collection(root)
                checkpoint_calls=[c for c in saving.call_args_list if str(c.args[0]).endswith('/fulltext-manifest.json')]
                self.assertEqual(len(checkpoint_calls),1)
