"""Synthetic delta publication contract; commands are mocked."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path[:0]=[str(Path(__file__).parents[1]/'scripts'),str(Path(__file__).parents[1]/'src')]
from package_precedents import package,release_tag
from publish_precedents import publish
from collect_precedents import published_targets
from bblove_fulltext_delta import new_manifest,delta_manifest,committed_snapshot,KIND
from bblove_law_all import save

class FulltextReleaseTests(unittest.TestCase):
    def test_package_contract_and_retry_unique(self):
        with tempfile.TemporaryDirectory() as t:
            source=Path(t)/'fulltext-delta';m=new_manifest('20261103');d=delta_manifest(m,[],[],0,0)
            tag=release_tag(KIND,m['date'],run_id='9',run_attempt='1')
            save(source/'fulltext-delta-manifest.json',d);save(source/'fulltext-manifest.json',committed_snapshot(m,d,tag))
            dest=Path(t)/'release';package(source,dest,KIND,part_bytes=100)
            info=json.loads((dest/'release-info.json').read_text())
            self.assertEqual((info['format_version'],info['delta'],info['sequence']),(2,True,1))
            self.assertTrue(all(p.stat().st_size<=100 for p in dest.glob('*.part-*')))
            self.assertTrue((dest/'fulltext-manifest.json').exists());self.assertTrue((dest/'SHA256SUMS').exists())
            calls=[];publish(dest,tag,'synthetic','abc',execute=lambda args,**kw:calls.append(args))
            self.assertEqual([c[2] for c in calls],['create','upload','edit'])
            self.assertNotEqual(tag,release_tag(KIND,m['date'],run_id='9',run_attempt='2'))
            self.assertEqual(published_targets([dict(tagName=tag,isDraft=False)]),set())
            m=committed_snapshot(m,d,tag);second=delta_manifest(m,[],[],0,0)
            self.assertEqual((second['sequence'],second['previous_tag']),(2,tag))
            for bad in ('prec-20261103','prec-fulltext-20261103'):
                with self.assertRaises(ValueError):publish(dest,bad,'synthetic','abc',execute=lambda *a,**k:self.fail('publish'))

    def test_two_deltas_merge_by_sequence_and_body_hash_idempotently(self):
        from bblove_precedent_collect import store_fulltext
        from test_precedent_fulltext import value
        with tempfile.TemporaryDirectory() as t:
            m=new_manifest('20261103');merged={};history=[]
            for number,body in enumerate(('first synthetic','changed synthetic'),1):
                source=Path(t)/str(number)/'fulltext-delta';v=value(body)
                store_fulltext(source,v,m)
                d=delta_manifest(m,[v['id']],[],1,0)
                tag=release_tag(KIND,m['date'],run_id=str(number),run_attempt='1')
                m=committed_snapshot(m,d,tag)
                save(source/'fulltext-delta-manifest.json',d);save(source/'fulltext-manifest.json',m)
                package(source,Path(t)/f'release-{number}',KIND)
                history.append(d)
            self.assertEqual(history[1]['previous_tag'],'prec-fulltext-20261103-r1-a1')
            sid=v['id'];self.assertEqual(history[1]['documents'][sid]['prev_body_sha256'],history[0]['documents'][sid]['body_sha256'])
            seen=set()
            for d in sorted(history+history,key=lambda x:x['sequence']):
                for sid,meta in d['documents'].items():
                    key=(sid,meta['body_sha256'])
                    if key not in seen: merged[sid]=meta['body_sha256'];seen.add(key)
            self.assertEqual(merged[sid],v['body_sha256']);self.assertEqual(len(seen),2)
