"""부칙(addenda) assets: every 부칙단위 of each collected document, flattened without dropping text.

Layout: addenda/<group_id>.json = [{"id": law_id, "k": kind, "t": title, "u": [[date ISO, number, text], ...]}, ...]
(one entry per document; units in official order, oldest first). meta.addenda = {group: unit count}.
Legacy Releases without meta.addenda stay valid only when no addenda/ folder exists.
"""
import json
from pathlib import Path
import re

from bblove_quality import credential_markers
from bblove_v107_pilot import check_date, records, text

LIMIT = 4 * 1024 * 1024
NUMBER = re.compile(r'\d{1,10}')


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def flatten(value):
    if value is None:
        return ''
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return '\n'.join(s for s in (flatten(v) for v in value) if s)
    if isinstance(value, dict):
        return '\n'.join(s for s in (flatten(v) for v in value.values()) if s)
    raise ValueError('ADDENDA_SHAPE_INVALID')


def iso(value):
    return f'{value[:4]}-{value[4:6]}-{value[6:]}'


def extract(doc, today=None):
    """[[date ISO, number, text], ...] in source order. Raises on shapes it cannot keep losslessly."""
    law = doc.get('법령') if isinstance(doc, dict) else None
    section = law.get('부칙') if isinstance(law, dict) else None
    if section in (None, '', {}):
        return []
    if not isinstance(section, dict):
        raise ValueError('ADDENDA_SHAPE_INVALID')
    raw_units = section.get('부칙단위')
    units = records(raw_units)  # a single addendum arrives as a dict, several as a list
    if isinstance(raw_units, list) and len(units) != len(raw_units):
        raise ValueError('ADDENDA_SHAPE_INVALID')
    result = []
    for unit in units:
        date = text(unit, '부칙공포일자')
        number = text(unit, '부칙공포번호')
        check_date(date, 'addenda', today)
        if not NUMBER.fullmatch(number):
            raise ValueError('ADDENDA_NUMBER_INVALID')
        body = flatten(unit.get('부칙내용')).strip()
        if not body:
            raise ValueError('ADDENDA_EMPTY')
        if credential_markers(body.encode('utf-8')):
            raise ValueError('ADDENDA_AUTH_MARKER')
        result.append([iso(date), number, body])
    return result


def write_assets(stage, grouped, identifiers, limit=LIMIT):
    """grouped: {group: [{"id","k","t","u"}]} -> files under stage/addenda; returns meta index {group: count}."""
    index = {}
    folder = Path(stage) / 'addenda'
    for group, entries in sorted(grouped.items()):
        entries = [e for e in entries if e['u']]
        if not entries:
            continue
        raw = encode(entries)
        if len(raw) > limit:
            raise ValueError('ADDENDA_FILE_SIZE_LIMIT')
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f'{identifiers[group]}.json').write_bytes(raw)
        index[group] = sum(len(e['u']) for e in entries)
    return index


def verify_assets(assets, index, groups, documents, today=None):
    """groups: {group: group_id}; documents: {law_id: {...doc, 'group': g}} from the verified group files."""
    folder = Path(assets) / 'addenda'
    if index is None:
        if folder.exists():
            raise ValueError('ADDENDA_FILES_WITHOUT_INDEX')
        return {'n_addenda': 0}
    if not isinstance(index, dict) or any(g not in groups for g in index):
        raise ValueError('ADDENDA_INDEX_INVALID')
    expected = {f'{groups[g]}.json' for g in index}
    present = {p.name for p in folder.iterdir()} if folder.exists() else set()
    if present != expected:
        raise ValueError('ADDENDA_FILE_SET_MISMATCH')
    total = 0
    for group, count in index.items():
        path = folder / f'{groups[group]}.json'
        if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= LIMIT:
            raise ValueError('ADDENDA_FILE_INVALID')
        raw = path.read_bytes()
        if credential_markers(raw):
            raise ValueError('ADDENDA_AUTH_MARKER')
        entries = json.loads(raw)
        if not isinstance(entries, list) or not entries:
            raise ValueError('ADDENDA_FILE_INVALID')
        seen, units = set(), 0
        for e in entries:
            doc = documents.get(e.get('id')) if isinstance(e, dict) else None
            if (doc is None or doc['group'] != group or e['id'] in seen or set(e) != {'id', 'k', 't', 'u'}
                    or e['k'] != doc['k'] or e['t'] != doc['t'] or not isinstance(e['u'], list) or not e['u']):
                raise ValueError('ADDENDA_ENTRY_MISMATCH')
            seen.add(e['id'])
            for unit in e['u']:
                if (not isinstance(unit, list) or len(unit) != 3 or not all(isinstance(x, str) for x in unit)
                        or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', unit[0]) or not NUMBER.fullmatch(unit[1])
                        or not unit[2].strip()):
                    raise ValueError('ADDENDA_UNIT_INVALID')
                check_date(unit[0].replace('-', ''), 'addenda', today)
            units += len(e['u'])
        if units != count:
            raise ValueError('ADDENDA_COUNT_MISMATCH')
        total += units
    return {'n_addenda': total, 'n_addenda_groups': len(index)}
