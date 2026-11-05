"""Public committee projection and fail-closed gate. No transport or source text logging."""
from collections import Counter
import json
from pathlib import Path
import re
import unicodedata
from bblove_precedent_collect import validated, verify_cache, committee_url
from bblove_storage import digest, strict_json
from bblove_v107_pilot import check_date

KIND = 'committee-summary-raw'
# Records that trip a pattern are always excluded; publication halts only when the excluded
# share exceeds this percentage. The 40-record decc sample showed 1 exclusion (2.5%), so a 1%
# halt would block every release; 5% still stops a systematic leak.
EXCLUSION_HALT_PERCENT = 5
FIELDS = ('source', 'id', 'case_no', 'committee', 'agency', 'decided', 'disposed',
          'title', 'holding', 'order', 'claim', 'type_name', 'type_code', 'official_url')
PATTERNS = {
    'RESIDENT_OR_FOREIGN_ID': r'(?<!\d)\d{6}[ -]?[1-8]\d{6}(?!\d)',
    'PHONE': r'(?<!\d)0(?:1[016789]|2|[3-6][1-5]|70)[ -]?\d{3,4}[ -]?\d{4}(?!\d)',
    'EMAIL': r'(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?!\w)',
    # At least three groups; excludes YYYY-MM-DD, case numbers, amounts and article numbers.
    'ACCOUNT': r'(?<![\d-])(?:\d{2,6}-\d{2,6}-\d{3,8}(?:-\d{1,6})?)(?![\d-])',
    'ACCOUNT_CONTEXT': r'(?:계좌|account)\s*(?:번호)?\s*[:：]?\s*\d{10,16}(?!\d)',
}


# Detection runs on a normalized copy (stored bytes never change): full-width digits/hyphens (NFKC),
# every Unicode dash/minus and spacing variant become ASCII, zero-width characters disappear, so
# 0１0－1234–5678 or 010\u200b-1234 cannot slip past the ASCII patterns.
_DASHES = dict.fromkeys(map(ord, '\u2010\u2011\u2012\u2013\u2014\u2015\u2212\ufe58\ufe63\uff0d\u30fc\u2027'), '-')
_INVISIBLE = dict.fromkeys(map(ord, '\u200b\u200c\u200d\u2060\ufeff\u00ad'), None)


def pii_text(value):
    text = unicodedata.normalize('NFKC', value).translate(_INVISIBLE).translate(_DASHES)
    return re.sub(r'[\u00a0\u2000-\u200a\u202f\u3000]', ' ', text)


def gate(row):
    codes = set()
    for key, value in row.items():
        if not isinstance(value, str) or key in ('id', 'source', 'decided', 'disposed', 'official_url'):
            continue
        for code, pattern in PATTERNS.items():
            for match in re.finditer(pattern, pii_text(value), re.I):
                if code == 'ACCOUNT' and re.fullmatch(r'\d{4}-\d{2}-\d{2}', match[0]):
                    continue
                codes.add(code)
    return sorted(codes)


def record_bytes(row):
    return json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def validate_record(row):
    if not isinstance(row, dict) or set(row) != set(FIELDS) or row['source'] != 'decc':
        raise ValueError('COMMITTEE_RECORD_SHAPE')
    if any(not isinstance(row[k], str) for k in FIELDS if k != 'disposed'):
        raise ValueError('COMMITTEE_RECORD_TYPE')
    if not re.fullmatch(r'[0-9]+', row['id']) or not all(row[k].strip() for k in ('title','case_no','committee')):
        raise ValueError('COMMITTEE_IDENTITY')
    for key in ('decided', 'disposed'):
        value = row[key]
        if key == 'disposed' and value is None:
            continue
        if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            raise ValueError('COMMITTEE_DATE')
        check_date(value.replace('-', ''), key)
    if committee_url({'행정심판례상세링크':row['official_url']}, row['id']) != row['official_url']:
        raise ValueError('COMMITTEE_URL')
    if gate(row):
        raise ValueError('COMMITTEE_PRIVACY_GATE')
    return row


