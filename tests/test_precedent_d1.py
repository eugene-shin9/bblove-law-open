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
from bblove_precedent_collect import collect, summary_bytes, validated, verify_cache
from bblove_storage import strict_json
from build_precedent_d1 import build, DAILY_LIMIT, remote_load, schema, write_bound
from precedent_search_eval import evaluate, match_query

DATE = '20261015'
PREC = {'판례일련번호': '1', '사건번호': '2099두1', '법원명': '대법원', '선고일자': '2026.01.01', '데이터출처명': '대법원',
        '사건명': '합성 명의신탁', '판시사항': '명의신탁 증여의제 쟁점', '판결요지': '합성 요지', '참조조문': '합성법'}
NTS = {'법령해석일련번호': '2', '안건번호': '합성-002', '안건명': '합성 해석', '회신일자': '20260101',
       '질의요지': '합성 질의', '회신': '합성 답변', '관련법령': '합성법',
       '법령해석상세링크':'https://www.law.go.kr/LSW/ntsCgmExpcInfoP.do?ntsCgmExpcSeq=2'}
# Live shapes (probes 2026-10-29).
TAX_PREC = {'판례일련번호': '613735', '사건번호': '대법원-2025-두-34754', '데이터출처명': '국세법령정보시스템',
            '사건종류명': '일반행정', '선고일자': '2025.12.11', '법원명': '', '사건명': '합성 의제배당 회피 거래 과세 적법'}
LABOR_PREC = {'판례일련번호': '700001', '사건번호': '', '데이터출처명': '근로복지공단산재판례',
              '선고일자': '0001.01.01', '법원명': '', '사건명': '합성 산재'}
LIVE_NTS = {'법령해석일련번호': '196750', '안건번호': '제도46014-11132', '안건명': '합성 연부연납 이자율',
            '해석일자': '2001.05.16', '해석기관명': '국세청',
            '법령해석상세링크': 'https://taxlaw.nts.go.kr/qt/USEQTA002P.do?ntstDcmId=010000000000053861'}
TT_ROW = {'특별행정심판재결례일련번호': '2079565', '청구번호': '조심 2025중2548', '의결일자': '2026.01.26',
          '재결구분명': '조세', '사건명': ''}
TT_DETAIL = {'특별행정심판재결례일련번호': '2079565', '청구번호': '', '사건번호': '', '의결일자': '20260126',
             '사건명': '합성 가업승계 증여세 과세특례(기각)', '재결요지': '', '세목': '증여', '재결례유형명': '조세',
             '관련법령': '', '이유': 'FULL_REASONS_MUST_NEVER_PERSIST'}


def encoded(doc):
    return json.dumps(doc, ensure_ascii=False, indent=2).encode('utf-8')


