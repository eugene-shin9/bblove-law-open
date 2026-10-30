"""Bounded, resumable tax administrative-rule collection and offline assets.

The guide documents fields, not the JSON envelope. Unknown envelopes/content
fail closed. Nothing in this module contacts DRF except the collect command.
"""
import argparse
from copy import deepcopy
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile

from bblove_law_all import Client, save, today, stamp, write_step_output, collection_complete
from bblove_quality import credential_markers
from bblove_storage import strict_json
from bblove_tax_p2_collect import PilotBlocked
from bblove_v107_pilot import check_date, sanitized, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from build_mcp_assets_official import encode, iso, META_LIMIT

# DRF org code -> ministry names the catalog may report for it. 기획재정부 was
# renamed 재정경제부 (law.go.kr now lists tax statutes under the new name) while
# its org code stayed; rows keep whichever name the DRF actually returned.
ORG = {'1210000': ('국세청',), '1051000': ('재정경제부', '기획재정부')}
ORG_NAMES = {name: code for code, names in ORG.items() for name in names}
KINDS = {'훈령', '예규', '고시'}
KEYWORDS = ('세', '조세', '국세', '조사', '징수', '체납', '부가', '소득', '법인', '상속', '증여')
POLICY = 'tax-admrul-v1-no-customs'
FILE_LIMIT = 8 * 1024 * 1024
NOTICE = '법원·납세자를 직접 구속하지 않을 수 있음'


def safe_raw(raw, credential=''):
    doc = strict_json(raw)
    clean, _ = sanitized(doc, credential)
    if clean != doc or credential_markers(raw):
        raise ValueError('CREDENTIAL_BEARING_ORIGINAL_NOT_PERSISTED')
    return doc


def checked_row(row, date):
    names = ('행정규칙ID', '행정규칙일련번호', '행정규칙명', '행정규칙종류', '소관부처명', '발령일자', '시행일자')
    if not isinstance(row, dict) or any(not text(row, n) for n in names):
        raise ValueError('ADMRUL_METADATA_MISSING')
    result = {n: text(row, n) for n in names}
    for n in names[:2]:
        if not re.fullmatch(r'[0-9]{1,20}', result[n]):
            raise ValueError('ADMRUL_ID_INVALID')
    check_date(result['발령일자'], 'admrul publication', date)
    check_date(result['시행일자'], 'admrul effective', date)
    return result


def exclusion(row):
    if row['소관부처명'] not in ORG_NAMES:
        return 'OUTSIDE_ORGANIZATIONS'
    if row['행정규칙종류'] not in KINDS:
        return 'OUTSIDE_KINDS'
    title = row['행정규칙명']
    # Initial scope is domestic tax administration, not customs/trade remedies.
    if '관세' in title:
        return 'CUSTOMS_EXCLUDED'
    if not any(k in title for k in KEYWORDS):
        return 'NO_TAX_KEYWORD'
    return None


def parse_page(doc, code, page, date):
    root = doc.get('AdmRulSearch') if isinstance(doc, dict) else None
    if not isinstance(root, dict) or not re.fullmatch(r'\d+', text(root, 'totalCnt')):
        raise ValueError('ADMRUL_CATALOG_SHAPE')
    count = int(text(root, 'totalCnt'))
    if count > 10000 or text(root, 'page') != str(page):
        raise ValueError('ADMRUL_CATALOG_COUNT_OR_PAGE')
    value = root.get('admrul', [])
    rows = [value] if isinstance(value, dict) else value
    if not isinstance(rows, list) or len(rows) > 100 or (count and not rows):
        raise ValueError('ADMRUL_CATALOG_ROWS')
    checked = []
    for r in rows:
        item = checked_row(r, date)
        # A different ministry in an org-filtered response signals a broken filter.
        if item['소관부처명'] not in ORG[code]:
            raise ValueError('ADMRUL_ORG_FILTER_MISMATCH')
        if text(r, '현행연혁구분') not in {'현행', '1'}:
            raise ValueError('ADMRUL_NOT_CURRENT')
        checked.append(item)
    return checked, count


