"""Offline coverage of current-law request admissions and resume scheduling."""
from copy import deepcopy
from datetime import datetime, timedelta
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
from urllib.parse import parse_qs, urlsplit
from zipfile import ZipFile

from test_collection_finish import response
from test_full_law_collection import DATE, DOC, ROW, ROOT

import bblove_law_all as law
from build_mcp_assets_official import build, encode
from release_info import build as release_info
from verify_mcp_assets_official import verify

MONDAY = datetime.fromisoformat('2026-11-23T01:20:00+09:00')


class TestDailyCalls(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.out = Path(work.name) / 'raw'
        self.out.mkdir()
        self.ledger = self.out / 'daily-calls.json'
        self.output = Path(work.name) / 'outputs'
        self.clock = MONDAY
        self.rows = [dict(ROW, 법령ID=f'{i:06d}', 법령명한글=f'테스트법{i}',
                          법령일련번호=str(200000 + i)) for i in range(1, 5)]
        self.calls = []

    def transport(self, request, timeout):
        query = parse_qs(urlsplit(request.full_url).query)
        target = query['target'][0]
        self.calls.append((target, query.get('MST', [''])[0]))
        if target == 'law':
            return response(encode({'LawSearch': {'totalCnt': len(self.rows), 'law': self.rows}}))
        row = next(r for r in self.rows if r['법령일련번호'] == query['MST'][0])
        doc = deepcopy(DOC)
        doc['법령']['기본정보'].update(법령ID=row['법령ID'], 법령명_한글=row['법령명한글'],
                                     법령일련번호=row['법령일련번호'])
        return response(encode(doc))

    def read(self, name):
        return json.loads((self.out / name).read_bytes())

    def known_pages(self, list_count=1):
        law.save(self.out / 'manifest.json', {'list_count': list_count})

    def run_collection(self, cap=None, *, transport=None):
        client_factory = law.Client
        env = {'LAW_OC': 'synthetic-credential', 'GITHUB_OUTPUT': str(self.output)}
        if cap is not None:
            env['LAW_ALL_DAILY_CALLS'] = str(cap)
        with (patch.dict('os.environ', env, clear=True),
              patch.object(law, 'now', side_effect=lambda: self.clock),
              patch.object(law, 'today', side_effect=lambda: self.clock.strftime('%Y%m%d')),
              patch.object(law, 'Client', side_effect=lambda *args, **kw: client_factory(
                  *args, transport=transport or self.transport, **kw)),
              patch.object(sys, 'argv', ['collector', '--out', str(self.out), '--partial-ok'])):
            return law.main()

    def test_cap_and_next_day_resume(self):
        self.known_pages()
        self.assertEqual(self.run_collection(3), 0)
        self.assertEqual(len(self.calls), 3)
        first = self.read('manifest.json')
        self.assertEqual(first['status'], 'INCOMPLETE')
        self.assertIn('DAILY_CALL_CAP', {d.get('reason') for d in first['documents']})
        self.assertFalse(law.collection_complete([{'status': 'FAILED', 'reason': 'DAILY_CALL_CAP'}]))
        self.assertIn('complete=false', self.output.read_text())
        first_msts = {mst for target, mst in self.calls if target == 'eflaw'}
        self.assertEqual(self.run_collection(3), 0)
        self.assertEqual(len(self.calls), 3)
        self.clock += timedelta(days=1)
        self.calls.clear()
        self.assertEqual(self.run_collection(3), 0)
        self.assertEqual(len(self.calls), 3)
        self.assertTrue(first_msts.isdisjoint(mst for _, mst in self.calls))
        self.assertEqual(self.read('manifest.json')['status'], 'COMPLETE')
        self.assertIn('complete=true', self.output.read_text())

        self.clock = MONDAY + timedelta(days=7)
        self.calls.clear()
        self.assertEqual(self.run_collection(3), 0)
        self.assertEqual({mst for target, mst in self.calls if target == 'eflaw'}, first_msts)
        self.assertEqual(self.read('manifest.json')['status'], 'COMPLETE')

    def test_same_day_uses_only_remaining_calls(self):
        self.known_pages()
        self.rows = self.rows[:1]
        self.assertEqual(self.run_collection(4), 0)
        self.assertEqual(len(self.calls), 2)
        self.rows += [dict(self.rows[0], 법령ID='000002', 법령명한글='다른법', 법령일련번호='200002'),
                      dict(self.rows[0], 법령ID='000003', 법령명한글='세번째법', 법령일련번호='200003')]
        self.assertEqual(self.run_collection(4), 0)
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(self.run_collection(4), 0)
        self.assertEqual(len(self.calls), 4)

    def test_catalog_retries_are_reserved_even_without_manifest(self):
        self.known_pages()
        attempts = []

        def fail(request, timeout):
            code = (429, 503)[len(attempts)]
            attempts.append(code)
            raise urllib.error.HTTPError('', code, 'FIXTURE', {}, None)

        self.assertEqual(self.run_collection(2, transport=fail), 0)
        self.assertEqual(attempts, [429, 503])
        self.assertEqual(self.read('attempt.json')['detail'], 'DAILY_CALL_CAP')
        self.assertEqual(self.read('daily-calls.json'), {'date': '2026-11-23', 'calls': 2})
        self.assertEqual(self.read('manifest.json'), {'list_count': 1})

    def test_catalog_failure_persists_usage(self):
        self.known_pages()
        self.assertEqual(self.run_collection(2, transport=lambda *_: response(b'{}')), 1)
        self.assertEqual(self.read('daily-calls.json'), {'date': '2026-11-23', 'calls': 1})
        self.assertEqual(self.read('attempt.json')['reason'], 'CATALOG_FAILED')
        self.known_pages(101)
        self.assertEqual(self.run_collection(2), 0)
        self.assertEqual(self.calls, [])

    def test_corrupt_ledger_and_invalid_capacity(self):
        self.known_pages()
        for invalid in (b'{', b'{"date":"2026-11-23","calls":-1}',
                        b'{"date":"2026-11-23","calls":true}',
                        b'{"date":"2026-11-24","calls":1}',
                        b'{"date":"2026-02-31","calls":1}'):
            with self.subTest(invalid=invalid):
                self.ledger.write_bytes(invalid)
                self.assertEqual(self.run_collection(2), 0)
                self.assertEqual(self.calls, [])
        self.ledger.unlink()
        for cap in ('3501', '0', '-1', '1.0', '', ' +2'):
            with self.subTest(cap=cap):
                self.assertEqual(self.run_collection(cap), 1)
                self.assertEqual(self.calls, [])

    def test_interrupted_manifest_save_cannot_lose_sent_calls(self):
        self.known_pages()
        save = law.save

        def interrupted(path, value):
            if path.name == 'manifest.json':
                raise KeyboardInterrupt
            save(path, value)

        with patch.object(law, 'save', side_effect=interrupted), self.assertRaises(KeyboardInterrupt):
            self.run_collection(1)
        self.assertEqual(len(self.calls), 1)
        self.assertGreaterEqual(self.read('daily-calls.json')['calls'], len(self.calls))
        self.assertEqual(self.run_collection(1), 0)
        self.assertEqual(len(self.calls), 1)

    def test_ledger_does_not_break_release_tools(self):
        self.known_pages()
        self.rows = self.rows[:1]
        self.assertEqual(self.run_collection(), 0)
        self.assertEqual(self.read('daily-calls.json')['calls'], 2)
        assets = self.out.parent / 'assets'
        self.assertEqual(build(self.out, ROOT, assets, DATE, enforce_thresholds=False)['n_docs'], 1)
        self.assertEqual(verify(assets, DATE, enforce_thresholds=False)['n_docs'], 1)
        release = self.out.parent / 'release'
        release.mkdir()
        archive = release / 'official-all.zip'
        with ZipFile(archive, 'w') as zip_file:
            for path in self.out.rglob('*'):
                if path.is_file() and path != self.ledger:
                    zip_file.write(path, Path('official-all') / path.relative_to(self.out))
            self.assertNotIn('official-all/daily-calls.json', zip_file.namelist())
            raw_path = self.read('manifest.json')['documents'][0]['path']
            self.assertIn(f'official-all/{raw_path}', zip_file.namelist())
        info = release_info(release, self.out / 'manifest.json', 'official-law-raw')
        self.assertEqual(info['collection_status'], 'COMPLETE')
        self.assertEqual(info['files'][archive.name]['bytes'], archive.stat().st_size)

    def test_resume_schedule_only_skips_a_recent_complete_manifest(self):
        manifest = {'status': 'COMPLETE', 'completed_at': (MONDAY + timedelta(hours=2)).isoformat()}
        tuesday = MONDAY + timedelta(days=1)
        self.assertTrue(law.resume_not_needed(manifest, tuesday))
        self.assertTrue(law.resume_not_needed(manifest, tuesday + timedelta(days=1)))
        self.assertFalse(law.resume_not_needed(manifest, tuesday + timedelta(days=7)))
        self.assertFalse(law.resume_not_needed({**manifest, 'status': 'INCOMPLETE'}, tuesday))
        self.assertFalse(law.resume_not_needed({**manifest, 'completed_at': 'invalid'}, tuesday))

    def test_cap_mid_catalog_preserves_previous_receipts(self):
        self.known_pages()
        previous = self.read('manifest.json')
        self.rows = [dict(ROW, 법령ID=f'{i:06d}') for i in range(1, 101)]

        def first_page(request, timeout):
            self.calls.append('list')
            return response(encode({'LawSearch': {'totalCnt': 101, 'law': self.rows}}))

        self.assertEqual(self.run_collection(1, transport=first_page), 0)
        self.assertEqual(self.calls, ['list'])
        self.assertEqual(self.read('manifest.json'), previous)
        self.assertEqual(self.read('daily-calls.json')['calls'], 1)
        self.assertEqual(self.read('attempt.json')['detail'], 'DAILY_CALL_CAP')
        self.assertIn('daily_cap_stop=1', self.output.read_text())

    def test_delayed_monday_expiry_still_caps_requests(self):
        self.known_pages()
        self.assertEqual(self.run_collection(3), 0)
        self.clock += timedelta(days=1)
        self.assertEqual(self.run_collection(3), 0)
        self.clock = MONDAY + timedelta(days=7, hours=3)
        self.calls.clear()
        self.assertEqual(self.run_collection(3), 0)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.read('manifest.json')['status'], 'INCOMPLETE')

    def test_resume_cli_does_not_need_a_credential(self):
        law.save(self.out / 'manifest.json', {'status': 'COMPLETE', 'completed_at': MONDAY.isoformat()})
        summary = self.out.parent / 'summary'
        with (patch.dict('os.environ', {'GITHUB_OUTPUT': str(self.output),
                                       'GITHUB_STEP_SUMMARY': str(summary)}, clear=True),
              patch.object(law, 'now', return_value=MONDAY + timedelta(days=1)),
              patch.object(law, 'Client', side_effect=AssertionError('unexpected API client')),
              patch.object(sys, 'argv', ['collector', '--out', str(self.out), '--check-resume'])):
            self.assertEqual(law.main(), 0)
        self.assertEqual(self.output.read_text(), 'skip=true\n')
        self.assertEqual(summary.read_text(), '이어받을 것 없음\n')

    def test_runner_loss_and_normal_settlement(self):
        tags = {}
        ref = None

        def api(method, path, payload=None):
            nonlocal ref
            if method == 'GET' and path.startswith('matching-refs/'):
                return [{'ref': f'refs/tags/{law.GitHubDailyCalls.TAG}', 'object': {'sha': ref}}] if ref else []
            if method == 'GET':
                return tags[path.removeprefix('tags/')]
            if path == 'tags':
                sha = str(len(tags) + 1)
                tags[sha] = payload
                return {'sha': sha}
            ref = payload['sha']
            return {'object': {'sha': ref}}

        def new_runner(out):
            remote = law.GitHubDailyCalls('fixture/repo', 'fixture-commit', 'fixture-token')
            remote.api = api
            return law.DailyCalls(out, 3, remote)

        with patch.object(law, 'now', side_effect=lambda: self.clock):
            original = new_runner(self.out)
            original.reserve(consume=True)
            next_runner = new_runner(self.out.parent / 'new-runner')
            with self.assertRaisesRegex(law.PilotBlocked, '^DAILY_CALL_CAP$'):
                next_runner.reserve(consume=True)
            self.assertEqual(next_runner.report(), 3)

            self.clock += timedelta(days=1)
            next_runner.reserve(consume=True)
            self.assertEqual(next_runner.report(), 1)
            next_runner.remote.settle()
            last_runner = new_runner(self.out.parent / 'last-runner')
            last_runner.reserve(consume=True)
            self.assertEqual(last_runner.report(), 2)
