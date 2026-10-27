"""Resumable summary-only DRF collection. CLI is live; tests inject Client transport.

Never persist complete responses: retain exact JSON key/value bytes ONLY for the
allowlisted summary fields. A manifest SHA covers those stored bytes, not the
discarded judgment. Existing Client, atomic save, strict JSON and date gates are
shared with the current-law collector.
"""
import argparse
import json
import os
from pathlib import Path
import re
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

from bblove_law_all import Client, save, stamp, today
from bblove_quality import credential_markers
from bblove_precedent_scope import tax_reasons
from bblove_storage import digest, strict_json
from bblove_tax_p3_editions import page_text
from bblove_v107_pilot import check_date, records, sanitized, text

TARGETS = ('prec', 'ntsCgmExpc', 'ttSpecialDecc')
FIELDS = {
    'prec': ('판례일련번호', '판례정보일련번호', '사건번호', '법원명', '선고일자',
             '사건명', '사건종류명', '사건종류', '판시사항', '판결요지', '참조조문'),
    'ntsCgmExpc': ('법령해석일련번호', '법령해석례일련번호', '일련번호', '안건번호',
                   '문서번호', '안건명', '제목', '회신일자', '해석일자', '생산일자',
                   '질의요지', '질의내용', '회신', '회신내용', '답변요지', '관련법령', '법령해석상세링크'),
    'ttSpecialDecc': ('특별행정심판재결례일련번호', '청구번호', '의결일자',
                      '재결구분명', '재결례유형명', '세목', '사건명', '재결요지',
                      '관련법령', '행정심판재결례상세링크'),
}
ID_KEYS = {'prec': ('판례일련번호', '판례정보일련번호'),
           'ntsCgmExpc': ('법령해석일련번호', '법령해석례일련번호', '일련번호'),
           'ttSpecialDecc': ('특별행정심판재결례일련번호',)}
ERROR_CODES = frozenset(('CATALOG_ROOT_INVALID', 'CATALOG_COUNT_CHANGED', 'CATALOG_ROW_INVALID',
    'CATALOG_DUPLICATE_ID', 'CATALOG_INCOMPLETE', 'ID_INVALID', 'DATE_FORMAT_INVALID',
    'TITLE_MISSING', 'CASE_OR_COURT_INVALID', 'INTERPRETATION_NUMBER_MISSING',
    'LIST_DETAIL_MISMATCH', 'SUMMARY_EMPTY', 'SUMMARY_OBJECT_MISSING', 'NONSCALAR_SUMMARY_FIELD',
    'CACHE_PATH_INVALID_OR_DUPLICATE', 'CACHE_PATH_INVALID', 'CACHE_SHA_MISMATCH',
    'CACHE_INVENTORY_MISMATCH', 'CACHE_ID_DUPLICATE_OR_MISMATCH', 'CREDENTIAL_MARKER',
    'MANIFEST_SCHEMA_INVALID', 'CACHE_SUMMARY_FIELDS_INVALID', 'CACHE_CATALOG_PAGE_GAP',
    'CACHE_CATALOG_TOTAL_MISMATCH', 'CACHE_CATALOG_DOCUMENT_MISMATCH',
    'INTERPRETATION_URL_INVALID', 'INTERPRETATION_URL_MISSING',
    'TRIBUNAL_NUMBER_INVALID', 'TRIBUNAL_METADATA_MISSING',
    'TRIBUNAL_URL_INVALID', 'TRIBUNAL_URL_MISSING',
    'CREDENTIAL_BEARING_SUMMARY', 'COLLECTION_BUDGET_EXHAUSTED', 'REQUEST_FAILED_AFTER_RETRIES'))


