"""Verify summary cache, build resumable SQL day batches, optionally load D1.

No API calls unless --load is explicitly supplied. Database identifiers are
non-secret CLI/config inputs. Authentication is inherited by Wrangler via env.
"""
import argparse
from datetime import datetime, timedelta, timezone
from fractions import Fraction
import json
import math
from pathlib import Path
import re
import sqlite3
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from bblove_law_all import save
from bblove_precedent_collect import SCHEMA, TARGETS, validated, verify_cache
from bblove_precedent_scope import tax_reasons
from bblove_storage import digest, strict_json

DATABASE_NAME = 'bblove-precedents'
DAILY_LIMIT = 85_000
CALIBRATION_DOCUMENTS = 2_000
SCHEMA_RESERVE = 512
WRITE_CHUNK_DOCUMENTS = 250
DB_LIMIT = 500_000_000
COLUMNS = {
    'precedents': ('id', 'case_no', 'court', 'date', 'title', 'issues', 'holding', 'refs', 'url'),
    'interpretations': ('id', 'number', 'title', 'date', 'question', 'answer', 'laws', 'url'),
    'tribunal_decisions': ('id', 'number', 'date', 'decision_type', 'tax_type', 'title', 'summary', 'laws', 'url'),
}
SEARCH = {'precedents': ('title', 'issues', 'holding', 'refs'),
          'interpretations': ('title', 'question', 'answer', 'laws'),
          'tribunal_decisions': ('title', 'summary', 'laws', 'decision_type', 'tax_type')}
TABLES = {'prec': 'precedents', 'ntsCgmExpc': 'interpretations', 'ttSpecialDecc': 'tribunal_decisions'}


def schema(tokenizer='trigram'):
    if tokenizer not in ('trigram', 'unicode61'):
        raise ValueError('TOKENIZER_INVALID')
    result = []
    for table, columns in COLUMNS.items():
        definitions = ','.join(c + (' TEXT PRIMARY KEY' if c == 'id' else ' TEXT NOT NULL') for c in columns)
        result.append(f'CREATE TABLE IF NOT EXISTS {table} ({definitions});')
        fields = ','.join(SEARCH[table])
        result.append(f"CREATE VIRTUAL TABLE IF NOT EXISTS {table}_fts USING fts5({fields}, content='{table}', content_rowid='rowid', tokenize='{tokenizer}');")
        result.append(f'CREATE TRIGGER IF NOT EXISTS {table}_ai AFTER INSERT ON {table} BEGIN '
                      f'INSERT INTO {table}_fts(rowid,{fields}) VALUES(new.rowid,'
                      + ','.join('new.' + c for c in SEARCH[table]) + '); END;')
    return '\n'.join(result) + '\n'


def literal(value):
    if '\x00' in value:
        raise ValueError('SQL_NUL_INVALID')
    return "'" + value.replace("'", "''") + "'"


def insert_sql(table, row):
    return f'INSERT OR IGNORE INTO {table} (' + ','.join(COLUMNS[table]) + ') VALUES (' + ','.join(literal(row[c]) for c in COLUMNS[table]) + ');\n'


def write_bound(table, row):
    """Conservative admission units, including FTS shadow rows and split reserve.

    A trigram can add <= 3 postings per character across boundaries. Reserve
    eight units per indexed code point, plus 64 for base/PK/shadow B-tree work.
    This deliberately overestimates the local total_changes measurement. It is
    a planning bound, not a claim about undocumented remote D1 billing internals.
    Remote rows_written is also checked; any bound breach stops further writes.
    """
    return 64 + 8 * sum(len(row[c]) for c in SEARCH[table])


