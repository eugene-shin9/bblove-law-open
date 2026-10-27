"""Synthetic fixtures only: never proof of live corpus counts or original accuracy."""
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'scripts'))
from bblove_law_all import Client, catalog, collect, grouping, save, supported
from bblove_tax_p2_collect import PilotBlocked
from build_mcp_assets_official import articles, build, encode, load_groups, group_id, GROUP_LIMIT, META_LIMIT
from verify_mcp_assets_official import verify

DATE = '20261014'
ROW = {'법령ID': '000001', '법령일련번호': '123456', '법령명한글': '테스트법',
       '시행일자': '20260101', '공포일자': '20250101', '법령구분명': '법률', '현행연혁코드': '현행'}
DOC = {'법령': {'기본정보': {'법령ID': '000001', '법령명_한글': '테스트법',
                           '시행일자': '20260101', '공포일자': '20250101'},
               '조문': {'조문단위': [
                   {'조문여부': 'N', '조문번호': '1', '조문내용': '제1장'},
                   {'조문여부': 'Y', '조문번호': '1', '조문가지번호': '2', '조문제목': '목적',
                    '조문내용': '제1조의2(목적) 원문', '항': {'항내용': '① 항 원문',
                    '호': {'호내용': '1. 호 원문', '목': {'목내용': '가. 목 원문'}}}}]}}}


class FakeClient:
    def __init__(self, rows=None, doc=None):
        self.rows = rows if rows is not None else [deepcopy(ROW)]
        self.doc = doc if doc is not None else deepcopy(DOC)
        self.calls = 0
        self.details = 0

    def get(self, endpoint, params, **kw):
        self.calls += 1
        if endpoint == 'lawSearch.do':
            return {'LawSearch': {'totalCnt': len(self.rows), 'law': self.rows}}, None
        self.details += 1
        return self.doc, encode(self.doc)