def members(raw):
    """Object members with original lexical JSON values, after strict decoding."""
    source = raw.decode('utf-8-sig')
    strict_json(raw)
    decoder, pos, result = json.JSONDecoder(), 0, {}
    def space(i):
        while i < len(source) and source[i].isspace():
            i += 1
        return i
    pos = space(pos)
    if source[pos] != '{':
        raise ValueError('OBJECT_REQUIRED')
    pos = space(pos + 1)
    while source[pos] != '}':
        start = pos
        key, end = decoder.raw_decode(source, pos)
        pos = space(end)
        if source[pos] != ':':
            raise ValueError('OBJECT_REQUIRED')
        pos = space(pos + 1)
        _, end = decoder.raw_decode(source, pos)
        result[key] = source[start:end].encode('utf-8')
        pos = space(end)
        if source[pos] == '}':
            break
        pos = space(pos + 1)
    return result


def summary_bytes(raw, target):
    """Find one summary object and preserve its allowed scalar value bytes."""
    doc = strict_json(raw)
    if not isinstance(doc, dict):
        raise ValueError('OBJECT_REQUIRED')
    if any(k in doc for k in ID_KEYS[target]):
        parts = members(raw)
        for k in FIELDS[target]:
            if isinstance(doc.get(k), (dict, list)):
                raise ValueError('NONSCALAR_SUMMARY_FIELD')
        return b'{' + b','.join(parts[k] for k in parts if k in FIELDS[target]) + b'}'
    candidates = []
    for key, value in doc.items():
        if isinstance(value, dict):
            # Locate the value span without reserializing its source bytes.
            part = members(raw)[key].decode('utf-8')
            _, end = json.JSONDecoder().raw_decode(part)
            child = part[end:].lstrip()[1:].lstrip().encode('utf-8')
            try:
                candidates.append(summary_bytes(child, target))
            except ValueError as exc:
                if str(exc) != 'SUMMARY_OBJECT_MISSING':
                    raise
    if len(candidates) != 1:
        raise ValueError('SUMMARY_OBJECT_MISSING')
    return candidates[0]


def identifier(row, target):
    values = {text(row, key) for key in ID_KEYS[target] if key in row}
    if len(values) != 1 or not all(re.fullmatch(r'[0-9]+', v) for v in values):
        raise ValueError('ID_INVALID')
    return values.pop()


def interpretation_url(row, sid):
    link = text(row, '법령해석상세링크')
    if not link:
        return ''  # Detail may omit the link; validated() uses the list's link.
    parts = urlsplit(urljoin('https://www.law.go.kr', link))
    query = parse_qs(parts.query)
    keys = [k for k in ('expcSeq', 'ntsCgmExpcSeq', 'ID') if query.get(k) == [sid]]
    if (parts.scheme not in ('http', 'https') or parts.netloc not in ('law.go.kr', 'www.law.go.kr')
            or not re.fullmatch(r'/LSW/[A-Za-z]+InfoP\.do', parts.path) or len(keys) != 1):
        raise ValueError('INTERPRETATION_URL_INVALID')
    return urlunsplit(('https', parts.netloc, parts.path, urlencode({keys[0]:sid}), ''))


def tribunal_url(row, sid):
    """Use a returned official permalink, never invent a tribunal page address.

    LSW links must carry the DRF ID. TT's own opaque IDs are not DRF IDs;
    retain its returned public decision link and compare list/detail links below.
    These URL shapes still require human confirmation against live API samples.
    """
    link = text(row, '행정심판재결례상세링크')
    if not link:
        return ''
    parts = urlsplit(urljoin('https://www.law.go.kr', link))
    query = parse_qs(parts.query, keep_blank_values=True)
    if (parts.scheme not in ('http', 'https') or parts.fragment
            or any(k.lower() in {'oc', 'token', 'access_token', 'api_key', 'password'} for k in query)):
        raise ValueError('TRIBUNAL_URL_INVALID')
    if parts.netloc in ('law.go.kr', 'www.law.go.kr'):
        keys = [k for k in ('deccSeq', 'specialDeccSeq', 'ttSpecialDeccSeq', 'ID') if k in query]
        if (not re.fullmatch(r'/LSW/[A-Za-z]*[dD]ecc[A-Za-z]*InfoP\.do', parts.path)
                or len(keys) != 1 or query[keys[0]] != [sid]):
            raise ValueError('TRIBUNAL_URL_INVALID')
        return urlunsplit(('https', parts.netloc, parts.path, urlencode({keys[0]: sid}), ''))
    if (parts.netloc not in ('tt.go.kr', 'www.tt.go.kr')
            or not re.fullmatch(r'/mUser/dem/[A-Za-z]+\.do', parts.path) or not query):
        raise ValueError('TRIBUNAL_URL_INVALID')
    return urlunsplit(('https', parts.netloc, parts.path, urlencode(sorted(query.items()), doseq=True), ''))


