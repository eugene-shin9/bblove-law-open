"""Offline history packaging, migration and verification. No collectors or secrets."""
from collections import defaultdict
import hashlib
from pathlib import Path
import re
import shutil
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from build_mcp_assets_official import encode, META_LIMIT
from bblove_storage import strict_json
from bblove_v107_pilot import check_date

PART_LIMIT = 8 * 1024 * 1024
FILE_LIMIT = 20 * 1024 * 1024  # transition: existing unpartitioned Release files


def date(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise ValueError('HISTORY_DATE_INVALID')
    check_date(value.replace('-', ''), 'history date')
    return value


def edition_key(e):
    date(e['eff'])
    if e.get('pub'):
        date(e['pub'])
    if not re.fullmatch(r'\d+', e.get('mst', '')):
        raise ValueError('HISTORY_MST_INVALID')
    return e['eff'], e.get('pub') or '', int(e['mst'])


def missing(editions):
    return sum(bool(e.get('missing')) for e in editions)


def validate_document(doc, lid, end):
    if (doc.get('id') != lid or not isinstance(doc.get('t'), str) or not doc['t']
            or doc.get('k') not in ('법률', '시행령', '시행규칙', '대통령령')):
        raise ValueError('HISTORY_IDENTITY_MISMATCH')
    editions, bodies = doc['editions'], doc['bodies']
    if not editions or not isinstance(bodies, dict):
        raise ValueError('HISTORY_EMPTY_OR_INVALID')
    keys = [edition_key(e) for e in editions]
    if keys != sorted(keys) or len(keys) != len(set(keys)):
        raise ValueError('HISTORY_EDITION_ORDER_OR_DUPLICATE')
    used = set()
    for e in editions:
        if e['eff'] > end or not isinstance(e.get('a'), list):
            raise ValueError('HISTORY_EDITION_INVALID')
        if e.get('missing') and e['a']:
            raise ValueError('HISTORY_GAP_HAS_ARTICLES')
        if not e.get('missing') and not e['a']:
            raise ValueError('HISTORY_EDITION_EMPTY')
        for no, title, h in e['a']:
            if not re.fullmatch(r'제\d+조(?:의\d+)?', no) or not isinstance(title, str) or h not in bodies:
                raise ValueError('HISTORY_ARTICLE_OR_BODY_MISSING')
            used.add(h)
    if used != set(bodies):
        raise ValueError('HISTORY_UNUSED_BODY')
    for h, body in bodies.items():
        if not isinstance(body, str) or hashlib.sha1(body.encode('utf-8')).hexdigest()[:16] != h:
            raise ValueError('HISTORY_BODY_HASH_MISMATCH')


def partition(doc, limit=PART_LIMIT):
    """Pack consecutive effective-day groups; encoded UTF-8 JSON overhead counts.

    All editions with the same eff stay together, including missing markers, so
    a query never needs another part to break a pub/MST tie or discover a gap.
    """
    if not 0 < limit <= PART_LIMIT:
        raise ValueError('HISTORY_PART_LIMIT_INVALID')
    metadata = {k: v for k, v in doc.items() if k not in ('editions', 'bodies')}
    base = len(encode({**metadata, 'editions': [], 'bodies': {}}))
    body_cost = {h: len(encode(h)) + 1 + len(encode(b)) for h, b in doc['bodies'].items()}
    groups = defaultdict(list)
    for e in sorted(doc['editions'], key=edition_key):
        groups[e['eff']].append(e)
    editions, bodies, edition_bytes, body_bytes = [], {}, 0, 0
    for group in groups.values():
        hashes = {h for e in group for _, _, h in e['a']}
        addition = sum(len(encode(e)) for e in group)
        new_hashes = hashes - bodies.keys()
        size = (base + edition_bytes + addition + max(0, len(editions) + len(group) - 1)
                + body_bytes + sum(body_cost[h] for h in new_hashes)
                + max(0, len(bodies) + len(new_hashes) - 1))
        if size > limit and editions:
            yield {**metadata, 'editions': editions, 'bodies': bodies}
            editions, bodies, edition_bytes, body_bytes = [], {}, 0, 0
            new_hashes = hashes
            size = (base + addition + len(group) - 1 + sum(body_cost[h] for h in hashes)
                    + max(0, len(hashes) - 1))
        if size > limit:
            raise ValueError('HISTORY_EFFECTIVE_DAY_TOO_LARGE')
        editions.extend(group)
        edition_bytes += addition
        # Stable first-reference ordering makes rebuilding deterministic.
        for e in group:
            for _, _, h in e['a']:
                if h not in bodies:
                    bodies[h] = doc['bodies'][h]
                    body_bytes += body_cost[h]
    if editions:
        yield {**metadata, 'editions': editions, 'bodies': bodies}


def write_assets(documents, dest, end, cutoff, limit=PART_LIMIT):
    end, cutoff = date(end), date(cutoff)
    if cutoff > end:
        raise ValueError('HISTORY_COVERAGE_INVALID')
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=dest.parent) as temp:
        stage = Path(temp)
        target = stage / 'history'
        target.mkdir()
        laws = {}
        for doc in documents:
            lid = doc['id']
            if not re.fullmatch(r'\d{6}', lid) or lid in laws:
                raise ValueError('HISTORY_LAW_ID_INVALID_OR_DUPLICATE')
            validate_document(doc, lid, end)
            parts = []
            (target / lid).mkdir()
            for i, part in enumerate(partition(doc, limit)):
                raw = encode(part)
                if len(raw) > limit:
                    raise ValueError('HISTORY_PART_TOO_LARGE')
                path = f'{lid}/{i}.json'
                (target / path).write_bytes(raw)
                es = part['editions']
                parts.append({'path': path, 'from': es[0]['eff'], 'to': es[-1]['eff'],
                              'n': len(es), 'missing': missing(es), 'bytes': len(raw),
                              'sha256': hashlib.sha256(raw).hexdigest()})
            laws[lid] = {'t': doc['t'], 'k': doc['k'], 'from': parts[0]['from'], 'to': parts[-1]['to'],
                         'n': sum(p['n'] for p in parts), 'missing': sum(p['missing'] for p in parts),
                         'bytes': sum(p['bytes'] for p in parts), 'parts': parts}
        (target / 'index.json').write_bytes(encode({'schema': 2, 'date': end, 'cutoff': cutoff, 'laws': laws}))
        result = verify_assets(stage)
        dest.mkdir(parents=True, exist_ok=True)
        if (dest / 'history').exists():
            shutil.rmtree(dest / 'history')
        shutil.move(str(target), dest / 'history')
    return result


