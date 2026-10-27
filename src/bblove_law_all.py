"""Resumable official current-law collection; never logs authenticated URLs."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import time
import threading
from zoneinfo import ZoneInfo

from bblove_quality import credential_markers
from bblove_storage import strict_json
from bblove_tax_p2_collect import BoundedClient, PilotBlocked
from bblove_law_transport import measured_transport
from bblove_v107_pilot import category, check_date, records, sanitized, text, validate_detail


def today():
    return datetime.now(ZoneInfo('Asia/Seoul')).strftime('%Y%m%d')


def stamp():
    return datetime.now(ZoneInfo('Asia/Seoul')).isoformat()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    tmp.replace(path)


# 대법원규칙(민사소송규칙·가사소송규칙·부동산등기규칙 등): 소장·신청서의 기재사항·첨부서류가 여기 있다.
COURT_RULE = '대법원규칙'


def supported(row):
    kind = text(row, '법령구분명')
    return kind in {'법률', '대통령령', '총리령', '부령', COURT_RULE} or kind.endswith('부령')


def collection_category(row):
    """V1.07 category plus court rules, which V1.07 classifies as '기타'."""
    return COURT_RULE if text(row, '법령구분명') == COURT_RULE else category(row)


class Client:
    """Three bounded in-flight requests, per-thread spacing and shared cooldown."""
    def __init__(self, credential, seconds=3500, transport=None):
        self.credential = credential
        self.deadline = time.monotonic() + min(seconds, 3500)
        self.transport = transport or measured_transport
        self.calls = 0
        self.lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(3)
        self.local = threading.local()
        self.cooldown_until = 0
        self.stats = Counter()

    def metrics(self):
        with self.lock:
            stats = dict(self.stats)
            calls = stats.get('transport_samples', 0)
            timed = stats.get('timed_samples', 0)
            return {**stats, 'api_calls': self.calls,
                    'mean_request_seconds': stats.get('transport_seconds', 0) / calls if calls else None,
                    'mean_network_seconds': stats.get('network_seconds', 0) / timed if timed else None,
                    'mean_process_overhead_seconds': stats.get('process_overhead_seconds', 0) / timed if timed else None}

    def _transport(self, request, timeout):
        start = time.monotonic()
        timing = None
        try:
            response = self.transport(request, timeout)
            timing = getattr(response, 'bblove_timing', None)
            return response
        except Exception as exc:
            timing = getattr(exc, 'bblove_timing', None)
            raise
        finally:
            with self.lock:
                self.stats['transport_samples'] += 1
                self.stats['transport_seconds'] += time.monotonic() - start
                if timing:
                    self.stats['timed_samples'] += 1
                    for key in ('network_seconds', 'process_overhead_seconds'):
                        self.stats[key] += timing[key]

    def _admit(self):
        while True:
            with self.lock:
                now = time.monotonic()
                delay = max(0, self.cooldown_until - now,
                            getattr(self.local, 'last', 0) + .3 - now)
                if now + delay + 32 >= self.deadline or self.calls >= 30000:
                    raise PilotBlocked('COLLECTION_BUDGET_EXHAUSTED')
                if delay <= 0:
                    self.calls += 1
                    self.local.last = now
                    return
            time.sleep(delay)

    def get(self, endpoint, params, *, original=False):
        for attempt in range(4):
            with self.slots:
                self._admit()
                with self.lock:
                    key = 'detail_request_attempts' if endpoint == 'lawService.do' else 'catalog_request_attempts'
                    self.stats[key] += 1
                client = BoundedClient(self.credential, self._transport, request_limit=1)
                try:
                    doc, raw, _ = client.json(endpoint, params, 'full-law-request')
                    if original:
                        client.original_safe(raw)
                        clean, _ = sanitized(doc, self.credential)
                        if clean != doc or credential_markers(raw):
                            raise PilotBlocked('CREDENTIAL_BEARING_ORIGINAL_NOT_PERSISTED')
                    else:
                        doc, _ = sanitized(doc, self.credential)
                    return doc, raw if original else None
                except (PilotBlocked, ValueError):
                    code = client.receipts[-1].get('http_status') if client.receipts else None
                    if code == 429 or (isinstance(code, int) and 500 <= code < 600):
                        with self.lock:
                            self.cooldown_until = max(self.cooldown_until, time.monotonic() + 2 ** attempt)
                            self.stats['global_backoffs'] += 1
                    if attempt == 3:
                        raise PilotBlocked('REQUEST_FAILED_AFTER_RETRIES') from None
            delay = 2 ** attempt
            if time.monotonic() + delay + 32 >= self.deadline:
                raise PilotBlocked('COLLECTION_BUDGET_EXHAUSTED') from None
            time.sleep(delay)


def catalog(client, date):
    rows, seen, total = [], set(), None
    for page in range(1, 1001):
        doc, _ = client.get('lawSearch.do', {'target': 'law', 'nw': '3',
                            'display': 100, 'page': page, 'sort': 'lasc'})
        root = doc.get('LawSearch', {})
        count = int(text(root, 'totalCnt'))
        if count <= 0 or (total is not None and count != total):
            raise ValueError('CATALOG_COUNT_CHANGED')
        total = count
        batch = records(root.get('law'))
        if not batch or (isinstance(root.get('law'), list) and len(batch) != len(root['law'])):
            raise ValueError('CATALOG_PAGE_INVALID')
        for row in batch:
            lid, mst = text(row, '법령ID'), text(row, '법령일련번호')
            if not re.fullmatch(r'\d{6}', lid) or not mst.isdigit() or lid in seen:
                raise ValueError('CATALOG_ID_INVALID_OR_DUPLICATE')
            seen.add(lid)
            if supported(row):
                # Reject future/non-current editions instead of silently omitting them.
                check_date(text(row, '시행일자'), 'catalog', date)
                check_date(text(row, '공포일자'), 'publication', date)
                if text(row, '현행연혁코드') not in {'현행', '3'}:
                    raise ValueError('CATALOG_NOT_CURRENT')
                if not text(row, '법령명한글', '법령명_한글'):
                    raise ValueError('CATALOG_TITLE_MISSING')
                rows.append(row)
        if len(seen) == total:
            return rows, total
        if len(seen) > total or len(batch) != 100:
            raise ValueError('CATALOG_INCOMPLETE')
    raise ValueError('CATALOG_PAGE_LIMIT')


def grouping(rows):
    laws = {text(r, '법령ID'): text(r, '법령명한글', '법령명_한글')
            for r in rows if category(r) == '법률'}
    names = set(laws.values())
    result, paths = {}, set()
    for row in sorted(rows, key=lambda r: text(r, '법령ID')):
        title = text(row, '법령명한글', '법령명_한글')
        kind = collection_category(row)
        parent = text(row, '상위법령ID', '상위법령아이디')
        base = re.sub(r'\s+시행(?:령|규칙)$', '', title)
        group = laws.get(parent) or (base if base in names else title)
        group = re.sub(r'[/\\\x00-\x1f]', '_', group)
        if group in {'', '.', '..'}:
            raise ValueError('GROUP_NAME_INVALID')
        # A court rule is stored in the rule slot; its exact 법령구분명 stays in the document (kc).
        slot = {'법률': '법률', '대통령령': '시행령', '총리령·부령': '시행규칙', COURT_RULE: '시행규칙'}[kind]
        if len(group.encode('utf-8')) > 180:
            group = group[:40] + '_' + hashlib.sha1(group.encode('utf-8')).hexdigest()[:10]
        path = f'{group}/{slot}.json'
        if path in paths:
            group = f'{title} [{text(row, "법령ID")}]'
            group = re.sub(r'[/\\\x00-\x1f]', '_', group)
            path = f'{group}/{slot}.json'
        if path in paths:
            raise ValueError('GROUP_PATH_COLLISION')
        paths.add(path)
        result[text(row, '법령ID')] = (group, slot, path)
    return result


def safe_detail(exc, credential=''):
    """Keep validator messages, but never authenticated URLs or credential values."""
    message, _ = sanitized(str(exc), credential)
    message = re.sub(r'https?://[^\s<>"\']+', '[URL REDACTED]', message)
    return message[:1000]


def validate_detail_all(doc, row, date):
    """Validate a projection using V1.07; never mutate the official document.

    Only whitespace-equivalent names, duplicate article numbers and explicitly
    marked empty deleted articles are warnings. IDs/dates/content loss are not.
    Duplicate originals stay in the asset in source order; none are discarded.
    """
    try:
        return validate_detail(doc, row, date), []
    except ValueError:
        pass
    projected = deepcopy(doc)
    law = projected.get('법령')
    basic = law.get('기본정보') if isinstance(law, dict) else None
    if not isinstance(basic, dict):
        return validate_detail(projected, row, date), []
    warnings = []
    expected = text(row, '법령명한글', '법령명_한글')
    actual = text(basic, '법령명_한글', '법령명한글')
    if expected and expected != actual and re.sub(r'\s+', '', expected) == re.sub(r'\s+', '', actual):
        basic['법령명_한글'] = expected
        warnings.append({'code': 'title_whitespace_difference',
                         'detail': 'detail: title/ID/effective date mismatch'})
    # Catalog row and detail can carry different 시행일자 for the same 법령ID/MST
    # (e.g. 부칙상 일부 시행). Accept only when the ID matches and the detail's own
    # date is a real, already-effective date; the asset then shows the detail's date.
    exp_date, act_date = text(row, '시행일자'), text(basic, '시행일자')
    # The catalog (현행, nw=3) is authoritative that this MST is the current edition.
    # A detail date after today means some provisions take effect later (부칙 일부 시행);
    # the edition itself is still current, so keep it and surface both dates.
    if (exp_date != act_date and text(row, '법령ID') == text(basic, '법령ID')
            and re.fullmatch(r'\d{8}', act_date or '')):
        try:
            check_date(act_date, 'detail effective date')
        except ValueError:
            pass
        else:
            basic['시행일자'] = exp_date
            warnings.append({'code': 'detail_date_after_today' if act_date > date
                             else 'effective_date_differs_from_catalog',
                             'catalog': exp_date, 'detail': act_date})
    section = law.get('조문')
    units = section.get('조문단위') if isinstance(section, dict) else None
    parsed = records(units)
    if isinstance(units, list) and len(parsed) != len(units):
        return validate_detail(projected, row, date), []
    article_units = [u for u in parsed if text(u, '조문여부') not in {'N', '전문'}]
    fresh = max([int(text(u, '조문번호')) for u in article_units
                 if re.fullmatch(r'[0-9]+', text(u, '조문번호'))] or [0]) + 1
    seen = set()
    for index, unit in enumerate(article_units):
        no, sub = text(unit, '조문번호'), text(unit, '조문가지번호') or '0'
        valid = (re.fullmatch(r'[0-9]+', no) and re.fullmatch(r'[0-9]+', sub)
                 and not isinstance(unit.get('조문가지번호'), (dict, list)))
        if not valid:
            continue  # V1.07 still rejects malformed identifiers below.
        key = (int(no), int(sub))
        number = f'제{key[0]}조' + (f'의{key[1]}' if key[1] else '')
        if not text(unit, '조문내용'):
            deleted = (text(unit, '조문제목') == '삭제' or text(unit, '조문삭제여부') in {'Y', '삭제'})
            if deleted and (unit.get('조문내용') is None or
                            isinstance(unit.get('조문내용'), str) and not unit['조문내용'].strip()):
                # Validation projection only. Asset body remains the original empty string.
                unit['조문내용'] = '삭제'
                warnings.append({'code': 'empty_deleted_article', 'article_index': index,
                                 'article': number, 'detail': 'detail: empty article text'})
        if key in seen:
            warnings.append({'code': 'duplicate_article_identifier', 'article_index': index,
                             'article': number, 'detail': 'detail: duplicate article identifier'})
            # Unique number only in the projection, after validating original syntax.
            unit['조문번호'], unit['조문가지번호'] = str(fresh), '0'
            fresh += 1
        seen.add(key)
    return validate_detail(projected, row, date), warnings


def validate_raw(raw, row, date, *, with_warnings=False):
    if credential_markers(raw):
        raise ValueError('CREDENTIAL_MARKER')
    doc = strict_json(raw)
    count, warnings = validate_detail_all(doc, row, date)
    basic = doc['법령']['기본정보']
    if text(basic, '공포일자') != text(row, '공포일자'):
        raise ValueError('PUBLICATION_MISMATCH')
    check_date(text(basic, '공포일자'), 'publication', date)
    return (doc, count, warnings) if with_warnings else (doc, count)


def collection_complete(documents):
    return bool(documents) and all(e.get('status') in {'SUCCESS', 'FAILED'} and
        e.get('reason') != 'COLLECTION_BUDGET_EXHAUSTED' for e in documents)


def failure_report(documents):
    return [{'title': e['title'], 'mst': e['mst'], 'reason': e.get('reason', 'UNKNOWN'),
             'detail': e.get('detail', e.get('reason', 'UNKNOWN'))}
            for e in documents if e.get('status') == 'FAILED']


def failure_distribution(documents):
    result = {}
    for entry in failure_report(documents):
        detail = entry['detail']
        item = result.setdefault(detail, {'count': 0, 'examples': []})
        item['count'] += 1
        if len(item['examples']) < 5:
            item['examples'].append({'title': entry['title'], 'mst': entry['mst']})
    return result


def collect_entry(client, out, entry, old, date):
    entry = dict(entry)
    phase = 'RAW_CACHE_READ_FAILED'
    try:
        file = out / entry['path']
        raw = file.read_bytes() if file.exists() else b''
        reuse = (old.get('mst') == entry['mst'] and
                 (old.get('status') == 'SUCCESS' or old.get('reason') == 'DETAIL_VALIDATION_FAILED'
                  or old.get('raw_status') in {'SUCCESS', 'DETAIL_VALIDATION_FAILED'}) and
                 old.get('sha256') == hashlib.sha256(raw).hexdigest())
        if not reuse:
            phase = 'DETAIL_FETCH_FAILED'
            _, raw = client.get('lawService.do', {'target': 'law', 'MST': entry['mst']}, original=True)
            # Persist safe RAW before semantic validation, including failing originals.
            if credential_markers(raw):
                raise ValueError('CREDENTIAL_BEARING_ORIGINAL_NOT_PERSISTED')
            phase = 'RAW_WRITE_FAILED'
            file.parent.mkdir(parents=True, exist_ok=True)
            tmp = file.with_suffix('.tmp')
            tmp.write_bytes(raw)
            tmp.replace(file)
        entry.update(sha256=hashlib.sha256(raw).hexdigest(),
                     collected_at=old.get('collected_at', stamp()) if reuse else stamp(), reused=reuse)
        phase = 'DETAIL_VALIDATION_FAILED'
        _, count, warnings = validate_raw(raw, entry['row'], date, with_warnings=True)
        entry.update(status='SUCCESS', raw_status='SUCCESS', articles=count, warnings=warnings)
    except Exception as exc:
        if phase == 'DETAIL_VALIDATION_FAILED':
            entry['raw_status'] = 'DETAIL_VALIDATION_FAILED'
        entry.update(status='FAILED', reason='COLLECTION_BUDGET_EXHAUSTED'
                     if isinstance(exc, PilotBlocked) and str(exc) == 'COLLECTION_BUDGET_EXHAUSTED'
                     else phase, detail=safe_detail(exc, getattr(client, 'credential', '')))
    return entry


def collect(client, out, date, *, outcome=None):
    outcome = outcome if outcome is not None else {}
    outcome['budget_only'] = False
    start = time.monotonic()
    manifest_path = out / 'manifest.json'
    previous = strict_json(manifest_path.read_bytes()) if manifest_path.exists() else {}
    cached = {r['id']: r for r in previous.get('documents', [])}
    manifest = {'schema': 2, 'date': date, 'started_at': stamp(), 'status': 'INCOMPLETE',
                'list_count': 0, 'api_calls': 0, 'documents': []}
    try:
        rows, total = catalog(client, date)
        groups = grouping(rows)
        manifest['list_count'] = total
        for row in rows:
            lid = text(row, '법령ID')
            group, kind, path = groups[lid]
            manifest['documents'].append({'id': lid, 'mst': text(row, '법령일련번호'),
                'title': text(row, '법령명한글', '법령명_한글'), 'group': group,
                'kind': kind, 'path': path, 'effective': text(row, '시행일자'),
                'published': text(row, '공포일자'), 'row': row, 'status': 'PENDING'})
            old = cached.get(lid, {})
            state = old.get('raw_status') or (old.get('reason') if old.get('status') == 'FAILED' else old.get('status'))
            if old.get('mst') == text(row, '법령일련번호') and state in {'SUCCESS', 'DETAIL_VALIDATION_FAILED'}:
                # Preserve known RAW receipts even if this run stops before replaying this entry.
                manifest['documents'][-1].update({k: old[k] for k in ('sha256', 'collected_at') if k in old})
                manifest['documents'][-1]['raw_status'] = state
    except Exception as exc:
        outcome['budget_only'] = isinstance(exc, PilotBlocked) and str(exc) == 'COLLECTION_BUDGET_EXHAUSTED'
        save(out / 'attempt.json', {'status': 'FAILED', 'reason': 'CATALOG_FAILED',
             'detail': safe_detail(exc, getattr(client, 'credential', '')), 'api_calls': client.calls})
        return False
    save(manifest_path, manifest)
    checkpoint_seconds = 0
    processed = 0
    last_checkpoint = time.monotonic()
    checkpoint_count = 0
    with ThreadPoolExecutor(max_workers=3) as pool:
        remaining = iter(enumerate(manifest['documents']))
        active = {}
        budget_exhausted = False
        while True:
            while not budget_exhausted and len(active) < 3:
                pair = next(remaining, None)
                if pair is None:
                    break
                index, entry = pair
                active[pool.submit(collect_entry, client, out, entry, cached.get(entry['id'], {}), date)] = index
            if not active:
                break
            done, _ = wait(active, return_when=FIRST_COMPLETED)
            for future in done:
                index = active.pop(future)
                result = future.result()
                manifest['documents'][index] = result
                processed += 1
                budget_exhausted |= result.get('reason') == 'COLLECTION_BUDGET_EXHAUSTED'
            manifest.update(api_calls=client.calls, elapsed_seconds=round(time.monotonic()-start, 2))
            if processed - checkpoint_count >= 25 or time.monotonic() - last_checkpoint >= 5 or budget_exhausted:
                checkpoint_start = time.monotonic()
                save(manifest_path, manifest)
                checkpoint_seconds += time.monotonic() - checkpoint_start
                last_checkpoint, checkpoint_count = time.monotonic(), processed
    documents = manifest['documents']
    outcome['budget_only'] = bool(documents) and all(
        e['status'] in {'SUCCESS', 'PENDING'} or e.get('reason') == 'COLLECTION_BUDGET_EXHAUSTED'
        for e in documents)
    manifest['status'] = 'COMPLETE' if collection_complete(documents) else 'INCOMPLETE'
    manifest.update(completed_at=stamp(), api_calls=client.calls,
                    elapsed_seconds=round(time.monotonic()-start, 2), processed_documents=processed,
                    reused_documents=sum(e.get('reused') is True for e in documents),
                    fetched_documents=sum(e.get('reused') is False for e in documents),
                    status_counts=dict(Counter(e['status'] for e in documents)),
                    failure_distribution=failure_distribution(documents),
                    checkpoint_seconds=round(checkpoint_seconds, 3),
                    performance=client.metrics() if hasattr(client, 'metrics') else {})
    save(manifest_path, manifest)
    save(out / 'collection-failures.json', failure_report(documents))
    # Numeric aggregate only; no URLs, credentials or transport exception strings.
    print(json.dumps({'collection_performance': manifest['performance'],
                      'processed_documents': processed, 'status_counts': manifest['status_counts'],
                      'reused_documents': manifest['reused_documents'],
                      'fetched_documents': manifest['fetched_documents'],
                      'checkpoint_seconds': manifest['checkpoint_seconds']}, ensure_ascii=False))
    return manifest['status'] == 'COMPLETE'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', type=Path, default=Path('auto/official-all'))
    ap.add_argument('--partial-ok', action='store_true',
                    help='exit 0 when the run stopped only at the time budget; next run resumes')
    args = ap.parse_args()
    credential = os.environ.get('LAW_OC', '')
    if not credential:
        print('LAW_OC_NOT_AVAILABLE')
        return 1
    outcome = {}
    ok = collect(Client(credential), args.out, today(), outcome=outcome)
    print('COLLECTION_COMPLETE' if ok else 'COLLECTION_INCOMPLETE')
    write_step_output(ok)
    # Only this invocation's budget outcome qualifies; old manifests cannot hide list failures.
    return 0 if ok or (args.partial_ok and outcome.get('budget_only')) else 1


def write_step_output(complete):
    """Expose completion to later workflow steps; build/publish/deploy run only when complete."""
    target = os.environ.get('GITHUB_OUTPUT')
    if target:
        with open(target, 'a', encoding='utf-8') as fh:
            fh.write(f"complete={'true' if complete else 'false'}\n")


if __name__ == '__main__':
    raise SystemExit(main())