def normalize(row, target, date):
    sid = identifier(row, target)
    value = (text(row, '의결일자') if target == 'ttSpecialDecc' else
             text(row, '선고일자') if target == 'prec' else text(row, '회신일자', '해석일자', '생산일자'))
    if not re.fullmatch(r'\d{8}|\d{4}\.\d{2}\.\d{2}\.?|\d{4}-\d{2}-\d{2}', value):
        raise ValueError('DATE_FORMAT_INVALID')
    day = re.sub(r'\D', '', value)
    check_date(day, 'summary', date)
    title = text(row, '사건명') if target in ('prec', 'ttSpecialDecc') else text(row, '안건명', '제목')
    if not title:
        raise ValueError('TITLE_MISSING')
    if target == 'prec':
        number, court = text(row, '사건번호'), text(row, '법원명')
        if not re.search(r'\d{2,4}[가-힣]+\s*\d+', number) or not court.endswith(('법원', '지원')):
            raise ValueError('CASE_OR_COURT_INVALID')
        return dict(id=sid, case_no=re.sub(r'\s', '', number), court=court, date=day,
                    title=title, issues=text(row, '판시사항'), holding=text(row, '판결요지'),
                    refs=text(row, '참조조문'), url=f'https://www.law.go.kr/LSW/precInfoP.do?precSeq={sid}')
    if target == 'ttSpecialDecc':
        number = re.sub(r'\s', '', text(row, '청구번호'))
        if not re.fullmatch(r'[가-힣]*\d{2,4}[가-힣]+\d+', number):
            raise ValueError('TRIBUNAL_NUMBER_INVALID')
        return dict(id=sid, number=number, date=day, title=title,
                    decision_type=text(row, '재결구분명', '재결례유형명'), tax_type=text(row, '세목'),
                    summary=text(row, '재결요지'), laws=text(row, '관련법령'), url=tribunal_url(row, sid))
    number = text(row, '안건번호', '문서번호')
    if not number:
        raise ValueError('INTERPRETATION_NUMBER_MISSING')
    return dict(id=sid, number=number, title=title, date=day,
                question=text(row, '질의요지', '질의내용'), answer=text(row, '회신', '회신내용', '답변요지'),
                laws=text(row, '관련법령'),
                url=interpretation_url(row, sid))


def validated(row, detail, target, date, listing_only=False):
    listed, result = normalize(row, target, date), normalize(detail, target, date)
    keys = ('id', 'case_no', 'court', 'date') if target == 'prec' else ('id', 'number', 'date')
    if any(listed[k] != result[k] for k in keys):
        raise ValueError('LIST_DETAIL_MISMATCH')
    if target == 'ntsCgmExpc':
        result['url'] = result['url'] or listed['url']
        if not result['url']:
            raise ValueError('INTERPRETATION_URL_MISSING')
    if target == 'ttSpecialDecc':
        for key in ('title', 'decision_type', 'tax_type', 'summary', 'laws'):
            listed[key] = page_text(listed[key].encode('utf-8'))
            result[key] = page_text(result[key].encode('utf-8'))
        if (listed['title'] != result['title'] or any(listed[k] and result[k] != listed[k]
                for k in ('decision_type', 'tax_type'))
                or (listed['url'] and result['url'] and listed['url'] != result['url'])):
            raise ValueError('LIST_DETAIL_MISMATCH')
        result['url'] = result['url'] or listed['url']
        if not result['url']:
            raise ValueError('TRIBUNAL_URL_MISSING')
        if not result['title'] or not result['decision_type'] or not result['tax_type']:
            raise ValueError('TRIBUNAL_METADATA_MISSING')
        if not result['summary']:
            raise ValueError('SUMMARY_EMPTY')
        return result
    fields = ('title', 'issues', 'holding', 'refs') if target == 'prec' else ('title', 'question', 'answer', 'laws')
    for key in fields:
        result[key] = page_text(result[key].encode('utf-8'))
    if not result['title']:
        raise ValueError('TITLE_MISSING')
    if not listing_only and not any(result[k].strip() for k in
            (('issues', 'holding') if target == 'prec' else ('question', 'answer'))):
        raise ValueError('SUMMARY_EMPTY')
    return result