def verify_assets(root, embed_index=False):
    """Check both schemas; only explicit embed_index mutates the existing meta.json."""
    root = Path(root)
    history = root / 'history'
    index_path = history / 'index.json'
    if history.is_symlink() or index_path.is_symlink() or index_path.stat().st_size > FILE_LIMIT:
        raise ValueError('HISTORY_INDEX_INVALID')
    index = strict_json(index_path.read_bytes())
    end, cutoff = date(index['date']), date(index['cutoff'])
    if cutoff > end or index.get('schema', 1) not in (1, 2) or not index.get('laws'):
        raise ValueError('HISTORY_INDEX_INVALID')
    expected, sizes, total_editions = {index_path}, [], 0
    for lid, info in index['laws'].items():
        if not re.fullmatch(r'\d{6}', lid):
            raise ValueError('HISTORY_LAW_ID_INVALID')
        is_new = index.get('schema') == 2
        parts = info['parts'] if is_new else [{'path': f'{lid}.json', **info}]
        if not isinstance(parts, list) or not parts:
            raise ValueError('HISTORY_PARTS_INVALID')
        counts, gaps, byte_count, last = 0, 0, 0, None
        for i, part in enumerate(parts):
            rel = f'{lid}/{i}.json' if is_new else f'{lid}.json'
            if part['path'] != rel:
                raise ValueError('HISTORY_PART_PATH_INVALID')
            path = history / rel
            if path.is_symlink() or path.parent.is_symlink() or path in expected:
                raise ValueError('HISTORY_PART_PATH_INVALID')
            raw = path.read_bytes()
            if not 0 < len(raw) <= (PART_LIMIT if is_new else FILE_LIMIT):
                raise ValueError('HISTORY_FILE_TOO_LARGE')
            if len(raw) != part['bytes'] or (is_new and hashlib.sha256(raw).hexdigest() != part['sha256']):
                raise ValueError('HISTORY_PART_BYTES_OR_HASH_MISMATCH')
            doc = strict_json(raw)
            validate_document(doc, lid, end)
            es = doc['editions']
            if (doc['t'], doc['k'], es[0]['eff'], es[-1]['eff'], len(es), missing(es)) != (
                    info['t'], info['k'], part['from'], part['to'], part['n'], part['missing']):
                raise ValueError('HISTORY_INDEX_FILE_MISMATCH')
            if last is not None and es[0]['eff'] <= last:
                raise ValueError('HISTORY_PART_RANGES_OVERLAP')
            last = es[-1]['eff']
            counts += len(es)
            gaps += missing(es)
            byte_count += len(raw)
            sizes.append(len(raw))
            expected.add(path)
        if (parts[0]['from'], parts[-1]['to'], counts, gaps, byte_count) != (
                info['from'], info['to'], info['n'], info['missing'], info['bytes']):
            raise ValueError('HISTORY_LAW_SUMMARY_MISMATCH')
        total_editions += counts
    if expected != {p for p in history.rglob('*') if p.is_file()}:
        raise ValueError('HISTORY_UNLISTED_OR_MISSING_FILE')
    if embed_index:
        # No second history/index.json fetch in the Worker, even on a cold query.
        meta_path = root / 'meta.json'
        meta = strict_json(meta_path.read_bytes())
        meta['history'] = index
        raw = encode(meta)
        if len(raw) > META_LIMIT:
            raise ValueError('HISTORY_EMBEDDED_META_TOO_LARGE')
        tmp = meta_path.with_suffix('.tmp')
        tmp.write_bytes(raw)
        tmp.replace(meta_path)
    return {'schema': index.get('schema', 1), 'laws': len(index['laws']), 'editions': total_editions,
            'parts': len(sizes), 'bytes': sum(sizes), 'max_file_bytes': max(sizes),
            'part_size_bytes': sorted(sizes), 'index_bytes': index_path.stat().st_size}


def repack_assets(source, dest, limit=PART_LIMIT):
    """Migrate already downloaded Release assets offline, preserving their coverage."""
    source, dest = Path(source), Path(dest)
    if (source.resolve() == dest.resolve() or source.resolve() in dest.resolve().parents
            or dest.resolve() in source.resolve().parents):
        raise ValueError('HISTORY_MIGRATION_DEST_MUST_BE_SEPARATE')
    verify_assets(source)
    index = strict_json((source / 'history/index.json').read_bytes())

    def documents():
        for lid, info in index['laws'].items():
            paths = [p['path'] for p in info['parts']] if index.get('schema') == 2 else [f'{lid}.json']
            combined = None
            for path in paths:
                doc = strict_json((source / 'history' / path).read_bytes())
                if combined is None:
                    combined = {**doc, 'editions': [], 'bodies': {}}
                combined['editions'].extend(doc['editions'])
                for h, body in doc['bodies'].items():
                    if combined['bodies'].setdefault(h, body) != body:
                        raise ValueError('HISTORY_BODY_HASH_COLLISION')
            yield combined
    return write_assets(documents(), dest, index['date'], index['cutoff'], limit)