def catalog(client, out, date):
    """Replay sanitized selection pages; only detail bytes are law originals."""
    checkpoint = out / 'catalog.json'
    cached = strict_json(checkpoint.read_bytes()) if checkpoint.exists() else {}
    reuse = cached.get('date') == date and cached.get('policy') == POLICY
    receipts, rows, seen = [], [], set()
    for code in ORG:
        total, received = None, 0
        for page in range(1, 102):
            path = f'catalog/{code}/{page}.json'
            old = next((x for x in cached.get('pages', []) if x.get('path') == path), {}) if reuse else {}
            target = out / path
            raw = target.read_bytes() if target.exists() else b''
            if old.get('sha256') != hashlib.sha256(raw).hexdigest():
                doc, _ = client.get('lawSearch.do', {'target': 'admrul', 'nw': '1', 'org': code,
                    'search': 1, 'display': 100, 'page': page, 'sort': 'lasc'})
                clean, _ = sanitized(doc, getattr(client, 'credential', ''))
                raw = encode(clean)  # selection evidence; never claimed as an original
            doc = safe_raw(raw, getattr(client, 'credential', ''))
            batch, count = parse_page(doc, code, page, date)
            # An empty required ministry may mean a retired org code. Do not
            # publish the other ministry alone, including from old checkpoints.
            if count == 0:
                raise ValueError('ADMRUL_ORG_CATALOG_EMPTY')
            if total is not None and count != total:
                raise ValueError('ADMRUL_CATALOG_CHANGED')
            total = count
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            receipts.append({'path': path, 'sha256': hashlib.sha256(raw).hexdigest(),
                             'role': 'sanitized_selection_evidence'})
            # Persist complete pages immediately, including before budget exhaustion.
            save(checkpoint, {'date': date, 'policy': POLICY, 'pages': receipts})
            for row in batch:
                if row['행정규칙ID'] in seen:
                    raise ValueError('ADMRUL_DUPLICATE_ID')
                seen.add(row['행정규칙ID'])
                rows.append(row)
            received += len(batch)
            if received == total:
                break
            if received > total or len(batch) != 100:
                raise ValueError('ADMRUL_CATALOG_INCOMPLETE')
        else:
            raise ValueError('ADMRUL_CATALOG_PAGE_LIMIT')
    return rows


