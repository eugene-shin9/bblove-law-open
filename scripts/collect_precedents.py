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
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from bblove_law_all import Client, today  # noqa: E402
from bblove_precedent_collect import collect  # noqa: E402

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


def run(out, scope, total_seconds, client_factory, clock=time.monotonic, collect_fn=collect, date=None):
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
        collect_fn(client_factory(remaining), out, date, scope, stop=stop)
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


def write_outputs(state):
    target = os.environ.get('GITHUB_OUTPUT')
    if target:
        with open(target, 'a', encoding='utf-8') as fh:
            fh.write(f'outcome={state}\ncomplete={"true" if state in ("COMPLETE", "PASS_DONE") else "false"}\n')


def summary(out):
    path = Path(out) / 'manifest.json'
    if not path.exists():
        return {}
    m = json.loads(path.read_text(encoding='utf-8'))
    counts = {}
    for d in m.get('documents', []):
        key = f"{d.get('target')}:{d.get('status')}"
        counts[key] = counts.get(key, 0) + 1
    return {'date': m.get('date'), 'status': m.get('status'), 'stop_reason': m.get('stop_reason'),
            'catalogs': {t: {k: c.get(k) for k in ('total', 'complete')} for t, c in m.get('catalogs', {}).items()},
            'counts': counts}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--out', type=Path, default=Path('auto/precedent-collection'))
    ap.add_argument('--scope', choices=('tax', 'all'), default='all')
    ap.add_argument('--seconds', type=float, default=18000)
    args = ap.parse_args()
    credential = os.environ.get('LAW_OC', '')
    if not credential:
        print('LAW_OC_NOT_AVAILABLE')
        write_outputs('FAILED')
        return 1
    state = run(args.out, args.scope, args.seconds, lambda remaining: Client(credential, seconds=remaining))
    write_outputs(state)
    print(json.dumps({'outcome': state, **summary(args.out)}, ensure_ascii=False))
    return 1 if state == 'FAILED' else 0


if __name__ == '__main__':
    raise SystemExit(main())