class FakeClient:
    def __init__(self, count=1, detail=None, nts_rows=None, budget_at=None, extra_prec=(), tt_rows=(),
                 tt_detail=None):
        self.calls, self.details, self.credential = 0, 0, 'synthetic-secret-value'
        self.rows = [{**PREC, '판례일련번호': str(n), '사건번호': f'2099두{n}'} for n in range(1, count+1)]
        self.rows += [dict(r) for r in extra_prec]
        self.detail, self.budget_at = detail, budget_at
        self.nts_rows = [NTS] if nts_rows is None else nts_rows
        self.tt_rows, self.tt_detail, self.requested = list(tt_rows), tt_detail, []

    def get(self, endpoint, params, **kwargs):
        self.calls += 1
        target = params['target']
        if endpoint == 'lawSearch.do':
            rows = self.rows if target == 'prec' else self.nts_rows if target == 'ntsCgmExpc' else self.tt_rows
            page = params['page']
            return {'Search': {'totalCnt': str(len(rows)), target: rows[(page-1)*100:page*100]}}, None
        self.details += 1
        self.requested.append((target, params['ID']))
        if self.budget_at == self.details:
            raise ValueError('COLLECTION_BUDGET_EXHAUSTED')
        if target == 'prec':
            sid = params['ID']
            row = next(r for r in self.rows if r['판례일련번호'] == sid)
            value = self.detail if self.detail is not None else {**row, '판례내용': 'FULL_JUDGMENT_MUST_NEVER_PERSIST'}
        elif target == 'ttSpecialDecc':
            value = self.tt_detail if self.tt_detail is not None else TT_DETAIL
            if value == 'NOT_FOUND':
                doc = {'Law': '일치하는 조세심판원 특별행정심판재결례이 없습니다.  특별행정심판명을 확인하여 주십시오.'}
                return doc, encoded(doc)
        else:
            raise AssertionError('interpretations have no DRF detail and must never be requested')
        doc = {'Service': value}
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
        variants = [('판례일련번호', '99'), ('사건번호', '2099두99'),
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

    def test_future_dated_listing_is_kept_in_catalog_but_fails_without_request(self):
        # law.go.kr listed 대법원 2025두34754 with 선고일자 2026.12.11 on 2026-10-29; the whole
        # catalog used to abort with CATALOG_OR_CACHE_FAILED on that one row.
        with tempfile.TemporaryDirectory() as t:
            out = Path(t)
            client = FakeClient(count=3)
            client.rows[1]['선고일자'] = '2026.12.11'
            self.assertFalse(collect(client, out, DATE))
            self.assertFalse((out/'attempt.json').exists())
            m = strict_json((out/'manifest.json').read_bytes())
            self.assertTrue(all(c['complete'] for c in m['catalogs'].values()))
            by_id = {d['id']: d for d in m['documents'] if d['target'] == 'prec'}
            self.assertEqual(by_id['2']['status'], 'FAILED')
            self.assertEqual(by_id['2']['reason'], 'DATE_IN_FUTURE')
            self.assertNotIn('sha256', by_id['2'])
            self.assertEqual({by_id['1']['status'], by_id['3']['status']}, {'SUCCESS'})
            self.assertEqual(client.details, 2)  # two precedents; none for the future row or the interpretation
            retry = FakeClient(count=3)
            retry.rows[1]['선고일자'] = '2026.12.11'
            self.assertFalse(collect(retry, out, DATE))
            self.assertEqual(retry.calls, 0)

    def test_interpretation_rows_under_live_root_key(self):
        # Live lawSearch.do returns CgmExpc.cgmExpc for target=ntsCgmExpc (probe 2026-10-29).
        class LiveKeys(FakeClient):
            def get(self, endpoint, params, **kwargs):
                doc, raw = super().get(endpoint, params, **kwargs)
                if endpoint == 'lawSearch.do' and params['target'] == 'ntsCgmExpc':
                    rows = doc['Search'].pop('ntsCgmExpc')
                    doc = {'CgmExpc': {'totalCnt': doc['Search']['totalCnt'], 'cgmExpc': rows}}
                return doc, raw
        with tempfile.TemporaryDirectory() as t:
            out = Path(t)
            self.assertTrue(collect(LiveKeys(), out, DATE))
            m = strict_json((out/'manifest.json').read_bytes())
            self.assertEqual(m['catalogs']['ntsCgmExpc']['total'], 1)
            self.assertEqual([d['status'] for d in m['documents'] if d['target'] == 'ntsCgmExpc'], ['SUCCESS'])

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
            self.assertEqual(second.calls, 101)  # no catalog, no first two details, no interpretation request

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

    def test_interpretations_are_listing_only_and_bad_links_fail_by_themselves(self):
        with tempfile.TemporaryDirectory() as t:
            out = Path(t)
            client = FakeClient(nts_rows=[LIVE_NTS, NTS])
            self.assertTrue(collect(client, out, DATE))
            self.assertFalse([r for r in client.requested if r[0] == 'ntsCgmExpc'])
            m = strict_json((out/'manifest.json').read_bytes())
            nts = [d for d in m['documents'] if d['target'] == 'ntsCgmExpc']
            self.assertEqual({d['kind'] for d in nts}, {'listing'})
            rec = validated(nts[0]['row'], strict_json((out/nts[0]['path']).read_bytes()), 'ntsCgmExpc', 'listing')
            self.assertEqual((rec['number'], rec['date'], rec['url']), ('제도46014-11132', '20010516',
                'https://taxlaw.nts.go.kr/qt/USEQTA002P.do?ntstDcmId=010000000000053861'))
        for link in ('https://example.invalid/qt/USEQTA002P.do?ntstDcmId=1',
                     'https://taxlaw.nts.go.kr/qt/USEQTA002P.do?ntstDcmId=1&OC=x',
                     'https://www.law.go.kr/LSW/expcInfoP.do?expcSeq=99'):
            with self.subTest(link=link), tempfile.TemporaryDirectory() as t:
                out = Path(t)
                self.assertFalse(collect(FakeClient(nts_rows=[{**LIVE_NTS, '법령해석상세링크': link}, NTS]), out, DATE))
                m = strict_json((out/'manifest.json').read_bytes())
                nts = {d['id']: d for d in m['documents'] if d['target'] == 'ntsCgmExpc'}
                self.assertEqual((nts['196750']['status'], nts['196750']['reason']), ('FAILED', 'INTERPRETATION_URL_INVALID'))
                self.assertEqual(nts['2']['status'], 'SUCCESS')

    def test_live_precedent_sources(self):
        with tempfile.TemporaryDirectory() as t:
            out = Path(t)
            client = FakeClient(count=1, extra_prec=[TAX_PREC, LABOR_PREC])
            self.assertTrue(collect(client, out, DATE))
            self.assertEqual(client.requested, [('prec', '1')])
            m = strict_json((out/'manifest.json').read_bytes())
            by_id = {d['id']: d for d in m['documents']}
            self.assertEqual((by_id['613735']['kind'], by_id['613735']['status']), ('listing', 'SUCCESS'))
            self.assertEqual((by_id['700001']['kind'], by_id['700001']['status'], by_id['700001']['reason']),
                             ('skipped', 'SKIPPED', 'SOURCE_NOT_COLLECTED'))
            self.assertNotIn('path', by_id['700001'])
            rec = validated(by_id['613735']['row'], strict_json((out/by_id['613735']['path']).read_bytes()), 'prec', 'listing')
            self.assertEqual((rec['court'], rec['case_no'], rec['date'], rec['holding']), ('대법원', '2025두34754', '20251211', ''))
            report = build(out, out/'sql', scope='tax')
            # 국세 source counts as tax; the synthetic court row has no tax signal; the labor row is skipped.
            self.assertEqual(report['total_documents'], 2)
            self.assertEqual((report['scope_selection']['excluded_documents'],
                              report['scope_selection']['skipped_sources'], report['pending_or_failed']), (1, 1, []))

    def test_court_prefixed_case_numbers(self):
        for number, court, case_no in (('대법원2026두31124', '대법원', '2026두31124'),
                                       ('서울행정법원2025구합55369', '서울행정법원', '2025구합55369'),
                                       ('부산고등법원(창원)-2015-누-10783', '부산고등법원(창원)', '2015누10783')):
            with self.subTest(number=number), tempfile.TemporaryDirectory() as t:
                out = Path(t)
                row = {**TAX_PREC, '사건번호': number}
                self.assertTrue(collect(FakeClient(count=0, extra_prec=[row]), out, DATE))
                m = strict_json((out/'manifest.json').read_bytes())
                e = next(d for d in m['documents'] if d['target'] == 'prec')
                rec = validated(e['row'], strict_json((out/e['path']).read_bytes()), 'prec', 'listing')
                self.assertEqual((rec['court'], rec['case_no']), (court, case_no))

    def test_precedent_detail_court_abbreviation_and_merged_numbers(self):
        lower = {**PREC, '판례일련번호': '624755', '사건번호': '2026고합1', '법원명': '창원지방법원'}
        for detail_no in ('2026고합1', '2026고합1, 2026고합7(병합)', '2026 고합 1'):
            with self.subTest(detail_no=detail_no), tempfile.TemporaryDirectory() as t:
                client = FakeClient(count=0, extra_prec=[lower],
                                    detail={**lower, '법원명': '창원지법', '사건번호': detail_no})
                self.assertTrue(collect(client, Path(t), DATE))

    def test_tribunal_detail_shapes_and_not_found(self):
        with tempfile.TemporaryDirectory() as t:
            out = Path(t)
            self.assertTrue(collect(FakeClient(tt_rows=[TT_ROW]), out, DATE))
            m = strict_json((out/'manifest.json').read_bytes())
            e = next(d for d in m['documents'] if d['target'] == 'ttSpecialDecc')
            raw = (out/e['path']).read_bytes()
            self.assertNotIn('FULL_REASONS'.encode(), raw)
            rec = validated(e['row'], strict_json(raw), 'ttSpecialDecc', e['kind'])
            self.assertEqual((rec['number'], rec['title'], rec['tax_type'], rec['summary']),
                             ('조심2025중2548', '합성 가업승계 증여세 과세특례(기각)', '증여', ''))
        with tempfile.TemporaryDirectory() as t:
            out = Path(t)
            bare = {**TT_DETAIL, '사건명': '', '주문': '심판청구를 기각한다.'}
            self.assertTrue(collect(FakeClient(tt_rows=[TT_ROW], tt_detail=bare), out, DATE))
            m = strict_json((out/'manifest.json').read_bytes())
            e = next(d for d in m['documents'] if d['target'] == 'ttSpecialDecc')
            self.assertEqual(validated(e['row'], strict_json((out/e['path']).read_bytes()), 'ttSpecialDecc', 'detail')['title'],
                             '주문: 심판청구를 기각한다.')
        with tempfile.TemporaryDirectory() as t:
            self.assertFalse(collect(FakeClient(tt_rows=[TT_ROW], tt_detail={**TT_DETAIL, '사건명': ''}), Path(t), DATE))
        with tempfile.TemporaryDirectory() as t:
            out = Path(t)
            titled = {**TT_ROW, '사건명': '합성 쟁점 여부(기각)'}
            self.assertTrue(collect(FakeClient(tt_rows=[titled], tt_detail='NOT_FOUND'), out, DATE))
            m = strict_json((out/'manifest.json').read_bytes())
            self.assertEqual(next(d for d in m['documents'] if d['target'] == 'ttSpecialDecc')['kind'], 'listing')
        with tempfile.TemporaryDirectory() as t:
            out = Path(t)
            self.assertFalse(collect(FakeClient(tt_rows=[TT_ROW], tt_detail='NOT_FOUND'), out, DATE))
            m = strict_json((out/'manifest.json').read_bytes())
            e = next(d for d in m['documents'] if d['target'] == 'ttSpecialDecc')
            self.assertEqual((e['status'], e['reason']), ('FAILED', 'DETAIL_NOT_FOUND'))

    def test_future_row_collected_after_its_date_keeps_cache_valid(self):
        with tempfile.TemporaryDirectory() as t:
            out = Path(t)
            rows = [{**TAX_PREC, '선고일자': '2026.12.11'}]
            self.assertFalse(collect(FakeClient(count=1, extra_prec=rows), out, DATE))
            self.assertTrue(collect(FakeClient(count=1, extra_prec=rows), out, '20261212'))
            m = strict_json((out/'manifest.json').read_bytes())
            self.assertEqual(m['date'], DATE)
            verify_cache(out, m)  # an earlier manifest date never rejects a later-collected entry
            self.assertTrue(collect(FakeClient(count=1, extra_prec=rows), out, DATE))

    def test_interrupted_run_leftovers_are_discarded_on_resume(self):
        with tempfile.TemporaryDirectory() as t:
            out = Path(t)
            first = FakeClient(count=3, budget_at=2)
            self.assertFalse(collect(first, out, DATE))
            (out/'summaries/prec/3.json').write_text('{}')         # written, sha never recorded
            (out/'summaries/prec/2.json.tmp').parent.mkdir(parents=True, exist_ok=True)
            (out/'summaries/prec/2.json.tmp').write_text('{}')
            (out/'pages/prec/9.json').write_text('{}')             # page past next_page
            self.assertTrue(collect(FakeClient(count=3), out, DATE))
            (out/'summaries/prec/extra.json').write_text('{}')     # anything else is still tampering
            self.assertFalse(collect(FakeClient(count=3), out, DATE))
            self.assertEqual(strict_json((out/'attempt.json').read_bytes())['reason'], 'CACHE_INVENTORY_MISMATCH')

    def test_manifest_is_written_in_batches(self):
        writes = []
        import bblove_precedent_collect as pc
        real = pc.save_manifest
        def counting(path, value):
            writes.append(1)
            real(path, value)
        with tempfile.TemporaryDirectory() as t, patch.object(pc, 'save_manifest', counting):
            self.assertTrue(collect(FakeClient(count=250), Path(t), DATE, clock=lambda: 0.0))
        self.assertLessEqual(len(writes), 4)  # catalog completions + final, not one per document

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
                    dict(nts_rows=[{**NTS, '법령해석상세링크':'https://example.invalid/LSW/expcInfoP.do?expcSeq=2'}]),
                    dict(nts_rows=[{**NTS, '법령해석상세링크':'https://www.law.go.kr/LSW/expcInfoP.do?expcSeq=99'}])]
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
            self.assertTrue(collect(FakeClient(nts_rows=[{**NTS,'안건명':attack}]),source,DATE))
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
