"""Offline fulltext delta contract. No source text is logged or retained in metadata."""
from copy import deepcopy
import json
from pathlib import Path
import re
from urllib.parse import quote, quote_plus
from bblove_storage import digest, strict_json
from bblove_v107_pilot import check_date

KIND = 'precedent-fulltext-raw'
TAG_PATTERN = r'prec-fulltext-[0-9]{8}-r[0-9]+-a[0-9]+'
MAX_ATTEMPTS = 3


def new_manifest(date):
    return dict(kind=KIND, format_version=2, date=date, documents={}, sequence=0, previous_tag=None)


def validate_manifest(m):
    if m.get('kind') != KIND or m.get('format_version') != 2 or not isinstance(m.get('documents'), dict):
        raise ValueError('FULLTEXT_MANIFEST_INVALID')
    check_date(m.get('date'), 'fulltext date')
    if type(m.get('sequence')) is not int or m['sequence'] < 0:
        raise ValueError('FULLTEXT_SEQUENCE_INVALID')
    if m.get('previous_tag') is not None and not re.fullmatch(TAG_PATTERN, m['previous_tag']):
        raise ValueError('FULLTEXT_TAG_INVALID')
    allowed = {'body_sha256','prev_body_sha256','bytes','prev_bytes','checked_date','status','release','attempts','reason'}
    for sid, meta in m['documents'].items():
        if not re.fullmatch(r'[0-9]+', sid) or set(meta) - allowed:
            raise ValueError('FULLTEXT_METADATA_INVALID')
        if meta.get('status') not in ('PUBLISHED','EXCLUDED','FAILED','STAGED'):
            raise ValueError('FULLTEXT_STATUS_INVALID')
        for key in ('bytes','prev_bytes','attempts'):
            if type(meta.get(key, 0)) is not int or meta.get(key, 0) < 0:
                raise ValueError('FULLTEXT_BYTES_INVALID')
        for key in ('body_sha256','prev_body_sha256'):
            if meta.get(key) is not None and not re.fullmatch(r'[0-9a-f]{64}', meta[key]):
                raise ValueError('FULLTEXT_HASH_INVALID')
        check_date(meta.get('checked_date'), 'checked date')
        if meta.get('release') is not None and not re.fullmatch(TAG_PATTERN, meta['release']):
            raise ValueError('FULLTEXT_TAG_INVALID')
        if meta.get('status') == 'PUBLISHED' and (not meta.get('body_sha256') or not meta.get('release')):
            raise ValueError('FULLTEXT_PUBLISHED_INVALID')
        if 'reason' in meta and not re.fullmatch(r'[A-Z_]+(?:,[A-Z_]+)*', meta['reason']):
            raise ValueError('FULLTEXT_REASON_INVALID')
    return m


def gate(value, credential=''):
    # Reuse the committee patterns, but NEVER inspect hashes/URLs/IDs/dates for PII.
    from bblove_committee_release import gate as privacy_gate
    codes = privacy_gate({key: value.get(key, '') for key in ('body','case_no','court')})
    raw = json.dumps(value, ensure_ascii=False).lower()
    if credential and any(v.lower() in raw for v in {credential, quote(credential, safe=''), quote_plus(credential, safe='')}):
        codes.append('CREDENTIAL')
    return sorted(set(codes))


def queue(entries, manifest, date):
    from datetime import datetime
    fresh, failed, old = [], [], []
    for entry in entries:
        meta = manifest['documents'].get(entry['id'], {})
        status = meta.get('status')
        if status == 'FAILED':
            if meta.get('attempts', 0) < MAX_ATTEMPTS:
                failed.append(entry)
        elif status in ('PUBLISHED', 'EXCLUDED'):
            # Exclusions also wait for recheck instead of looping on every small delta.
            age = (datetime.strptime(date, '%Y%m%d') - datetime.strptime(meta['checked_date'], '%Y%m%d')).days
            if age >= 180:
                old.append(entry)
        else:
            fresh.append(entry)
    old.sort(key=lambda e: manifest['documents'][e['id']]['checked_date'])
    return fresh + failed + old


def delta_manifest(manifest, changed, withdrawn, total, excluded):
    halted = excluded > max(total * .05, 3)
    return dict(kind=KIND, format_version=2, delta=True, date=manifest['date'],
                sequence=manifest['sequence']+1, previous_tag=manifest.get('previous_tag'),
                documents={sid: dict(manifest['documents'][sid]) for sid in changed}, withdrawn=withdrawn,
                total=total, excluded=excluded, status='HALTED' if halted else 'READY')


def verify_delta(source):
    source = Path(source)
    d = strict_json((source/'fulltext-delta-manifest.json').read_bytes())
    if d.get('kind') != KIND or d.get('format_version') != 2 or d.get('delta') is not True or d.get('status') != 'READY':
        raise ValueError('FULLTEXT_DELTA_INVALID')
    if type(d.get('sequence')) is not int or d['sequence'] < 1:
        raise ValueError('FULLTEXT_SEQUENCE_INVALID')
    if (d['sequence'] == 1) != (d.get('previous_tag') is None):
        raise ValueError('FULLTEXT_CHAIN_INVALID')
    validate_manifest(dict(d, sequence=d['sequence']-1))
    if d['excluded'] > max(.05*d['total'], 3):
        raise ValueError('FULLTEXT_GATE_HALTED')
    expected = {f'prec/{sid}.json' for sid in d['documents']}
    actual = {str(p.relative_to(source)) for p in (source/'prec').rglob('*') if p.is_file()}
    if expected != actual or any(p.is_symlink() for p in source.rglob('*')):
        raise ValueError('FULLTEXT_INVENTORY_INVALID')
    for sid, meta in d['documents'].items():
        raw = (source/f'prec/{sid}.json').read_bytes(); value = strict_json(raw)
        if value.get('id') != sid or value.get('body_sha256') != meta['body_sha256'] or digest(value['body'].encode()) != meta['body_sha256'] or len(raw) != meta['bytes'] or gate(value):
            raise ValueError('FULLTEXT_OBJECT_INVALID')
    seen = set()
    for entry in d['withdrawn']:
        if set(entry) != {'id','reason'} or not re.fullmatch(r'[0-9]+', entry['id']) or entry['id'] in seen or entry['id'] in d['documents'] or not re.fullmatch(r'[A-Z_]+(?:,[A-Z_]+)*', entry['reason']):
            raise ValueError('FULLTEXT_WITHDRAWN_INVALID')
        seen.add(entry['id'])
    return d


def committed_snapshot(manifest, delta, tag):
    validate_manifest(manifest)
    if not re.fullmatch(TAG_PATTERN, tag) or not tag.startswith('prec-fulltext-'+delta['date']+'-'):
        raise ValueError('FULLTEXT_TAG_INVALID')
    result = deepcopy(manifest)
    for sid, meta in delta['documents'].items():
        if result['documents'][sid]['body_sha256'] != meta['body_sha256']:
            raise ValueError('FULLTEXT_FINALIZE_MISMATCH')
        result['documents'][sid].update(status='PUBLISHED', release=tag)
    result.update(sequence=delta['sequence'], previous_tag=tag)
    return validate_manifest(result)
