"""Offline release contracts with synthetic objects."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).parents[1]/'scripts'))
from package_precedents import package, release_tag
from collect_precedents import published_targets
from publish_precedents import publish

class FulltextReleaseTests(unittest.TestCase):
    def test_package(self):
        with tempfile.TemporaryDirectory() as t:
            source=Path(t)/'fulltext-collection';source.mkdir()
            (source/'fulltext-manifest.json').write_text(json.dumps(dict(kind='precedent-fulltext-raw',format_version=1,date='20261103',status='PAUSED',documents={})))
            dest=Path(t)/'release'
            package(source,dest,'precedent-fulltext-raw',part_bytes=100)
            info=json.loads((dest/'release-info.json').read_text())
            self.assertEqual(info['kind'],'precedent-fulltext-raw')
            self.assertEqual(info['format_version'],1)
            parts=list(dest.glob('*.part-*'))
            self.assertTrue(parts)
            self.assertTrue(all(p.stat().st_size<=100 for p in parts))
            self.assertEqual(release_tag(info['kind'],'20261103',True),'prec-fulltext-20261103-partial')
            self.assertTrue((dest/'SHA256SUMS').is_file())
            calls=[]
            publish(dest,'prec-fulltext-20261103-partial','synthetic','abc',execute=lambda args, **kw: calls.append(args))
            self.assertEqual([a[2] for a in calls],['create','upload','edit'])
            self.assertIn('--draft',calls[0])
            self.assertIn('--draft=false',calls[-1])
            with self.assertRaises(ValueError):
                publish(dest,'prec-20261103','synthetic','abc',execute=lambda *a, **k: self.fail('must not publish'))
    def test_summary_coverage_ignores_fulltext(self):
        self.assertEqual(published_targets([dict(tagName='prec-fulltext-20261103',isDraft=False)]),set())