def verify_cache(out, manifest):
    """Check the entire inventory before any network request or SQL build."""
    if manifest.get('schema') != 'precedent-summary-v1':
        raise ValueError('MANIFEST_SCHEMA_INVALID')
    expected, seen = set(), set()
    for item in manifest.get('pages', []) + manifest.get('documents', []):
        if 'sha256' not in item:
            continue
        path = item['path']
        if not re.fullmatch(r'(?:pages|summaries)/(?:prec|ntsCgmExpc|ttSpecialDecc)/[0-9]+\.json', path) or path in expected:
            raise ValueError('CACHE_PATH_INVALID_OR_DUPLICATE')
        expected.add(path)
        file = out / path
        if file.is_symlink() or not file.resolve().is_relative_to(out.resolve()):
            raise ValueError('CACHE_PATH_INVALID')
        raw = file.read_bytes()
        if digest(raw) != item['sha256']:
            raise ValueError('CACHE_SHA_MISMATCH')
        if credential_markers(raw):
            raise ValueError('CREDENTIAL_MARKER')
        value = strict_json(raw)
        if path.startswith('summaries/') and (not isinstance(value, dict) or set(value) - set(FIELDS[item['target']])):
            raise ValueError('CACHE_SUMMARY_FIELDS_INVALID')
    actual = {str(p.relative_to(out)) for folder in ('pages', 'summaries')
              for p in (out / folder).rglob('*') if p.is_file()}
    if actual != expected:
        raise ValueError('CACHE_INVENTORY_MISMATCH')
    listed = {}
    for target, state in manifest.get('catalogs', {}).items():
        pages = [p for p in manifest.get('pages', []) if p['target'] == target]
        if [p['page'] for p in pages] != list(range(1, state['next_page'])):
            raise ValueError('CACHE_CATALOG_PAGE_GAP')
        for page in pages:
            value = strict_json((out / page['path']).read_bytes())
            if value['totalCnt'] != state['total']:
                raise ValueError('CACHE_CATALOG_TOTAL_MISMATCH')
            for row in value['rows']:
                key = (target, identifier(row, target))
                if key in listed:
                    raise ValueError('CACHE_ID_DUPLICATE_OR_MISMATCH')
                listed[key] = row
        if state['complete'] and sum(k[0] == target for k in listed) != state['total']:
            raise ValueError('CACHE_CATALOG_TOTAL_MISMATCH')
    for e in manifest.get('documents', []):
        key = (e['target'], e['id'])
        if (key in seen or identifier(e['row'], e['target']) != e['id']
                or listed.get(key) != e['row']):
            raise ValueError('CACHE_ID_DUPLICATE_OR_MISMATCH')
        seen.add(key)
        if 'sha256' in e and e['path'] != f"summaries/{e['target']}/{e['id']}.json":
            raise ValueError('CACHE_PATH_INVALID')
        if e['status'] == 'SUCCESS':
            validated(e['row'], strict_json((out / e['path']).read_bytes()),
                      e['target'], manifest['date'], e['listing_only'])
    if set(listed) != seen:
        raise ValueError('CACHE_CATALOG_DOCUMENT_MISMATCH')


def put_bytes(out, path, raw):
    file = out / path
    file.parent.mkdir(parents=True, exist_ok=True)
    tmp = file.with_suffix('.tmp')
    tmp.write_bytes(raw)
    tmp.replace(file)
    return digest(raw)


