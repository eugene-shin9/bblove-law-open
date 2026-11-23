"""Manual, main-only law-egress diagnostic; <=30 transport attempts, no persisted RAW.

For eight current laws, fetch the catalog row (same target=law/nw=3 list the
collector uses) and the body two ways: (a) target=law&MST and (b)
target=eflaw&MST&efYd=<catalog effective date>. Report only metadata, a hash of
the article wording (조문·항·호·목 text only), the article labels whose wording
differs, a marker present/absent, and the addenda (공포일자·공포번호) found on one
side only. No law text, request URL, credential or tailnet detail is printed.
"""
from collections import Counter
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
# (법령ID, 검색어, 조문번호, 가지번호, 표지 문자열); 표지가 None이면 대조군(ⓐ=ⓑ 문안 일치를 본다).
LAWS = (
    ('001561', '상속세 및 증여세법', 44, 0, '다자간매매체결회사'),
    ('001571', '부가가치세법', 21, 0, '2025.12.23'),
    ('001565', '소득세법', 59, 2, '2026.4.21'),
    ('001585', '국세징수법', 41, 0, '생계비계좌'),
    ('001583', '조세범 처벌법', None, None, None),
    ('009873', '종합부동산세법', None, None, None),
    ('001649', '지방세법', None, None, None),
    ('011311', '지방세특례제한법 시행규칙', None, None, None),
)
METHODS = ('law-mst', 'eflaw-mst-efyd')
DIFF_LIST_LIMIT = 30
# Wording fields, in a fixed order that does not depend on the response's key order.
CONTENT = ('조문내용', '항내용', '호내용', '목내용')
CHILDREN = ('항', '호', '목')


def method_params(method, row):
    if method == 'law-mst':
        return {'target': 'law', 'MST': text(row, '법령일련번호')}
    return {'target': 'eflaw', 'MST': text(row, '법령일련번호'), 'efYd': text(row, '시행일자')}


def _number(value):
    value = str(value or '').strip()
    return int(value) if re.fullmatch(r'\d+', value) else 0


def _strings(value, issues):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item, issues)
    elif value is not None:
        issues.append('content')  # dict/number where text was expected: cannot compare it


def _children(value, issues):
    """항·호·목 children; any non-dict item (e.g. a list wrapped once more) is a shape issue."""
    if value is None or value == '':
        return []
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list) and all(isinstance(v, dict) for v in value):
        return value
    issues.append('children')
    found = []
    def dig(v):  # still read what is there, but the law is flagged as not comparable
        if isinstance(v, dict):
            found.append(v)
        elif isinstance(v, list):
            for item in v:
                dig(item)
    dig(value)
    return found


def wording(node, issues=None):
    """조문·항·호·목 text only, whitespace collapsed; field names, order and metadata excluded.

    Shapes the walk cannot read losslessly are appended to `issues` (never ignored silently).
    """
    issues = [] if issues is None else issues
    parts = []
    def walk(n):
        for key in CONTENT:
            parts.extend(' '.join(s.split()) for s in _strings(n.get(key), issues) if s.strip())
        for key in CHILDREN:
            for child in _children(n.get(key), issues):
                walk(child)
    walk(node)
    return '\n'.join(parts)


def labelled(units, issues):
    """[(label, wording)] in source order; label is the article number (or heading position)."""
    result, seen = [], Counter()
    for unit in units:
        no, branch = _number(text(unit, '조문번호')), _number(text(unit, '조문가지번호'))
        label = f'제{no}조' + (f'의{branch}' if branch else '')
        if text(unit, '조문여부') == '전문':
            label = '전문(' + label + ')'
        seen[label] += 1
        if seen[label] > 1:
            label += f'#{seen[label]}'
        words = wording(unit, issues)
        if not words and text(unit, '조문여부') != '전문' and not (
                text(unit, '조문제목') == '삭제' or text(unit, '조문삭제여부') in {'Y', '삭제'}):
            issues.append('empty_article')
        result.append((label, words))
    return result


def marker_present(units, number, branch, marker):
    hits = [u for u in units if text(u, '조문여부') != '전문'
            and _number(text(u, '조문번호')) == number and _number(text(u, '조문가지번호')) == branch]
    if not hits:
        return 'ARTICLE_MISSING'
    return '있음' if any(marker in wording(u) for u in hits) else '없음'


