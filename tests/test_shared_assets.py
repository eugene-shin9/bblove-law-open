"""Offline checks for the shared addenda dependency."""
import sys
from pathlib import Path
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from bblove_addenda_assets import extract

class AddendaTests(unittest.TestCase):
    def test_nested_text_preserved(self):
        doc = {'법령': {'부칙': {'부칙단위': {'부칙공포일자': '20200101',
            '부칙공포번호': '00100', '부칙내용': [['시행일', '경과조치']]}}}}
        rows = extract(doc, '20261108')
        self.assertEqual(rows[0][:2], ['2020-01-01', '00100'])
        self.assertIn('시행일', rows[0][2])
        self.assertIn('경과조치', rows[0][2])

    def test_empty_body_rejected(self):
        doc = {'법령': {'부칙': {'부칙단위': {'부칙공포일자': '20200101',
            '부칙공포번호': '00100', '부칙내용': []}}}}
        with self.assertRaisesRegex(ValueError, 'ADDENDA_EMPTY'):
            extract(doc, '20261108')
