"""TASK_008 acceptance: SYNTHETIC data, injected transport, no live services."""
from copy import deepcopy
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'scripts'))
from bblove_law_all import Client, save
from bblove_precedent_collect import collect, summary_bytes, verify_cache
from bblove_storage import strict_json
from build_precedent_d1 import build, DAILY_LIMIT, remote_load, schema, write_bound
from precedent_search_eval import evaluate, match_query

DATE = '20261015'
PREC = {'판례일련번호': '1', '사건번호': '2099두1', '법원명': '대법원', '선고일자': '2026.01.01',
        '사건명': '합성 명의신탁', '판시사항': '명의신탁 증여의제 쟁점', '판결요지': '합성 요지', '참조조문': '합성법'}
NTS = {'법령해석일련번호': '2', '안건번호': '합성-002', '안건명': '합성 해석', '회신일자': '20260101',
       '질의요지': '합성 질의', '회신': '합성 답변', '관련법령': '합성법',
       '법령해석상세링크':'https://www.law.go.kr/LSW/ntsCgmExpcInfoP.do?ntsCgmExpcSeq=2'}


def encoded(doc):
    return json.dumps(doc, ensure_ascii=False, indent=2).encode('utf-8')


class FakeClient:
    def __init__(self, count=1, detail=None, nts_detail=None, empty_nts=False, budget_at=None):
        self.calls, self.details, self.credential = 0, 0, 'synthetic-secret-value'
        self.rows = [{**PREC, '판례일련번호': str(n), '사건번호': f'2099두{n}'} for n in range(1, count+1)]
        self.detail, self.nts_detail, self.empty_nts, self.budget_at = detail, nts_detail, empty_nts, budget_at

    def get(self, endpoint, params, **kwargs):
        self.calls += 1
        target = params['target']
        if endpoint == 'lawSearch.do':
            rows = self.rows if target == 'prec' else [NTS] if target == 'ntsCgmExpc' else []
            page = params['page']
            return {'Search': {'totalCnt': str(len(rows)), target: rows[(page-1)*100:page*100]}}, None
        self.details += 1
        if self.budget_at == self.details:
            raise ValueError('COLLECTION_BUDGET_EXHAUSTED')
        if target == 'prec':
            sid = params['ID']
            value = self.detail if self.detail is not None else {**self.rows[int(sid)-1], '판례내용': 'FULL_JUDGMENT_MUST_NEVER_PERSIST'}
        else:
            value = self.nts_detail if self.nts_detail is not None else NTS
        doc = {} if target != 'prec' and self.empty_nts else {'Service': value}
        return doc, encoded(doc)