def addenda_keys(law):
    section = law.get('부칙') if isinstance(law.get('부칙'), dict) else {}
    return [(text(u, '부칙공포일자'), text(u, '부칙공포번호')) for u in records(section.get('부칙단위'))]


def summarize(doc, lid, row, today, spec):
    law = doc.get('법령') if isinstance(doc, dict) else None
    if not isinstance(law, dict) or not isinstance(law.get('기본정보'), dict):
        return {'status': 'NO_LAW_IN_RESPONSE'}, None
    basic = law['기본정보']
    section = law.get('조문') if isinstance(law.get('조문'), dict) else {}
    issues = []
    units = _children(section.get('조문단위'), issues)
    pairs = labelled(units, issues)
    annex = law.get('별표') if isinstance(law.get('별표'), dict) else {}
    annexes = [(text(u, '별표번호'), text(u, '별표가지번호'), text(u, '별표구분')) for u in records(annex.get('별표단위'))]
    effective = text(basic, '시행일자')
    addenda = addenda_keys(law)
    mst = text(law, '법령일련번호') or text(basic, '법령일련번호')
    result = {'status': 'SUCCESS', 'id_matches': text(basic, '법령ID') == lid,
        'effective': effective, 'published': text(basic, '공포일자'), 'number': text(basic, '공포번호'),
        'effective_le_today': bool(re.fullmatch(r'\d{8}', effective)) and effective <= today,
        'mst_in_response': ('MATCH' if mst == text(row, '법령일련번호') else 'DIFFERENT') if mst else 'ABSENT',
        'articles': sum(text(u, '조문여부') not in ('N', '전문') for u in units),
        'addenda': len(addenda), 'annexes': len(annexes), 'shape_issues': len(issues),
        'wording_sha256': hashlib.sha256('\n'.join(w for _, w in pairs).encode('utf-8')).hexdigest()}
    if spec[4] is not None:
        result['marker'] = marker_present(units, spec[2], spec[3], spec[4])
    return result, {'pairs': pairs, 'addenda': addenda, 'annexes': annexes}


def difference(a, b):
    """Labels whose wording differs (or exists on one side), and addenda on one side only."""
    left, right = dict(a['pairs']), dict(b['pairs'])
    order = [label for label, _ in a['pairs']] + [label for label, _ in b['pairs'] if label not in left]
    changed = [label for label in order if left.get(label) != right.get(label)]
    ca, cb = Counter(a['addenda']), Counter(b['addenda'])
    def listed(counter):
        return [f'{d}/{n}' for (d, n), k in sorted(counter.items()) for _ in range(k)]
    xa, xb = Counter(a['annexes']), Counter(b['annexes'])
    return {'wording_equal': not changed, 'changed_articles_count': len(changed),
            'annexes_only_in_law_mst': ['-'.join(k) for k in sorted((xa - xb).elements())],
            'annexes_only_in_eflaw': ['-'.join(k) for k in sorted((xb - xa).elements())],
            'changed_articles': changed[:DIFF_LIST_LIMIT],
            'addenda_only_in_law_mst': listed(ca - cb), 'addenda_only_in_eflaw': listed(cb - ca)}


def compare(client, today, remaining=lambda: CALL_LIMIT):
    """`remaining()` = transport attempts left; budget stops get their own fixed code."""
    results, comparisons = [], []
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
        detail = {}
        for method in METHODS:
            if row is None:
                results.append({'law': lid, 'request': method, 'status': 'NOT_RUN_NO_CATALOG_ROW'})
                continue
            doc, error = fetch('lawService.do', method_params(method, row))
            result = {'status': error}
            if not error:
                try:
                    result, detail[method] = summarize(doc, lid, row, today, spec)
                except Exception:
                    result = {'status': 'REQUEST_OR_RESPONSE_FAILED'}
            results.append({'law': lid, 'request': method, **result})
        if all(detail.get(m) for m in METHODS):
            comparisons.append({'law': lid, 'control': spec[4] is None, **difference(*(detail[m] for m in METHODS))})
        else:
            comparisons.append({'law': lid, 'control': spec[4] is None, 'status': 'NOT_COMPARABLE'})
    return results, comparisons, verdict(results, comparisons)