class BodyHTML(HTMLParser):
    """Convert textual HTML to lines; attachments/scripts never become invented text."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style', 'iframe', 'img', 'object'}:
            raise ValueError('ADMRUL_UNSUPPORTED_BODY_HTML')
        if tag in {'p', 'div', 'br', 'tr', 'li', 'td', 'th'}:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in {'p', 'div', 'tr', 'li'}:
            self.parts.append('\n')

    def handle_data(self, value):
        self.parts.append(value)


def body_text(value):
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        value = '\n'.join(value)
    if not isinstance(value, str):
        raise ValueError('ADMRUL_BODY_SHAPE')
    if re.search(r'</?[A-Za-z][^>]*>', value):
        parser = BodyHTML()
        parser.feed(value)
        parser.close()
        value = ''.join(parser.parts)
    if not value.strip():
        raise ValueError('ADMRUL_BODY_MISSING')
    return value.strip()


def article_rows(value):
    body = body_text(value)
    matches = list(re.finditer(r'(?m)^\s*(제\d+조(?:의\d+)?)(?:\s*\(([^)\n]*)\))?', body))
    if not matches:
        return [[f'문단{i}', '', p.strip()] for i, p in enumerate(re.split(r'\n+', body), 1) if p.strip()]
    out = []
    if body[:matches[0].start()].strip():
        out.append(['전문', '', body[:matches[0].start()].strip()])
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        out.append([m[1], m[2] or '', body[m.start():end].strip()])
    if len({a[0] for a in out}) != len(out):
        raise ValueError('ADMRUL_DUPLICATE_ARTICLE')
    return out


def validate_raw(raw, row, date, credential=''):
    doc = safe_raw(raw, credential)
    service = doc.get('AdmRulService') if isinstance(doc, dict) else None
    if not isinstance(service, dict):
        raise ValueError('ADMRUL_DETAIL_SHAPE')
    basic = service.get('행정규칙기본정보')
    actual, expected = checked_row(basic, date), checked_row(row, date)
    if actual != expected or exclusion(actual):
        raise ValueError('ADMRUL_METADATA_MISMATCH')
    if text(basic, '현행여부') != 'Y':
        raise ValueError('ADMRUL_NOT_CURRENT')
    if text(basic, '소관부처코드') != ORG_NAMES[actual['소관부처명']]:
        raise ValueError('ADMRUL_ORG_CODE_MISMATCH')
    # Guide declares 조문내용, not the normal law's 조문/조문단위 envelope.
    # No guessed dictionary flattening: a different JSON nesting must be reviewed.
    articles = article_rows(service.get('조문내용'))
    if service.get('부칙'):
        value = service['부칙']
        if isinstance(value, dict):
            value = value.get('부칙내용')
        articles.append(['부칙', '', body_text(value)])
    return actual, articles


def code(exc, fallback):
    value = str(exc)
    return value if re.fullmatch(r'(?:ADMRUL|CREDENTIAL|COLLECTION)_[A-Z_]+', value) else fallback


def collect(client, out, date):
    out = Path(out)
    check_date(date, 'collection date')
    path = out / 'manifest.json'
    previous = strict_json(path.read_bytes()) if path.exists() else {}
    old = {e['id']: e for e in previous.get('documents', [])}
    manifest = {'schema': 1, 'policy': POLICY, 'date': date, 'status': 'INCOMPLETE',
                'started_at': stamp(), 'documents': [], 'excluded': [], 'api_calls': 0}
    try:
        rows = catalog(client, out, date)
    except Exception as exc:
        reason = code(exc, 'ADMRUL_CATALOG_FAILED')
        save(out / 'attempt.json', {'status': 'INCOMPLETE' if reason == 'COLLECTION_BUDGET_EXHAUSTED' else 'FAILED',
             'reason': reason, 'api_calls': client.calls})
        # Never let a previous complete checkpoint publish after a list failure.
        manifest['catalog_failure'] = reason
        manifest['documents'] = previous.get('documents', [])
        save(path, manifest)
        return False
    for row in rows:
        entry = {'id': row['행정규칙ID'], 'mst': row['행정규칙일련번호'],
                 'title': row['행정규칙명'], 'row': row, 'status': 'PENDING',
                 'path': f"raw/{row['행정규칙ID']}/{row['행정규칙일련번호']}.json"}
        reason = exclusion(row)
        if reason:
            manifest['excluded'].append({**entry, 'reason': reason})
        else:
            cached = old.get(entry['id'], {})
            if cached.get('row') == row:
                # Preserve receipts for entries not reached before this run's budget.
                entry.update({k:cached[k] for k in ('sha256','collected_at') if k in cached})
            manifest['documents'].append(entry)
    budget = False
    for entry in manifest['documents']:
        if budget:
            break
        target = out / entry['path']
        cached = old.get(entry['id'], {})
        try:
            raw = target.read_bytes() if target.exists() else b''
            reuse = (cached.get('row') == entry['row'] and cached.get('sha256') == hashlib.sha256(raw).hexdigest())
            if not reuse:
                _, raw = client.get('lawService.do', {'target': 'admrul', 'ID': entry['mst']}, original=True)
            safe_raw(raw, getattr(client, 'credential', ''))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)  # safe exact original, even when semantic validation fails
            entry.update(sha256=hashlib.sha256(raw).hexdigest(), reused=reuse,
                         collected_at=cached.get('collected_at', stamp()) if reuse else stamp())
            _, arts = validate_raw(raw, entry['row'], date, getattr(client, 'credential', ''))
            entry.update(status='SUCCESS', articles=len(arts))
        except Exception as exc:
            reason = code(exc, 'ADMRUL_DETAIL_FAILED')
            entry.update(status='FAILED', reason=reason)
            budget = reason == 'COLLECTION_BUDGET_EXHAUSTED'
        manifest['api_calls'] = client.calls
        save(path, manifest)
    manifest['status'] = 'COMPLETE' if collection_complete(manifest['documents']) else 'INCOMPLETE'
    manifest['api_calls'] = client.calls
    manifest['finished_at'] = stamp()
    save(path, manifest)
    save(out / 'collection-failures.json', [
        {k: e[k] for k in ('id', 'mst', 'title', 'status', 'reason') if k in e}
        for e in manifest['documents'] if e['status'] != 'SUCCESS'])
    return manifest['status'] == 'COMPLETE'


def only_budget(out):
    manifest = strict_json((out / 'manifest.json').read_bytes())
    if manifest.get('catalog_failure'):
        return manifest['catalog_failure'] == 'COLLECTION_BUDGET_EXHAUSTED'
    docs = manifest['documents']
    return bool(docs) and all(e['status'] in {'SUCCESS', 'PENDING'} or
        e.get('reason') == 'COLLECTION_BUDGET_EXHAUSTED' for e in docs)


def build(source, dest):
    source, dest = Path(source), Path(dest)
    manifest = strict_json((source / 'manifest.json').read_bytes())
    if manifest.get('status') != 'COMPLETE' or not collection_complete(manifest['documents']):
        raise ValueError('ADMRUL_INCOMPLETE_COLLECTION')
    check_date(manifest['date'], 'admrul collection date')
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=dest.parent) as temp:
        stage = Path(temp)
        (stage / 'admrul/docs').mkdir(parents=True)
        entries, seen = {}, set()
        for e in manifest['documents']:
            row = checked_row(e['row'], manifest['date'])
            if (e['id'], e['mst'], e['title'], e['path']) != (
                    row['행정규칙ID'], row['행정규칙일련번호'], row['행정규칙명'],
                    f"raw/{e['id']}/{e['mst']}.json") or e['id'] in seen:
                raise ValueError('ADMRUL_MANIFEST_MISMATCH')
            seen.add(e['id'])
            if e['status'] != 'SUCCESS':
                continue
            raw = (source / e['path']).read_bytes()
            if hashlib.sha256(raw).hexdigest() != e['sha256']:
                raise ValueError('ADMRUL_RAW_HASH_MISMATCH')
            _, arts = validate_raw(raw, row, manifest['date'])
            if len(arts) != e['articles']:
                raise ValueError('ADMRUL_ARTICLE_COUNT_MISMATCH')
            doc = {'id': e['id'], 'mst': e['mst'], 'k': '행정규칙', 'kc': row['행정규칙종류'],
                   'org': row['소관부처명'], 't': e['title'], 'eff': iso(row['시행일자']),
                   'pub': iso(row['발령일자']), 'collected': iso(manifest['date']),
                   'st': '현행(수집 기준) · 별표·첨부파일은 공식 링크에서 별도 확인',
                   'url': f"https://www.law.go.kr/LSW/admRulLsInfoP.do?admRulSeq={e['mst']}", 'a': arts}
            data = encode([doc])
            if len(data) > FILE_LIMIT:
                raise ValueError('ADMRUL_FILE_TOO_LARGE')
            (stage / f"admrul/docs/{e['mst']}.json").write_bytes(data)
            group = f"{e['title']} ({row['소관부처명']} {row['행정규칙종류']} {e['id']})"
            entries[group] = {k: doc[k] for k in ('id', 'mst', 't', 'kc', 'org', 'eff', 'pub', 'collected')}
            entries[group].update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
        if not entries:
            raise ValueError('ADMRUL_NO_SUCCESSFUL_DOCUMENTS')
        failures = [{k: e[k] for k in ('id', 'mst', 'title', 'reason') if k in e}
                    for e in manifest['documents'] if e['status'] == 'FAILED']
        save(stage / 'admrul/index.json', {'schema': 1, 'policy': POLICY,
            'date': iso(manifest['date']), 'documents': entries, 'failures': failures,
            'excluded': [{k: e[k] for k in ('id', 'title', 'reason')} for e in manifest['excluded']]})
        result = verify(stage)
        dest.mkdir(parents=True, exist_ok=True)
        if (dest / 'admrul').exists():
            shutil.rmtree(dest / 'admrul')
        shutil.move(str(stage / 'admrul'), dest / 'admrul')
    return result


def verify(root, merge_meta=False):
    root = Path(root)
    index_path = root / 'admrul/index.json'
    if index_path.is_symlink() or index_path.stat().st_size > FILE_LIMIT:
        raise ValueError('ADMRUL_INDEX_INVALID')
    index = safe_raw(index_path.read_bytes())
    if index.get('schema') != 1 or index.get('policy') != POLICY or not index.get('documents'):
        raise ValueError('ADMRUL_INDEX_INVALID')
    check_date(index['date'].replace('-', ''), 'admrul asset date')
    expected, ids, msts, sizes = {index_path}, set(), set(), []
    for group, e in index['documents'].items():
        row = checked_row(dict(zip(('행정규칙ID','행정규칙일련번호','행정규칙명','행정규칙종류','소관부처명','발령일자','시행일자'),
            (e['id'],e['mst'],e['t'],e['kc'],e['org'],e['pub'].replace('-',''),e['eff'].replace('-','')))), index['date'].replace('-',''))
        if exclusion(row) or e['id'] in ids or e['mst'] in msts or e['collected'] != index['date']:
            raise ValueError('ADMRUL_ASSET_METADATA')
        if group != f"{e['t']} ({e['org']} {e['kc']} {e['id']})":
            raise ValueError('ADMRUL_GROUP_INVALID')
        path = root / f"admrul/docs/{e['mst']}.json"
        if any(p.is_symlink() for p in (root/'admrul',path.parent,path)):
            raise ValueError('ADMRUL_PATH_INVALID')
        raw = path.read_bytes()
        if not 0 < len(raw) <= FILE_LIMIT or len(raw) != e['bytes'] or hashlib.sha256(raw).hexdigest() != e['sha256']:
            raise ValueError('ADMRUL_BYTES_OR_HASH_MISMATCH')
        docs = safe_raw(raw)
        if not isinstance(docs, list) or len(docs) != 1:
            raise ValueError('ADMRUL_DOCUMENT_SHAPE')
        d = docs[0]
        if any(d[k] != e[k] for k in ('id','mst','t','kc','org','eff','pub','collected')) or d['k'] != '행정규칙':
            raise ValueError('ADMRUL_INDEX_FILE_MISMATCH')
        if d['url'] != f"https://www.law.go.kr/LSW/admRulLsInfoP.do?admRulSeq={e['mst']}" or not d.get('a'):
            raise ValueError('ADMRUL_URL_OR_BODY_INVALID')
        if any(not isinstance(a,list) or len(a)!=3 or not all(isinstance(s,str) for s in a)
               or not a[0] or not a[2].strip() for a in d['a']) or len({a[0] for a in d['a']}) != len(d['a']):
            raise ValueError('ADMRUL_ARTICLES_INVALID')
        ids.add(e['id']); msts.add(e['mst']); expected.add(path); sizes.append(len(raw))
    if expected != {p for p in (root/'admrul').rglob('*') if p.is_file()}:
        raise ValueError('ADMRUL_UNLISTED_OR_MISSING_FILE')
    if merge_meta:
        meta_path = root / 'meta.json'
        meta = deepcopy(strict_json(meta_path.read_bytes()))
        previous = set(meta.get('admrul', {}))
        for key in previous:
            meta['groups'].pop(key, None)
        meta['alias'] = {a:g for a,g in meta['alias'].items() if g not in previous}
        meta['admrul'] = {}
        for group, e in index['documents'].items():
            if group in meta['groups']:
                raise ValueError('ADMRUL_GROUP_COLLISION')
            meta['groups'][group] = [f"admrul/{e['mst']}", e['t'], e['eff'], 0]
            meta['admrul'][group] = {k:e[k] for k in ('kc','org','collected')}
            # Existing law/curated aliases always win; scoped names stay addressable.
            for alias in (group, e['t'], e['t'].replace(' ',''), f"{e['org']} {e['t']}"):
                meta['alias'].setdefault(alias, group)
        raw = encode(meta)
        if len(raw) > META_LIMIT:
            raise ValueError('ADMRUL_META_TOO_LARGE')
        tmp = meta_path.with_suffix('.tmp')
        tmp.write_bytes(raw); tmp.replace(meta_path)
    return {'documents':len(ids), 'failed':len(index['failures']), 'max_file_bytes':max(sizes),
            'bytes':sum(sizes), 'deterministic_status':'PASS', 'official_original_review':'REVIEW_REQUIRED'}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('command', choices=('collect','build','verify'))
    ap.add_argument('--out', type=Path, default=Path('auto/admrul'))
    ap.add_argument('--dest', type=Path, default=Path('admrul-assets'))
    ap.add_argument('--seconds', type=int, default=660)
    ap.add_argument('--partial-ok', action='store_true')
    ap.add_argument('--merge-meta', action='store_true')
    args = ap.parse_args()
    try:
        if args.command == 'collect':
            write_step_output(False)
            # 공개 저장소: Actions 분 제한이 없어 Client 상한(약 58분) 안에서 길게 돈다.
            if not 60 <= args.seconds <= 3400:
                raise ValueError('ADMRUL_BUDGET_INVALID')
            credential = os.environ.get('LAW_OC', '')
            if not credential:
                raise ValueError('CREDENTIAL_NOT_AVAILABLE')
            complete = collect(Client(credential, seconds=args.seconds), args.out, today())
            write_step_output(complete)
            print('ADMRUL_COMPLETE' if complete else 'ADMRUL_INCOMPLETE')
            return 0 if complete or (args.partial_ok and only_budget(args.out)) else 1
        result = build(args.out, args.dest) if args.command == 'build' else verify(args.dest, args.merge_meta)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception:
        print('ADMRUL_COMMAND_FAILED')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
