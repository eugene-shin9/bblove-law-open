#!/usr/bin/env python3
"""Build article and annex assets from validated official RAW bytes."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from bblove_law_all import save, today, validate_raw, collection_complete, failure_report
from bblove_storage import strict_json
from bblove_annex_assets import (extract as extract_annex, write_assets as write_annex_assets,
                                 verify_assets as verify_annex_assets)
from bblove_v107_pilot import records, text

GROUP_LIMIT = 4 * 1024 * 1024
# Whole-corpus meta (alias + per-group article titles for search) was ~3MB with the
# legacy subset; the official full corpus needs more. Cloudflare static assets allow
# 25 MiB per file, so 12MB keeps a wide margin while still catching runaway growth.
META_LIMIT = 12_000_000
REQUIRED = {'근로기준법': '제36조', '지방세법': '제1조', '임금채권보장법': '제7조',
            '헌법재판소법': '제1조', '이자제한법': '제2조'}


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def content(value):
    # Official JSON uses str, list[str], and for tables nested lists / dicts of str.
    # Flatten in source order without dropping any text.
    if value is None:
        return ''
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return '\n'.join(s for s in (content(v) for v in value) if s)
    if isinstance(value, dict):
        return '\n'.join(s for s in (content(v) for v in value.values()) if s)
    raise ValueError('UNSUPPORTED_TEXT_SHAPE')


def paragraphs(node, level=0):
    lines = []
    for key, field in (('항', '항내용'), ('호', '호내용'), ('목', '목내용')):
        value = node.get(key)
        children = records(value)
        if value is not None and not isinstance(value, (dict, list)):
            raise ValueError('UNSUPPORTED_PARAGRAPH_SHAPE')
        if isinstance(value, list) and len(children) != len(value):
            raise ValueError('MALFORMED_PARAGRAPH')
        for child in children:
            body = content(child.get(field))
            if key == '항':
                body = re.sub(r'^([①-⑳㉑-㉟㊱-㊿])', r'**\1**', body)
                indent = ''
            else:
                body = re.sub(r'^(\d+)\.', r'\1\\.', body)
                indent = '  ' * (1 if key == '호' else 2)
            if body:
                lines.append(indent + body.replace('\n', '\n' + indent))
            lines.extend(paragraphs(child, level + 1))
    return lines


def articles(doc):
    result = []
    for unit in records(doc['법령']['조문']['조문단위']):
        if text(unit, '조문여부') in {'N', '전문'}:
            continue
        number = f'제{int(text(unit, "조문번호"))}조'
        sub = int(text(unit, '조문가지번호') or '0')
        if sub:
            number += f'의{sub}'
        body = '\n'.join([content(unit.get('조문내용')), *paragraphs(unit)])
        result.append([number, text(unit, '조문제목'), body])
    return result


def iso(value):
    return f'{value[:4]}-{value[4:6]}-{value[6:]}'


def load_groups(source, date, annexes=None, annex_skips=None):
    manifest = strict_json((source / 'manifest.json').read_bytes())
    if manifest.get('status') != 'COMPLETE' or not collection_complete(manifest.get('documents', [])):
        raise ValueError('INCOMPLETE_COLLECTION')
    groups, ids, paths = {}, set(), set()
    for entry in manifest['documents']:
        if entry['id'] in ids or entry['path'] in paths:
            raise ValueError('INVALID_MANIFEST_ENTRY')
        ids.add(entry['id']); paths.add(entry['path'])
        if entry['status'] == 'FAILED':
            continue
        path = (source / entry['path']).resolve()
        if not path.is_relative_to(source.resolve()):
            raise ValueError('UNSAFE_RAW_PATH')
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != entry['sha256']:
            raise ValueError('RAW_HASH_MISMATCH')
        row = entry['row']
        if (entry['id'], entry['mst'], entry['effective'], entry['published'], entry['title']) != (
                text(row, '법령ID'), text(row, '법령일련번호'), text(row, '시행일자'),
                text(row, '공포일자'), text(row, '법령명한글', '법령명_한글')):
            raise ValueError('MANIFEST_METADATA_MISMATCH')
        doc, count, warnings = validate_raw(raw, row, date, with_warnings=True)
        if entry.get('warnings', []) != warnings:
            raise ValueError('VALIDATION_WARNINGS_MISMATCH')
        if annexes is not None:
            # A malformed annex must never block the article build; credential markers still fail it.
            try:
                annexes.setdefault(entry['group'], []).extend(extract_annex(doc, entry))
            except ValueError as exc:
                if str(exc) == 'ANNEX_AUTH_MARKER':
                    raise
                if annex_skips is not None:
                    annex_skips.append({'id': entry['id'], 'title': entry['title'], 'reason': str(exc)[:80]})
        arts = articles(doc)
        if count != len(arts) or count != entry['articles']:
            raise ValueError('ARTICLE_COUNT_MISMATCH')
        groups.setdefault(entry['group'], []).append({'k': entry['kind'],
            'kc': text(row, '법령구분명'), 't': entry['title'], 'id': entry['id'],
            'eff': iso(entry['effective']), 'pub': iso(entry['published']), 'st': '시행',
            'url': 'https://www.law.go.kr/법령/' + quote(entry['title']), 'a': arts,
            **({'eff2': iso(w['detail'])} if (w := next((w for w in warnings if w.get('code') in
                {'effective_date_differs_from_catalog', 'detail_date_after_today'}), None)) else {})})
    if not groups:
        raise ValueError('NO_SUCCESSFUL_DOCUMENTS')
    for docs in groups.values():
        has_law = any(d['k'] == '법률' for d in docs)
        for d in docs:
            if d['k'] == '시행령' and not has_law:
                d['k'] = '대통령령'
        docs.sort(key=lambda d: (['법률', '시행령', '시행규칙', '대통령령'].index(d['k']), d['id']))
    if annexes is not None:
        by_id = {d['id']: d for values in groups.values() for d in values}
        for values in annexes.values():
            for item in values:
                item['kind'] = by_id[item['law_id']]['k']
    return manifest, dict(sorted(groups.items()))


def group_id(name):
    return hashlib.sha1(name.encode('utf-8')).hexdigest()[:12]


def build(source, repo, out, date=None, enforce_thresholds=True):
    date = date or today()
    annexes, annex_skips = {}, []
    manifest, groups = load_groups(source, date, annexes, annex_skips)
    docs = [d for values in groups.values() for d in values]
    checks = {title: any(d['t'] == title and d['k'] == '법률' and
                        any(a[0] == no and a[2].strip() for a in d['a']) for d in docs)
              for title, no in REQUIRED.items()}
    stats = {'n_groups': len(groups), 'n_docs': len(docs),
             'n_articles': sum(len(d['a']) for d in docs),
             'n_laws': sum(d['k'] == '법률' for d in docs)}
    if enforce_thresholds and (not all(checks.values()) or stats['n_laws'] < 1650 or
                               stats['n_docs'] < 4500 or stats['n_articles'] < 170000):
        raise ValueError('ACCEPTANCE_COUNTS_OR_REQUIRED_ARTICLES_FAILED ' + json.dumps(
            {'required': checks, **stats}, ensure_ascii=False))
    light, sizes, seen_ids = {}, [], set()
    # Build privately, then replace the output only after every gate succeeds.
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=out.parent) as temp:
        stage = Path(temp) / 'assets'
        (stage / 'groups').mkdir(parents=True)
        for group, values in groups.items():
            identifier = group_id(group)
            if identifier in seen_ids:
                raise ValueError('GROUP_ID_COLLISION')
            seen_ids.add(identifier)
            raw = encode(values)
            if not values or not 0 < len(raw) <= GROUP_LIMIT:
                raise ValueError('GROUP_FILE_SIZE_INVALID')
            (stage / 'groups' / f'{identifier}.json').write_bytes(raw)
            sizes.append(len(raw))
            law = next((d for d in values if d['k'] == '법률'), values[0])
            light[group] = [identifier, law['t'], law['eff'], 0]
        annex_index, annex_report = write_annex_assets(stage, annexes,
                                                      {g: group_id(g) for g in groups})
        verify_annex_assets(stage, annex_index, {g: group_id(g) for g in groups},
                            {d['id']: {**d, 'group': g} for g, values in groups.items() for d in values})
        alias = {}
        manual = strict_json((repo / 'scripts/alias_manual.json').read_bytes())
        for group, values in groups.items():
            # Manual keys are space-free folder names; official group names keep spaces.
            names = set(manual.get(group, [])) | set(manual.get(group.replace(' ', ''), []))
            for d in values:
                title = d['t']
                names.update((title, title.replace(' ', '')))
                match = re.match(r'^(.*?)(에 관한|의) (특별법|법률|특례법|기본법|보호법)$', title)
                if match:
                    names.add(match[1].replace(' ', '') + '법')
            for name in sorted(names):
                alias.setdefault(name, group)
        routes = {f.stem.replace('route_map_', ''): f.read_text(encoding='utf-8')
                  for f in sorted((repo / 'scripts').glob('route_map_*.md'))}
        for name, file in (('loan', 'BBLOVE-PACK-LOAN-V1/1.0.0/06_분야_route_대여금.md'),
                           ('dismissal', 'BBLOVE-PACK-DISMISSAL-V1/1.0.0/해고_04_분야_route_해고.md'),
                           ('tax', 'BBLOVE-PACK-TAXAUDIT-V1/1.0.2/세법_09_분야_route_조세조사.md')):
            # 공개 수집 저장소에는 지도(packs)가 없다. 있으면 쓰고, 없으면 검증용 빌드에서 생략한다.
            if (repo / 'packs' / file).is_file():
                routes[name] = (repo / 'packs' / file).read_text(encoding='utf-8')
        warnings = [{'id': e['id'], 'title': e['title'], 'mst': e['mst'], 'warnings': e['warnings']}
                    for e in manifest['documents'] if e['status'] == 'SUCCESS' and e.get('warnings')]
        meta = {**stats, 'annex': annex_index, 'warnings': warnings, 'groups': light, 'alias': alias, 'routes': routes,
                'precedents': [p.read_text(encoding='utf-8') for p in sorted(
                    (repo / 'auto/precedents').glob('*.md')) if p.name != 'INDEX.md'],
                # Compact form {group: [[no, title], ...]}: the group name is not repeated per article.
                'titles': {g: rows for g, values in groups.items() if (rows := [
                    [a[0], a[1]] for d in values if d['k'] == '법률' for a in d['a'] if a[1]])},
                'shards': [], 'source_commit': 'law.go.kr DRF ' + iso(manifest['date'])}
        meta_raw = encode(meta)
        if len(meta_raw) > META_LIMIT:
            raise ValueError('META_SIZE_LIMIT_EXCEEDED ' + json.dumps(
                {'meta_bytes': len(meta_raw), 'limit': META_LIMIT,
                 'by_key': {k: len(encode(v)) for k, v in meta.items()}}))
        (stage / 'meta.json').write_bytes(meta_raw)
        if out.exists():
            shutil.rmtree(out)
        shutil.move(str(stage), out)
    annex_report['skipped_documents'] = annex_skips
    return {**stats, 'annex': annex_report, 'required_articles': checks, 'future_documents': 0, 'empty_documents': 0,
            'shards': 0, 'group_files': len(sizes), 'max_group_bytes': max(sizes),
            'meta_bytes': len(meta_raw), 'warning_documents': len(warnings),
            'excluded_documents': len(failure_report(manifest['documents'])),
            'deterministic_status': 'PASS', 'official_original_review': 'REVIEW_REQUIRED'}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--source', type=Path, default=Path('auto/official-all'))
    ap.add_argument('--repo', type=Path, default=Path('.'))
    ap.add_argument('--out', type=Path, default=Path('mcp/assets'))
    ap.add_argument('--report', type=Path, default=Path('official-assets-report.json'))
    args = ap.parse_args()
    try:
        result = build(args.source, args.repo, args.out)
        # Run the deploy-time verifier now so a Release that deploy would reject is never published.
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from verify_mcp_assets_official import verify
        verify(args.out)
        result['release_verify'] = 'PASS'
    except Exception as exc:
        save(args.report, {'deterministic_status': 'FAIL', 'reason': 'OFFICIAL_ASSET_VALIDATION_FAILED',
                           'detail': f'{type(exc).__name__}: {str(exc)[:500]}'})
        print('OFFICIAL_ASSET_VALIDATION_FAILED')
        return 1
    save(args.report, result)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
