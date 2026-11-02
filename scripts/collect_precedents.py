#!/usr/bin/env python3
"""Long-running, resumable collection of precedent / NTS interpretation / tax-tribunal summaries.

Runs bblove_precedent_collect.collect() repeatedly with a fresh Client (each Client is capped
at about 58 minutes by design) until the whole catalog pass finishes or the overall time budget
is used. The cache directory is the only state; the next run resumes from it.

Outcomes (also written as `outcome=` / `complete=` step outputs):
  COMPLETE        every listed document collected
  PASS_DONE       every document attempted; some FAILED (listed in the manifest)
  PAUSED          time budget reached; resume next run (exit 0)
  FAILED          catalog/cache failure (attempt.json); exit 1
Only summary fields are stored (no full text), as in the original collector.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from bblove_law_all import Client, today  # noqa: E402
from bblove_precedent_collect import collect, verify_cache  # noqa: E402

MIN_SLICE = 120  # do not start a new Client with less than two minutes left
# The live catalog can change while it is being paged (new decisions are listed first by default).
# Before any detail of that target is fetched, restarting that one catalog is safe and loses nothing.
RESETTABLE = {'CATALOG_COUNT_CHANGED', 'CATALOG_DUPLICATE_ID', 'CATALOG_INCOMPLETE'}
TARGET_ORDER = ('prec', 'ntsCgmExpc', 'ttSpecialDecc')
MAX_RESETS = 2


def outcome(out):
    path = Path(out) / 'manifest.json'
    if not path.exists():
        return 'FAILED'
    m = json.loads(path.read_text(encoding='utf-8'))
    docs = m.get('documents', [])
    catalogs_done = bool(m.get('catalogs')) and all(c.get('complete') for c in m['catalogs'].values())
    if m.get('incremental') and not m['incremental'].get('complete'):
        return 'PAUSED'
    if m.get('status') == 'COMPLETE' and catalogs_done:
        return 'COMPLETE'
    if m.get('stop_reason') or any(d.get('status') == 'PENDING' for d in docs) or not catalogs_done:
        return 'PAUSED'
    return 'PASS_DONE'


def reset_catalog(out):
    """Restart the first unfinished catalog if none of its details were fetched. Returns the target or None."""
    out = Path(out)
    path = out / 'manifest.json'
    if not path.exists():
        return None
    m = json.loads(path.read_text(encoding='utf-8'))
    catalogs = m.get('catalogs', {})
    target = next((t for t in TARGET_ORDER if t in catalogs and not catalogs[t].get('complete')), None)
    if target is None or any(d.get('target') == target and 'sha256' in d for d in m.get('documents', [])):
        return None
    for page in [p for p in m.get('pages', []) if p.get('target') == target]:
        (out / page['path']).unlink(missing_ok=True)
    m['pages'] = [p for p in m.get('pages', []) if p.get('target') != target]
    m['documents'] = [d for d in m.get('documents', []) if d.get('target') != target]
    catalogs[target] = {'next_page': 1, 'complete': False}
    m.pop('stop_reason', None)
    path.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding='utf-8')
    return target


def run(out, scope, total_seconds, client_factory, clock=time.monotonic, collect_fn=collect, date=None, min_days=1):
    out = Path(out)
    deadline = clock() + total_seconds
    stop = lambda: 'TIME_BUDGET' if clock() >= deadline else None  # noqa: E731
    date = date or today()
    attempt = out / 'attempt.json'
    resets = 0
    while True:
        remaining = deadline - clock()
        if remaining < MIN_SLICE:
            return 'PAUSED'
        if attempt.exists():
            attempt.unlink()  # only this invocation's failures count
        collect_fn(client_factory(remaining), out, date, scope, stop=stop,
                   **({'incremental': True, 'min_days': min_days} if collect_fn is collect else {}))
        if attempt.exists():
            reason = json.loads(attempt.read_text(encoding='utf-8')).get('reason')
            if reason in RESETTABLE and resets < MAX_RESETS and reset_catalog(out):
                resets += 1
                print(f'CATALOG_RESET {reason} ({resets}/{MAX_RESETS})')
                continue
            return 'FAILED'
        state = outcome(out)
        if state != 'PAUSED':
            return state


def write_outputs(state, partial_targets=()):
    target = os.environ.get('GITHUB_OUTPUT')
    if target:
        with open(target, 'a', encoding='utf-8') as fh:
            fh.write(f'outcome={state}\ncomplete={"true" if state in ("COMPLETE", "PASS_DONE") else "false"}\n')
            fh.write(f'partial_targets={",".join(partial_targets)}\n')


def finished_targets(manifest):
    """Catalogs that are fully listed and have no PENDING document, in TARGET_ORDER."""
    catalogs = manifest.get('catalogs', {})
    pending = {d.get('target') for d in manifest.get('documents', []) if d.get('status') == 'PENDING'}
    return [t for t in TARGET_ORDER if catalogs.get(t, {}).get('complete') and t not in pending]


def export_complete(out, dest):
    """Copy the finished catalogs of an unfinished collection into dest/precedent-collection.

    Returns the exported targets ([] when nothing is finished or everything is, in which case the
    whole collection is published instead). The exported manifest keeps the cache schema, lists
    only the exported catalogs, pages and documents, and records them in `partial_targets` so a
    consumer never pages the missing catalogs itself.
    """
    out, dest = Path(out), Path(dest)
    path = out / 'manifest.json'
    if not path.exists():
        return []
    m = json.loads(path.read_text(encoding='utf-8'))
    targets = finished_targets(m)
    if not targets or len(targets) == len(TARGET_ORDER):
        return []
    keep = set(targets)
    export = dest / 'precedent-collection'
    if export.exists():
        shutil.rmtree(export)
    export.mkdir(parents=True)
    pages = [p for p in m.get('pages', []) if p.get('target') in keep]
    docs = [d for d in m.get('documents', []) if d.get('target') in keep]
    for item in pages + docs:
        if 'sha256' not in item:
            continue
        src = out / item['path']
        target_file = export / item['path']
        target_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, target_file)
    exported = dict(m)
    exported.update(
        catalogs={t: m['catalogs'][t] for t in targets}, pages=pages, documents=docs, partial_targets=targets,
        status='COMPLETE' if all(d.get('status') in ('SUCCESS', 'SKIPPED') for d in docs) else 'INCOMPLETE')
    exported.pop('stop_reason', None)
    (export / 'manifest.json').write_text(json.dumps(exported, ensure_ascii=False, indent=2), encoding='utf-8')
    try:
        verify_cache(export, exported)  # the export must pass the same checks a consumer applies
    except Exception as exc:
        shutil.rmtree(export)
        print(f'EXPORT_VERIFY_FAILED {exc}' if str(exc) in ('MANIFEST_SCHEMA_INVALID',) or str(exc).startswith('CACHE_')
              else 'EXPORT_VERIFY_FAILED')
        return []
    return targets


def summary(out):
    path = Path(out) / 'manifest.json'
    if not path.exists():
        return {}
    m = json.loads(path.read_text(encoding='utf-8'))
    counts, failures = {}, {}
    for d in m.get('documents', []):
        key = f"{d.get('target')}:{d.get('status')}"
        counts[key] = counts.get(key, 0) + 1
        if d.get('status') == 'FAILED':
            # Reasons are fixed allowlisted codes (never exception text), safe to publish.
            reason = f"{d.get('target')}:{d.get('reason')}"
            failures[reason] = failures.get(reason, 0) + 1
    return {'date': m.get('date'), 'status': m.get('status'), 'stop_reason': m.get('stop_reason'),
            **({'partial_targets': m['partial_targets']} if m.get('partial_targets') else {}),
            'catalogs': {t: {k: c.get(k) for k in ('total', 'complete')} for t, c in m.get('catalogs', {}).items()},
            **({'incremental': m['incremental']} if m.get('incremental') else {}),
            'counts': counts, 'failure_reasons': failures}


RELEASE_HISTORY_LIMIT = 1000


def published_targets(release_rows):
    """Union of every published precedent release's coverage, including full releases."""
    if not isinstance(release_rows, list):
        raise ValueError('RELEASE_HISTORY_INVALID')
    targets = set()
    for row in release_rows:
        if (not isinstance(row, dict) or not isinstance(row.get('tagName'), str)
                or type(row.get('isDraft')) is not bool):
            raise ValueError('RELEASE_HISTORY_INVALID')
        if row['isDraft']:
            continue
        tag = row['tagName']
        if re.fullmatch(r'prec-[0-9]{8}', tag):
            targets.update(TARGET_ORDER)
        elif tag.startswith('prec-'):
            match = re.fullmatch(r'prec-[0-9]{8}-partial-(.+)', tag)
            parts = match[1].split('-') if match else []
            if not parts or len(set(parts)) != len(parts) or not set(parts) <= set(TARGET_ORDER):
                raise ValueError('RELEASE_HISTORY_INVALID')
            targets.update(parts)
    return targets


