"""Resumable summary-only DRF collection. CLI is live; tests inject Client transport.

Live law.go.kr shapes (probes 2026-10-29) decide what each entry can be:
- prec rows from 데이터출처명 '대법원' have a JSON detail (판시사항·판결요지) -> kind 'detail';
  rows from '국세법령정보시스템' have no DRF detail (case number '대법원-2025-두-34754',
  사건명 is a one-line gist) -> kind 'listing' (the list row itself is stored);
  every other source (e.g. 근로복지공단산재판례 with empty numbers) -> kind 'skipped'.
- ntsCgmExpc has no DRF detail at all -> always 'listing'.
- ttSpecialDecc has a JSON detail -> 'detail'.
A 'detail' entry whose detail says "not found" falls back to 'listing' when the list row
carries a title; otherwise it fails with DETAIL_NOT_FOUND.

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
import time
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

from bblove_law_all import Client, save, stamp, today
from bblove_quality import credential_markers
from bblove_precedent_scope import tax_reasons
from bblove_storage import digest, strict_json
from bblove_tax_p3_editions import page_text
from bblove_v107_pilot import check_date, records, sanitized, text

TARGETS = ('prec', 'ntsCgmExpc', 'ttSpecialDecc')
FIELDS = {
    'prec': ('판례일련번호', '판례정보일련번호', '사건번호', '법원명', '선고일자', '데이터출처명',
             '사건명', '사건종류명', '사건종류', '판시사항', '판결요지', '참조조문'),
    'ntsCgmExpc': ('법령해석일련번호', '법령해석례일련번호', '일련번호', '안건번호',
                   '문서번호', '안건명', '제목', '회신일자', '해석일자', '생산일자',
                   '질의요지', '질의내용', '회신', '회신내용', '답변요지', '관련법령', '법령해석상세링크'),
    'ttSpecialDecc': ('특별행정심판재결례일련번호', '청구번호', '의결일자',
                      '재결구분명', '재결례유형명', '세목', '사건명', '재결요지', '관련법령', '주문'),
}
# Row array key inside the lawSearch.do root, as law.go.kr returns it (probe 2026-10-29:
# PrecSearch.prec, CgmExpc.cgmExpc, Decc.decc). The target name is accepted first.
ROW_KEYS = {'prec': ('prec',), 'ntsCgmExpc': ('cgmExpc', 'expc'), 'ttSpecialDecc': ('decc',)}
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
    'CREDENTIAL_BEARING_SUMMARY', 'COLLECTION_BUDGET_EXHAUSTED', 'REQUEST_FAILED_AFTER_RETRIES',
    'DATE_IN_FUTURE', 'DETAIL_NOT_FOUND', 'SOURCE_NOT_COLLECTED', 'CACHE_KIND_INVALID'))
SCHEMA = 'precedent-summary-v2'
PREC_KINDS = {'대법원': {'detail', 'listing'}, '국세법령정보시스템': {'listing'}}
# 국세법령정보시스템 rows put the court into the number: '대법원-2025-두-34754',
# '대법원2026두31124', '서울행정법원2025구합55369', '부산고등법원(창원)-2015-누-10783'.
COURT_CASE = re.compile(r'(?P<court>[가-힣]+(?:법원|지원)(?:\([가-힣]+\))?)-?(?P<year>\d{2,4})-?(?P<code>[가-힣]+)-?(?P<serial>\d+)')
SAVE_EVERY_SECONDS = 60


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


def default_kind(row, target):
    """Decided from the list row only, so verify_cache can re-derive it."""
    if target == 'ntsCgmExpc':
        return 'listing'
    if target == 'ttSpecialDecc':
        return 'detail'
    kinds = PREC_KINDS.get(text(row, '데이터출처명'))
    return ('detail' if 'detail' in kinds else 'listing') if kinds else 'skipped'


def allowed_kinds(row, target):
    if target == 'ntsCgmExpc':
        return {'listing'}
    if target == 'ttSpecialDecc':
        return {'detail', 'listing'}
    return PREC_KINDS.get(text(row, '데이터출처명'), {'skipped'})


def interpretation_url(row):
    """Return the public interpretation page, or '' when the row has no link."""
    link = text(row, '법령해석상세링크')
    if not link:
        return ''
    parts = urlsplit(urljoin('https://www.law.go.kr', link))
    query = parse_qs(parts.query, keep_blank_values=True)
    if parts.scheme not in ('http', 'https') or parts.fragment:
        raise ValueError('INTERPRETATION_URL_INVALID')
    if parts.netloc == 'taxlaw.nts.go.kr':
        doc = query.get('ntstDcmId', [''])
        if (parts.path != '/qt/USEQTA002P.do' or set(query) != {'ntstDcmId'}
                or len(doc) != 1 or not re.fullmatch(r'[0-9]{6,30}', doc[0])):
            raise ValueError('INTERPRETATION_URL_INVALID')
        return urlunsplit(('https', parts.netloc, parts.path, urlencode({'ntstDcmId': doc[0]}), ''))
    sid = identifier(row, 'ntsCgmExpc')
    keys = [k for k in ('expcSeq', 'ntsCgmExpcSeq', 'ID') if query.get(k) == [sid]]
    if (parts.netloc not in ('law.go.kr', 'www.law.go.kr')
            or not re.fullmatch(r'/LSW/[A-Za-z]+InfoP\.do', parts.path) or len(keys) != 1
            or any(k.lower() == 'oc' for k in query)):
        raise ValueError('INTERPRETATION_URL_INVALID')
    return urlunsplit(('https', parts.netloc, parts.path, urlencode({keys[0]: sid}), ''))


def catalog_rows(root, target):
    for key in (target,) + ROW_KEYS[target]:
        if key in root:
            return root[key]
    return []


def row_date(row, target):
    value = (text(row, '의결일자') if target == 'ttSpecialDecc' else
             text(row, '선고일자') if target == 'prec' else text(row, '해석일자', '회신일자', '생산일자'))
    if not re.fullmatch(r'\d{8}|\d{4}\.\d{2}\.\d{2}\.?|\d{4}-\d{2}-\d{2}', value):
        raise ValueError('DATE_FORMAT_INVALID')
    day = re.sub(r'\D', '', value)
    try:
        check_date(day, 'summary')
    except ValueError:
        raise ValueError('DATE_FORMAT_INVALID') from None
    return day


def case_numbers(value):
    """Every case number in a (possibly merged) field, spacing and hyphens removed."""
    compact = re.sub(r'[\s-]', '', COURT_CASE.sub(lambda m: m['year'] + m['code'] + m['serial'], value or ''))
    return set(re.findall(r'\d{2,4}[가-힣]+\d+', compact))


def listed(row, target):
    """Entry-level checks of one list row (no request). Never decides the date gate."""
    sid, day = identifier(row, target), row_date(row, target)
    if target == 'prec':
        number = re.sub(r'\s', '', text(row, '사건번호'))
        prefixed = COURT_CASE.fullmatch(number)
        if prefixed:
            court, case_no = prefixed['court'], prefixed['year'] + prefixed['code'] + prefixed['serial']
        else:
            court, case_no = text(row, '법원명'), number
        if not court or not case_numbers(case_no):
            raise ValueError('CASE_OR_COURT_INVALID')
        title = page_text(text(row, '사건명').encode('utf-8'))
        if not title:
            raise ValueError('TITLE_MISSING')
        return dict(id=sid, case_no=case_no, court=court, date=day, title=title, issues='', holding='',
                    refs='', url=f'https://www.law.go.kr/LSW/precInfoP.do?precSeq={sid}')
    if target == 'ttSpecialDecc':
        number = re.sub(r'\s', '', text(row, '청구번호'))
        if not re.search(r'\d', number):
            raise ValueError('TRIBUNAL_NUMBER_INVALID')
        return dict(id=sid, number=number, date=day, decision_type=text(row, '재결례유형명', '재결구분명'),
                    tax_type=text(row, '세목'), title=page_text(text(row, '사건명').encode('utf-8')),
                    summary='', laws='', url='')
    number = text(row, '안건번호', '문서번호')
    if not number:
        raise ValueError('INTERPRETATION_NUMBER_MISSING')
    title = page_text(text(row, '안건명', '제목').encode('utf-8'))
    if not title:
        raise ValueError('TITLE_MISSING')
    return dict(id=sid, number=number, title=title, date=day,
                question=page_text(text(row, '질의요지', '질의내용').encode('utf-8')),
                answer=page_text(text(row, '회신', '회신내용', '답변요지').encode('utf-8')),
                laws=page_text(text(row, '관련법령').encode('utf-8')), url=interpretation_url(row))


def listing_bytes(row):
    return json.dumps(row, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def validated(row, summary, target, kind):
    """Rebuild the stored record from the list row and its stored summary.

    No date gate here: a decision listed with a future date is held back by collect()
    before any request, and a cache written later must stay valid on every later day.
    """
    base = listed(row, target)
    if kind == 'listing':
        if summary != row:
            raise ValueError('LIST_DETAIL_MISMATCH')
        if target == 'ttSpecialDecc' and not base['title']:
            raise ValueError('TITLE_MISSING')
        return base
    if kind != 'detail' or target == 'ntsCgmExpc':
        raise ValueError('CACHE_KIND_INVALID')
    if identifier(summary, target) != base['id'] or row_date(summary, target) != base['date']:
        raise ValueError('LIST_DETAIL_MISMATCH')
    clean = lambda key: page_text(text(summary, key).encode('utf-8'))  # noqa: E731
    if target == 'prec':
        # The id already proves identity; the number must still agree (merged cases by membership).
        # The detail's court name is abbreviated (창원지법), so the list's court is kept.
        if not case_numbers(text(summary, '사건번호')) & case_numbers(base['case_no']):
            raise ValueError('LIST_DETAIL_MISMATCH')
        base.update(title=clean('사건명') or base['title'], issues=clean('판시사항'),
                    holding=clean('판결요지'), refs=clean('참조조문'))
        if not (base['issues'].strip() or base['holding'].strip()):
            raise ValueError('SUMMARY_EMPTY')
        return base
    # Recent decisions come without 사건명/재결요지 (probe 2026-10-29); the short 주문 (disposition)
    # then stands in as an explicitly labelled title.
    order = clean('주문')
    base.update(title=clean('사건명') or base['title'] or (f'주문: {order[:200]}' if order else ''),
                summary=clean('재결요지'), laws=clean('관련법령'),
                decision_type=text(summary, '재결례유형명') or base['decision_type'],
                tax_type=text(summary, '세목') or base['tax_type'])
    if not base['title']:
        raise ValueError('TITLE_MISSING')
    return base


def discard_unrecorded(out, manifest):
    """Remove files an interrupted run wrote before the manifest recorded them.

    Only collect() calls this: temporary files, catalog pages past a target's recorded
    pages, and summaries of documents that have no recorded sha256. Any other extra
    file still fails verify_cache with CACHE_INVENTORY_MISMATCH.
    """
    for folder in ('pages', 'summaries'):
        for tmp in (out / folder).rglob('*.tmp'):
            if tmp.is_file() and not tmp.is_symlink():
                tmp.unlink()
    for target, state in manifest.get('catalogs', {}).items():
        folder = out / 'pages' / target
        for page in folder.glob('*.json') if folder.is_dir() else ():
            if page.stem.isdigit() and int(page.stem) >= state['next_page'] and not page.is_symlink():
                page.unlink()
    for entry in manifest.get('documents', []):
        if 'sha256' not in entry:
            file = out / 'summaries' / entry['target'] / f"{entry['id']}.json"
            if file.is_file() and not file.is_symlink():
                file.unlink()


def verify_cache(out, manifest):
    """Check the entire inventory before any network request or SQL build."""
    if manifest.get('schema') != SCHEMA:
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
    listed_rows = {}
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
                if key in listed_rows:
                    raise ValueError('CACHE_ID_DUPLICATE_OR_MISMATCH')
                listed_rows[key] = row
        if state['complete'] and sum(k[0] == target for k in listed_rows) != state['total']:
            raise ValueError('CACHE_CATALOG_TOTAL_MISMATCH')
    for e in manifest.get('documents', []):
        key = (e['target'], e['id'])
        if (key in seen or identifier(e['row'], e['target']) != e['id']
                or listed_rows.get(key) != e['row']):
            raise ValueError('CACHE_ID_DUPLICATE_OR_MISMATCH')
        seen.add(key)
        if (e.get('kind') not in allowed_kinds(e['row'], e['target'])
                or (e['kind'] == 'skipped') != (e['status'] == 'SKIPPED')
                or (e['kind'] == 'skipped' and 'sha256' in e)
                or e['status'] not in ('PENDING', 'SUCCESS', 'FAILED', 'SKIPPED')):
            raise ValueError('CACHE_KIND_INVALID')
        if 'sha256' in e and e['path'] != f"summaries/{e['target']}/{e['id']}.json":
            raise ValueError('CACHE_PATH_INVALID')
        if e['status'] == 'SUCCESS':
            validated(e['row'], strict_json((out / e['path']).read_bytes()), e['target'], e['kind'])
    if set(listed_rows) != seen:
        raise ValueError('CACHE_CATALOG_DOCUMENT_MISMATCH')


def save_manifest(path, value):
    """Compact atomic write: the manifest holds every listed row (hundreds of MB)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_bytes(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
    tmp.replace(path)


def not_found(doc):
    """law.go.kr answers an unknown or not-yet-published ID with {'Law': '일치하는 … 없습니다 …'}."""
    return (isinstance(doc, dict) and len(doc) == 1 and isinstance(doc.get('Law'), str)
            and '없습니다' in doc['Law'])


def tax_first(entry):
    row = entry['row']
    return not (entry['target'] in ('ntsCgmExpc', 'ttSpecialDecc')
                or text(row, '데이터출처명') == '국세법령정보시스템' or tax_reasons({}, row))


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


def store_entry(client, out, entry, date, check_budget=lambda: None):
    """Fetch (detail kind) or take (listing kind) one summary and write it; returns its bytes."""
    target, sid = entry['target'], entry['id']
    # Entry-level checks and the date gate cost no request.
    if listed(entry['row'], target)['date'] > date:
        raise ValueError('DATE_IN_FUTURE')
    if entry['kind'] == 'listing':
        raw = listing_bytes(entry['row'])
    else:
        check_budget()
        doc, raw = client.get('lawService.do', dict(target=target, ID=sid), original=True)
        if not_found(doc):
            if not listed(entry['row'], target)['title']:
                raise ValueError('DETAIL_NOT_FOUND')
            entry['kind'] = 'listing'
            raw = listing_bytes(entry['row'])
        else:
            raw = summary_bytes(raw, target)
    clean, _ = sanitized(strict_json(raw), getattr(client, 'credential', ''))
    if clean != strict_json(raw) or credential_markers(raw):
        raise ValueError('CREDENTIAL_BEARING_SUMMARY')
    path = f'summaries/{target}/{sid}.json'
    entry.update(path=path, sha256=put_bytes(out, path, raw))
    return raw


def collect(client, out, date, scope='all', stop=None, account=None, clock=time.monotonic):
    if scope not in ('tax', 'all'):
        raise ValueError('SCOPE_INVALID')
    out = Path(out)
    checkpoint = out / 'manifest.json'
    manifest = strict_json(checkpoint.read_bytes()) if checkpoint.exists() else {
        'schema': SCHEMA, 'date': date, 'pages': [], 'documents': [], 'catalogs': {}}
    last = [clock()]
    def checkpoint_now(force=False):
        if force or clock() - last[0] >= SAVE_EVERY_SECONDS:
            save_manifest(checkpoint, manifest)
            last[0] = clock()
    try:
        if checkpoint.exists():
            discard_unrecorded(out, manifest)
        verify_cache(out, manifest)
        manifest.pop('stop_reason', None)
        if account:
            for entry in manifest['documents']:
                if entry['status'] == 'SUCCESS':
                    account(entry, validated(entry['row'], strict_json((out / entry['path']).read_bytes()),
                                             entry['target'], entry['kind']))
        def check_budget():
            reason = stop() if stop else None
            if reason:
                raise CollectionPaused(reason)
        for target in TARGETS:
            state = manifest['catalogs'].setdefault(target, {'next_page': 1, 'complete': False})
            known = {e['id'] for e in manifest['documents'] if e['target'] == target}
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
                raw_rows = catalog_rows(root, target)
                batch = records(raw_rows)
                if isinstance(raw_rows, list) and len(raw_rows) != len(batch):
                    raise ValueError('CATALOG_ROW_INVALID')
                rows = [{k: r[k] for k in FIELDS[target] if k in r} for r in batch]
                fresh = set()
                for row in rows:
                    if any(isinstance(v, (dict, list)) for v in row.values()):
                        raise ValueError('NONSCALAR_SUMMARY_FIELD')
                    # Catalog-time checks are id and duplicates only: one odd row must
                    # fail by itself later, never the whole catalog.
                    sid = identifier(row, target)
                    if sid in known or sid in fresh:
                        raise ValueError('CATALOG_DUPLICATE_ID')
                    fresh.add(sid)
                if len(known) + len(fresh) > total or (len(known) + len(fresh) < total and len(rows) != 100) or (not rows and total):
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
                for r in rows:
                    kind = default_kind(r, target)
                    entry = dict(target=target, id=identifier(r, target), row=r, kind=kind, status='PENDING')
                    if kind == 'skipped':
                        entry.update(status='SKIPPED', reason='SOURCE_NOT_COLLECTED')
                    manifest['documents'].append(entry)
                known |= fresh
                state.update(total=total, next_page=page + 1, complete=len(known) == total)
                checkpoint_now(force=state['complete'])
        entries = manifest['documents']
        if scope == 'tax':
            entries = sorted(entries, key=tax_first)
        for entry in entries:
            target, sid = entry['target'], entry['id']
            if entry['status'] in ('SUCCESS', 'SKIPPED'):
                continue
            try:
                raw = (store_entry(client, out, entry, date, check_budget) if 'sha256' not in entry
                       else (out / entry['path']).read_bytes())
                result = validated(entry['row'], strict_json(raw), target, entry['kind'])
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
                    entry.update(status='PENDING')
                    entry.pop('reason', None)
                    if stop:
                        raise CollectionPaused('TIME_BUDGET') from None
                    break
            checkpoint_now()
        manifest.update(status='COMPLETE' if all(e['status'] in ('SUCCESS', 'SKIPPED') for e in manifest['documents'])
                        else 'INCOMPLETE', updated_at=stamp(), api_calls=client.calls)
        checkpoint_now(force=True)
        return manifest['status'] == 'COMPLETE'
    except Exception as exc:
        if isinstance(exc, CollectionPaused) or (stop and str(exc) == 'COLLECTION_BUDGET_EXHAUSTED'):
            manifest.update(status='INCOMPLETE', stop_reason=str(exc) if isinstance(exc, CollectionPaused) else 'TIME_BUDGET',
                            updated_at=stamp(), api_calls=client.calls)
            checkpoint_now(force=True)
            return not any(e['status'] == 'FAILED' for e in manifest['documents'])
        reason = str(exc) if str(exc) in ERROR_CODES else 'CATALOG_OR_CACHE_FAILED'
        # Keep finished work (the in-memory manifest only ever records files already written).
        if manifest['pages']:
            save_manifest(checkpoint, manifest)
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
