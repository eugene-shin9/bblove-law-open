"""TASK_004 synthetic replay and bounded concurrency; no live API or credentials."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from urllib.request import Request
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'scripts'))
from test_full_law_collection import FakeClient, ROW, DOC, DATE
from bblove_law_all import (Client, collect, collection_complete, validate_raw, validate_detail_all,
                            failure_distribution, safe_detail)
from bblove_v107_pilot import validate_detail
from bblove_tax_p2_collect import PilotBlocked, RemoteNetworkError
from bblove_law_transport import measured_transport
from build_mcp_assets_official import build, encode, load_groups
from verify_mcp_assets_official import verify
from diagnose_law_collection import diagnose


def slow_fixture_worker(connection, url, headers, timeout, read_limit):
    time.sleep(.08)
    connection.send(('OK', 200, 'application/json', b'{}', .08))
    connection.close()


def stuck_fixture_worker(connection, url, headers, timeout, read_limit):
    time.sleep(10)


def response(raw=b'{}', status=200):
    r = io.BytesIO(raw)
    r.status = status
    r.headers = {'Content-Type': 'application/json'}
    return r


class CollectionFinishTests(unittest.TestCase):
    def test_projection_warns_without_changing_raw_or_v107(self):
        doc = deepcopy(DOC)
        doc['법령']['기본정보']['법령명_한글'] = '테스트 법'
        units = doc['법령']['조문']['조문단위']
        units.append(deepcopy(units[-1]))
        units.append({'조문여부': 'Y', '조문번호': '2', '조문내용': '', '조문제목': '삭제'})
        raw = encode(doc)
        with self.assertRaises(ValueError):
            validate_detail(doc, ROW, DATE)
        returned, count, warnings = validate_raw(raw, ROW, DATE, with_warnings=True)
        self.assertEqual(count, 3)
        self.assertEqual(encode(returned), raw)
        self.assertEqual({w['code'] for w in warnings},
                         {'title_whitespace_difference', 'duplicate_article_identifier', 'empty_deleted_article'})
        with tempfile.TemporaryDirectory() as tmp:
            source, out = Path(tmp) / 'raw', Path(tmp) / 'assets'
            self.assertTrue(collect(FakeClient(doc=doc), source, DATE))
            self.assertEqual((source / '테스트법/법률.json').read_bytes(), raw)
            build(source, ROOT, out, DATE, enforce_thresholds=False)
            meta = json.loads((out / 'meta.json').read_bytes())
            self.assertEqual(meta['warnings'][0]['warnings'], warnings)
            self.assertEqual(verify(out, DATE, enforce_thresholds=False)['n_articles'], 3)
            _, groups = load_groups(source, DATE)
            self.assertEqual(groups['테스트법'][0]['a'][-1][2], '')
            self.assertEqual(groups['테스트법'][0]['a'][0], groups['테스트법'][0]['a'][1])
            replay = diagnose(source, DATE)
            self.assertEqual(replay['replay_counts'], {'SUCCESS': 1})
            meta['warnings'] = []
            (out / 'meta.json').write_bytes(encode(meta))
            with self.assertRaisesRegex(ValueError, 'ARTICLE_WARNINGS_MISMATCH'):
                verify(out, DATE, enforce_thresholds=False)

    def test_identifiers_dates_and_unmarked_missing_content_stay_hard_errors(self):
        for change in ('id', 'publication', 'title', 'empty', 'identifier', 'malformed'):
            with self.subTest(change=change):
                doc = deepcopy(DOC)
                basic = doc['법령']['기본정보']
                unit = doc['법령']['조문']['조문단위'][-1]
                if change == 'id': basic['법령ID'] = '999999'
                elif change == 'publication': basic['공포일자'] = '20240101'
                elif change == 'title': basic['법령명_한글'] = '다른법'
                elif change == 'empty': unit['조문내용'] = ''
                elif change == 'identifier': unit['조문번호'] = 'bad'
                elif change == 'malformed': doc['법령']['조문']['조문단위'].append('bad')
                with self.assertRaises(ValueError):
                    validate_raw(encode(doc), ROW, DATE, with_warnings=True)

    def test_past_detail_effective_date_differing_from_catalog_is_warning(self):
        doc = deepcopy(DOC)
        doc['법령']['기본정보']['시행일자'] = '20200101'
        validated, count, warnings = validate_raw(encode(doc), ROW, DATE, with_warnings=True)
        self.assertEqual(validated, doc)  # original untouched; asset shows the detail's own date
        self.assertEqual(warnings[0]['code'], 'effective_date_differs_from_catalog')
        self.assertEqual(warnings[0]['detail'], '20200101')

    def test_effective_date_warnings_pass_release_verifier(self):
        for detail, code in (('20200101', 'effective_date_differs_from_catalog'),
                             ('29991231', 'detail_date_after_today')):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as tmp:
                doc = deepcopy(DOC)
                doc['법령']['기본정보']['시행일자'] = detail
                source, out = Path(tmp) / 'raw', Path(tmp) / 'assets'
                self.assertTrue(collect(FakeClient(doc=doc), source, DATE))
                build(source, ROOT, out, DATE, enforce_thresholds=False)
                meta = json.loads((out / 'meta.json').read_bytes())
                self.assertEqual(meta['warnings'][0]['warnings'][0]['code'], code)
                self.assertIsInstance(meta['titles'], dict)
                self.assertEqual(verify(out, DATE, enforce_thresholds=False)['n_docs'], 1)
                group = next((out / 'groups').iterdir())
                docs = json.loads(group.read_bytes())
                self.assertEqual(docs[0]['eff2'], f'{detail[:4]}-{detail[4:6]}-{detail[6:]}')
                docs[0]['eff2'] = '2001-01-01'
                group.write_bytes(encode(docs))
                with self.assertRaisesRegex(ValueError, 'EFFECTIVE_DATE_WARNING_MISMATCH'):
                    verify(out, DATE, enforce_thresholds=False)

    def test_manual_alias_applies_to_spaced_official_group_name(self):
        doc = deepcopy(DOC)
        doc['법령']['기본정보']['법령명_한글'] = '상속세 및 증여세법'
        row = dict(ROW, 법령명한글='상속세 및 증여세법')
        with tempfile.TemporaryDirectory() as tmp:
            source, out = Path(tmp) / 'raw', Path(tmp) / 'assets'
            self.assertTrue(collect(FakeClient(rows=[row], doc=doc), source, DATE))
            build(source, ROOT, out, DATE, enforce_thresholds=False)
            alias = json.loads((out / 'meta.json').read_bytes())['alias']
            self.assertEqual(alias.get('상증법'), '상속세 및 증여세법')
            self.assertEqual(verify(out, DATE, enforce_thresholds=False)['n_docs'], 1)

    def test_future_detail_date_on_current_edition_is_warning(self):
        doc = deepcopy(DOC)
        doc['법령']['기본정보']['시행일자'] = '29991231'
        _, _, warnings = validate_raw(encode(doc), ROW, DATE, with_warnings=True)
        self.assertEqual(warnings[0]['code'], 'detail_date_after_today')

    def test_impossible_detail_date_stays_error(self):
        doc = deepcopy(DOC)
        doc['법령']['기본정보']['시행일자'] = '20261341'
        with self.assertRaises(ValueError):
            validate_raw(encode(doc), ROW, DATE, with_warnings=True)

    def test_duplicate_number_preserves_different_text(self):
        doc = deepcopy(DOC)
        unit = deepcopy(doc['법령']['조문']['조문단위'][-1])
        unit['조문내용'] = '삭제 <2020. 1. 1.>'
        doc['법령']['조문']['조문단위'].append(unit)
        validated, count, warnings = validate_raw(encode(doc), ROW, DATE, with_warnings=True)
        self.assertEqual(count, 2)
        self.assertEqual(validated, doc)
        self.assertEqual(warnings[0]['code'], 'duplicate_article_identifier')

    def test_failed_raw_replays_without_network_and_can_recover_after_rule_change(self):
        doc = deepcopy(DOC)
        doc['법령']['조문']['조문단위'].append(deepcopy(doc['법령']['조문']['조문단위'][-1]))
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)
            with patch('bblove_law_all.validate_detail_all', side_effect=ValueError('detail: duplicate article identifier')):
                self.assertTrue(collect(FakeClient(doc=doc), source, DATE))
            manifest = json.loads((source / 'manifest.json').read_bytes())
            e = manifest['documents'][0]
            self.assertEqual(e['detail'], 'detail: duplicate article identifier')
            self.assertEqual(e['sha256'], hashlib.sha256(encode(doc)).hexdigest())
            retry = FakeClient(doc=doc)
            self.assertTrue(collect(retry, source, DATE))
            self.assertEqual(retry.details, 0)
            recovered = json.loads((source / 'manifest.json').read_bytes())['documents'][0]
            self.assertEqual(recovered['status'], 'SUCCESS')
            self.assertTrue(recovered['reused'])
            changed = FakeClient(rows=[{**ROW, '법령일련번호': '123457'}], doc=doc)
            self.assertTrue(collect(changed, source, DATE))
            self.assertEqual(changed.details, 1)

    def test_pending_checkpoint_keeps_previous_raw_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)
            collect(FakeClient(), source, DATE)
            manifest = json.loads((source / 'manifest.json').read_bytes())
            manifest['documents'][0]['status'] = 'PENDING'
            manifest['status'] = 'INCOMPLETE'
            (source / 'manifest.json').write_bytes(encode(manifest))
            client = FakeClient()
            self.assertTrue(collect(client, source, DATE))
            self.assertEqual(client.details, 0)

    def test_manifest_detail_does_not_store_credential_or_authenticated_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)
            client = FakeClient()
            client.credential = 'synthetic-credential'
            error = ValueError('detail synthetic-credential https://example.test/?OC=synthetic-credential')
            with patch('bblove_law_all.validate_detail_all', side_effect=error):
                collect(client, source, DATE)
            raw = (source / 'manifest.json').read_text()
            self.assertNotIn('synthetic-credential', raw)
            self.assertNotIn('https://example.test', raw)
            self.assertIn('REDACTED', raw)

    def test_failed_raw_corruption_requires_refetch(self):
        doc = deepcopy(DOC); doc['법령']['기본정보']['법령ID'] = '999999'
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)
            collect(FakeClient(doc=doc), source, DATE)
            (source / '테스트법/법률.json').write_bytes(b'{}')
            retry = FakeClient()
            collect(retry, source, DATE)
            self.assertEqual(retry.details, 1)
            self.assertEqual(json.loads((source / 'manifest.json').read_bytes())['documents'][0]['status'], 'SUCCESS')

    def test_complete_with_failed_excludes_failed_docs_and_exports_reasons(self):
        second = {**ROW, '법령ID': '000002', '법령일련번호': '222222', '법령명한글': '실패법'}
        with tempfile.TemporaryDirectory() as tmp:
            source, out = Path(tmp) / 'raw', Path(tmp) / 'assets'
            self.assertTrue(collect(FakeClient(rows=[ROW, second]), source, DATE))
            manifest = json.loads((source / 'manifest.json').read_bytes())
            self.assertEqual(manifest['status_counts'], {'SUCCESS': 1, 'FAILED': 1})
            failures = json.loads((source / 'collection-failures.json').read_bytes())
            self.assertEqual(failures[0]['mst'], '222222')
            self.assertEqual(failures[0]['detail'], 'detail: title/ID/effective date mismatch')
            result = build(source, ROOT, out, DATE, enforce_thresholds=False)
            self.assertEqual(result['n_docs'], 1)
            self.assertEqual(result['excluded_documents'], 1)
            with self.assertRaisesRegex(ValueError, 'ACCEPTANCE_COUNTS'):
                build(source, ROOT, out, DATE)

    def test_pending_or_budget_failure_blocks_complete_and_build(self):
        self.assertFalse(collection_complete([]))
        for entry in ({'status': 'PENDING'}, {'status': 'FAILED', 'reason': 'COLLECTION_BUDGET_EXHAUSTED'}):
            self.assertFalse(collection_complete([entry]))
        self.assertTrue(collection_complete([{'status': 'FAILED', 'reason': 'DETAIL_VALIDATION_FAILED'}]))
        class BudgetClient(FakeClient):
            def get(self, endpoint, params, **kw):
                if endpoint == 'lawService.do': raise PilotBlocked('COLLECTION_BUDGET_EXHAUSTED')
                return super().get(endpoint, params, **kw)
        with tempfile.TemporaryDirectory() as tmp:
            rows = [{**ROW, '법령ID': f'{i:06d}', '법령명한글': f'법{i}'} for i in range(1, 8)]
            source = Path(tmp)
            self.assertFalse(collect(BudgetClient(rows=rows), source, DATE))
            manifest = json.loads((source / 'manifest.json').read_bytes())
            self.assertGreater(manifest['status_counts'].get('PENDING', 0), 0)
            manifest['status'] = 'COMPLETE'
            (source / 'manifest.json').write_bytes(encode(manifest))
            with self.assertRaisesRegex(ValueError, 'INCOMPLETE_COLLECTION'):
                load_groups(source, DATE)

    def test_distribution_examples_and_unavailable_raw_are_not_invented(self):
        entries = [{'status': 'FAILED', 'title': f'법{i}', 'mst': str(i), 'detail': 'reason'} for i in range(8)]
        self.assertEqual(failure_distribution(entries)['reason']['count'], 8)
        self.assertEqual(len(failure_distribution(entries)['reason']['examples']), 5)
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)
            collect(FakeClient(), source, DATE)
            manifest = json.loads((source / 'manifest.json').read_bytes())
            manifest['documents'][0].update(status='FAILED', reason='DETAIL_VALIDATION_FAILED')
            (source / 'manifest.json').write_bytes(encode(manifest))
            (source / '테스트법/법률.json').unlink()
            report = diagnose(source, DATE)
            self.assertEqual(report['replay_counts'], {'UNAVAILABLE': 1})
            self.assertIn('RAW_CACHE_UNAVAILABLE', report['strict_failure_distribution'])

    def test_error_details_redact_credentials_and_urls(self):
        message = safe_detail(ValueError('detail synthetic-credential https://example.test/?OC=synthetic-credential'), 'synthetic-credential')
        self.assertNotIn('synthetic-credential', message)
        self.assertNotIn('https://', message)
        self.assertEqual(safe_detail(ValueError('detail: duplicate article identifier')), 'detail: duplicate article identifier')

    def test_semaphore_caps_concurrency_and_spacing_per_thread(self):
        lock = threading.Lock()
        starts = {}
        active = peak = 0
        def transport(req, timeout):
            nonlocal active, peak
            with lock:
                active += 1; peak = max(peak, active)
                starts.setdefault(threading.get_ident(), []).append(time.monotonic())
            time.sleep(.08)
            with lock: active -= 1
            return response()
        client = Client('synthetic-credential', transport=transport)
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(lambda _: client.get('lawService.do', {'target': 'law', 'MST': '123'}), range(9)))
        self.assertEqual(peak, 3)
        self.assertEqual(client.calls, 9)
        for values in starts.values():
            for a, b in zip(values, values[1:]): self.assertGreaterEqual(b - a, .29)
        self.assertGreater(client.metrics()['mean_request_seconds'], .07)
        self.assertIsNone(client.metrics()['mean_network_seconds'])

    def test_429_and_5xx_cooldown_is_shared_with_other_threads(self):
        for code in (429, 503):
            with self.subTest(code=code):
                starts = []
                lock = threading.Lock()
                def transport(req, timeout):
                    with lock:
                        starts.append(time.monotonic()); n = len(starts)
                    if n == 1:
                        raise urllib.error.HTTPError('', code, 'HTTP_ERROR', {}, None)
                    return response()
                client = Client('synthetic-credential', transport=transport)
                # Wait for shared backoff, then start a different worker request.
                with ThreadPoolExecutor(max_workers=2) as pool:
                    first = pool.submit(client.get, 'lawService.do', {'target': 'law'})
                    deadline = time.monotonic() + 2
                    while not client.cooldown_until and time.monotonic() < deadline: time.sleep(.005)
                    self.assertGreater(client.cooldown_until, 0)
                    second = pool.submit(client.get, 'lawService.do', {'target': 'law'})
                    first.result(); second.result()
                self.assertGreaterEqual(starts[1] - starts[0], .98)
                self.assertEqual(client.metrics()['global_backoffs'], 1)

    def test_measured_process_transport_splits_numeric_timings(self):
        r = measured_transport(Request('https://fixture.invalid/'), 5, worker=slow_fixture_worker)
        self.assertEqual(r.read(), b'{}')
        self.assertEqual(r.bblove_timing['network_seconds'], .08)
        self.assertGreater(r.bblove_timing['process_overhead_seconds'], 0)
        self.assertGreaterEqual(r.bblove_timing['transport_seconds'], .08)

    def test_measured_transport_hard_deadline(self):
        start = time.monotonic()
        with self.assertRaises(RemoteNetworkError):
            measured_transport(Request('https://fixture.invalid/'), .1, worker=stuck_fixture_worker)
        self.assertLess(time.monotonic() - start, 3)


if __name__ == '__main__':
    unittest.main()