def budget_model(state, day):
    """Only successful REMOTE document receipts from earlier UTC dates qualify.

    SQLite total_changes, today's receipts, schema writes, different databases
    and different FTS schemas cannot calibrate the next admission window.
    """
    fingerprint = digest(schema().encode('utf-8'))
    samples = []
    for sample in state.get('measurements', []):
        if (sample.get('source') != 'remote_meta.rows_written' or sample.get('day', day) >= day
                or sample.get('database_id') != state.get('database_id')
                or sample.get('schema_sha256') != fingerprint):
            continue
        if (type(sample.get('documents')) is not int or sample['documents'] <= 0
                or type(sample.get('rows_written')) is not int or sample['rows_written'] <= 0
                or not re.fullmatch(r'[0-9a-f]{64}', sample.get('sql_sha256', ''))):
            raise ValueError('REMOTE_MEASUREMENT_INVALID')
        samples.append(sample)
    if not samples:
        return {'mode': 'conservative', 'formula': '64 + 8 * indexed_characters'}
    documents = sum(s['documents'] for s in samples)
    writes = sum(s['rows_written'] for s in samples)
    return dict(mode='measured', source='remote_meta.rows_written', documents=documents,
                rows_written=writes, rows_per_document=writes / documents,
                multiplier=1.5, samples=len(samples), measured_through=max(s['day'] for s in samples),
                schema_sha256=fingerprint,
                evidence_sha256=digest(json.dumps(samples, sort_keys=True).encode('utf-8')))


def reservation_cost(table, row, model=None):
    if not model or model['mode'] == 'conservative':
        return Fraction(write_bound(table, row))
    if model['mode'] != 'measured' or model['documents'] <= 0 or model['rows_written'] <= 0:
        raise ValueError('BUDGET_MODEL_INVALID')
    # Keep 13.5 exact for a measured rate of 9; round once per SQL chunk.
    return Fraction(model['rows_written'] * 3, model['documents'] * 2)


def load_rows(source, scope='all'):
    if scope not in ('tax', 'all'):
        raise ValueError('SCOPE_INVALID')
    manifest = strict_json((source / 'manifest.json').read_bytes())
    if manifest.get('schema') != SCHEMA:
        raise ValueError('MANIFEST_SCHEMA_INVALID')
    verify_cache(source, manifest)
    rows, failures, excluded, skipped = [], [], 0, 0
    for entry in manifest['documents']:
        if entry['status'] == 'SKIPPED':
            skipped += 1  # source not collected by design (e.g. rows without case numbers)
            continue
        if entry['status'] != 'SUCCESS':
            failures.append({'target': entry['target'], 'id': entry['id'], 'reason': entry.get('reason', 'PENDING')})
            continue
        detail = strict_json((source / entry['path']).read_bytes())
        row = validated(entry['row'], detail, entry['target'], entry['kind'])
        if (scope == 'tax' and entry['target'] == 'prec'
                and not (tax_reasons(row, entry['row']) or tax_reasons(row, detail))):
            excluded += 1
            continue
        rows.append((TABLES[entry['target']], row))
    manifest['scope_selection'] = dict(scope=scope, excluded_documents=excluded, skipped_sources=skipped, rule='tax-v2')
    return manifest, rows, failures


