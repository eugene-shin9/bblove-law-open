"""Manual, main-only law-egress diagnostic; <=30 transport attempts, no persisted RAW.

For five current tax laws, fetch the catalog row (same target=law/nw=3 list the
collector uses) and the body four ways, and report only metadata, hashes and
whether a marker string from a recent amendment is present. No law text, request
URL, credential or tailnet detail is printed.
"""
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from zoneinfo import ZoneInfo
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from bblove_law_all import Client
from bblove_law_transport import measured_transport
from bblove_v107_pilot import records, text

CALL_LIMIT = 30
# (법령ID, 검색어, 조문번호, 가지번호, 표지 문자열); 표지가 None이면 대조군(해시 일치만 본다).
LAWS = (
    ('001561', '상속세 및 증여세법', 44, 0, '다자간매매체결회사'),
    ('001571', '부가가치세법', 21, 0, '2025.12.23'),
    ('001565', '소득세법', 118, 10, '국외전출자국내주식등'),
    ('001585', '국세징수법', 41, 0, '생계비계좌'),
    ('001583', '조세범 처벌법', None, None, None),
)
METHODS = ('law-mst', 'eflaw-mst-efyd', 'law-id', 'eflaw-id')


def method_params(method, lid, row):
    if method == 'law-mst':
        return {'target': 'law', 'MST': text(row, '법령일련번호')}
    if method == 'eflaw-mst-efyd':
        return {'target': 'eflaw', 'MST': text(row, '법령일련번호'), 'efYd': text(row, '시행일자')}
    if method == 'law-id':
        return {'target': 'law', 'ID': lid}
    return {'target': 'eflaw', 'ID': lid}


def _number(value):
    value = str(value or '').strip()
    return int(value) if re.fullmatch(r'\d+', value) else 0


def marker_present(units, number, branch, marker):
    """Marker anywhere in the article's unit (조문내용·항·호·목·개정 표시)."""
    hits = [u for u in units if text(u, '조문여부') != '전문'
            and _number(text(u, '조문번호')) == number and _number(text(u, '조문가지번호')) == branch]
    if not hits:
        return 'ARTICLE_MISSING'
    return '있음' if any(marker in json.dumps(u, ensure_ascii=False) for u in hits) else '없음'


def summarize(doc, lid, row, today, spec):
    law = doc.get('법령') if isinstance(doc, dict) else None
    if not isinstance(law, dict) or not isinstance(law.get('기본정보'), dict):
        return {'status': 'NO_LAW_IN_RESPONSE'}
    basic = law['기본정보']
    section = law.get('조문') if isinstance(law.get('조문'), dict) else {}
    units = records(section.get('조문단위'))
    articles = [u for u in units if text(u, '조문여부') not in ('N', '전문')]
    addenda = law.get('부칙') if isinstance(law.get('부칙'), dict) else {}
    effective = text(basic, '시행일자')
    mst = text(law, '법령일련번호') or text(basic, '법령일련번호')
    result = {'status': 'SUCCESS', 'id_matches': text(basic, '법령ID') == lid,
        'effective': effective, 'published': text(basic, '공포일자'), 'number': text(basic, '공포번호'),
        'effective_le_today': bool(re.fullmatch(r'\d{8}', effective)) and effective <= today,
        'mst_in_response': 'MATCH' if mst and row and mst == text(row, '법령일련번호')
                           else ('DIFFERENT' if mst else 'ABSENT'),
        'articles': len(articles), 'addenda': len(records(addenda.get('부칙단위'))),
        'body_sha256': hashlib.sha256(json.dumps(section, ensure_ascii=False, sort_keys=True)
                                      .encode('utf-8')).hexdigest()}
    if spec[4] is not None:
        result['marker'] = marker_present(units, spec[2], spec[3], spec[4])
    return result