def export(source, dest):
    source, dest = Path(source), Path(dest)
    m = strict_json((source / 'manifest.json').read_bytes())
    verify_cache(source, m)
    if (set(m['catalogs']) != {'decc'} or not m['catalogs']['decc']['complete']
            or m.get('incremental', {}).get('complete') is False
            or m.get('incremental', {}).get('recovery', {}).get('complete') is False
            or any(e['status'] == 'PENDING' for e in m['documents'])):
        raise ValueError('COMMITTEE_INCOMPLETE')
    accepted, reasons, excluded = [], Counter(), 0
    for entry in sorted(m['documents'], key=lambda e: e['id']):
        if entry['target'] != 'decc':
            raise ValueError('COMMITTEE_TARGET')
        if entry['status'] != 'SUCCESS':
            excluded += 1
            reasons['SOURCE_NOT_SUCCESS'] += 1
            continue
        detail = strict_json((source / entry['path']).read_bytes())
        value = validated(entry['row'], detail, 'decc', entry['kind'])
        row = {k:value[k] for k in FIELDS}
        codes = gate(row)
        if codes:
            excluded += 1
            reasons.update(codes)
            continue
        validate_record(row)
        accepted.append(row)
    total = len(m['documents'])
    if excluded * 100 > total * EXCLUSION_HALT_PERCENT:
        print('::error::COMMITTEE_EXCLUSION_RATE_EXCEEDED')
        raise ValueError('COMMITTEE_EXCLUSION_RATE_EXCEEDED')
    manifest = dict(kind=KIND, format_version=1, total=total, accepted=len(accepted), excluded=excluded,
                    exclusion_reasons=dict(sorted(reasons.items())),
                    source_total=m['catalogs']['decc'].get('increments', [{}])[-1].get('total',m['catalogs']['decc']['total']),
                    records={r['id']:digest(record_bytes(r)) for r in accepted})
    dest.mkdir(parents=True, exist_ok=False)
    (dest/'records.jsonl').write_bytes(b''.join(record_bytes(r)+b'\n' for r in accepted))
    (dest/'committee-manifest.json').write_bytes(record_bytes(manifest)+b'\n')
    verify(dest)
    return manifest


def verify(source):
    source = Path(source)
    allowed = {'records.jsonl','committee-manifest.json','release-info.json','SHA256SUMS','d1-loaded.json'}
    if any(p.name not in allowed or not p.is_file() or p.is_symlink() for p in source.iterdir()):
        raise ValueError('COMMITTEE_INVENTORY')
    m = strict_json((source/'committee-manifest.json').read_bytes())
    if m.get('kind') != KIND or m.get('format_version') != 1:
        raise ValueError('COMMITTEE_KIND')
    for key in ('total','accepted','excluded','source_total'):
        if type(m.get(key)) is not int or m[key] < 0:
            raise ValueError('COMMITTEE_COUNTS')
    if m['accepted']+m['excluded'] != m['total'] or m['excluded']*100 > m['total']*EXCLUSION_HALT_PERCENT or m['source_total'] != m['total']:
        raise ValueError('COMMITTEE_COUNTS')
    reasons=m.get('exclusion_reasons')
    if not isinstance(reasons,dict) or set(reasons)-set(PATTERNS)-{'SOURCE_NOT_SUCCESS'} or any(type(n) is not int or n < 1 for n in reasons.values()):
        raise ValueError('COMMITTEE_REASONS')
    rows, hashes = [], {}
    for raw in (source/'records.jsonl').read_bytes().splitlines():
        row = validate_record(strict_json(raw))
        if row['id'] in hashes or raw != record_bytes(row):
            raise ValueError('COMMITTEE_DUPLICATE_OR_ENCODING')
        rows.append(row)
        hashes[row['id']] = digest(raw)
    if len(rows) != m['accepted'] or hashes != m.get('records'):
        raise ValueError('COMMITTEE_HASH')
    return rows