def count_catalogs(client):
    """Exactly one page per target (transport retries remain Client's policy).

    Only numeric counts and fixed statuses leave memory; no response bodies,
    snippets, links, authenticated URLs or credentials are written.
    """
    counts = {}
    for target in TARGETS:
        try:
            doc, _ = client.get('lawSearch.do', dict(target=target, display=1, page=1))
            roots = [v for v in doc.values() if isinstance(v, dict) and 'totalCnt' in v]
            value = text(roots[0], 'totalCnt') if len(roots) == 1 else ''
            if not re.fullmatch(r'[0-9]+', value):
                raise ValueError('COUNT_INVALID')
            counts[target] = dict(status='SUCCESS', total=int(value))
        except Exception:
            counts[target] = dict(status='FAILED', reason='COUNT_REQUEST_OR_RESPONSE_FAILED')
    return dict(mode='count', counted_at=stamp(), counts=counts,
                status='COMPLETE' if all(counts[t]['status'] == 'SUCCESS' for t in TARGETS) else 'INCOMPLETE')


class CollectionPaused(Exception):
    pass


def collect(client, out, date, scope='all', stop=None, account=None):
    if scope not in ('tax', 'all'):
        raise ValueError('SCOPE_INVALID')
    out = Path(out)
    checkpoint = out / 'manifest.json'
    manifest = strict_json(checkpoint.read_bytes()) if checkpoint.exists() else {
        'schema': 'precedent-summary-v1', 'date': date, 'pages': [], 'documents': [], 'catalogs': {}}
    try:
        verify_cache(out, manifest)
        manifest.pop('stop_reason', None)
        if account:
            for entry in manifest['documents']:
                if entry['status'] == 'SUCCESS':
                    account(entry, validated(entry['row'], strict_json((out / entry['path']).read_bytes()),
                                             entry['target'], date, entry['listing_only']))
        def check_budget():
            reason = stop() if stop else None
            if reason:
                raise CollectionPaused(reason)
        for target in TARGETS:
            state = manifest['catalogs'].setdefault(target, {'next_page': 1, 'complete': False})
            while not state['complete']:
                check_budget()
                page = state['next_page']
                doc, _ = client.get('lawSearch.do', dict(target=target, display=100, page=page))
                roots = [v for v in doc.values() if isinstance(v, dict) and 'totalCnt' in v]
                if len(roots) != 1:
                    raise ValueError('CATALOG_ROOT_INVALID')
                root = roots[0]
                total = int(text(root, 'totalCnt'))
                if total < 0 or state.get('total', total) != total:
                    raise ValueError('CATALOG_COUNT_CHANGED')
                raw_rows = root.get(target, root.get('decc' if target == 'ttSpecialDecc' else 'expc', []))
                batch = records(raw_rows)
                if isinstance(raw_rows, list) and len(raw_rows) != len(batch):
                    raise ValueError('CATALOG_ROW_INVALID')
                rows = [{k: r[k] for k in FIELDS[target] if k in r} for r in batch]
                known = {e['id'] for e in manifest['documents'] if e['target'] == target}
                for row in rows:
                    if target == 'ttSpecialDecc' and any(isinstance(v, (dict, list)) for v in row.values()):
                        raise ValueError('NONSCALAR_SUMMARY_FIELD')
                    sid = identifier(row, target)
                    if sid in known:
                        raise ValueError('CATALOG_DUPLICATE_ID')
                    normalize(row, target, date)
                    known.add(sid)
                if len(known) > total or (len(known) < total and len(rows) != 100) or (not rows and total):
                    raise ValueError('CATALOG_INCOMPLETE')
                # Catalogs are sanitized projections. Detail values below retain
                # exact lexical source bytes; neither cache can contain full text.
                _, raw = sanitized({'rows': rows, 'totalCnt': total}, getattr(client, 'credential', ''))
                if credential_markers(raw):
                    raise ValueError('CREDENTIAL_MARKER')
                rows = strict_json(raw)['rows']  # Manifest and page share the sanitized projection.
                path = f'pages/{target}/{page}.json'
                sha = put_bytes(out, path, raw)
                manifest['pages'].append(dict(target=target, page=page, path=path, sha256=sha))
                manifest['documents'].extend(dict(target=target, id=identifier(r, target), row=r,
                    status='PENDING') for r in rows)
                state.update(total=total, next_page=page + 1, complete=len(known) == total)
                save(checkpoint, manifest)
        # Prioritize likely tax cases, NTS entries and tax tribunals. Other list
        # entries still need detail refs before the SQL scope decision is final.
        entries = manifest['documents']
        if scope == 'tax':
            entries = sorted(entries, key=lambda e: not (e['target'] in ('ntsCgmExpc', 'ttSpecialDecc') or tax_reasons({}, e['row'])))
        for entry in entries:
            target, sid = entry['target'], entry['id']
            if entry['status'] == 'SUCCESS':
                continue
            try:
                if 'sha256' not in entry:
                    check_budget()
                    doc, raw = client.get('lawService.do', dict(target=target, ID=sid), original=True)
                    # A successful empty response is the only automatic fallback.
                    # Transport/auth/rate-limit failures and malformed details fail.
                    listing_only = target == 'ntsCgmExpc' and (doc == {} or doc == {'NtsCgmExpcService': {}})
                    if listing_only:
                        raw = json.dumps(entry['row'], ensure_ascii=False).encode('utf-8')
                    raw = summary_bytes(raw, target)
                    clean, _ = sanitized(strict_json(raw), getattr(client, 'credential', ''))
                    if clean != strict_json(raw) or credential_markers(raw):
                        raise ValueError('CREDENTIAL_BEARING_SUMMARY')
                    path = f'summaries/{target}/{sid}.json'
                    entry.update(path=path, sha256=put_bytes(out, path, raw), listing_only=listing_only)
                else:
                    raw = (out / entry['path']).read_bytes()
                result = validated(entry['row'], strict_json(raw), target, date, entry['listing_only'])
                entry.update(status='SUCCESS')
                entry.pop('reason', None)
                if account:
                    account(entry, result)
            except CollectionPaused:
                raise
            except Exception as exc:
                # Fixed/allowlisted codes only: never transport exception strings.
                code = str(exc)
                reason = code if code in ERROR_CODES else 'DETAIL_VALIDATION_OR_REQUEST_FAILED'
                entry.update(status='FAILED', reason=reason)
                if reason == 'COLLECTION_BUDGET_EXHAUSTED':
                    if stop:
                        entry.update(status='PENDING')
                        entry.pop('reason', None)
                        raise CollectionPaused('TIME_BUDGET') from None
                    break
            save(checkpoint, manifest)
        manifest.update(status='COMPLETE' if all(e['status'] == 'SUCCESS' for e in manifest['documents']) else 'INCOMPLETE',
                        updated_at=stamp(), api_calls=client.calls)
        save(checkpoint, manifest)
        return manifest['status'] == 'COMPLETE'
    except Exception as exc:
        if isinstance(exc, CollectionPaused) or (stop and str(exc) == 'COLLECTION_BUDGET_EXHAUSTED'):
            manifest.update(status='INCOMPLETE', stop_reason=str(exc) if isinstance(exc, CollectionPaused) else 'TIME_BUDGET',
                            updated_at=stamp(), api_calls=client.calls)
            save(checkpoint, manifest)
            return not any(e['status'] == 'FAILED' for e in manifest['documents'])
        reason = str(exc) if str(exc) in ERROR_CODES else 'CATALOG_OR_CACHE_FAILED'
        save(out / 'attempt.json', dict(status='FAILED', reason=reason, api_calls=client.calls))
        return False


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--out', type=Path, default=Path('release/precedent-collection'))
    ap.add_argument('--scope', choices=('tax', 'all'), default='tax')
    args = ap.parse_args()
    credential = os.environ.get('LAW_OC', '')
    if not credential:
        print('LAW_OC_NOT_AVAILABLE')
        return 1
    ok = collect(Client(credential), args.out, today(), args.scope)
    print('COLLECTION_COMPLETE' if ok else 'COLLECTION_INCOMPLETE')
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