def compare(client, today, remaining=lambda: CALL_LIMIT):
    """`remaining()` = transport attempts left; budget stops get their own fixed code."""
    results = []
    def fetch(endpoint, params):
        if remaining() <= 0:
            return None, 'NOT_RUN_CALL_LIMIT'
        try:
            doc, _ = client.get(endpoint, params, attempts=2)
            return doc, None
        except Exception as exc:
            if remaining() <= 0:
                return None, 'STOPPED_AT_CALL_LIMIT'
            if str(exc) == 'COLLECTION_BUDGET_EXHAUSTED':
                return None, 'STOPPED_AT_TIME_LIMIT'
            return None, 'REQUEST_OR_RESPONSE_FAILED'
    for spec in LAWS:
        lid, query = spec[0], spec[1]
        row = None
        doc, error = fetch('lawSearch.do', {'target': 'law', 'nw': '3', 'query': query,
                                            'display': 100, 'page': 1, 'sort': 'lasc'})
        if error:
            results.append({'law': lid, 'request': 'catalog', 'status': error})
        else:
            try:
                rows = [r for r in records(doc.get('LawSearch', {}).get('law')) if text(r, '법령ID') == lid]
            except Exception:
                rows = []
            row = rows[0] if len(rows) == 1 else None
            results.append({'law': lid, 'request': 'catalog', 'status': 'SUCCESS' if row else 'CATALOG_ROW_MISSING',
                **({'mst': text(row, '법령일련번호'), 'effective': text(row, '시행일자'),
                    'published': text(row, '공포일자')} if row else {})})
        for method in METHODS:
            if row is None and method in ('law-mst', 'eflaw-mst-efyd'):
                results.append({'law': lid, 'request': method, 'status': 'NOT_RUN_NO_CATALOG_ROW'})
                continue
            doc, error = fetch('lawService.do', method_params(method, lid, row))
            if error:
                result = {'status': error}
            else:
                try:
                    result = summarize(doc, lid, row, today, spec)
                except Exception:
                    result = {'status': 'REQUEST_OR_RESPONSE_FAILED'}
            results.append({'law': lid, 'request': method, **result})
    for spec in LAWS:
        if spec[4] is None:
            mine = [r for r in results if r['law'] == spec[0] and r['request'] in METHODS]
            base = next((r.get('body_sha256') for r in mine if r['request'] == 'law-mst'), None)
            for r in mine:
                # No baseline or no body: leave it unknown rather than "different".
                r['same_as_law_mst'] = (r['body_sha256'] == base) if base and r.get('body_sha256') else None
    return results


def table(results, attempts):
    head = ('| 법령ID | 조회 | 상태 | ID 일치 | 시행일자 | 시행일≤오늘 | 공포일자 | 공포번호 | 응답 MST | 조문 수 | 부칙 수 '
            '| 본문 sha256(앞 12자) | 표지 / ⓐ와 같음 |')
    lines = [f'### 현행 본문 조회 방식 비교 (전송 시도 {attempts}/{CALL_LIMIT}회)', '', head,
             '|' + '---|' * (head.count('|') - 1)]
    yes = {True: '예', False: '아니오', None: ''}
    for r in results:
        if r['request'] == 'catalog':
            lines.append(f"| {r['law']} | 목록 | {r['status']} | | {r.get('effective', '')} | | "
                         f"{r.get('published', '')} | | {r.get('mst', '')} | | | | |")
            continue
        last = r.get('marker', '')
        if 'same_as_law_mst' in r:
            last = yes[r['same_as_law_mst']] or '알 수 없음'
        lines.append(f"| {r['law']} | {r['request']} | {r['status']} | {yes[r.get('id_matches')]} | "
                     f"{r.get('effective', '')} | {yes[r.get('effective_le_today')]} | {r.get('published', '')} | "
                     f"{r.get('number', '')} | {r.get('mst_in_response', '')} | {r.get('articles', '')} | "
                     f"{r.get('addenda', '')} | {(r.get('body_sha256') or '')[:12]} | {last} |")
    return '\n'.join(lines) + '\n'


def capped(send):
    """Hard cap on transport attempts (retries included)."""
    attempts = 0
    def transport(request, timeout):
        nonlocal attempts
        if attempts >= CALL_LIMIT:
            raise ValueError('PROBE_CALL_LIMIT')
        attempts += 1
        return send(request, timeout)
    return transport, lambda: attempts


def main():
    credential = os.environ.get('LAW_OC', '')
    if not credential:
        print('LAW_OC_NOT_AVAILABLE')
        return 1
    transport, count = capped(measured_transport)
    today = datetime.now(ZoneInfo('Asia/Seoul')).strftime('%Y%m%d')
    result = compare(Client(credential, seconds=300, interval=1.0, transport=transport), today,
                     lambda: CALL_LIMIT - count())
    attempts = count()
    report = json.dumps({'transport_attempts': attempts, 'today': today, 'comparison': result},
                        ensure_ascii=False)
    summary = table(result, attempts)
    if credential.lower() in (report + summary).lower():
        print('PROBE_REPORT_REDACTED')
        return 1
    print(report)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as fh:
            fh.write(summary + '\n<details><summary>JSON</summary>\n\n```json\n' + report + '\n```\n</details>\n')
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
