"""국세법령정보시스템 case-number spellings (rule_version 3); synthetic transport only."""
import json
from pathlib import Path
import tempfile
import unittest
from test_precedent_d1 import FakeClient, DATE, PREC
from bblove_precedent_collect import collect, listed, nts_court_case, save_manifest, verify_cache

# Real spellings from the 2026-11-05 failure list (public law.go.kr listing data).
CASES = {
    '서울고법-97-구-21422': ('서울고등법원', '97구21422'),
    '서울고등-92-구-14863': ('서울고등법원', '92구14863'),
    '대구고법-96-구-9301': ('대구고등법원', '96구9301'),
    '전주부-2010-누-479': ('광주고등법원(전주)', '2010누479'),
    '광주고등법원전주부-2006-누-601': ('광주고등법원(전주)', '2006누601'),
    '광주고등법원제주부-2008-누-0046': ('광주고등법원(제주)', '2008누46'),
    '부산고등법원-창원-2019-누-11562': ('부산고등법원(창원)', '2019누11562'),
    '대전고등법원(청주)-2023-재누-16(2023.11.30.)': ('대전고등법원(청주)', '2023재누16'),
    '서울고등법원-(춘천)2023-누-35': ('서울고등법원(춘천)', '2023누35'),
    '대전고등법원(△△)-2018-누-907': ('대전고등법원', '2018누907'),
    '서울행정법원-201-4재구합94': ('서울행정법원', '2014재구합94'),
    '부산지방법원-20-24구합-20131': ('부산지방법원', '2024구합20131'),
    '서울중앙지방법원-2024-가단-5506074(2025.5.13.)': ('서울중앙지방법원', '2024가단5506074'),
    '서울중앙지방법원2024가단5444558(2025.3.13)': ('서울중앙지방법원', '2024가단5444558'),
    '대법원-2025-두-34741 (2026.01.29)': ('대법원', '2025두34741'),
    '수원지방법원성남지원-2023-가합-402000사해행위취소': ('수원지방법원성남지원', '2023가합402000'),
    '수원지방법원-성남지원-2024-가단-224835': ('수원지방법원성남지원', '2024가단224835'),
    '부산지방법원동부지원-2006-가단-35621배당이의': ('부산지방법원동부지원', '2006가단35621'),
    '광주지방법원-2006-가단-106373호': ('광주지방법원', '2006가단106373'),
    '대구고등법원-96-구-8865판결': ('대구고등법원', '96구8865'),
    '서울행정법원-2023-구합-757,764(병합)': ('서울행정법원', '2023구합757'),
    '전주지방법원행정부-2005-구합-1951': ('전주지방법원', '2005구합1951'),
    '○○지방법원2009구합2710': ('○○지방법원', '2009구합2710'),
    'OO지방법원-2014-구합-1989': ('OO지방법원', '2014구합1989'),
    '2023구합53621': ('법원 미기재', '2023구합53621'),
    '2024가합101943': ('법원 미기재', '2024가합101943'),
}
# Not guessed: a place name alone, a number with no case type, a garbled year.
REJECTED = ['춘천-2025-가단-30760', '서울고등법원-2023-73016', '서울행정법원구합-1035-1',
            '춘천지방법원-20274-구합*318', '', '대법원',
            # 'ㄴ' could be a 지방법원 항소부 case: a bare 재판부 name is read only with 고등법원 codes (누·구).
            '제주부-2007-나-1205',
            # A hyphenated tail is never glued onto the serial.
            '서울행정법원-2023-구합-757-764(병합)', '서울고등법원-2023-누-1234-1', '서울고등법원-2023-누-1234사건',
            # 재판부 under the wrong 고등법원.
            '서울고등법원전주부-2010-누-479', '부산고등법원-청주-2019-누-1',
            # Impossible years and serial 0.
            '서울고법-202-9구-1', '서울고법-24-20구-1', '서울고법-2023-누-0', '1947구1']
# The strict form (unchanged, so cached rows stay valid) still reads these; the fallback alone must not.
FALLBACK_REJECTED = ['서울행정법원-202-9구합1', '서울고등법원-2023-누-0']


def nts_row(number, sid='900'):
    return {**PREC, '판례일련번호': sid, '사건번호': number, '법원명': '', '데이터출처명': '국세법령정보시스템',
            '사건명': '필요경비 불산입'}


