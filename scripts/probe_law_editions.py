"""Manual, main-only law-egress diagnostic; <=10 transport attempts, no persisted RAW."""
import hashlib
import json
import os
from pathlib import Path
import re
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from bblove_law_all import Client
from bblove_law_transport import measured_transport
from bblove_v107_pilot import records, text


def compare(client):
    results = []
    def request(label, params):
        try:
            doc, _ = client.get('lawService.do', params)
            law = doc.get('법령', {})
            basic = law.get('기본정보', {})
            units = records(law.get('조문', {}).get('조문단위'))
            actual = [u for u in units if text(u, '조문여부') not in ('N', '전문')]
            body = '\n'.join(text(u, '조문내용') for u in units)
            result = {'request': label, 'status': 'SUCCESS', 'units': len(units), 'articles': len(actual),
                'first_article_present': any(text(u, '조문번호') in ('1', '0001') for u in actual),
                'body_sha256': hashlib.sha256(body.encode()).hexdigest(),
                'repeal_only': len(units) == 1 and '폐지한다' in body,
                'id_matches': text(basic, '법령ID') == '000623'}
        except Exception:
            result = {'request': label, 'status': 'REQUEST_OR_RESPONSE_FAILED'}
        results.append(result)
    request('law-current-mst', {'target': 'law', 'MST': '276071'})
    request('eflaw-current-mst', {'target': 'eflaw', 'MST': '276071', 'efYd': '20260102'})
    request('eflaw-id-asof', {'target': 'eflaw', 'ID': '000623', 'efYd': '20260102'})
    try:
        doc, _ = client.get('lawSearch.do', {'target': 'eflaw', 'LID': '000623', 'nw': '1', 'display': 100, 'page': 1})
        rows = records(doc.get('LawSearch', {}).get('law'))
        rows = [r for r in rows if text(r, '법령ID') == '000623' and
                re.fullmatch(r'\d+', text(r, '법령일련번호')) and text(r, '법령일련번호') != '276071'
                and re.fullmatch(r'\d{8}', text(r, '시행일자')) and text(r, '시행일자') <= '20260102']
        if rows:
            prior = max(rows, key=lambda r: (text(r, '시행일자'), text(r, '공포일자'), int(text(r, '법령일련번호'))))
            request('law-prior-mst', {'target': 'law', 'MST': text(prior, '법령일련번호')})
            request('eflaw-prior-mst', {'target': 'eflaw', 'MST': text(prior, '법령일련번호'), 'efYd': text(prior, '시행일자')})
        else:
            results.append({'request': 'prior-catalog', 'status': 'NO_PRIOR_EDITION'})
    except Exception:
        results.append({'request': 'prior-catalog', 'status': 'REQUEST_OR_RESPONSE_FAILED'})
    return results


def main():
    credential = os.environ.get('LAW_OC', '')
    if not credential:
        print('LAW_OC_NOT_AVAILABLE')
        return 1
    attempts = 0
    def transport(request, timeout):
        nonlocal attempts
        if attempts >= 10:
            raise ValueError('PROBE_CALL_LIMIT')
        attempts += 1
        return measured_transport(request, timeout)
    result = compare(Client(credential, seconds=300, interval=1.0, transport=transport))
    report = json.dumps({'transport_attempts': attempts, 'comparison': result,
        'applicability': 'REVIEW_REQUIRED; prior articles do not prove current applicability'}, ensure_ascii=False)
    if credential.lower() in report.lower():
        print('PROBE_REPORT_REDACTED')
        return 1
    print(report)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as fh:
            fh.write('```json\n' + report + '\n```\n')
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
