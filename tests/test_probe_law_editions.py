from pathlib import Path
import sys
import unittest
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'src'))
from probe_law_editions import CALL_LIMIT, LAWS, METHODS, capped, compare, table

ROWS = {'001561': ('276123', '20260102', '20251001'), '001571': ('276117', '20260102', '20251001'),
        '001565': ('280405', '20260701', '20251223'), '001585': ('280377', '20261001', '20260602'),
        '001583': ('270000', '20250101', '20241231')}
ARTICLE = {'001561': ('44', ''), '001571': ('21', ''), '001565': ('118', '10'), '001585': ('41', '')}


class Fake:
    """Synthetic DRF: target=law by MST is stale (no marker); eflaw and ID lookups carry it."""
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def get(self, endpoint, params, **kw):
        self.calls.append((endpoint, dict(params), kw))
        if self.fail:
            raise ValueError('PRIVATE_RAW_ERROR https://example.invalid/?OC=secret')
        if endpoint == 'lawSearch.do':
            rows = [{'법령ID': lid, '법령일련번호': m, '시행일자': e, '공포일자': p}
                    for lid, (m, e, p) in ROWS.items()]
            rows.append({'법령ID': '009999', '법령일련번호': '1', '시행일자': '20200101', '공포일자': '20200101'})
            return {'LawSearch': {'law': rows}}, None
        lid = params.get('ID') or next(k for k, v in ROWS.items() if v[0] == params['MST'])
        stale = params['target'] == 'law' and 'MST' in params and lid != '001583'
        no, branch = ARTICLE.get(lid, ('1', ''))
        marker = next((s[4] for s in LAWS if s[0] == lid), None) or ''
        unit = {'조문번호': no, '조문가지번호': branch, '조문여부': '조문',
                '조문내용': '합성 본문', '항': [{'항내용': '합성 항 ' + ('' if stale else marker)}]}
        decoy = {'조문번호': no, '조문여부': '전문', '조문내용': marker}
        # A by-ID eflaw lookup may answer with a not-yet-effective (시행 예정) edition.
        effective = '20270101' if (lid, params['target'], 'ID' in params) == ('001585', 'eflaw', True) else ROWS[lid][1]
        return {'법령': {'기본정보': {'법령ID': lid, '시행일자': effective, '공포일자': ROWS[lid][2],
                                      '공포번호': '1'},
                         '조문': {'조문단위': [decoy, unit]}, '부칙': {'부칙단위': [{'부칙내용': '합성'}]}}}, None