def build(source, out, loaded=None, tokenizer='trigram', daily_limit=DAILY_LIMIT,
          model=None, scope='all', max_batch_documents=None):
    if not 1 <= daily_limit <= DAILY_LIMIT:
        raise ValueError('DAILY_LIMIT_INVALID')
    if max_batch_documents is not None and max_batch_documents < 1:
        raise ValueError('BATCH_DOCUMENT_LIMIT_INVALID')
    manifest, rows, failures = load_rows(source, scope)
    loaded = {table: set(ids) for table, ids in (loaded or {}).items()}
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob('batch-*.sql'):
        old.unlink()
    (out / 'schema.sql').write_text(schema(tokenizer), encoding='utf-8')
    batch, units, batches, selected, skipped = [], Fraction(0), [], [], 0
    day, day_units = 1, 0
    def flush():
        nonlocal batch, units
        if not batch:
            return
        name = f'batch-{len(batches)+1:05d}.sql'
        raw = ''.join(insert_sql(t, r) for t, r in batch).encode('utf-8')
        (out / name).write_bytes(raw)
        batches.append(dict(path=name, sha256=digest(raw), documents=len(batch), write_units=math.ceil(units), day=day,
                            ids={t: [r['id'] for table, r in batch if table == t] for t in COLUMNS}))
        batch, units = [], Fraction(0)
    for table, row in rows:
        if row['id'] in loaded.get(table, []):
            skipped += 1
            continue
        cost = reservation_cost(table, row, model)
        if cost > daily_limit or len(insert_sql(table, row).encode('utf-8')) > 90_000:
            raise ValueError('SINGLE_DOCUMENT_EXCEEDS_SAFE_BUDGET')
        # Sum rounded chunk reservations within each day, so fractional rows
        # cannot accumulate past the daily cap at chunk boundaries.
        extra = math.ceil(units + cost) - math.ceil(units)
        chunk_full = max_batch_documents is not None and len(batch) >= max_batch_documents
        if chunk_full:
            flush()
            extra = math.ceil(cost)
        if day_units + extra > daily_limit:
            flush()
            day, day_units, extra = day + 1, 0, math.ceil(cost)
        batch.append((table, row))
        selected.append((table, row))
        units += cost
        day_units += extra
    flush()
    # Materialize the exact schema/index for a reproducible size estimate.
    db = sqlite3.connect(':memory:')
    db.executescript(schema(tokenizer))
    changes = db.total_changes
    for table, row in rows:
        db.execute(insert_sql(table, row))
    changes = db.total_changes - changes
    size = db.execute('PRAGMA page_count').fetchone()[0] * db.execute('PRAGMA page_size').fetchone()[0]
    db.close()
    if size > DB_LIMIT:
        raise ValueError('DATABASE_EXCEEDS_FREE_LIMIT')
    report = dict(tokenizer=tokenizer, total_documents=len(rows), pending_or_failed=failures,
                  skipped_ids=skipped, selected_documents=len(selected), batches=batches,
                  estimated_days=batches[-1]['day'] if batches else 0, local_database_bytes=size,
                  scope_selection=manifest['scope_selection'], budget_model=model or {'mode': 'conservative'},
                  daily_limit=daily_limit, local_total_changes=changes, schema_write_reserve=SCHEMA_RESERVE,
                  # Old two-target snapshots remain buildable; missing tribunal
                  # catalogs are unknown, never reported as a measured zero.
                  catalog_totals={t: manifest['catalogs'].get(t, {}).get('total') for t in TARGETS},
                  status='READY' if not failures else 'PARTIAL_WITH_FAILURES')
    save(out / 'build-report.json', report)
    return report


