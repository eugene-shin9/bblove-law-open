"""Offline carry receipts, preservation across collection, and public asset compatibility."""
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from test_current_edition import Recording, age_receipts, manifest
from test_full_law_collection import DATE, ROOT, ROW, encode
import bblove_addenda_assets
import bblove_law_all
from bblove_tax_p2_collect import PilotBlocked
from build_mcp_assets_official import build, group_id
from release_info import build as release_info
from verify_mcp_assets_official import verify


def unit(number, body, date='20250101'):
    return {'부칙공포일자': date, '부칙공포번호': number, '부칙내용': body}


A = unit('00001', ['첫째 부칙', {'문장': '원문을 유지한다.'}])
B = unit('00002', '둘째 부칙')
C = unit('00003', '셋째 부칙')


def set_units(doc, units):
    doc['법령']['부칙'] = {'부칙단위': units}


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def file_hashes(path):
    return {str(p.relative_to(path)): file_hash(p) for p in path.rglob('*') if p.is_file()}


class CarryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.source = self.root / 'raw'

    @property
    def entry(self):
        return manifest(self.source)['documents'][0]

    @property
    def raw(self):
        return self.source / self.entry['path']

    @property
    def sidecar(self):
        return self.raw.with_suffix('.000001.carry.json')

    def refetch(self, units, *, date=DATE, force=True, row=None, edit=None, complete=True):
        if force and self.source.exists():
            age_receipts(self.source, 7)

        def alter(doc):
            set_units(doc, units)
            if edit:
                edit(doc)

        client = Recording(rows=[row] if row else None, alter=alter)
        self.assertEqual(bblove_law_all.collect(client, self.source, date), complete)
        return client.details

    def carried(self):
        self.assertTrue(self.sidecar.is_file())
        value = json.loads(self.sidecar.read_bytes())
        self.assertEqual((value['schema'], value['id']), (1, ROW['법령ID']))
        self.assertEqual(self.entry['carried_addenda'], len(value['units']))
        self.assertEqual(self.entry['carried_addenda_sha256'], file_hash(self.sidecar))
        return value['units']

    def test_two_disappear_then_return_and_remove_sidecar(self):
        self.refetch([A, B, C])
        self.refetch([C])
        rows = self.carried()
        self.assertEqual([item['unit'] for item in rows], [A, B])
        self.assertEqual(self.entry['dropped_addenda'], ['20250101/00001', '20250101/00002'])
        self.assertEqual({item['first_missing'] for item in rows}, {DATE})
        self.assertEqual(rows[0]['from_collected_at'], rows[1]['from_collected_at'])
        self.assertGreaterEqual(bblove_law_all.now() - bblove_law_all.datetime.fromisoformat(
            rows[0]['from_collected_at']), timedelta(days=7))

        self.refetch([A, C])
        self.assertEqual(self.carried(), rows[1:])
        self.refetch([A, B, C])
        self.assertFalse(self.sidecar.exists())
        self.assertNotIn('carried_addenda', self.entry)
        self.assertNotIn('carried_addenda_sha256', self.entry)

    def test_carry_provenance_survives_three_refetches(self):
        self.refetch([A, B, C])
        self.refetch([B, C])
        first = self.carried()[0]
        self.refetch([C], date='20261015')
        self.refetch([], date='20261016')
        self.assertEqual(self.carried()[0], first)
        self.assertEqual([u['first_missing'] for u in self.carried()], [DATE, '20261015', '20261016'])

    def test_counter_preserves_missing_same_key_occurrence_and_ignores_relabel(self):
        same_key = unit('00002', '같은 번호의 다른 부칙')
        relabeled = unit('00009', '첫째부칙 원문을유지한다.', '20250201')
        self.refetch([A, same_key, B])
        self.refetch([B, relabeled])
        self.assertEqual(self.entry['dropped_addenda'], ['20250101/00001', '20250101/00002'])
        self.assertEqual([u['unit'] for u in self.carried()], [same_key])
        self.refetch([B, relabeled])
        self.assertEqual([u['unit'] for u in self.carried()], [same_key])
        self.refetch([same_key, B, relabeled])
        self.assertFalse(self.sidecar.exists())

    def test_same_key_partial_return_prefers_the_returning_body(self):
        same_key = unit('00002', '같은 번호의 다른 부칙')
        self.refetch([same_key, B])
        self.refetch([])
        self.assertEqual([item['unit'] for item in self.carried()], [same_key, B])
        self.refetch([B])
        self.assertEqual([item['unit'] for item in self.carried()], [same_key])

    def test_prior_carry_removed_when_text_reappears_under_another_key(self):
        self.refetch([A])
        self.refetch([])
        self.carried()
        self.refetch([unit('00019', '첫째 부칙\n원문을 유지한다.')])
        self.assertFalse(self.sidecar.exists())

    def test_reuse_failed_and_discard_preserve_carry(self):
        self.refetch([A])
        self.refetch([])
        receipt = self.carried()
        digest = file_hash(self.sidecar)
        self.assertEqual(self.refetch([A], force=False), 0)
        self.assertEqual(file_hash(self.sidecar), digest)
        self.refetch([A], edit=lambda doc: doc['법령'].pop('조문'))
        self.assertEqual(self.entry['reason'], 'DETAIL_VALIDATION_FAILED')
        self.assertEqual(self.carried(), receipt)
        self.assertEqual(file_hash(self.sidecar), digest)
        raw = self.raw
        self.refetch([], edit=lambda doc: doc['법령']['기본정보'].update(법령ID='000777'))
        self.assertEqual(self.entry['reason'], 'CURRENT_EDITION_MISMATCH')
        self.assertFalse(raw.exists())
        self.assertEqual(self.carried(), receipt)
        self.assertEqual(file_hash(self.sidecar), digest)

        class Offline(Recording):
            def get(self, endpoint, params, **kw):
                if endpoint == 'lawService.do':
                    raise PilotBlocked('REQUEST_FAILED_AFTER_RETRIES')
                return super().get(endpoint, params, **kw)

        self.assertTrue(bblove_law_all.collect(Offline(), self.source, DATE))
        self.assertEqual(self.entry['reason'], 'DETAIL_FETCH_FAILED')
        self.assertEqual(file_hash(self.sidecar), digest)

    def test_failed_raw_replace_does_not_discard_a_returning_unit_before_retry(self):
        self.refetch([A, B])
        self.refetch([B])
        before = self.raw.read_bytes()
        original_replace = Path.replace

        def fail_raw_replace(path, target):
            if target == self.raw:
                raise OSError('fixture RAW rename failure')
            return original_replace(path, target)

        with patch.object(Path, 'replace', fail_raw_replace):
            self.refetch([A])
        self.assertEqual(self.entry['reason'], 'RAW_WRITE_FAILED')
        self.assertEqual(self.raw.read_bytes(), before)
        self.assertEqual([item['unit'] for item in self.carried()], [A, B])
        self.refetch([A])
        self.assertEqual([item['unit'] for item in self.carried()], [B])

    def test_other_law_raw_cannot_create_carry(self):
        self.refetch([A])
        other = json.loads(self.raw.read_bytes())
        other['법령']['기본정보']['법령ID'] = '000777'
        self.raw.write_bytes(encode(other))
        self.refetch([])
        self.assertFalse(self.sidecar.exists())
        self.assertNotIn('carried_addenda', self.entry)

    def test_group_rename_retains_sidecar_and_missing_old_raw_units(self):
        self.refetch([A, B])
        self.refetch([B])
        old_sidecar = self.sidecar
        self.refetch([C], row={**ROW, '법령명한글': '새 이름'})
        self.assertNotEqual(old_sidecar, self.sidecar)
        self.assertFalse(old_sidecar.exists())
        self.assertEqual([u['unit'] for u in self.carried()], [A, B])

    def test_backfill_cli_is_offline_idempotent_and_changes_only_carry(self):
        self.refetch([A, A, B])
        old = self.source.rename(self.root / 'old')
        old_entry = manifest(old)['documents'][0]
        self.refetch([B])
        ledger = self.source / 'daily-calls.json'
        ledger.write_text('ledger sentinel')
        before = file_hashes(self.source)
        expected_manifest = manifest(self.source)
        with patch.object(sys, 'argv', ['collector', '--out', str(self.source), '--backfill-carry', str(old)]), \
                patch('bblove_law_all.os.environ', {}), \
                patch('bblove_law_all.Client', side_effect=AssertionError('network is forbidden')), \
                patch('bblove_law_all.DailyCalls', side_effect=AssertionError('ledger is forbidden')):
            self.assertEqual(bblove_law_all.main(), 0)
            first = file_hashes(self.source)
            self.assertEqual(bblove_law_all.main(), 0)
            self.assertEqual(file_hashes(self.source), first)
        rows = self.carried()
        self.assertEqual([item['unit'] for item in rows], [A, A])
        self.assertEqual({item['from_collected_at'] for item in rows}, {old_entry['collected_at']})
        self.assertEqual({item['first_missing'] for item in rows}, {DATE})
        actual = manifest(self.source)
        for field in ('carried_addenda', 'carried_addenda_sha256'):
            actual['documents'][0].pop(field)
        self.assertEqual(expected_manifest, actual)
        self.assertEqual({k for k in before if before[k] != first[k]}, {'manifest.json'})

    def test_carry_does_not_write_credential_marker(self):
        self.refetch([A])
        raw = self.raw
        marked = json.loads(raw.read_bytes())
        set_units(marked, [unit('00001', '합성 ?' + 'OC=fixture_marker')])
        raw.write_bytes(encode(marked))
        before = raw.read_bytes()
        self.refetch([], complete=False)
        self.assertEqual(self.entry['reason'], 'ADDENDA_CARRY_WRITE_FAILED')
        self.assertEqual(self.entry['detail'], 'ADDENDA_CARRY_AUTH_MARKER')
        self.assertFalse(self.sidecar.exists())
        self.assertFalse(self.sidecar.with_suffix('.json.tmp').exists())
        self.assertEqual(raw.read_bytes(), before)

    def test_public_assets_carry_rows_provenance_and_corrupt_hash(self):
        self.refetch([A, B])
        self.refetch([B])
        self.carried()
        out = self.root / 'assets'
        report = build(self.source, ROOT, out, DATE, enforce_thresholds=False)
        group = self.entry['group']
        meta = json.loads((out / 'meta.json').read_bytes())
        self.assertEqual(meta['addenda_carried'], [[group, ROW['법령ID'], '2025-01-01', '00001']])
        rows = json.loads((out / 'addenda' / f'{group_id(group)}.json').read_bytes())[0]['u']
        self.assertEqual(rows, [['2025-01-01', '00001', '첫째 부칙\n원문을 유지한다.'],
                                ['2025-01-01', '00002', '둘째 부칙']])
        self.assertEqual(report['addenda']['n_addenda'], 2)
        self.assertEqual(verify(out, DATE, enforce_thresholds=False)['n_addenda'], 2)

        release = self.root / 'release'
        release.mkdir()
        archive = release / 'official-all.zip'
        with zipfile.ZipFile(archive, 'w') as payload:
            for path in self.source.rglob('*.json'):
                payload.write(path, path.relative_to(self.root))
            self.assertIn(str(self.sidecar.relative_to(self.root)), payload.namelist())
        info = release_info(release, self.source / 'manifest.json', 'official-law-raw')
        self.assertEqual(info['files'][archive.name]['sha256'], file_hash(archive))

        with self.sidecar.open('ab') as stream:
            stream.write(b' ')
        report = build(self.source, ROOT, out, DATE, enforce_thresholds=False)
        self.assertEqual(report['addenda']['n_addenda'], 1)
        self.assertNotIn('addenda_carried', json.loads((out / 'meta.json').read_bytes()))
        self.assertEqual([s['reason'] for s in report['addenda']['skipped_documents']],
                         ['ADDENDA_CARRY_HASH_MISMATCH'])

    def test_asset_raw_key_wins_and_invalid_carry_does_not_block_other_units(self):
        self.refetch([A, B, unit('00004', '', '20251101'), unit('00005', '본문', '20250230')])
        self.refetch([B])
        self.carried()
        sidecar = json.loads(self.sidecar.read_bytes())
        sidecar['units'].append({'key': '20250101/00002', 'unit': unit('00002', '겹치는 곁파일')})
        self.sidecar.write_bytes(encode(sidecar))
        m = manifest(self.source)
        m['documents'][0].update(carried_addenda=4, carried_addenda_sha256=file_hash(self.sidecar))
        (self.source / 'manifest.json').write_bytes(encode(m))
        report = build(self.source, ROOT, self.root / 'assets', DATE, enforce_thresholds=False)
        self.assertEqual(report['addenda']['n_addenda'], 2)
        self.assertEqual([(s['index'], s['reason']) for s in report['addenda']['skipped_documents']],
                         [(1, 'ADDENDA_EMPTY'), (2, 'addenda: impossible calendar date')])

    def test_shared_extract_keeps_legacy_signature_and_validates_carried_units_individually(self):
        doc = {}
        self.assertEqual(bblove_addenda_assets.extract(doc, DATE), [])
        doc = {'법령': {}}
        set_units(doc, [B, A])
        rows = bblove_addenda_assets.extract(doc, DATE)
        self.assertEqual(rows, [['2025-01-01', '00002', '둘째 부칙'],
                                ['2025-01-01', '00001', '첫째 부칙\n원문을 유지한다.']])
        sidecar = {'schema': 1, 'units': [
            {'key': '20250101/00002', 'unit': B},
            {'key': '20250101/invalid', 'unit': unit('invalid', '부칙')},
            {'key': '20250101/00001', 'unit': []},
            {'key': '20250101/00001', 'unit': A},
        ]}
        self.assertEqual(bblove_addenda_assets.extract_carried(sidecar, DATE), (rows, [
            {'index': 1, 'reason': 'ADDENDA_NUMBER_INVALID'},
            {'index': 2, 'reason': 'ADDENDA_SHAPE_INVALID'},
        ]))

    def test_carry_free_assets_keep_baseline_bytes(self):
        self.refetch([B, A])
        out = self.root / 'assets'
        build(self.source, ROOT, out, DATE, enforce_thresholds=False)
        self.assertEqual(file_hashes(out), BASELINE_ASSETS)


# Captured with the same offline fixture on 92afd5a, including RAW source order.
BASELINE_ASSETS = {
    'meta.json': 'b7865b2654adb9d7334ccb456ddde40ddcee7f67829ad9e14c1b9c3e611ae16c',
    'addenda/aad141402551.json': 'fb668990e3b3d0cc3c1688d2320d1d8037d3c150792086317abb84ccd81e2d35',
    'groups/aad141402551.json': '621c088b40aebae4336318b2c9926356aed92633ebda0f9eedd177a07675f010',
}


if __name__ == '__main__':
    unittest.main()