class NtsCaseNumberTests(unittest.TestCase):
    def test_observed_spellings(self):
        for raw, expected in CASES.items():
            with self.subTest(raw=raw):
                self.assertEqual(nts_court_case(raw), expected)
                self.assertEqual((listed(nts_row(raw), 'prec')['court'], listed(nts_row(raw), 'prec')['case_no']), expected)
        for raw in FALLBACK_REJECTED:
            with self.subTest(raw=raw):
                self.assertIsNone(nts_court_case(raw, '', '2026'))
        for raw in REJECTED:
            with self.subTest(raw=raw):
                self.assertIsNone(nts_court_case(raw, '', '2026'))
                with self.assertRaisesRegex(ValueError, 'CASE_OR_COURT_INVALID'):
                    listed(nts_row(raw), 'prec')

    def test_year_must_not_follow_the_decision(self):
        self.assertIsNone(nts_court_case('서울고법-2027-구-1', '', '2026'))
        self.assertEqual(nts_court_case('서울고법-2026-구-1', '', '2026'), ('서울고등법원', '2026구1'))
        row = {**nts_row('서울고법-2027-구-1'), '선고일자': '2026.01.02'}
        with self.assertRaisesRegex(ValueError, 'CASE_OR_COURT_INVALID'):
            listed(row, 'prec')

    def test_listed_court_column(self):
        self.assertEqual(nts_court_case('2023구합53621', '대전지방법원'), ('대전지방법원', '2023구합53621'))
        self.assertEqual(nts_court_case('대전지법-2022-구합-295', '대전지방법원'), ('대전지방법원', '2022구합295'))
        self.assertIsNone(nts_court_case('대전지법-2022-구합-295', '서울행정법원'))

    def test_strict_form_wins_and_other_sources_unchanged(self):
        # Rows accepted before keep exactly the same court/case_no.
        r = listed(nts_row('부산고등법원(창원)-2015-누-10783'), 'prec')
        self.assertEqual((r['court'], r['case_no']), ('부산고등법원(창원)', '2015누10783'))
        r = listed({**PREC, '사건번호': '2099두7'}, 'prec')
        self.assertEqual((r['court'], r['case_no']), ('대법원', '2099두7'))
        # The fallback is only for 국세법령정보시스템 rows.
        with self.assertRaisesRegex(ValueError, 'CASE_OR_COURT_INVALID'):
            listed({**PREC, '법원명': '', '사건번호': '서울고법-97-구-21422'}, 'prec')

    def test_failed_rows_reread_once_without_requests(self):
        with tempfile.TemporaryDirectory() as t:
            out = Path(t)
            rows = [nts_row('서울고법-97-구-21422', '901'), nts_row('서울고등법원-2023-73016', '902')]
            collect(FakeClient(extra_prec=rows), out, DATE)
            m = json.loads((out/'manifest.json').read_text())
            entries = {e['id']: e for e in m['documents'] if e['target'] == 'prec'}
            # Simulate the state before this rule: both listing rows failed.
            for sid in ('901', '902'):
                e = entries[sid]
                self.assertEqual(e['kind'], 'listing')
                for key in ('sha256', 'path'):
                    e.pop(key, None)
                e.update(status='FAILED', reason='CASE_OR_COURT_INVALID')
            m['rule_version'] = 2
            save_manifest(out/'manifest.json', m)
            client = FakeClient(extra_prec=rows)
            collect(client, out, DATE, incremental=True)
            self.assertEqual(client.details, 0)
            m = json.loads((out/'manifest.json').read_text())
            entries = {e['id']: e for e in m['documents'] if e['target'] == 'prec'}
            self.assertEqual(m['rule_version'], 3)
            self.assertEqual(m['court_case_reread'], 2)
            self.assertEqual(entries['901']['status'], 'SUCCESS')
            self.assertEqual((entries['902']['status'], entries['902']['reason']), ('FAILED', 'CASE_OR_COURT_INVALID'))
            verify_cache(out, m)
            # Once only: a later run does not reset the remaining failure again.
            collect(FakeClient(extra_prec=rows), out, DATE, incremental=True)
            m2 = json.loads((out/'manifest.json').read_text())
            self.assertEqual(m2['court_case_reread'], 2)
            self.assertEqual({e['id']: e['status'] for e in m2['documents'] if e['id'] == '902'}, {'902': 'FAILED'})


if __name__ == '__main__':
    unittest.main()