def may_publish_partial(current, release_rows):
    current = set(current)
    if not current or not current < set(TARGET_ORDER):
        return False
    return published_targets(release_rows) <= current


def partial_publication_allowed(current, execute=subprocess.run):
    """Fail closed on request errors, stderr, invalid JSON or truncated history."""
    try:
        result = execute(['gh', 'release', 'list', '--limit', str(RELEASE_HISTORY_LIMIT),
                          '--json', 'tagName,isDraft'], capture_output=True, text=True, timeout=120)
        if result.returncode or result.stderr.strip():
            raise ValueError('RELEASE_HISTORY_REQUEST_FAILED')
        rows = json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        raise ValueError('RELEASE_HISTORY_REQUEST_FAILED') from None
    published_targets(rows)  # distinguish a valid [] from invalid/incomplete history
    if len(rows) >= RELEASE_HISTORY_LIMIT:
        raise ValueError('RELEASE_HISTORY_TRUNCATED')
    return may_publish_partial(current, rows)


def should_publish(manifest):
    inc = manifest.get('incremental')
    migrated = (manifest.get('summary_empty_migrated', 0) > 0
                and manifest.get('summary_empty_completed_date') == manifest.get('date'))
    return not inc or (inc.get('complete', False) and (inc['new_documents'] > 0 or migrated))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--out', type=Path, default=Path('auto/precedent-collection'))
    ap.add_argument('--scope', choices=('tax', 'all'), default='all')
    ap.add_argument('--min-days', type=int, default=1)
    ap.add_argument('--seconds', type=float, default=18000)
    ap.add_argument('--interval', type=float, default=1.0,
                    help='seconds between requests (sequential collector; >= 0.3)')
    ap.add_argument('--export-complete', type=Path, default=None,
                    help='no collection: copy the finished catalogs of --out here and print them')
    ap.add_argument('--finished-targets', action='store_true',
                    help='no collection: only write/print which catalogs of --out could be exported')
    ap.add_argument('--may-publish-partial', help='check release coverage for comma-separated targets; no collection')
    args = ap.parse_args()
    if args.may_publish_partial is not None:
        try:
            print('true' if partial_publication_allowed(args.may_publish_partial.split(',')) else 'false')
        except ValueError:
            print('::error::PARTIAL_RELEASE_HISTORY_FAILED')
            return 1
        return 0
    if args.finished_targets:
        path = args.out / 'manifest.json'
        m = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        targets = finished_targets(m)
        targets = [] if len(targets) == len(TARGET_ORDER) else targets
        write_outputs(outcome(args.out), targets)
        print(json.dumps({'partial_targets': targets}, ensure_ascii=False))
        return 0
    if args.export_complete is not None:
        targets = export_complete(args.out, args.export_complete)
        write_outputs(outcome(args.out), targets)
        print(json.dumps({'partial_targets': targets}, ensure_ascii=False))
        return 0
    credential = os.environ.get('LAW_OC', '')
    if not credential:
        print('LAW_OC_NOT_AVAILABLE')
        write_outputs('FAILED')
        return 1
    state = run(args.out, args.scope, args.seconds,
                lambda remaining: Client(credential, seconds=remaining, interval=args.interval), min_days=args.min_days)
    write_outputs(state)
    print(json.dumps({'outcome': state, **summary(args.out)}, ensure_ascii=False))
    return 1 if state == 'FAILED' else 0


if __name__ == '__main__':
    raise SystemExit(main())
