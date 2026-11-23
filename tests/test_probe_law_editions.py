from copy import deepcopy
from pathlib import Path
import sys
import unittest
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'src'))
from probe_law_editions import CALL_LIMIT, LAWS, capped, compare, table, wording

TODAY = '20261123'
ROWS = {s[0]: (str(270000 + i), '20260102', '20251001') for i, s in enumerate(LAWS)}
ARTICLE = {s[0]: (str(s[2]), str(s[3]) if s[3] else '', s[4]) for s in LAWS if s[4]}


def body(lid, target):
    """law target: stale edition (no marker, one addendum fewer). Field order differs by target."""
    no, branch, marker = ARTICLE.get(lid, ('1', '', None))
    fresh = target == 'eflaw'
    unit = {'조문번호': no, '조문가지번호': branch, '조문여부': '조문', '조문내용': f'제{no}조(합성) 합성 본문',
            '항': [{'항내용': '① 합성 항 ' + (marker if fresh and marker else ''),
                    '호': [{'호내용': '1. 합성 호', '목': {'목내용': ['가. 합성 목']}}]}]}
    if fresh:  # eflaw: extra metadata and another key order; same wording
        unit = {'조문시행일자': '20260102', **dict(reversed(list(unit.items())))}
        unit['항'][0]['항내용'] = '  ' + unit['항'][0]['항내용'].replace(' ', '   ') + ' '
    heading = {'조문번호': no, '조문여부': '전문', '조문내용': '제1장 합성 ' + (marker or '')}
    addenda = [{'부칙공포일자': '20201229', '부칙공포번호': '17761', '부칙내용': '합성 부칙'}]
    if fresh and marker:
        addenda.append({'부칙공포일자': '20251223', '부칙공포번호': '21221', '부칙내용': '합성 부칙'})
    return {'법령': {'기본정보': {'법령ID': lid, '시행일자': ROWS[lid][1], '공포일자': ROWS[lid][2], '공포번호': '1'},
                     '조문': {'조문단위': [heading, unit]}, '부칙': {'부칙단위': addenda}}}


class Fake:
    def __init__(self, fail=False, tweak=None):
        self.calls, self.fail, self.tweak = [], fail, tweak

    def get(self, endpoint, params, **kw):
        self.calls.append((endpoint, dict(params), kw))
        if self.fail:
            raise ValueError('PRIVATE_RAW_ERROR https://example.invalid/?OC=secret')
        if endpoint == 'lawSearch.do':
            rows = [{'법령ID': lid, '법령일련번호': m, '시행일자': e, '공포일자': p} for lid, (m, e, p) in ROWS.items()]
            rows.append({'법령ID': '009999', '법령일련번호': '1', '시행일자': '20200101', '공포일자': '20200101'})
            return {'LawSearch': {'law': rows}}, None
        lid = next(k for k, v in ROWS.items() if v[0] == params['MST'])
        doc = body(lid, params['target'])
        if self.tweak:
            self.tweak(lid, params['target'], doc)
        return doc, None


