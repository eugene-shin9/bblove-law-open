import unittest
from probe_law_editions import compare

class ProbeTests(unittest.TestCase):
    def test_compare_is_read_only_and_selects_prior(self):
        class Fake:
            def __init__(self): self.calls=[]
            def get(self, endpoint, params):
                self.calls.append((endpoint,params))
                if endpoint=='lawSearch.do':
                    return {'LawSearch':{'law':[{'법령ID':'000623','법령일련번호':'123456','시행일자':'20250101','공포일자':'20241201'}]}},None
                return {'법령':{'기본정보':{'법령ID':'000623'},'조문':{'조문단위':{'조문번호':'1','조문여부':'Y','조문내용':'합성 본문'}}}},None
        c=Fake(); report=compare(c)
        self.assertEqual(len(c.calls),6)
        self.assertEqual(len(report),5)
        self.assertTrue(all(r['first_article_present'] for r in report))
        self.assertNotIn('합성 본문',str(report))
        self.assertEqual(c.calls[-1][1]['MST'],'123456')

    def test_errors_are_fixed_codes(self):
        class Fake:
            def get(self,*a,**k): raise ValueError('PRIVATE_RAW_ERROR')
        r=compare(Fake())
        self.assertNotIn('PRIVATE_RAW_ERROR',str(r))
        self.assertTrue(all(x['status']=='REQUEST_OR_RESPONSE_FAILED' for x in r))
