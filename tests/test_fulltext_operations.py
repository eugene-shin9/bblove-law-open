"""Failure isolation and manifest-only workflow cache contracts, all offline."""
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
sys.path[:0]=[str(Path(__file__).parents[1]/'scripts'),str(Path(__file__).parents[1]/'src')]
import collect_precedents as wrapper
from fulltext_release import recover,finalize
from bblove_fulltext_delta import new_manifest
from bblove_law_all import save
from workflow_contract import load_workflow,step_by_id
ROOT=Path(__file__).parents[1]

class FulltextOperationTests(unittest.TestCase):
    def test_summary_outputs_survive_both_optional_failures(self):
        with tempfile.TemporaryDirectory() as t:
            outputs=Path(t)/'outputs'
            with patch.dict(os.environ,{'LAW_OC':'synthetic','GITHUB_OUTPUT':str(outputs),'FULLTEXT_READY':'success'}),patch.object(sys,'argv',['collect','--committee','--fulltext']),patch.object(wrapper,'run',return_value='COMPLETE'),patch.object(wrapper,'summary',return_value={}),patch.object(wrapper,'collect_fulltext',side_effect=ValueError('synthetic')):
                # run_committee catches its own failure, before fulltext runs.
                original=wrapper.run_committee
                def failed_committee(*args,**kwargs):
                    with patch.object(wrapper,'run',side_effect=ValueError('synthetic')):return original(*args,**kwargs)
                with patch.object(wrapper,'run_committee',side_effect=failed_committee):
                    self.assertEqual(wrapper.main(),0)
            result=outputs.read_text()
            self.assertIn('complete=true',result);self.assertIn('committee_outcome=FAILED',result)
            self.assertIn('fulltext_outcome=FAILED',result);self.assertIn('fulltext_delta=0',result)

    def test_reserved_time_and_recovery_failure_skip(self):
        with tempfile.TemporaryDirectory() as t:
            args=SimpleNamespace(fulltext_max_seconds=7200,out=Path(t)/'summary',fulltext_out=Path(t)/'cache')
            stage=Path(t)/'fulltext-delta/fulltext-delta/fulltext-delta-manifest.json'
            save(stage,{'status':'READY','documents':{},'withdrawn':[]})
            with patch.object(wrapper,'collect_fulltext',return_value='COMPLETE') as call:
                self.assertEqual(wrapper.run_fulltext('COMPLETE',args,5000,None,clock=lambda:1000),'COMPLETE')
                self.assertEqual(call.call_args.kwargs['max_seconds'],2200)
                self.assertEqual(call.call_args.args[3],3200)
            with patch.dict(os.environ,{'FULLTEXT_READY':'failure'}),patch.object(wrapper,'collect_fulltext') as call:
                self.assertEqual(wrapper.run_fulltext('COMPLETE',args,20000,None,clock=lambda:0),'SKIPPED');call.assert_not_called()

    def test_no_release_and_failed_attestation_do_not_create_cache(self):
        with tempfile.TemporaryDirectory() as t:
            cache=Path(t)/'fulltext-manifest.json'
            self.assertEqual(recover(cache,lambda args:'[]'),'EMPTY');self.assertFalse(cache.exists())
            def fail(args):
                if args[0]=='api':return json.dumps([[{'tag_name':'prec-fulltext-20261103-r1-a1','draft':False,'prerelease':False}]])
                if args[0]=='attestation':raise ValueError('synthetic rejected attestation')
                return ''
            with self.assertRaises(ValueError):recover(cache,fail)
            self.assertFalse(cache.exists())
            for status in ('FAILED','SKIPPED','HALTED',''):
                self.assertEqual(finalize(cache,Path(t),'success',0,status,''),'FAILED')

    @unittest.skipUnless((ROOT/'.github/workflows/collect-precedents.yml').exists(),'public workflow only')
    def test_fulltext_workflow_fail_closed_and_independent(self):
        wf=load_workflow(ROOT/'.github/workflows/collect-precedents.yml');job=wf['jobs']['collect']
        self.assertEqual(job['timeout-minutes'],355)
        collect=step_by_id(job,'collect');self.assertIn('--fulltext',collect['run'])
        restore=step_by_id(job,'ft_restore');self.assertEqual(restore['with']['path'],'auto/fulltext-collection/fulltext-manifest.json')
        scan=step_by_id(job,'ft_scan');self.assertIn('auto/fulltext-delta',scan['run'])
        for name in ('ft_scan','ft_package','ft_attest','ft_publish','ft_finalize'):
            step=step_by_id(job,name);self.assertIn('always()',step['if']);self.assertNotIn('committee_',step['if'])
        save_step=next(s for s in job['steps'] if s.get('uses','').startswith('actions/cache/save@') and s['with']['key'].startswith('fulltext-v2-'))
        self.assertEqual(save_step['with']['path'],restore['with']['path'])
        self.assertIn("state == 'PUBLISHED'",save_step['if']);self.assertIn("state == 'NO_DELTA'",save_step['if'])
        self.assertNotIn('FAILED',save_step['if'])
        final=step_by_id(job,'ft_finalize');self.assertIn('FULLTEXT_DELTA',final['env']);self.assertIn('PUBLISH_OUTCOME',final['env'])
