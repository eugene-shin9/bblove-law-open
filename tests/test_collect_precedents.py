"""collect_precedents.py: multi-client loop, time budget, outcomes and step outputs."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'tests'))
import collect_precedents as cp
from test_precedent_d1 import FakeClient, DATE, TT_ROW
from bblove_precedent_collect import collect, verify_cache
from bblove_storage import strict_json


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def write_manifest(out, status, docs, catalogs_complete=True, stop_reason=None):
    m = {'date': DATE, 'status': status, 'documents': docs,
         'catalogs': {'prec': {'complete': catalogs_complete, 'total': len(docs)}}}
    if stop_reason:
        m['stop_reason'] = stop_reason
    Path(out, 'manifest.json').write_text(json.dumps(m))


class LoopTests(unittest.TestCase):
    def test_fresh_client_per_slice_until_complete(self):
        clock, created, calls = Clock(), [], []

        def factory(remaining):
            created.append(remaining)
            return object()

        def fake_collect(client, out, date, scope, stop=None):
            calls.append(client)
            clock.now += 3000
            if len(calls) < 3:
                write_manifest(out, 'INCOMPLETE', [{'status': 'PENDING'}], stop_reason='TIME_BUDGET')
            else:
                write_manifest(out, 'COMPLETE', [{'status': 'SUCCESS'}])
            return True

        with tempfile.TemporaryDirectory() as t:
            state = cp.run(t, 'all', 18000, factory, clock=clock, collect_fn=fake_collect, date=DATE)
        self.assertEqual(state, 'COMPLETE')
        self.assertEqual(len(set(map(id, calls))), 3)
        self.assertEqual(created, [18000, 15000, 12000])

    def test_overall_budget_pauses_and_stop_callback_fires(self):
        clock, seen = Clock(), []

        def fake_collect(client, out, date, scope, stop=None):
            clock.now += 5000
            seen.append(stop())
            write_manifest(out, 'INCOMPLETE', [{'status': 'PENDING'}], stop_reason='TIME_BUDGET')
            return True

        with tempfile.TemporaryDirectory() as t:
            state = cp.run(t, 'all', 10000, lambda r: object(), clock=clock, collect_fn=fake_collect, date=DATE)
        self.assertEqual(state, 'PAUSED')
        self.assertEqual(seen, [None, 'TIME_BUDGET'])

    def test_catalog_or_cache_failure_is_failed(self):
        def fake_collect(client, out, date, scope, stop=None):
            Path(out, 'attempt.json').write_text('{"status": "FAILED"}')
            return False

        with tempfile.TemporaryDirectory() as t:
            Path(t, 'attempt.json').write_text('{"old": true}')
            self.assertEqual(cp.run(t, 'all', 18000, lambda r: object(), collect_fn=fake_collect, date=DATE), 'FAILED')

    def test_stale_attempt_from_previous_run_is_ignored(self):
        def fake_collect(client, out, date, scope, stop=None):
            write_manifest(out, 'COMPLETE', [{'status': 'SUCCESS'}])
            return True

        with tempfile.TemporaryDirectory() as t:
            Path(t, 'attempt.json').write_text('{"old": true}')
            self.assertEqual(cp.run(t, 'all', 18000, lambda r: object(), collect_fn=fake_collect, date=DATE), 'COMPLETE')

    def test_pass_done_when_some_documents_failed(self):
        with tempfile.TemporaryDirectory() as t:
            write_manifest(t, 'INCOMPLETE', [{'status': 'SUCCESS'}, {'status': 'FAILED'}])
            self.assertEqual(cp.outcome(t), 'PASS_DONE')
            write_manifest(t, 'INCOMPLETE', [{'status': 'SUCCESS'}], catalogs_complete=False)
            self.assertEqual(cp.outcome(t), 'PAUSED')

    def test_real_collector_resumes_across_clients(self):
        with tempfile.TemporaryDirectory() as t:
            clients = [FakeClient(count=3, budget_at=2), FakeClient(count=3)]
            state = cp.run(t, 'all', 18000, lambda r: clients.pop(0), date=DATE)
            self.assertEqual(state, 'COMPLETE')
            self.assertEqual(clients, [])
            m = json.loads(Path(t, 'manifest.json').read_text())
            self.assertTrue(all(d['status'] == 'SUCCESS' for d in m['documents']))
            for p in Path(t, 'summaries').rglob('*.json'):
                self.assertNotIn(b'FULL_JUDGMENT_MUST_NEVER_PERSIST', p.read_bytes())

    def test_summary_counts_failure_reasons_only_as_codes(self):
        with tempfile.TemporaryDirectory() as out:
            docs = [{'target': 'prec', 'status': 'FAILED', 'reason': 'DETAIL_NOT_FOUND'},
                    {'target': 'prec', 'status': 'FAILED', 'reason': 'DETAIL_NOT_FOUND'},
                    {'target': 'ttSpecialDecc', 'status': 'FAILED', 'reason': 'DATE_IN_FUTURE'},
                    {'target': 'prec', 'status': 'SUCCESS'}]
            Path(out, 'manifest.json').write_text(json.dumps({'date': DATE, 'documents': docs}), encoding='utf-8')
            s = cp.summary(out)
        self.assertEqual(s['failure_reasons'], {'prec:DETAIL_NOT_FOUND': 2, 'ttSpecialDecc:DATE_IN_FUTURE': 1})
        self.assertEqual(s['counts']['prec:SUCCESS'], 1)

    def test_step_outputs(self):
        with tempfile.TemporaryDirectory() as t:
            target = Path(t) / 'out.txt'
            with patch.dict(os.environ, {'GITHUB_OUTPUT': str(target)}):
                cp.write_outputs('PASS_DONE')
                cp.write_outputs('PAUSED')
            self.assertEqual(target.read_text(), 'outcome=PASS_DONE\ncomplete=true\npartial_targets=\noutcome=PAUSED\ncomplete=false\npartial_targets=\n')


if __name__ == '__main__':
    unittest.main()


class CatalogResetTests(unittest.TestCase):
    def test_live_catalog_growth_restarts_that_catalog_once_and_completes(self):
        class Growing(FakeClient):
            def __init__(self):
                super().__init__(count=150)
                self.grew = False

            def get(self, endpoint, params, **kw):
                if endpoint == 'lawSearch.do' and params['target'] == 'prec' and params['page'] == 2 and not self.grew:
                    self.grew = True  # a new decision is published while page 2 is requested
                    self.rows.append({**self.rows[0], '판례일련번호': '151', '사건번호': '2099두151'})
                return super().get(endpoint, params, **kw)

        client = Growing()
        with tempfile.TemporaryDirectory() as t:
            state = cp.run(t, 'all', 18000, lambda r: client, date=DATE)
            self.assertEqual(state, 'COMPLETE')
            m = json.loads(Path(t, 'manifest.json').read_text())
            self.assertEqual(m['catalogs']['prec']['total'], 151)
            self.assertEqual(sum(d['target'] == 'prec' for d in m['documents']), 151)

    def test_no_reset_after_details_were_fetched(self):
        with tempfile.TemporaryDirectory() as t:
            m = {'schema': 'precedent-summary-v1', 'date': DATE, 'pages': [],
                 'catalogs': {'prec': {'next_page': 3, 'complete': False}},
                 'documents': [{'target': 'prec', 'id': '1', 'sha256': 'x', 'path': 'summaries/prec/1.json'}]}
            Path(t, 'manifest.json').write_text(json.dumps(m))
            self.assertIsNone(cp.reset_catalog(t))

    def test_persistent_catalog_failure_stops_after_two_resets(self):
        calls = []

        def failing(client, out, date, scope, stop=None):
            calls.append(1)
            Path(out, 'manifest.json').write_text(json.dumps({'schema': 'precedent-summary-v1', 'date': DATE, 'pages': [],
                'catalogs': {'prec': {'next_page': 2, 'complete': False}}, 'documents': []}))
            Path(out, 'attempt.json').write_text(json.dumps({'status': 'FAILED', 'reason': 'CATALOG_COUNT_CHANGED'}))
            return False

        with tempfile.TemporaryDirectory() as t:
            self.assertEqual(cp.run(t, 'all', 18000, lambda r: object(), collect_fn=failing, date=DATE), 'FAILED')
        self.assertEqual(len(calls), 3)


class PacingTests(unittest.TestCase):
    def test_client_interval_is_configurable_with_floor(self):
        from bblove_law_all import Client
        self.assertEqual(Client('x').interval, .3)
        self.assertEqual(Client('x', interval=1.0).interval, 1.0)
        self.assertEqual(Client('x', interval=.01).interval, .3)


class PartialExportTests(unittest.TestCase):
    """Finished catalogs of an unfinished collection are exported for early publication."""

    def paused_collection(self, out):
        # prec (2 details) finishes; the tribunal catalog is listed but its detail hits the budget.
        client = FakeClient(count=2, tt_rows=[TT_ROW], budget_at=3)
        collect(client, out, DATE, stop=lambda: None)
        m = json.loads(Path(out, 'manifest.json').read_text())
        self.assertEqual(cp.finished_targets(m), ['prec', 'ntsCgmExpc'])
        return m

    def test_export_keeps_only_finished_catalogs_and_verifies(self):
        with tempfile.TemporaryDirectory() as t, tempfile.TemporaryDirectory() as d:
            self.paused_collection(t)
            self.assertEqual(cp.export_complete(t, d), ['prec', 'ntsCgmExpc'])
            export = Path(d, 'precedent-collection')
            m = strict_json((export / 'manifest.json').read_bytes())
            self.assertEqual(m['partial_targets'], ['prec', 'ntsCgmExpc'])
            self.assertEqual(set(m['catalogs']), {'prec', 'ntsCgmExpc'})
            self.assertEqual(m['status'], 'COMPLETE')
            self.assertNotIn('stop_reason', m)
            self.assertTrue(all(d['target'] in ('prec', 'ntsCgmExpc') for d in m['documents']))
            self.assertFalse((export / 'pages/ttSpecialDecc').exists())
            verify_cache(export, m)  # inventory matches the exported manifest exactly
            self.assertEqual(cp.outcome(export), 'COMPLETE')
            # Re-export replaces a stale export directory.
            (export / 'stale.json').write_text('{}')
            cp.export_complete(t, d)
            self.assertFalse((export / 'stale.json').exists())

    def test_export_is_empty_when_nothing_or_everything_is_finished(self):
        with tempfile.TemporaryDirectory() as t, tempfile.TemporaryDirectory() as d:
            self.assertEqual(cp.export_complete(t, d), [])  # no manifest yet
            collect(FakeClient(count=1, tt_rows=[TT_ROW], budget_at=1), t, DATE, stop=lambda: None)  # all PENDING
            self.assertEqual(cp.export_complete(t, d), [])
            self.assertTrue(collect(FakeClient(count=1, tt_rows=[TT_ROW]), t, DATE))  # everything finished → whole collection
            self.assertEqual(cp.export_complete(t, d), [])
            self.assertFalse(Path(d, 'precedent-collection').exists())

    def test_loader_resumes_partial_export_without_paging_missing_catalogs(self):
        with tempfile.TemporaryDirectory() as t, tempfile.TemporaryDirectory() as d:
            self.paused_collection(t)
            cp.export_complete(t, d)
            export = Path(d, 'precedent-collection')
            client = FakeClient(count=2, tt_rows=[TT_ROW])
            self.assertTrue(collect(client, export, DATE))
            self.assertEqual(client.calls, 0)  # nothing listed or fetched: no tribunal paging
            m = strict_json((export / 'manifest.json').read_bytes())
            self.assertEqual(set(m['catalogs']), {'prec', 'ntsCgmExpc'})

    def test_unknown_partial_target_is_refused(self):
        with tempfile.TemporaryDirectory() as t:
            self.assertTrue(collect(FakeClient(count=1), t, DATE))
            m = json.loads(Path(t, 'manifest.json').read_text())
            m['partial_targets'] = ['prec', 'bogus']
            Path(t, 'manifest.json').write_text(json.dumps(m, ensure_ascii=False))
            self.assertFalse(collect(FakeClient(count=1), t, DATE))
            self.assertTrue(Path(t, 'attempt.json').exists())

    def test_finished_targets_cli_is_empty_when_whole_collection_is_done(self):
        with tempfile.TemporaryDirectory() as t, tempfile.TemporaryDirectory() as d:
            target = Path(d) / 'out.txt'
            self.assertTrue(collect(FakeClient(count=1, tt_rows=[TT_ROW]), t, DATE))
            with patch.dict(os.environ, {'GITHUB_OUTPUT': str(target)}), \
                    patch('sys.argv', ['collect_precedents.py', '--out', t, '--finished-targets']):
                self.assertEqual(cp.main(), 0)
            self.assertIn('complete=true\npartial_targets=\n', target.read_text())
            t2 = Path(d, 'paused')
            t2.mkdir()
            self.paused_collection(t2)  # tribunal row pending → prec,nts exportable
            target.write_text('')
            with patch.dict(os.environ, {'GITHUB_OUTPUT': str(target)}), \
                    patch('sys.argv', ['collect_precedents.py', '--out', str(t2), '--finished-targets']):
                self.assertEqual(cp.main(), 0)
            self.assertIn('complete=false\npartial_targets=prec,ntsCgmExpc\n', target.read_text())
            self.assertFalse(Path(d, 'precedent-collection').exists())  # nothing copied

    def test_export_verifies_itself_and_summary_marks_partial(self):
        with tempfile.TemporaryDirectory() as t, tempfile.TemporaryDirectory() as d:
            self.paused_collection(t)
            cp.export_complete(t, d)
            self.assertEqual(cp.summary(Path(d, 'precedent-collection'))['partial_targets'], ['prec', 'ntsCgmExpc'])
            self.assertNotIn('partial_targets', cp.summary(t))
            # A corrupted source summary makes the export fail its own verification and leaves nothing behind.
            victim = next(Path(t, 'summaries/prec').glob('*.json'))
            victim.write_bytes(victim.read_bytes() + b' ')
            self.assertEqual(cp.export_complete(t, d), [])
            self.assertFalse(Path(d, 'precedent-collection').exists())

    def test_export_cli_writes_step_outputs(self):
        with tempfile.TemporaryDirectory() as t, tempfile.TemporaryDirectory() as d:
            self.paused_collection(t)
            target = Path(d) / 'out.txt'
            with patch.dict(os.environ, {'GITHUB_OUTPUT': str(target)}), \
                    patch('sys.argv', ['collect_precedents.py', '--out', t, '--export-complete', str(Path(d, 'x'))]):
                self.assertEqual(cp.main(), 0)
            self.assertIn('partial_targets=prec,ntsCgmExpc\n', target.read_text())
            self.assertIn('complete=false\n', target.read_text())


class SummaryMigrationPublicationTests(unittest.TestCase):
    def test_migrated_content_is_published_on_its_completion_date(self):
        m = {'date': '20261102', 'incremental': {'complete': True, 'new_documents': 0},
             'summary_empty_migrated': 3, 'summary_empty_completed_date': '20261102'}
        self.assertTrue(cp.should_publish(m))
        m['date'] = '20261103'
        self.assertFalse(cp.should_publish(m))


class PublicationCoverageTests(unittest.TestCase):
    def row(self, tag, draft=False):
        return {'tagName': tag, 'isDraft': draft}

    def test_full_release_blocks_f2_reproduction(self):
        rows = [self.row('prec-20261101')]
        self.assertEqual(cp.published_targets(rows), set(cp.TARGET_ORDER))
        self.assertFalse(cp.may_publish_partial(['prec'], rows))

    def test_union_not_largest_partial_and_drafts_ignored(self):
        rows = [self.row('prec-20261101-partial-prec'), self.row('prec-20261102-partial-ntsCgmExpc')]
        self.assertFalse(cp.may_publish_partial(['prec'], rows))
        self.assertTrue(cp.may_publish_partial(['prec', 'ntsCgmExpc'], rows))
        self.assertTrue(cp.may_publish_partial(['prec'], [self.row('prec-20261101', True)]))
        self.assertTrue(cp.may_publish_partial(['prec'], []))
        self.assertFalse(cp.may_publish_partial([], []))

    def test_history_errors_are_not_absence(self):
        import subprocess
        for rc, out, err in [(1, '[]', ''), (0, '[]', 'warning'), (0, 'not json', ''),
                             (0, '{}', ''), (0, '[{}]', '')]:
            with self.subTest(rc=rc, out=out, err=err), self.assertRaises(ValueError):
                cp.partial_publication_allowed(['prec'], lambda *a, **kw: subprocess.CompletedProcess(a, rc, out, err))
        self.assertTrue(cp.partial_publication_allowed(['prec'], lambda *a, **kw: subprocess.CompletedProcess(a, 0, '[]', '')))
        with self.assertRaisesRegex(ValueError, 'TRUNCATED'):
            cp.partial_publication_allowed(['prec'], lambda *a, **kw: subprocess.CompletedProcess(
                a, 0, json.dumps([self.row('law-20261101')] * cp.RELEASE_HISTORY_LIMIT), ''))

    def test_workflow_calls_guard_and_cli_failure_is_error(self):
        from contextlib import redirect_stdout
        import io
        text = (ROOT/'.github/workflows/collect-precedents.yml').read_text()
        self.assertIn('if ! publish=$(python3 scripts/collect_precedents.py --may-publish-partial "$PARTIAL"); then', text)
        self.assertNotIn('max_by(length)', text)
        self.assertIn('::error::PARTIAL_RELEASE_HISTORY_FAILED', text)
        out = io.StringIO()
        with patch.object(cp, 'partial_publication_allowed', side_effect=ValueError('synthetic')), \
                patch('sys.argv', ['collect_precedents.py', '--may-publish-partial', 'prec']), redirect_stdout(out):
            self.assertEqual(cp.main(), 1)
        self.assertIn('::error::', out.getvalue())
        self.assertNotIn('true', out.getvalue())
