"""Deterministic intake classification, not a legal applicability judgment."""
import re

from bblove_tax_p3_editions import page_text
from bblove_v107_pilot import text

TAX_LAWS = (
    '국세기본법', '국세징수법', '소득세법', '법인세법', '부가가치세법',
    '상속세및증여세법', '조세특례제한법', '종합부동산세법', '개별소비세법',
    '증권거래세법', '인지세법', '주세법', '관세법', '지방세법', '지방세기본법',
    '지방세징수법', '지방세특례제한법', '국제조세조정에관한법률',
    '조세범처벌법', '조세범처벌절차법', '교통·에너지·환경세법',
)
TAX_TITLE = re.compile(
    r'조세|세무조사|가산세|증여의제|경정청구|'
    r'(?:법인|소득|부가가치|상속|증여|종합부동산|취득|재산|등록면허|개별소비|증권거래|인지|관|주|지방|국)세')


def tax_reasons(row, metadata=None):
    metadata = metadata or {}
    kind = text(metadata, '사건종류명', '사건종류')
    title = page_text((row.get('title') or text(metadata, '사건명')).encode('utf-8'))
    refs = page_text((row.get('refs') or text(metadata, '참조조문')).encode('utf-8'))
    reasons = []
    # 국세법령정보시스템 lists only tax cases; its rows carry no 참조조문 to match on.
    if text(metadata, '데이터출처명') == '국세법령정보시스템':
        reasons.append('source')
    if '조세' in kind or '세무' in kind:
        reasons.append('case_kind')
    if TAX_TITLE.search(title):
        reasons.append('case_title')
    compact = re.sub(r'[\s·ㆍ]+', '', title + ' ' + refs)
    reasons.extend('law:' + law for law in TAX_LAWS if re.sub(r'[·ㆍ]', '', law) in compact)
    return reasons