class ProbeTests(unittest.TestCase):
    def test_five_laws_four_methods_within_limit(self):
        c = Fake()
        report = compare(c, '20261123')
        self.assertEqual(len(c.calls), 25)
        # Up to 2 attempts per request; the transport cap (capped) bounds the total at 30.
        self.assertTrue(all(kw.get('attempts') == 2 for _, _, kw in c.calls))
        self.assertEqual(len(report), 25)
        searches = [p for e, p, _ in c.calls if e == 'lawSearch.do']
        self.assertTrue(all(p['target'] == 'law' and p['nw'] == '3' for p in searches))
        bodies = [(p.get('target'), tuple(sorted(k for k in p if k != 'target')))
                  for e, p, _ in c.calls if e == 'lawService.do']
        self.assertEqual(bodies[:4], [('law', ('MST',)), ('eflaw', ('MST', 'efYd')), ('law', ('ID',)), ('eflaw', ('ID',))])
        efyd = next(p for e, p, _ in c.calls if p.get('target') == 'eflaw' and 'MST' in p)
        self.assertEqual((efyd['MST'], efyd['efYd']), ('276123', '20260102'))
        by = {(r['law'], r['request']): r for r in report}
        for lid in ARTICLE:
            self.assertEqual(by[(lid, 'law-mst')]['marker'], '없음')
            for m in METHODS[1:]:
                self.assertEqual(by[(lid, m)]['marker'], '있음', (lid, m))
                self.assertTrue(by[(lid, m)]['id_matches'])
        self.assertFalse(by[('001585', 'eflaw-id')]['effective_le_today'])
        self.assertTrue(by[('001561', 'eflaw-id')]['effective_le_today'])
        self.assertTrue(all(by[('001583', m)]['same_as_law_mst'] for m in METHODS))
        self.assertNotIn('marker', by[('001583', 'law-mst')])
        self.assertEqual(by[('001561', 'law-mst')]['mst_in_response'], 'ABSENT')
        rendered = str(report) + table(report, 25)
        self.assertNotIn('합성', rendered)
        self.assertIn('25/30', table(report, 25))

    def test_transport_cap_is_thirty(self):
        sent = []
        transport, count = capped(lambda request, timeout: sent.append(request) or 'ok')
        for i in range(CALL_LIMIT):
            self.assertEqual(transport(i, 1), 'ok')
        with self.assertRaisesRegex(ValueError, 'PROBE_CALL_LIMIT'):
            transport('extra', 1)
        self.assertEqual((CALL_LIMIT, len(sent), count()), (30, 30, 30))

    def test_call_limit_stops_are_not_reported_as_method_failures(self):
        from bblove_tax_p2_collect import PilotBlocked
        flaky = {'n': 0}
        def send(request, timeout):
            flaky['n'] += 1
            if flaky['n'] % 2:
                raise OSError('transient')
            return 'ok'
        transport, count = capped(send)
        class Flaky(Fake):
            """Every request needs two transport attempts (first one fails)."""
            def get(self, endpoint, params, **kw):
                for _ in range(kw['attempts']):
                    try:
                        transport('r', 1)
                    except OSError:
                        continue
                    except ValueError:
                        break
                    return super().get(endpoint, params, **kw)
                self.calls.append((endpoint, dict(params), kw))
                raise PilotBlocked('REQUEST_FAILED_AFTER_RETRIES')
        # One attempt already spent (it succeeds), so request 15 straddles the cap.
        flaky['n'] = 1
        transport('spent', 1)
        c = Flaky()
        report = compare(c, '20261123', lambda: CALL_LIMIT - count())
        self.assertEqual(count(), CALL_LIMIT)
        by = {(r['law'], r['request']): r['status'] for r in report}
        self.assertEqual(by[('001565', 'eflaw-id')], 'STOPPED_AT_CALL_LIMIT')
        self.assertEqual(by[('001565', 'law-id')], 'SUCCESS')
        self.assertTrue(all(by[(lid, m)] == 'NOT_RUN_CALL_LIMIT' for lid in ('001585', '001583')
                            for m in ('catalog', 'law-id', 'eflaw-id')))
        self.assertNotIn('REQUEST_OR_RESPONSE_FAILED', by.values())
        control = [r for r in report if r['law'] == '001583' and r['request'] in METHODS]
        self.assertTrue(all(r['same_as_law_mst'] is None for r in control))
        self.assertIn('알 수 없음', table(report, count()))

    def test_time_limit_is_its_own_code(self):
        from bblove_tax_p2_collect import PilotBlocked
        class Late(Fake):
            def get(self, *a, **k):
                raise PilotBlocked('COLLECTION_BUDGET_EXHAUSTED')
        self.assertEqual({r['status'] for r in compare(Late(), '20261123')},
                         {'STOPPED_AT_TIME_LIMIT', 'NOT_RUN_NO_CATALOG_ROW'})

    def test_errors_are_fixed_codes(self):
        c = Fake(fail=True)
        report = compare(c, '20261123')
        rendered = str(report) + table(report, 5)
        self.assertNotIn('PRIVATE_RAW_ERROR', rendered)
        self.assertNotIn('secret', rendered)
        self.assertEqual(len(c.calls), 15)  # body by MST needs the catalog row
        self.assertEqual({r['status'] for r in report}, {'REQUEST_OR_RESPONSE_FAILED', 'NOT_RUN_NO_CATALOG_ROW'})


if __name__ == '__main__':
    unittest.main()