class SummaryCollectionTests(unittest.TestCase):
    def test_resume_zero_requests_and_no_full_text(self):
        with tempfile.TemporaryDirectory() as t:
            out = Path(t)
            self.assertTrue(collect(FakeClient(), out, DATE))
            raw = (out/'summaries/prec/1.json').read_bytes()
            self.assertIn('"판결요지": "합성 요지"'.encode(), raw)
            before = raw
            client = FakeClient()
            self.assertTrue(collect(client, out, DATE))
            self.assertEqual((client.calls, client.details), (0, 0))
            self.assertEqual((out/'summaries/prec/1.json').read_bytes(), before)
            build(out, out/'sql')
            all_bytes = b''.join(p.read_bytes() for p in out.rglob('*') if p.is_file())
            self.assertNotIn(b'FULL_JUDGMENT', all_bytes)
            self.assertNotIn('판례내용'.encode(), all_bytes)
            self.assertNotIn(client.credential.encode(), all_bytes)

    def test_sha_tamper_stops_before_request_and_builder_rejects(self):
        with tempfile.TemporaryDirectory() as t:
            out = Path(t)/'source'
            self.assertTrue(collect(FakeClient(), out, DATE))
            path = out/'summaries/prec/1.json'
            path.write_bytes(path.read_bytes()+b' ')
            client = FakeClient()
            self.assertFalse(collect(client, out, DATE))
            self.assertEqual(client.calls, 0)
            self.assertEqual(strict_json((out/'attempt.json').read_bytes())['reason'], 'CACHE_SHA_MISMATCH')
            with self.assertRaisesRegex(ValueError, 'CACHE_SHA_MISMATCH'):
                build(out, Path(t)/'sql')

    def test_mismatches_and_empty_summary_are_reported_and_not_refetched(self):
        variants = [('판례일련번호', '99'), ('사건번호', '2099두99'), ('법원명', '서울고등법원'),
                    ('선고일자', '20260102'), ('선고일자', '20260230'), ('선고일자', '2026.01-01')]
        for key, value in variants:
            with self.subTest(key=key), tempfile.TemporaryDirectory() as t:
                out = Path(t)
                self.assertFalse(collect(FakeClient(detail={**PREC, key:value}), out, DATE))
                m = strict_json((out/'manifest.json').read_bytes())
                self.assertEqual(m['documents'][0]['status'], 'FAILED')
                retry = FakeClient()
                self.assertFalse(collect(retry, out, DATE))
                self.assertEqual(retry.calls, 0)
        with tempfile.TemporaryDirectory() as t:
            out = Path(t)
            self.assertFalse(collect(FakeClient(detail={**PREC, '판시사항':'', '판결요지':''}), out, DATE))
            self.assertEqual(strict_json((out/'manifest.json').read_bytes())['documents'][0]['reason'], 'SUMMARY_EMPTY')

    def test_pagination_and_budget_resume(self):
        with tempfile.TemporaryDirectory() as t:
            out = Path(t)
            first = FakeClient(count=103, budget_at=3)
            self.assertFalse(collect(first, out, DATE))
            m = strict_json((out/'manifest.json').read_bytes())
            self.assertEqual(len(m['pages']), 4)  # includes the empty tribunal catalog
            self.assertEqual(len(m['documents']), 104)
            second = FakeClient(count=103)
            self.assertTrue(collect(second, out, DATE))
            self.assertEqual(second.calls, 102)  # no catalog or first two details

    def test_catalog_duplicate_and_total_drift(self):
        for mode in ('duplicate', 'drift'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as t:
                client = FakeClient(count=101)
                if mode == 'duplicate':
                    client.rows[100] = client.rows[0]
                else:
                    get = client.get
                    def changed(endpoint, params, **kw):
                        doc, raw = get(endpoint, params, **kw)
                        if params.get('page') == 2:
                            doc['Search']['totalCnt'] = '102'
                        return doc, raw
                    client.get = changed
                self.assertFalse(collect(client, Path(t), DATE))
                self.assertIn(strict_json((Path(t)/'attempt.json').read_bytes())['reason'],
                              ('CATALOG_DUPLICATE_ID', 'CATALOG_COUNT_CHANGED'))

    def test_interpretation_fallback_is_explicit_and_mismatch_fails(self):
        with tempfile.TemporaryDirectory() as t:
            out = Path(t)
            self.assertTrue(collect(FakeClient(empty_nts=True), out, DATE))
            m = strict_json((out/'manifest.json').read_bytes())
            self.assertTrue(m['documents'][1]['listing_only'])
        for detail in ({**NTS, '법령해석일련번호':'3'}, {**NTS, '안건번호':'다름'},
                       {**NTS, '질의요지':'', '회신':''}, {'error':'not authorized'}):
            with tempfile.TemporaryDirectory() as t:
                out = Path(t)
                self.assertFalse(collect(FakeClient(nts_detail=detail), out, DATE))
                self.assertEqual(strict_json((out/'manifest.json').read_bytes())['documents'][1]['status'], 'FAILED')

    def test_real_shared_client_sanitizes_and_never_persists_auth_variants(self):
        from urllib.parse import parse_qs, quote, quote_plus, urlsplit
        secret = 'SYNTHETIC oc+value@example.invalid'
        seen = []
        def transport(request, timeout):
            params = parse_qs(urlsplit(request.full_url).query)
            seen.append(params)
            if 'ID' in params:
                # Deliberate leak in a field excluded from the projection: Client
                # still rejects the entire unsafe original before persistence.
                doc = {'Service': {**(PREC if params['target']==['prec'] else NTS), 'unexpected':quote(secret, safe='')}}
            else:
                target = params['target'][0]
                row = PREC if target == 'prec' else NTS
                rows = [] if target == 'ttSpecialDecc' else [{**row, 'link':'?OC='+quote_plus(secret)}]
                doc = {'Search': {'totalCnt':str(len(rows)), target:rows}}
            response = io.BytesIO(encoded(doc)); response.status = 200
            response.headers = {'Content-Type':'application/json'}
            return response
        client = Client(secret, transport=transport)
        client._admit = lambda: None
        with tempfile.TemporaryDirectory() as t, patch('bblove_law_all.time.sleep'):
            out = Path(t)
            self.assertFalse(collect(client, out, DATE))
            payload = b''.join(p.read_bytes() for p in out.rglob('*') if p.is_file())
            for variant in (secret, quote(secret, safe=''), quote_plus(secret)):
                self.assertNotIn(variant.encode(), payload)
            self.assertNotIn(b'OC=', payload)
            self.assertTrue(all(p['OC']==[secret] for p in seen))

    def test_raw_lexical_json_values_retained(self):
        # Unicode escapes are deliberately retained in the stored summary.
        raw = json.dumps({'Service':PREC}, ensure_ascii=True).encode()
        result = summary_bytes(raw, 'prec')
        self.assertIn(b'\\u', result)
        self.assertEqual(strict_json(result), PREC)

    def test_html_only_summary_and_bad_official_links_fail(self):
        variants = [dict(detail={**PREC, '판시사항':'<p>&nbsp;</p>', '판결요지':'<br/>'}),
                    dict(detail={**PREC, '판례정보일련번호':'99'}),
                    dict(nts_detail={**NTS, '법령해석상세링크':'https://example.invalid/LSW/expcInfoP.do?expcSeq=2'}),
                    dict(nts_detail={**NTS, '법령해석상세링크':'https://www.law.go.kr/LSW/expcInfoP.do?expcSeq=99'})]
        for kwargs in variants:
            with self.subTest(kwargs=kwargs), tempfile.TemporaryDirectory() as t:
                self.assertFalse(collect(FakeClient(**kwargs),Path(t),DATE))


class D1BuildTests(unittest.TestCase):
    def test_batches_under_90k_and_skip_loaded_ids_and_fts_replay(self):
        with tempfile.TemporaryDirectory() as t:
            source, out = Path(t)/'source', Path(t)/'sql'
            client = FakeClient(count=301)
            for row in client.rows:
                row['판결요지'] = '합성 요지' * 8
            self.assertTrue(collect(client, source, DATE))
            report = build(source, out)
            self.assertGreater(len(report['batches']), 1)
            self.assertTrue(all(b['write_units']<=90_000 and b['documents']<=90_000 for b in report['batches']))
            db = sqlite3.connect(':memory:')
            db.executescript((out/'schema.sql').read_text())
            for batch in report['batches']:
                db.executescript((out/batch['path']).read_text())
            self.assertEqual(db.execute('SELECT count(*) FROM precedents').fetchone()[0], 301)
            changes = db.total_changes
            for batch in report['batches']:
                db.executescript((out/batch['path']).read_text())
            self.assertEqual(db.total_changes, changes)
            self.assertEqual(db.execute("SELECT count(*) FROM precedents_fts WHERE precedents_fts MATCH '\"명의신\"'").fetchone()[0], 301)
            report = build(source, out, {'precedents':[str(i) for i in range(1,302)], 'interpretations':['2']})
            self.assertEqual((report['skipped_ids'], report['selected_documents'], report['batches']), (302,0,[]))
            self.assertFalse(list(out.glob('batch-*.sql')))

    def test_partial_failures_visible_in_report_and_sql_escaping(self):
        with tempfile.TemporaryDirectory() as t:
            source = Path(t)/'source'
            self.assertFalse(collect(FakeClient(detail={**PREC,'판결요지':'','판시사항':''}),source,DATE))
            report = build(source, Path(t)/'sql')
            self.assertEqual(report['status'], 'PARTIAL_WITH_FAILURES')
            self.assertEqual(len(report['pending_or_failed']), 1)
            self.assertEqual(report['total_documents'], 1)
        with tempfile.TemporaryDirectory() as t:
            source, out = Path(t)/'source', Path(t)/'sql'
            attack = "합성 '; DROP TABLE precedents; --\n줄바꿈"
            self.assertTrue(collect(FakeClient(nts_detail={**NTS,'안건명':attack}),source,DATE))
            report = build(source,out)
            db = sqlite3.connect(':memory:')
            db.executescript((out/'schema.sql').read_text())
            for b in report['batches']:
                db.executescript((out/b['path']).read_text())
            self.assertEqual(db.execute('SELECT count(*) FROM precedents').fetchone()[0],1)
            self.assertIn('DROP TABLE',db.execute('SELECT title FROM interpretations').fetchone()[0])

    def test_unsafe_or_extra_cache_inventory_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            source = Path(t)/'source'
            self.assertTrue(collect(FakeClient(),source,DATE))
            (source/'summaries/prec/extra.txt').write_text('extra')
            with self.assertRaisesRegex(ValueError, 'CACHE_INVENTORY_MISMATCH'):
                build(source,Path(t)/'sql')

    def test_manifest_omission_and_catalog_tamper_are_detected(self):
        for mode in ('omission', 'catalog'):
            with tempfile.TemporaryDirectory() as t:
                source = Path(t)/'source'
                self.assertTrue(collect(FakeClient(),source,DATE))
                if mode == 'omission':
                    m = strict_json((source/'manifest.json').read_bytes())
                    removed = m['documents'].pop()
                    (source/removed['path']).unlink()
                    save(source/'manifest.json',m)
                else:
                    p = source/'pages/prec/1.json'
                    p.write_bytes(p.read_bytes()+b' ')
                with self.assertRaises(ValueError):
                    build(source,Path(t)/'sql')

    def test_remote_resume_and_reservation_before_failure(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t); source, out, state = root/'source', root/'sql', root/'state.json'
            self.assertTrue(collect(FakeClient(),source,DATE))
            db = sqlite3.connect(':memory:')
            seen = []
            def execute(command):
                before = db.total_changes
                if '--file' in command:
                    # Assert crash-safe reservation was persisted before mutation.
                    ledger = strict_json(state.read_bytes())['days']['2026-10-15']
                    self.assertGreater(ledger['reserved'], 0)
                    db.executescript(Path(command[command.index('--file')+1]).read_text())
                    rows = []
                else:
                    cursor = db.execute(command[command.index('--command')+1])
                    rows = [dict(zip([c[0] for c in cursor.description], row)) for row in cursor.fetchall()]
                seen.append(command)
                size = db.execute('PRAGMA page_count').fetchone()[0] * db.execute('PRAGMA page_size').fetchone()[0]
                return [dict(success=True,results=rows,meta={'rows_written':db.total_changes-before,'size_after':size})]
            remote_load(source,out,state,root/'config','test-db','wrangler',execute,day='2026-10-15',clock=lambda:'2026-10-15')
            writes = db.total_changes
            remote_load(source,out,state,root/'config','test-db','wrangler',execute,day='2026-10-15',clock=lambda:'2026-10-15')
            self.assertEqual(db.total_changes,writes)
            self.assertEqual(strict_json((out/'build-report.json').read_bytes())['skipped_ids'], 2)
            self.assertLessEqual(strict_json(state.read_bytes())['days']['2026-10-15']['reserved'],DAILY_LIMIT)
            # Unknown outcome retains the reservation; no refund on exceptions.
            other = root/'failed-state.json'
            def fail(command):
                raise ValueError('synthetic transport failure')
            with self.assertRaises(ValueError):
                remote_load(source,out,other,root/'config','test-db','wrangler',fail,day='2026-10-15',clock=lambda:'2026-10-15')
            self.assertEqual(strict_json(other.read_bytes())['days']['2026-10-15']['reserved'],512)

    def test_quota_closed_on_midnight_or_exhaustion_or_missing_receipt(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t); source, out, state = root/'source',root/'sql',root/'state.json'
            self.assertTrue(collect(FakeClient(),source,DATE))
            with self.assertRaisesRegex(ValueError,'UTC_WRITE_WINDOW_CHANGED'):
                remote_load(source,out,state,root/'config','test','wrangler',lambda c:[],
                            day='2026-10-15',clock=lambda:'2026-10-16')
            save(state,{'days':{'2026-10-15':{'reserved':DAILY_LIMIT,'rows_written':0}}})
            calls = []
            remote_load(source,out,state,root/'config','test','wrangler',lambda c:calls.append(c),
                        day='2026-10-15',clock=lambda:'2026-10-15')
            self.assertEqual(calls,[])
            save(state,{'days':{}})
            with self.assertRaisesRegex(ValueError,'REMOTE_WRITE_RECEIPT_MISSING'):
                remote_load(source,out,state,root/'config','test','wrangler',lambda c:[{'success':True}],
                            day='2026-10-15',clock=lambda:'2026-10-15')
            self.assertEqual(strict_json(state.read_bytes())['days']['2026-10-15']['reserved'],512)

    def test_query_quality_and_match_syntax(self):
        self.assertEqual(evaluate('trigram')['hits'],20)
        self.assertEqual(evaluate('unicode61')['hits'],20)
        self.assertNotIn('*',match_query('" OR * NEAR()'))
        self.assertEqual(match_query('!!'), '')



if __name__ == '__main__':
    unittest.main()
