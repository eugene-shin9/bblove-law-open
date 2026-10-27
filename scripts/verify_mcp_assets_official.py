#!/usr/bin/env python3
"""Offline Release validation immediately before deployment; no credentials."""
import argparse
import re
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from bblove_law_all import today
from bblove_storage import strict_json
from bblove_annex_assets import verify_assets as verify_annex_assets
from bblove_v107_pilot import check_date
from build_mcp_assets_official import GROUP_LIMIT, META_LIMIT, REQUIRED, group_id


def verify(assets, date=None, enforce_thresholds=True):
    date = date or today()
    meta_raw = (assets / 'meta.json').read_bytes()
    if not 0 < len(meta_raw) <= META_LIMIT:
        raise ValueError('META_SIZE_LIMIT_EXCEEDED')
    meta = strict_json(meta_raw)
    if meta['shards'] != [] or not meta['groups']:
        raise ValueError('GROUP_FORMAT_REQUIRED')
    if not meta['source_commit'].startswith('law.go.kr DRF '):
        raise ValueError('NOT_OFFICIAL_SOURCE')
    groups, docs, found, ids = {}, [], set(), set()
    listed_files = set()
    titles, annex_laws = {}, {}
    warning_docs = meta.get('warnings', [])
    warning_map = {w['id']: w['warnings'] for w in warning_docs}
    if len(warning_map) != len(warning_docs) or any(not w for w in warning_map.values()):
        raise ValueError('INVALID_WARNING_DOCUMENTS')
    for group, info in meta['groups'].items():
        identifier = group_id(group)
        if not isinstance(info, list) or len(info) != 4 or info[0] != identifier:
            raise ValueError('GROUP_ID_MISMATCH')
        filename = f'{identifier}.json'
        if filename in listed_files:
            raise ValueError('GROUP_ID_COLLISION')
        listed_files.add(filename)
        path = assets / 'groups' / filename
        if not path.is_file() or path.is_symlink() or not 0 < path.stat().st_size <= GROUP_LIMIT:
            raise ValueError('GROUP_FILE_MISSING_OR_SIZE_INVALID')
        values = strict_json(path.read_bytes())
        if not isinstance(values, list) or not values:
            raise ValueError('EMPTY_OR_INVALID_GROUP')
        groups[group] = identifier
        representative = next((d for d in values if d['k'] == '법률'), values[0])
        if info != [identifier, representative['t'], representative['eff'], 0]:
            raise ValueError('GROUP_INDEX_MISMATCH')
        for d in values:
            annex_laws[d['id']] = {**d, 'group': group}
            if d['id'] in ids or not d['a'] or d['st'] != '시행':
                raise ValueError('DUPLICATE_EMPTY_OR_NONCURRENT_DOCUMENT')
            ids.add(d['id'])
            check_date(d['eff'].replace('-', ''), 'effective', date)
            check_date(d['pub'].replace('-', ''), 'published', date)
            article_ids = set()
            declared = warning_map.get(d['id'], [])
            observed = []
            for index, a in enumerate(d['a']):
                if len(a) != 3:
                    raise ValueError('INVALID_ARTICLE')
                if not a[2].strip():
                    observed.append(('empty_deleted_article', index, a[0]))
                if a[0] in article_ids:
                    observed.append(('duplicate_article_identifier', index, a[0]))
                article_ids.add(a[0])
                if d['k'] == '법률' and a[1]:
                    titles.setdefault(group, []).append([a[0], a[1]])
                if d['k'] == '법률' and REQUIRED.get(d['t']) == a[0]:
                    found.add(d['t'])
            expected = []
            has_date_warning = False
            for warning in declared:
                code = warning['code']
                if code in {'empty_deleted_article', 'duplicate_article_identifier'}:
                    index = warning['article_index']
                    if not isinstance(index, int) or not 0 <= index < len(d['a']) or d['a'][index][0] != warning['article']:
                        raise ValueError('ARTICLE_WARNING_TARGET_MISMATCH')
                    # An empty deleted RAW heading can still contain preserved child text.
                    if code != 'empty_deleted_article' or not d['a'][index][2].strip():
                        expected.append((code, index, warning['article']))
                elif code in {'effective_date_differs_from_catalog', 'detail_date_after_today'}:
                    # Catalog date stays in 'eff'; the detail's own date must be surfaced as 'eff2'.
                    detail = warning.get('detail', '')
                    if (warning.get('catalog', '').replace('-', '') != d['eff'].replace('-', '') or
                            not re.fullmatch(r'\d{8}', detail) or
                            d.get('eff2') != f'{detail[:4]}-{detail[4:6]}-{detail[6:]}' or
                            (code == 'effective_date_differs_from_catalog' and detail > date)):
                        raise ValueError('EFFECTIVE_DATE_WARNING_MISMATCH')
                    has_date_warning = True
                elif code != 'title_whitespace_difference':
                    raise ValueError('UNKNOWN_VALIDATION_WARNING')
            if sorted(expected) != sorted(observed):
                raise ValueError('ARTICLE_WARNINGS_MISMATCH')
            if 'eff2' in d and not has_date_warning:
                raise ValueError('UNDECLARED_EFFECTIVE_DATE')
            docs.append(d)
    if set(warning_map) - ids:
        raise ValueError('UNKNOWN_WARNING_DOCUMENT')
    if listed_files != {p.name for p in (assets / 'groups').iterdir()}:
        raise ValueError('UNLISTED_GROUP_FILE')
    if (assets / 'shards').exists():
        raise ValueError('LEGACY_SHARDS_PRESENT')
    if meta['titles'] != titles:
        raise ValueError('TITLE_INDEX_MISMATCH')
    counts = {'n_groups': len(groups), 'n_docs': len(docs),
              'n_articles': sum(len(d['a']) for d in docs)}
    if any(meta[key] != value for key, value in counts.items()):
        raise ValueError('META_COUNTS_MISMATCH')
    if any(g not in groups for g in meta['alias'].values()):
        raise ValueError('DANGLING_ALIAS')
    if enforce_thresholds and (found != set(REQUIRED) or counts['n_docs'] < 4500 or
                               counts['n_articles'] < 170000 or sum(d['k'] == '법률' for d in docs) < 1650):
        raise ValueError('CORPUS_ACCEPTANCE_FAILED')
    return {**counts, **verify_annex_assets(assets, meta.get('annex'), groups, annex_laws)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--assets', type=Path, required=True)
    args = parser.parse_args()
    try:
        print(verify(args.assets))
    except Exception:
        print('RELEASE_ASSET_VALIDATION_FAILED')
        raise SystemExit(1) from None