def wrangler_output(command, as_json=True):
    """Suppress raw CLI stderr/stdout on failure: it may contain request URLs."""
    try:
        proc = subprocess.run(command + (['--json'] if as_json else []), capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        raise ValueError('WRANGLER_EXECUTE_TIMEOUT') from None
    if proc.returncode:
        raise ValueError('WRANGLER_EXECUTE_FAILED')
    if not as_json:
        return None  # d1 create has no --json flag; never parse/log human output.
    decoder = json.JSONDecoder()
    for start in re.finditer(r'[\[{]', proc.stdout):
        try:
            result, end = decoder.raw_decode(proc.stdout, start.start())
        except ValueError:
            continue
        if not proc.stdout[end:].strip():
            return result
    raise ValueError('WRANGLER_RESPONSE_INVALID')


def wrangler_json(command):
    result = wrangler_output(command)
    if not isinstance(result, list) or not result or any(not x.get('success') for x in result):
        raise ValueError('WRANGLER_RESPONSE_FAILED')
    return result


def utc_day():
    return datetime.now(timezone.utc).strftime('%Y-%m-%d')


def remote_load(source, out, state_file, config, database, wrangler, execute=wrangler_json,
                day=None, clock=None, scope='all'):
    """One serialized loader, one UTC ledger shared across workflow runs.

    Reserve admission units BEFORE each remote write. A crash or unknown result
    consumes the reservation. Rebuild from remote IDs on every run, so a previous
    partial import is safe and cache eviction cannot duplicate document writes.
    """
    clock = clock or utc_day
    day = day or clock()
    # Verify all local input before any remote mutation.
    load_rows(source, scope)
    state = strict_json(state_file.read_bytes()) if state_file.exists() else {'days': {}}
    ledger = state['days'].setdefault(day, {'reserved': 0, 'rows_written': 0})
    model = ledger.setdefault('budget_model', budget_model(state, day))
    fingerprint = digest(schema().encode('utf-8'))
    if model['mode'] == 'measured' and model.get('schema_sha256') != fingerprint:
        # An existing UTC reservation is never refunded on schema expansion.
        model = ledger['budget_model'] = {'mode': 'conservative', 'formula': '64 + 8 * indexed_characters'}
    ledger.setdefault('documents_written', 0)
    if ledger.get('halted') or max(ledger['reserved'], ledger['rows_written']) >= DAILY_LIMIT:
        return state
    base = [wrangler, 'd1', 'execute', database, '--remote', '--config', str(config)]
    def reserve(cost):
        if day != clock():
            raise ValueError('UTC_WRITE_WINDOW_CHANGED')
        if max(ledger['reserved'], ledger['rows_written']) + cost > DAILY_LIMIT:
            return False
        ledger['reserved'] = max(ledger['reserved'], ledger['rows_written']) + cost
        save(state_file, state)
        return True
    def write(path, cost, batch=None):
        if not reserve(cost):
            return False
        result = execute(base + ['--file', str(path)])
        if any(type(x.get('meta', {}).get('rows_written')) is not int or x['meta']['rows_written'] < 0 for x in result):
            raise ValueError('REMOTE_WRITE_RECEIPT_MISSING')
        observed = sum(x['meta']['rows_written'] for x in result)
        ledger['rows_written'] += observed
        if batch:
            ledger['documents_written'] += batch['documents']
            state.setdefault('measurements', []).append(dict(
                source='remote_meta.rows_written', day=day, documents=batch['documents'], rows_written=observed,
                database_id=state.get('database_id'), schema_sha256=digest(schema().encode('utf-8')),
                sql_sha256=batch['sha256'], reserved=cost, scope=scope))
        save(state_file, state)
        if observed > cost:
            ledger['halted'] = 'REMOTE_WRITE_BOUND_EXCEEDED'
            ledger['reserved'] = max(ledger['reserved'], ledger['rows_written'])
            save(state_file, state)
            raise ValueError('REMOTE_WRITE_BOUND_EXCEEDED')
        return True
    out.mkdir(parents=True, exist_ok=True)
    (out / 'schema.sql').write_text(schema(), encoding='utf-8')
    if not state.get('schema_ready') or state.get('schema_sha256') != fingerprint:
        if not write(out / 'schema.sql', SCHEMA_RESERVE):
            return state
        state['schema_ready'] = True
        state['schema_sha256'] = fingerprint
        save(state_file, state)
    loaded = {}
    for table in COLUMNS:
        ids, cursor = [], ''
        while True:
            query = f'SELECT id FROM {table} WHERE id > {literal(cursor)} ORDER BY id LIMIT 1000;'
            results = execute(base + ['--command', query])
            page = [str(r['id']) for block in results for r in block['results']]
            if any(not re.fullmatch(r'[0-9]+', i) for i in page) or (page and page[-1] <= cursor):
                raise ValueError('REMOTE_ID_PAGE_INVALID')
            ids.extend(page)
            if len(page) < 1000:
                break
            cursor = page[-1]
        loaded[table] = ids
    # Cache hint only; every import still reads authoritative remote IDs.
    receipt_file = source / 'd1-loaded.json'
    save(receipt_file, dict(database_id=state.get('database_id'), ids=loaded))
    # Inspect actual DB size before each admission, leaving a conservative SQL
    # payload multiplier for the next FTS index growth.
    # Remote D1 exposes size_after in query metadata. Avoid relying on PRAGMAs
    # that its SQL allowlist may reject even though local SQLite permits them.
    sizes = execute(base + ['--command', 'SELECT 1 AS capacity_probe;'])
    size = sizes[-1].get('meta', {}).get('size_after')
    if type(size) is not int or size < 0:
        raise ValueError('REMOTE_DATABASE_SIZE_MISSING')
    remaining = DAILY_LIMIT - max(ledger['reserved'], ledger['rows_written'])
    if remaining <= 0:
        return state
    chunk_documents = WRITE_CHUNK_DOCUMENTS
    if model['mode'] == 'conservative':
        chunk_documents = min(chunk_documents, CALIBRATION_DOCUMENTS - ledger['documents_written'])
        if chunk_documents <= 0:
            return state
    report = build(source, out, loaded, daily_limit=remaining, model=model, scope=scope,
                   max_batch_documents=chunk_documents)
    for batch in report['batches']:
        if batch['day'] != 1:
            break
        if model['mode'] == 'conservative' and ledger['documents_written'] + batch['documents'] > CALIBRATION_DOCUMENTS:
            break
        file = out / batch['path']
        if digest(file.read_bytes()) != batch['sha256']:
            raise ValueError('SQL_BATCH_SHA_MISMATCH')
        if size + file.stat().st_size * 16 + 1_000_000 > DB_LIMIT:
            raise ValueError('REMOTE_DATABASE_CAPACITY_RESERVE_EXCEEDED')
        if not write(file, batch['write_units'], batch):
            break
        for table, ids in batch['ids'].items():
            loaded[table].extend(ids)
        save(receipt_file, dict(database_id=state.get('database_id'), ids=loaded))
        size += file.stat().st_size * 16
    # The durable GitHub state stays small: SQL ID inventories belong to the
    # build artifact, not duplicated into the quota/measurement ledger.
    state.update(last_build={k: report[k] for k in ('selected_documents', 'skipped_ids', 'estimated_days',
                 'scope_selection', 'budget_model', 'status')}, updated_utc=day)
    save(state_file, state)
    return state


def progress(source, out, state_file):
    manifest = strict_json((source / 'manifest.json').read_bytes()) if (source / 'manifest.json').exists() else {}
    report = strict_json((out / 'build-report.json').read_bytes()) if (out / 'build-report.json').exists() else {}
    state = strict_json(state_file.read_bytes()) if state_file.exists() else {}
    next_day = (datetime.strptime(utc_day(), '%Y-%m-%d') + timedelta(days=1)).strftime('%Y-%m-%d')
    next_policy = budget_model(state, next_day)
    capacity = (DAILY_LIMIT * next_policy['documents'] * 2 // (next_policy['rows_written'] * 3)
                if next_policy['mode'] == 'measured' else 'UNMEASURED')
    docs = manifest.get('documents', [])
    loaded_total = sum(d.get('documents_written', 0) for d in state.get('days', {}).values())
    totals = [manifest.get('catalogs', {}).get(t, {}).get('total') for t in TARGETS]
    estimated_total = sum(totals) if all(type(n) is int for n in totals) else None
    successful_days = [d.get('documents_written', 0) for d in state.get('days', {}).values()
                       if d.get('documents_written', 0) > 0]
    # Observed daily throughput includes the time cap; row-only capacity is not an ETA.
    rate = sum(successful_days) / len(successful_days) if successful_days else 0
    if isinstance(capacity, int):
        rate = min(rate, capacity)
    remaining_days = math.ceil(max(0, estimated_total - loaded_total) / rate) if estimated_total is not None and rate else '미측정'
    summary = (f"- 이번 실행 {state.get('last_run', {}).get('elapsed_minutes', 0):.2f}분 · "
               f"누적 적재 {loaded_total:,}행 / 추정 전체 {estimated_total if estimated_total is not None else '미측정'}행 · "
               f"남은 예상 실행 일수 {remaining_days}일")
    lines = ['# Precedent D1 progress', summary,
             '- 적재 행은 문서 행(FTS 쓰기 제외); 전체 카탈로그 수는 tax 범위의 상한. 일수는 관측 일별 처리량 기준 추정이며 초기에는 미측정.', f"- Collection: {manifest.get('status', 'NOT_STARTED')}; documents={len(docs)}, success={sum(e['status']=='SUCCESS' for e in docs)}",
             f"- D1 database ID: {state.get('database_id', 'NOT_PREPARED')}; name={state.get('database_name', 'NOT_PREPARED')}",
             f"- Catalog totals: {json.dumps(report.get('catalog_totals', manifest.get('catalogs', {})), ensure_ascii=False)}",
             f"- Build: {report.get('status', 'NOT_BUILT')}; skipped IDs={report.get('skipped_ids', 0)}; current-window policy plan days={report.get('estimated_days', '?')}",
             f"- Tokenizer={report.get('tokenizer', 'trigram')}; local estimated DB bytes={report.get('local_database_bytes', '?')}",
             f"- UTC write ledger: {json.dumps(state.get('days', {}), sort_keys=True)}",
             f"- Next UTC day budget ({next_day}): {json.dumps(next_policy, ensure_ascii=False, sort_keys=True)}; estimated documents/day={capacity}",
             f"- Scope: {json.dumps(report.get('scope_selection', {}), ensure_ascii=False)}",
             '- Independent official-original comparison and event-time applicability: REVIEW_REQUIRED.']
    for e in docs:
        if e['status'] == 'FAILED':
            lines.append(f"- FAILED {e['target']}/{e['id']}: {e['reason']}")
    return '\n'.join(lines) + '\n'


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--source', type=Path, default=Path('release/precedent-collection'))
    ap.add_argument('--out', type=Path, default=Path('release/precedent-sql'))
    ap.add_argument('--loaded', type=Path, help='JSON {precedents:[id],interpretations:[id],tribunal_decisions:[id]}')
    ap.add_argument('--state', type=Path, default=Path('release/precedent-d1-state.json'))
    ap.add_argument('--tokenizer', choices=['trigram', 'unicode61'], default='trigram')
    ap.add_argument('--load', action='store_true')
    ap.add_argument('--progress', action='store_true')
    ap.add_argument('--config', type=Path)
    ap.add_argument('--database')
    ap.add_argument('--scope', choices=('tax', 'all'), default='tax')
    ap.add_argument('--claimed-day', help='UTC day reserved durably by the workflow')
    ap.add_argument('--wrangler', default='mcp/node_modules/.bin/wrangler')
    args = ap.parse_args()
    if args.progress:
        print(progress(args.source, args.out, args.state), end='')
        return 0
    try:
        if args.load:
            if not args.config or not args.database:
                raise ValueError('REMOTE_CONFIG_REQUIRED')
            remote_load(args.source, args.out, args.state, args.config, args.database, args.wrangler,
                        day=args.claimed_day, scope=args.scope)
        else:
            loaded = strict_json(args.loaded.read_bytes()) if args.loaded else None
            state = strict_json(args.state.read_bytes()) if args.state.exists() else {}
            build(args.source, args.out, loaded, args.tokenizer, model=budget_model(state, utc_day()), scope=args.scope)
    except Exception:
        print('PRECEDENT_D1_BUILD_OR_LOAD_FAILED')
        return 1
    print('PRECEDENT_D1_READY')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
