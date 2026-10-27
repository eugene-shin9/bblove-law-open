#!/usr/bin/env python3
"""Pre-collect past editions (시행일법령, target=eflaw) of the core tax laws and build
per-law history assets for the MCP server's `as_of` lookups.

Collection keeps the exact DRF JSON bytes (resumable, like the current-law collector);
the build packs effective-day groups into <=8MiB history parts, deduplicating
article bodies within each part. Collection and its selected window are unchanged.

  LAW_OC=... python src/bblove_law_history.py collect --assets mcp/assets --out auto/history
  python src/bblove_law_history.py build --out auto/history --dest history-assets
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from bblove_law_all import Client, save, safe_detail, stamp, today, write_step_output
from bblove_storage import strict_json
from bblove_tax_p2_collect import PilotBlocked
from bblove_v107_pilot import records, text, check_date
from build_mcp_assets_official import articles, encode, iso
from bblove_quality import credential_markers
from bblove_history_assets import write_assets, repack_assets, PART_LIMIT, FILE_LIMIT

TARGETS = ('국세기본법', '소득세법', '법인세법', '부가가치세법', '상속세 및 증여세법', '조세특례제한법',
           '국세징수법', '지방세기본법', '지방세법', '지방세특례제한법', '조세범 처벌법',
           '국제조세조정에 관한 법률', '종합부동산세법', '개별소비세법')
YEARS = 15


def cutoff(date, years=YEARS):
    return f'{int(date[:4]) - years}{date[4:]}'


def targets(assets):
    """(법령ID, title, kind) of every document in the target groups of the deployed current assets."""
    meta = strict_json((assets / 'meta.json').read_bytes())
    out = []
    for name in TARGETS:
        info = meta['groups'].get(name)
        if not info:
            raise ValueError(f'TARGET_GROUP_MISSING {name}')
        for d in strict_json((assets / 'groups' / f'{info[0]}.json').read_bytes()):
            if not re.fullmatch(r'\d{6}', d['id']):
                raise ValueError('TARGET_ID_INVALID')
            out.append({'id': d['id'], 'title': d['t'], 'kind': d['k'], 'group': name})
    return out


def edition_rows(client, lid):
    rows, seen, total = [], set(), None
    for page in range(1, 21):
        doc, _ = client.get('lawSearch.do', {'target': 'eflaw', 'LID': lid, 'nw': '1',
                                             'display': '100', 'page': str(page)})
        root = doc.get('LawSearch', {}) if isinstance(doc, dict) else {}
        total = int(text(root, 'totalCnt') or 0)
        batch = records(root.get('law'))
        for row in batch:
            key = (text(row, '법령일련번호'), text(row, '시행일자'))
            if (text(row, '법령ID') == lid and key[0].isdigit() and re.fullmatch(r'\d{8}', key[1])
                    and key not in seen):
                seen.add(key)
                rows.append({k: text(row, k) for k in ('법령ID', '법령명한글', '법령일련번호', '시행일자',
                                                       '공포일자', '공포번호', '제개정구분명', '법령구분명')})
        if len(batch) < 100 or page * 100 >= total:
            break
    return rows


def edition_key(r):
    # Same 시행일 → latest 공포일, then numerically largest 일련번호 (same order as the Worker's live pick).
    return (r['시행일자'], r.get('공포일자') or '', int(r['법령일련번호']))


def select(rows, date):
    """Editions in force at some point in the window: every one effective on/after the
    cutoff, plus the one already in force on the cutoff day. Never a future edition."""
    start = cutoff(date)
    past = sorted((r for r in rows if r['시행일자'] <= date), key=edition_key)
    keep = [r for r in past if r['시행일자'] >= start]
    before = [r for r in past if r['시행일자'] < start]
    return ([before[-1]] if before else []) + keep


def fetch_edition(client, out, lid, row, old):
    rel = f'{lid}/{row["법령일련번호"]}_{row["시행일자"]}.json'
    entry = {**row, 'path': rel}
    file = out / rel
    try:
        raw = file.read_bytes() if file.exists() else b''
        if not (old.get('status') == 'SUCCESS' and old.get('sha256') == hashlib.sha256(raw).hexdigest()):
            _, raw = client.get('lawService.do', {'target': 'eflaw', 'MST': row['법령일련번호'],
                                                  'efYd': row['시행일자']}, original=True)
            if credential_markers(raw):
                raise ValueError('CREDENTIAL_BEARING_ORIGINAL_NOT_PERSISTED')
            file.parent.mkdir(parents=True, exist_ok=True)
            tmp = file.with_suffix('.tmp')
            tmp.write_bytes(raw)
            tmp.replace(file)
        validate_edition(raw, lid, row)
        entry.update(status='SUCCESS', sha256=hashlib.sha256(raw).hexdigest(),
                     collected_at=old.get('collected_at') or stamp())
    except Exception as exc:
        entry.update(status='FAILED', detail=safe_detail(exc, getattr(client, 'credential', '')),
                     budget=isinstance(exc, PilotBlocked) and str(exc) == 'COLLECTION_BUDGET_EXHAUSTED')
    return entry


def validate_edition(raw, lid, row=None):
    doc = strict_json(raw)
    basic = doc.get('법령', {}).get('기본정보', {}) if isinstance(doc, dict) else {}
    if text(basic, '법령ID') != lid:
        raise ValueError('EDITION_LAW_ID_MISMATCH')
    if row:
        # The body must be the edition that was requested (A03): same 시행일, 일련번호, 공포번호 when given.
        for body_key, row_key in (('시행일자', '시행일자'), ('법령일련번호', '법령일련번호'), ('공포번호', '공포번호')):
            if text(basic, body_key) and text(row, row_key) and text(basic, body_key) != text(row, row_key):
                raise ValueError('EDITION_IDENTITY_MISMATCH')
    arts = articles(doc)
    if not arts:
        raise ValueError('EDITION_WITHOUT_ARTICLES')
    units = [u for u in records(doc['법령']['조문']['조문단위']) if text(u, '조문여부') not in {'N', '전문'}]
    deleted = {i for i, u in enumerate(units) if text(u, '조문제목') == '삭제' or text(u, '조문삭제여부') in {'Y', '삭제'}}
    seen = {}
    for i, (no, title, body) in enumerate(arts):
        if not body.strip() and i not in deleted:
            raise ValueError('EDITION_EMPTY_ARTICLE')  # A07: an empty non-deleted article is a defect, not text
        if seen.setdefault(no, body) != body:
            raise ValueError('EDITION_CONFLICTING_DUPLICATE_ARTICLE')
    return doc, arts


def collect(client, assets, out, date):
    path = out / 'history-manifest.json'
    previous = strict_json(path.read_bytes()) if path.exists() else {}
    old = {(e['법령ID'], e['법령일련번호'], e['시행일자']): e
           for law in previous.get('laws', []) for e in law.get('editions', [])}
    manifest = {'schema': 1, 'date': date, 'cutoff': cutoff(date), 'started_at': stamp(), 'laws': []}
    for target in targets(assets):
        law = {**target, 'editions': []}
        try:
            chosen = select(edition_rows(client, target['id']), date)
            if not chosen:
                raise ValueError('NO_EDITIONS')
            with ThreadPoolExecutor(max_workers=3) as pool:
                law['editions'] = list(pool.map(lambda r: fetch_edition(
                    client, out, target['id'], r, old.get((r['법령ID'], r['법령일련번호'], r['시행일자']), {})), chosen))
        except Exception as exc:
            law.update(status='LIST_FAILED', detail=safe_detail(exc, getattr(client, 'credential', '')))
        manifest['laws'].append(law)
        save(path, manifest)
    editions = [e for law in manifest['laws'] for e in law['editions']]
    manifest.update(completed_at=stamp(), api_calls=client.calls,
                    # complete = every list fetched and every edition attempted (not cut by the time budget);
                    # individual validation failures are reported and excluded, never silently dropped.
                    complete=all('status' not in law for law in manifest['laws'])
                    and not any(e.get('budget') for e in editions),
                    counts={'laws': len(manifest['laws']), 'editions': len(editions),
                            'success': sum(e['status'] == 'SUCCESS' for e in editions)})
    save(path, manifest)
    return manifest


def budget_only(manifest):
    """True when every reason for incompleteness is the time budget (resumable), not a real list failure."""
    laws = manifest.get('laws', [])
    list_failures = [law for law in laws if 'status' in law]
    return all(law.get('detail') == 'COLLECTION_BUDGET_EXHAUSTED' for law in list_failures)


def build(out, dest, date):
    manifest = strict_json((out / 'history-manifest.json').read_bytes())

    def documents():
        for law in manifest['laws']:
            editions, bodies = [], {}
            for e in sorted(law['editions'], key=edition_key):
                pub = iso(e['공포일자']) if re.fullmatch(r'\d{8}', e.get('공포일자') or '') else ''
                if e['status'] != 'SUCCESS':
                    # A02: keep the gap, including at a part boundary.
                    editions.append({'eff': iso(e['시행일자']), 'pub': pub, 'mst': e['법령일련번호'], 'missing': True, 'a': []})
                    continue
                raw = (out / e['path']).read_bytes()
                if hashlib.sha256(raw).hexdigest() != e['sha256']:
                    raise ValueError('RAW_HASH_MISMATCH')
                check_date(e['시행일자'], 'history effective', date)
                _, arts = validate_edition(raw, law['id'], e)
                rows = []
                for no, title, body in arts:
                    h = hashlib.sha1(body.encode('utf-8')).hexdigest()[:16]
                    if bodies.setdefault(h, body) != body:
                        raise ValueError('BODY_HASH_COLLISION')
                    rows.append([no, title, h])
                editions.append({'eff': iso(e['시행일자']), 'pub': pub,
                                 'mst': e['법령일련번호'], 'amend': e['제개정구분명'], 'title': e['법령명한글'] or law['title'],
                                 'a': rows})
            if editions:
                yield {'id': law['id'], 't': law['title'], 'k': law['kind'], 'group': law['group'],
                       'editions': editions, 'bodies': bodies}
    return write_assets(documents(), dest, iso(manifest['date']), iso(manifest['cutoff']))


def report(manifest, built=None):
    lines = [f"# 세법 과거 판본 수집 ({manifest['date']}, {iso(manifest['cutoff'])} 이후)",
             f"- complete: {manifest.get('complete')} / counts: {json.dumps(manifest.get('counts'), ensure_ascii=False)}"
             f" / api_calls: {manifest.get('api_calls')}"]
    if built:
        lines.append(f'- build: {json.dumps(built, ensure_ascii=False)}')
    for law in manifest['laws']:
        ok = sum(e['status'] == 'SUCCESS' for e in law['editions'])
        bad = [e for e in law['editions'] if e['status'] != 'SUCCESS']
        lines.append(f"- {law['title']} ({law['id']}): {ok}/{len(law['editions'])}"
                     + (f" {law.get('status')} {law.get('detail', '')}" if law.get('status') else '')
                     + ''.join(f"\n  - FAILED {e['시행일자']} {e['법령일련번호']}: {e.get('detail', '')}" for e in bad[:5]))
    return '\n'.join(lines) + '\n'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('command', choices=['collect', 'build', 'report', 'repack'])
    ap.add_argument('--assets', type=Path, default=Path('mcp/assets'))
    ap.add_argument('--out', type=Path, default=Path('auto/history'))
    ap.add_argument('--dest', type=Path, default=Path('history-assets'))
    ap.add_argument('--report', type=Path)
    ap.add_argument('--partial-ok', action='store_true',
                    help='exit 0 when the run stopped only at the time budget; next run resumes')
    args = ap.parse_args()
    date = today()
    if args.command == 'collect':
        credential = os.environ.get('LAW_OC', '')
        if not credential:
            print('LAW_OC_NOT_AVAILABLE')
            return 1
        m = collect(Client(credential), args.assets, args.out, date)
        print(json.dumps({'complete': m['complete'], 'counts': m['counts'], 'api_calls': m['api_calls']}, ensure_ascii=False))
        write_step_output(m['complete'])
        return 0 if m['complete'] or (args.partial_ok and budget_only(m)) else 1
    if args.command == 'build':
        result = build(args.out, args.dest, date)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    if args.command == 'repack':
        print(json.dumps(repack_assets(args.assets, args.dest), ensure_ascii=False))
        return 0
    manifest = strict_json((args.out / 'history-manifest.json').read_bytes())
    built = None
    index = args.dest / 'history' / 'index.json'
    if index.exists():
        idx = strict_json(index.read_bytes())['laws']
        built = {'laws': len(idx), 'editions': sum(v['n'] for v in idx.values())}
    text_out = report(manifest, built)
    if args.report:
        args.report.write_text(text_out, encoding='utf-8')
    print(text_out)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