class ProbeTests(unittest.TestCase):
    def test_eight_laws_two_methods_within_limit(self):
        c = Fake()
        results, comparisons, gate = compare(c, TODAY)
        self.assertEqual(len(c.calls), 24)
        self.assertLessEqual(len(c.calls), CALL_LIMIT)
        self.assertTrue(all(kw.get('attempts') == 2 for _, _, kw in c.calls))
        self.assertTrue(all(p['target'] == 'law' and p['nw'] == '3' for e, p, _ in c.calls if e == 'lawSearch.do'))
        bodies = [p for e, p, _ in c.calls if e == 'lawService.do']
        self.assertEqual(bodies[:2], [{'target': 'law', 'MST': '270000'},
                                      {'target': 'eflaw', 'MST': '270000', 'efYd': '20260102'}])
        by = {(r['law'], r['request']): r for r in results}
        for lid in ARTICLE:
            self.assertEqual(by[(lid, 'law-mst')]['marker'], '없음')
            self.assertEqual(by[(lid, 'eflaw-mst-efyd')]['marker'], '있음')
        self.assertEqual(ARTICLE['001565'], ('59', '2', '2026.4.21'))
        diff = {c['law']: c for c in comparisons}
        for lid in ('001583', '009873', '001649', '011311'):
            self.assertTrue(diff[lid]['control'])
            # Field order, extra metadata and whitespace do not change the wording hash.
            self.assertTrue(diff[lid]['wording_equal'], lid)
            self.assertEqual(by[(lid, 'law-mst')]['wording_sha256'], by[(lid, 'eflaw-mst-efyd')]['wording_sha256'])
        self.assertEqual(diff['001561']['changed_articles'], ['제44조'])
        self.assertEqual(diff['001565']['changed_articles'], ['제59조의2'])
        self.assertEqual(diff['001561']['addenda_only_in_eflaw'], ['20251223/21221'])
        self.assertEqual(diff['001561']['addenda_only_in_law_mst'], [])
        self.assertTrue(gate['stage2_method_b'])
        rendered = str(results) + str(comparisons) + table(results, comparisons, gate, 24)
        self.assertNotIn('합성', rendered)
        self.assertIn('24/30', rendered)

    def test_wording_ignores_fields_and_order_but_not_text(self):
        unit = {'조문내용': '제1조 본문', '항': {'항내용': '① 항', '호': [{'호내용': '1. 호'}]}, '조문시행일자': '1'}
        moved = {'항': {'호': [{'호내용': '1.  호'}], '항내용': '① 항'}, '조문내용': ' 제1조 본문', '조문이동': 'x'}
        self.assertEqual(wording(unit), wording(moved))
        changed = deepcopy(unit)
        changed['항']['호'][0]['호내용'] = '1. 호 <개정 2026.4.21>'
        self.assertNotEqual(wording(unit), wording(changed))

    def test_unreadable_bodies_never_pass_as_equal(self):
        def empty(lid, target, doc):
            if lid == '001583':
                doc['법령']['조문']['조문단위'] = []
        results, comparisons, gate = compare(Fake(tweak=empty), TODAY)
        self.assertTrue({c['law']: c for c in comparisons}['001583']['wording_equal'])
        self.assertFalse(gate['bodies_readable'])
        self.assertFalse(gate['stage2_method_b'])
        def no_addenda(lid, target, doc):
            if lid == '011311':
                doc['법령']['부칙'] = {}
        self.assertFalse(compare(Fake(tweak=no_addenda), TODAY)[2]['stage2_method_b'])

    def test_unexpected_shapes_are_counted_not_dropped(self):
        issues = []
        a = wording({'조문내용': '제1조', '항': [[{'항내용': '① A'}]]}, issues)
        self.assertEqual(issues, ['children'])
        self.assertIn('① A', a)  # still read, and flagged
        issues = []
        wording({'조문내용': {'내용': '제1조'}}, issues)
        self.assertEqual(issues, ['content'])
        def wrapped(lid, target, doc):
            if lid == '009873':
                unit = doc['법령']['조문']['조문단위'][1]
                unit['항'] = [unit['항']]
        results, _, gate = compare(Fake(tweak=wrapped), TODAY)
        self.assertEqual({(r['law'], r['request']): r for r in results}[('009873', 'law-mst')]['shape_issues'], 1)
        self.assertFalse(gate['bodies_readable'])
        self.assertFalse(gate['stage2_method_b'])
        def blank(lid, target, doc):
            if lid == '001649' and target == 'eflaw':
                doc['법령']['조문']['조문단위'][1] = {'조문번호': '1', '조문여부': '조문', '조문내용': ' '}
        self.assertFalse(compare(Fake(tweak=blank), TODAY)[2]['bodies_readable'])

    def test_annexes_on_one_side_are_reported(self):
        def annex(lid, target, doc):
            if lid == '001561':
                units = [{'별표번호': '0001', '별표가지번호': '00', '별표구분': '별표', '별표내용': '합성 별표'}]
                if target == 'law':
                    units.append({'별표번호': '0002', '별표가지번호': '00', '별표구분': '별표', '별표내용': '합성 별표'})
                doc['법령']['별표'] = {'별표단위': units}
        results, comparisons, gate = compare(Fake(tweak=annex), TODAY)
        diff = {c['law']: c for c in comparisons}['001561']
        self.assertEqual(diff['annexes_only_in_law_mst'], ['0002-00-별표'])
        self.assertEqual(diff['annexes_only_in_eflaw'], [])
        rendered = table(results, comparisons, gate, 24)
        self.assertIn('0002-00-별표', rendered)
        self.assertNotIn('합성', rendered)

    def test_gate_stops_on_control_difference(self):
        def tweak(lid, target, doc):
            if lid == '001649' and target == 'eflaw':
                doc['법령']['조문']['조문단위'][1]['조문내용'] += ' 다름'
        results, comparisons, gate = compare(Fake(tweak=tweak), TODAY)
        diff = {c['law']: c for c in comparisons}
        self.assertFalse(diff['001649']['wording_equal'])
        self.assertEqual(diff['001649']['changed_articles'], ['제1조'])
        self.assertFalse(gate['controls_wording_equal'])
        self.assertFalse(gate['stage2_method_b'])

    def test_gate_stops_when_eflaw_drops_an_addendum(self):
        def tweak(lid, target, doc):
            if lid == '009873' and target == 'eflaw':
                doc['법령']['부칙']['부칙단위'] = []
        results, comparisons, gate = compare(Fake(tweak=tweak), TODAY)
        diff = {c['law']: c for c in comparisons}
        self.assertEqual(diff['009873']['addenda_only_in_law_mst'], ['20201229/17761'])
        self.assertFalse(gate['no_addenda_missing_in_eflaw'])
        self.assertFalse(gate['stage2_method_b'])
        self.assertIn('20201229/17761', table(results, comparisons, gate, 24))

    def test_gate_stops_when_a_marker_is_missing(self):
        def tweak(lid, target, doc):
            if lid == '001565' and target == 'eflaw':
                doc['법령']['조문']['조문단위'][1]['항'][0]['항내용'] = '① 합성 항'
        _, _, gate = compare(Fake(tweak=tweak), TODAY)
        self.assertFalse(gate['markers_present_in_eflaw'])
        self.assertFalse(gate['stage2_method_b'])

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
        results, comparisons, gate = compare(Flaky(), TODAY, lambda: CALL_LIMIT - count())
        self.assertEqual(count(), CALL_LIMIT)
        by = {(r['law'], r['request']): r['status'] for r in results}
        self.assertEqual(by[('001583', 'eflaw-mst-efyd')], 'STOPPED_AT_CALL_LIMIT')  # 15th request
        self.assertEqual(by[('001583', 'law-mst')], 'SUCCESS')
        self.assertEqual(by[('009873', 'catalog')], 'NOT_RUN_CALL_LIMIT')
        self.assertNotIn('REQUEST_OR_RESPONSE_FAILED', by.values())
        self.assertIn({'law': '001583', 'control': True, 'status': 'NOT_COMPARABLE'}, comparisons)
        self.assertFalse(gate['stage2_method_b'])

    def test_time_limit_is_its_own_code(self):
        from bblove_tax_p2_collect import PilotBlocked
        class Late(Fake):
            def get(self, *a, **k):
                raise PilotBlocked('COLLECTION_BUDGET_EXHAUSTED')
        results, _, gate = compare(Late(), TODAY)
        self.assertEqual({r['status'] for r in results}, {'STOPPED_AT_TIME_LIMIT', 'NOT_RUN_NO_CATALOG_ROW'})
        self.assertFalse(gate['stage2_method_b'])

    def test_errors_are_fixed_codes(self):
        c = Fake(fail=True)
        results, comparisons, gate = compare(c, TODAY)
        rendered = str(results) + str(comparisons) + table(results, comparisons, gate, 8)
        self.assertNotIn('PRIVATE_RAW_ERROR', rendered)
        self.assertNotIn('secret', rendered)
        self.assertEqual(len(c.calls), 8)  # body by MST needs the catalog row
        self.assertEqual({r['status'] for r in results}, {'REQUEST_OR_RESPONSE_FAILED', 'NOT_RUN_NO_CATALOG_ROW'})


if __name__ == '__main__':
    unittest.main()