class FullLawTests(unittest.TestCase):
    def test_raw_resume_and_hash_corruption_refetch(self):
        with tempfile.TemporaryDirectory() as t:
            path = Path(t)
            first = FakeClient()
            self.assertTrue(collect(first, path, DATE))
            raw_path = path / '테스트법/법률.json'
            self.assertEqual(raw_path.read_bytes(), encode(DOC))
            second = FakeClient()
            self.assertTrue(collect(second, path, DATE))
            self.assertEqual(second.details, 0)
            raw_path.write_bytes(b'{}')
            third = FakeClient()
            self.assertTrue(collect(third, path, DATE))
            self.assertEqual(third.details, 1)
            changed = FakeClient(rows=[{**ROW, '법령일련번호': '123457'}])
            self.assertTrue(collect(changed, path, DATE))
            self.assertEqual(changed.details, 1)

    def test_failure_is_resumable_and_cannot_build(self):
        bad = deepcopy(DOC)
        bad['법령']['기본정보']['법령ID'] = '999999'
        with tempfile.TemporaryDirectory() as t:
            source = Path(t) / 'raw'
            self.assertTrue(collect(FakeClient(doc=bad), source, DATE))
            manifest = json.loads((source / 'manifest.json').read_bytes())
            self.assertEqual(manifest['documents'][0]['status'], 'FAILED')
            with self.assertRaises(ValueError):
                load_groups(source, DATE)
            retry = FakeClient()
            self.assertTrue(collect(retry, source, DATE))
            self.assertEqual(retry.details, 0)
            self.assertEqual(json.loads((source / 'manifest.json').read_bytes())['documents'][0]['status'], 'FAILED')

    def test_future_catalog_and_duplicate_rejected(self):
        for rows in ([{**ROW, '시행일자': '20270101'}], [ROW, ROW]):
            with self.assertRaises(ValueError):
                catalog(FakeClient(rows=rows), DATE)

    def test_failed_catalog_preserves_previous_manifest(self):
        with tempfile.TemporaryDirectory() as t:
            source = Path(t)
            self.assertTrue(collect(FakeClient(), source, DATE))
            previous = (source / 'manifest.json').read_bytes()
            self.assertFalse(collect(FakeClient(rows=[]), source, DATE))
            self.assertEqual(previous, (source / 'manifest.json').read_bytes())
            self.assertTrue((source / 'attempt.json').exists())

    def test_grouping_parent_fallback_orphan_and_collision(self):
        rows = [ROW,
                {**ROW, '법령ID': '000002', '법령명한글': '다른 이름', '법령구분명': '대통령령', '상위법령ID': '000001'},
                {**ROW, '법령ID': '000003', '법령명한글': '테스트법 시행규칙', '법령구분명': '기획재정부령'},
                {**ROW, '법령ID': '000004', '법령명한글': '독립령', '법령구분명': '대통령령'},
                {**ROW, '법령ID': '000005', '법령명한글': '테스트법 시행규칙', '법령구분명': '총리령'}]
        result = grouping(rows)
        self.assertEqual(result['000002'][0], '테스트법')
        self.assertEqual(result['000003'][0], '테스트법')
        self.assertEqual(result['000004'][0], '독립령')
        self.assertEqual(len({r[2] for r in result.values()}), 5)
        self.assertFalse(supported({'법령구분명': '훈령'}))
        self.assertFalse(supported({'법령구분명': '규칙'}))

    def test_article_markdown_and_heading_exclusion(self):
        result = articles(DOC)
        self.assertEqual(result, [['제1조의2', '목적',
            '제1조의2(목적) 원문\n**①** 항 원문\n  1\\. 호 원문\n    가. 목 원문']])

    def test_group_file_byte_limits_and_staged_failure(self):
        with tempfile.TemporaryDirectory() as t:
            source, out = Path(t) / 'raw', Path(t) / 'assets'
            self.assertTrue(collect(FakeClient(), source, DATE))
            build(source, ROOT, out, DATE, enforce_thresholds=False)
            path = out / 'groups' / (group_id('테스트법') + '.json')
            original = path.read_bytes()
            with patch('build_mcp_assets_official.GROUP_LIMIT', len(original)):
                build(source, ROOT, out, DATE, enforce_thresholds=False)
            with patch('build_mcp_assets_official.GROUP_LIMIT', len(original) - 1):
                with self.assertRaisesRegex(ValueError, 'GROUP_FILE_SIZE_INVALID'):
                    build(source, ROOT, out, DATE, enforce_thresholds=False)
            self.assertEqual(path.read_bytes(), original)
            path.write_bytes(original + b' ' * (GROUP_LIMIT - len(original)))
            verify(out, DATE, enforce_thresholds=False)
            with path.open('ab') as file:
                file.write(b' ')
            with self.assertRaisesRegex(ValueError, 'GROUP_FILE_MISSING_OR_SIZE_INVALID'):
                verify(out, DATE, enforce_thresholds=False)

    def test_meta_limit_in_builder_and_verifier(self):
        with tempfile.TemporaryDirectory() as t:
            source, out = Path(t) / 'raw', Path(t) / 'assets'
            self.assertTrue(collect(FakeClient(), source, DATE))
            build(source, ROOT, out, DATE, enforce_thresholds=False)
            path = out / 'meta.json'
            original = path.read_bytes()
            with patch('build_mcp_assets_official.META_LIMIT', len(original)):
                build(source, ROOT, out, DATE, enforce_thresholds=False)
            with patch('build_mcp_assets_official.META_LIMIT', len(original) - 1):
                with self.assertRaisesRegex(ValueError, 'META_SIZE_LIMIT_EXCEEDED'):
                    build(source, ROOT, out, DATE, enforce_thresholds=False)
            self.assertEqual(path.read_bytes(), original)
            path.write_bytes(original + b' ' * (META_LIMIT - len(original)))
            verify(out, DATE, enforce_thresholds=False)
            with path.open('ab') as file:
                file.write(b' ')
            with self.assertRaisesRegex(ValueError, 'META_SIZE_LIMIT_EXCEEDED'):
                verify(out, DATE, enforce_thresholds=False)

    def test_missing_empty_extra_and_mismatched_group_files(self):
        for case in ('missing', 'empty', 'empty_array', 'wrapped', 'extra', 'metadata', 'id', 'titles', 'count', 'shards'):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as t:
                source, out = Path(t) / 'raw', Path(t) / 'assets'
                self.assertTrue(collect(FakeClient(), source, DATE))
                build(source, ROOT, out, DATE, enforce_thresholds=False)
                path = out / 'groups' / (group_id('테스트법') + '.json')
                meta = json.loads((out / 'meta.json').read_bytes())
                if case == 'missing': path.unlink()
                elif case == 'empty': path.write_bytes(b'')
                elif case == 'empty_array': path.write_bytes(b'[]')
                elif case == 'wrapped': path.write_bytes(encode({'테스트법': json.loads(path.read_bytes())}))
                elif case == 'extra': (out / 'groups/extra.json').write_bytes(b'[]')
                elif case == 'metadata': meta['groups']['테스트법'][1] = '다른법'
                elif case == 'id': meta['groups']['테스트법'][0] = '../outside'
                elif case == 'titles': meta['titles'] = []
                elif case == 'count': meta['n_docs'] += 1
                elif case == 'shards': meta['shards'] = [{'file': '000.json'}]
                (out / 'meta.json').write_bytes(encode(meta))
                with self.assertRaises(ValueError):
                    verify(out, DATE, enforce_thresholds=False)

    def test_one_file_per_group_and_id_collision_rejection(self):
        with tempfile.TemporaryDirectory() as t:
            source, out = Path(t) / 'raw', Path(t) / 'assets'
            self.assertTrue(collect(FakeClient(), source, DATE))
            manifest, groups = load_groups(source, DATE)
            second = deepcopy(groups['테스트법'])
            second[0].update(id='000002', t='다른법')
            groups['다른법'] = second
            with patch('build_mcp_assets_official.load_groups', return_value=(manifest, groups)):
                result = build(source, ROOT, out, DATE, enforce_thresholds=False)
                self.assertEqual(result['group_files'], 2)
                self.assertEqual(verify(out, DATE, enforce_thresholds=False)['n_docs'], 2)
                self.assertEqual({p.name for p in (out / 'groups').iterdir()},
                                 {hashlib.sha1(g.encode('utf-8')).hexdigest()[:12] + '.json' for g in groups})
                with patch('build_mcp_assets_official.group_id', return_value='a' * 12):
                    with self.assertRaisesRegex(ValueError, 'GROUP_ID_COLLISION'):
                        build(source, ROOT, out, DATE, enforce_thresholds=False)

    def test_build_contract_release_verifier_and_tamper(self):
        with tempfile.TemporaryDirectory() as t:
            source, out = Path(t) / 'raw', Path(t) / 'assets'
            self.assertTrue(collect(FakeClient(), source, DATE))
            stats = build(source, ROOT, out, DATE, enforce_thresholds=False)
            self.assertEqual(stats['n_docs'], 1)
            self.assertEqual(verify(out, DATE, enforce_thresholds=False)['n_articles'], 1)
            meta = json.loads((out / 'meta.json').read_bytes())
            self.assertEqual(meta['groups']['테스트법'], [hashlib.sha1('테스트법'.encode('utf-8')).hexdigest()[:12], '테스트법', '2026-01-01', 0])
            with self.assertRaises(ValueError):
                build(source, ROOT, out, DATE)
            self.assertEqual(meta['shards'], [])
            self.assertFalse((out / 'shards').exists())
            path = out / 'groups' / (group_id('테스트법') + '.json')
            values = json.loads(path.read_bytes())
            self.assertIsInstance(values, list)
            values[0]['eff'] = '2099-01-01'
            path.write_bytes(encode(values))
            with self.assertRaises(ValueError):
                verify(out, DATE, enforce_thresholds=False)

    def test_empty_articles_fail_and_future_detail_date_is_flagged(self):
        empty = deepcopy(DOC); empty['법령']['조문']['조문단위'] = []
        with tempfile.TemporaryDirectory() as t:
            self.assertTrue(collect(FakeClient(doc=empty), Path(t), DATE))
            manifest = json.loads((Path(t) / 'manifest.json').read_bytes())
            self.assertEqual(manifest['documents'][0]['status'], 'FAILED')
        # The catalog (현행) already says this MST is current; a later detail date
        # means provisions with deferred effect. Keep it, but flag it.
        future = deepcopy(DOC); future['법령']['기본정보']['시행일자'] = '20270101'
        with tempfile.TemporaryDirectory() as t:
            self.assertTrue(collect(FakeClient(doc=future), Path(t), DATE))
            entry = json.loads((Path(t) / 'manifest.json').read_bytes())['documents'][0]
            self.assertEqual(entry['status'], 'SUCCESS')
            self.assertEqual(entry['warnings'][0]['code'], 'detail_date_after_today')

    def test_retry_spacing_and_secret_response_rejection(self):
        attempts = []
        def transport(req, timeout):
            attempts.append(1)
            response = io.BytesIO(b'{"echo":"synthetic-credential"}')
            response.status = 200
            response.headers = {'Content-Type': 'application/json'}
            return response
        with patch('bblove_law_all.time.sleep') as sleep:
            client = Client('synthetic-credential', transport=transport)
            with self.assertRaises(PilotBlocked) as error:
                client.get('lawService.do', {'target': 'law', 'MST': '123456'}, original=True)
            self.assertNotIn('synthetic-credential', str(error.exception))
            self.assertEqual(client.calls, 4)
            self.assertEqual(len(attempts), 4)
            self.assertTrue(any(c.args == (4,) for c in sleep.call_args_list))



    def test_deadline_stops_before_request(self):
        client = Client('synthetic-credential', seconds=1)
        with self.assertRaises(PilotBlocked):
            client.get('lawSearch.do', {'target': 'law'})
        self.assertEqual(client.calls, 0)


if __name__ == '__main__':
    unittest.main()