def verdict(results, comparisons):
    """Stage-2 gate for method (b): every criterion must hold."""
    b = {r['law']: r for r in results if r['request'] == 'eflaw-mst-efyd'}
    markers = all(b.get(s[0], {}).get('marker') == '있음' for s in LAWS if s[4] is not None)
    bound = all(r.get('status') == 'SUCCESS' and r.get('id_matches') and r.get('effective_le_today')
                for r in b.values()) and len(b) == len(LAWS)
    # "Read nothing" or "read the same broken shape on both sides" must never pass as "equal".
    bodies = [r for r in results if r['request'] in METHODS]
    readable = (len(bodies) == 2 * len(LAWS) and all(
        r.get('status') == 'SUCCESS' and r.get('articles', 0) > 0 and r.get('shape_issues') == 0 for r in bodies)
        and all(r.get('addenda', 0) > 0 for r in bodies if r['law'] in
                {s[0] for s in LAWS if s[4] is None}))
    controls = all(c.get('wording_equal') is True for c in comparisons if c['control'])
    addenda = all('status' not in c and not c['addenda_only_in_law_mst'] for c in comparisons)
    checks = {'bodies_readable': readable, 'markers_present_in_eflaw': markers, 'eflaw_id_and_date_ok': bound,
              'controls_wording_equal': controls, 'no_addenda_missing_in_eflaw': addenda}
    return {**checks, 'stage2_method_b': all(checks.values())}


def table(results, comparisons, gate, attempts):
    yes = {True: '예', False: '아니오', None: ''}
    lines = [f'### 현행 본문 조회 ⓐ law+MST / ⓑ eflaw+MST+efYd 비교 (전송 시도 {attempts}/{CALL_LIMIT}회)', '',
             '| 법령ID | 조회 | 상태 | ID 일치 | 시행일자 | 시행일≤오늘 | 공포일자 | 공포번호 | 응답 MST | 조문 수 | 부칙 수 '
             '| 별표 수 | 형태 이상 | 문안 sha256(앞 12자) | 표지 |', '|' + '---|' * 15]
    for r in results:
        if r['request'] == 'catalog':
            lines.append(f"| {r['law']} | 목록 | {r['status']} | | {r.get('effective', '')} | | "
                         f"{r.get('published', '')} | | {r.get('mst', '')} | | | | | | |")
            continue
        name = 'ⓐ law-mst' if r['request'] == 'law-mst' else 'ⓑ eflaw-mst-efyd'
        lines.append(f"| {r['law']} | {name} | {r['status']} | {yes[r.get('id_matches')]} | "
                     f"{r.get('effective', '')} | {yes[r.get('effective_le_today')]} | {r.get('published', '')} | "
                     f"{r.get('number', '')} | {r.get('mst_in_response', '')} | {r.get('articles', '')} | "
                     f"{r.get('addenda', '')} | {r.get('annexes', '')} | {r.get('shape_issues', '')} | "
                     f"{(r.get('wording_sha256') or '')[:12]} | {r.get('marker', '')} |")
    lines += ['', '| 법령ID | 대조군 | 문안 ⓐ=ⓑ | 문안이 다른 조문 | ⓐ에만 있는 부칙(공포일자/번호) | ⓑ에만 있는 부칙 '
              '| ⓐ에만 있는 별표 | ⓑ에만 있는 별표 |', '|---|---|---|---|---|---|---|---|']
    for c in comparisons:
        if 'status' in c:
            lines.append(f"| {c['law']} | {yes[c['control']]} | {c['status']} | | | | | |")
            continue
        changed = ', '.join(c['changed_articles'])
        if c['changed_articles_count'] > len(c['changed_articles']):
            changed += f" 외 {c['changed_articles_count'] - len(c['changed_articles'])}개"
        lines.append(f"| {c['law']} | {yes[c['control']]} | {yes[c['wording_equal']]} | {changed} | "
                     f"{', '.join(c['addenda_only_in_law_mst'])} | {', '.join(c['addenda_only_in_eflaw'])} | "
                     f"{', '.join(c['annexes_only_in_law_mst'])} | {', '.join(c['annexes_only_in_eflaw'])} |")
    lines += ['', '2단계(ⓑ) 기준: ' + ', '.join(f'{k}={yes[v]}' for k, v in gate.items())]
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
    results, comparisons, gate = compare(Client(credential, seconds=300, interval=1.0, transport=transport),
                                         today, lambda: CALL_LIMIT - count())
    attempts = count()
    report = json.dumps({'transport_attempts': attempts, 'today': today, 'comparison': results,
                         'differences': comparisons, 'gate': gate}, ensure_ascii=False)
    summary = table(results, comparisons, gate, attempts)
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
