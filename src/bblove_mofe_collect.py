"""재정경제부(구 기획재정부) 세법해석 사례 incremental collector (mofe.go.kr, public HTML).

Source: https://mofe.go.kr/lw/intrprt/TaxLawIntrPrtCaseList.do (bbsId MOSFBBS_000000000237), a
public board of the ministry's own tax-law interpretation replies (저작권법 제24조의2 공공저작물).
No credential is involved. Pacing: one request per 1.5 s, one in flight, bounded by --seconds.

List pages are newest first. A run walks page 1 onward and stops at the first page whose rows are
all already collected with unchanged list rows, unless --full is given. Detail pages are fetched
once per id and kept as the four labelled sections only (질문요지·사실관계·회신·관계법령); no
HTML, scripts or attachments are stored. The export is one JSON file for the private loader.
"""
import argparse
from datetime import datetime
import hashlib
import html
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from bblove_law_all import save, stamp, today, write_step_output
from bblove_storage import strict_json

SCHEMA = 1
POLICY = 'mofe-tax-interpretations-v1'
BASE = 'https://mofe.go.kr/lw/intrprt/'
BBS_ID = 'MOSFBBS_000000000237'
LIST_URL = BASE + 'TaxLawIntrPrtCaseList.do?bbsId=' + BBS_ID + '&menuNo=8120300&pageIndex={page}'
DETAIL_URL = BASE + 'TaxLawIntrPrtCaseView.do?bbsId=' + BBS_ID + '&searchNttId1={id}'
SECTIONS = ('질문요지', '사실관계', '회신', '관계법령')
ID_PATTERN = re.compile(r'MOSF_[0-9]{15}')
DOC_PATTERN = re.compile(r'[가-힣]+과-[0-9]{1,8}')
MAX_PAGES = 300
MAX_BYTES = 4 * 1024 * 1024
USER_AGENT = 'BBLOVE-law-open/1.0 (+https://github.com/eugene-shin9/bblove-law-open)'


class Budget(RuntimeError):
    pass


class Client:
    """Serial, paced HTTP GET. HTTPS_PROXY from the environment is honoured by urllib."""
    def __init__(self, seconds=3300, interval=1.5, transport=None):
        self.deadline = time.monotonic() + max(30, min(seconds, 20000))
        self.interval = max(1.0, float(interval))
        self.transport = transport or self._fetch
        self.calls, self.last = 0, 0.0

    @staticmethod
    def _fetch(url, timeout):
        request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT, 'Accept': 'text/html'})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                raise ValueError('MOFE_HTTP_STATUS')
            data = response.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ValueError('MOFE_RESPONSE_TOO_LARGE')
        return data

    def get(self, url):
        for attempt in range(3):
            wait = max(0.0, self.last + self.interval - time.monotonic())
            if time.monotonic() + wait + 35 >= self.deadline:
                raise Budget('COLLECTION_BUDGET_EXHAUSTED')
            time.sleep(wait)
            self.last = time.monotonic()
            self.calls += 1
            try:
                return self.transport(url, 30)
            except (urllib.error.URLError, TimeoutError, OSError, ValueError):
                if attempt == 2:
                    raise ValueError('MOFE_REQUEST_FAILED') from None
                time.sleep(self.interval * (attempt + 2))


def clean(text):
    text = html.unescape(re.sub(r'<[^>]+>', ' ', text))
    return re.sub(r'[ \t\r\f\v\xa0]+', ' ', re.sub(r'\s*\n\s*', '\n', text)).strip()


def iso_date(value):
    digits = re.sub(r'\D', '', value or '')
    if len(digits) != 8:
        raise ValueError('MOFE_DATE_INVALID')
    try:
        datetime.strptime(digits, '%Y%m%d')
    except ValueError:
        raise ValueError('MOFE_DATE_INVALID') from None
    return digits[:4] + '.' + digits[4:6] + '.' + digits[6:] + '.'


class _ListParser(HTMLParser):
    """Collect <li> blocks of ul.explnList: h3>a[fn_egov_select('MOSF_…')], span.depart, span.date."""
    def __init__(self):
        super().__init__()
        self.rows, self.current, self.capture, self.depth = [], None, None, 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'li':
            self.depth += 1
            if self.current is None:
                self.current = {'id': '', 'title': '', 'doc': '', 'date': '', 'li_depth': self.depth}
        if self.current is None:
            return
        if tag == 'a' and not self.current['id']:
            target = (attrs.get('href') or '') + (attrs.get('onclick') or '')
            matched = ID_PATTERN.search(target) if 'fn_egov_select' in target else None
            if matched:
                self.current['id'] = matched[0]
                self.capture = 'title'
        elif tag == 'span' and 'depart' in (attrs.get('class') or '').split():
            self.capture = 'doc'
        elif tag == 'span' and 'date' in (attrs.get('class') or '').split():
            self.capture = 'date'

    def handle_data(self, data):
        if self.current is not None and self.capture:
            self.current[self.capture] += data

    def handle_endtag(self, tag):
        if tag in ('a', 'span') and self.capture:
            self.capture = None
        if tag == 'li' and self.current is not None:
            if self.depth == self.current['li_depth']:
                row = self.current
                self.current = None
                if row['id']:
                    self.rows.append(row)
            self.depth -= 1
        elif tag == 'li':
            self.depth -= 1


def parse_list(raw):
    parser = _ListParser()
    parser.feed(raw.decode('utf-8', errors='strict'))
    rows = []
    for row in parser.rows:
        doc = DOC_PATTERN.search(clean(row['doc']))
        date = re.search(r'\d{4}\.\s*\d{1,2}\.\s*\d{1,2}', row['date'])
        title = clean(row['title'])
        if not (doc and date and title):
            raise ValueError('MOFE_LIST_ROW_INVALID')
        rows.append(dict(id=row['id'], title=title, doc=doc[0], date=iso_date(date[0])))
    if len({r['id'] for r in rows}) != len(rows):
        raise ValueError('MOFE_LIST_DUPLICATE')
    return rows


def parse_detail(raw):
    text = raw.decode('utf-8', errors='strict')
    board = re.search(r'<div class="detailBoard"[^>]*>(.*)', text, re.S)
    if not board:
        raise ValueError('MOFE_DETAIL_LAYOUT')
    body = board[1]
    title = re.search(r'<h3[^>]*>(.*?)</h3>', body, re.S)
    doc = re.search(r'<span class="depart[^"]*"[^>]*>(.*?)</span>', body, re.S)
    date = re.search(r'<span class="date[^"]*"[^>]*>(.*?)</span>', body, re.S)
    sections = {}
    for name, content in re.findall(r'<h4 class="taxLawCase"[^>]*>\s*(.*?)\s*</h4>\s*<div class="editorCont taxLawCase"[^>]*>(.*?)</div>\s*(?=<h4 class="taxLawCase"|<div class="btn|<!--|</div>)', body, re.S):
        sections[clean(name)] = clean(content)
    if not title or not doc or not date or set(sections) < set(SECTIONS):
        raise ValueError('MOFE_DETAIL_SECTIONS')
    doc_match = DOC_PATTERN.search(clean(doc[1]))
    date_match = re.search(r'\d{4}\.\s*\d{1,2}\.\s*\d{1,2}', clean(date[1]))
    if not doc_match or not date_match:
        raise ValueError('MOFE_DETAIL_HEADER')
    return dict(title=clean(title[1]), doc=doc_match[0], date=iso_date(date_match[0]),
                sec={k: sections[k] for k in SECTIONS})


def collect(client, out, date, full=False, stop=None):
    out = Path(out)
    manifest_path = out / 'manifest.json'
    previous = strict_json(manifest_path.read_bytes()) if manifest_path.exists() else {}
    old = {e['id']: e for e in previous.get('documents', []) if e.get('status') == 'SUCCESS'}
    manifest = dict(schema=SCHEMA, policy=POLICY, date=date, status='INCOMPLETE', started_at=stamp(),
                    source=LIST_URL.split('?')[0], documents=[], pages=0, requests=0, mode='full' if full else 'incremental')
    seen, listed = set(), []
    try:
        for page in range(1, MAX_PAGES + 1):
            rows = parse_list(client.get(LIST_URL.format(page=page)))
            manifest['pages'] = page
            if not rows:
                break
            fresh = [r for r in rows if r['id'] not in seen]
            seen.update(r['id'] for r in rows)
            listed.extend(fresh)
            unchanged = all(old.get(r['id'], {}).get('row') == r for r in rows)
            if not fresh or (unchanged and not full):
                break
            if stop and stop():
                raise Budget('COLLECTION_BUDGET_EXHAUSTED')
        else:
            raise ValueError('MOFE_PAGE_LIMIT')
        if not full:
            # Keep everything collected earlier that this incremental walk did not revisit.
            listed.extend(e['row'] for i, e in old.items() if i not in seen)
        for row in listed:
            entry = dict(id=row['id'], title=row['title'], doc=row['doc'], date=row['date'], row=row,
                         status='PENDING', path=f"raw/{row['id']}.json")
            cached = old.get(row['id'])
            target = out / entry['path']
            if cached and cached.get('row') == row and target.exists() \
                    and hashlib.sha256(target.read_bytes()).hexdigest() == cached.get('sha256'):
                entry.update(status='SUCCESS', sha256=cached['sha256'], collected_at=cached.get('collected_at'), reused=True)
                manifest['documents'].append(entry)
                continue
            try:
                detail = parse_detail(client.get(DETAIL_URL.format(id=row['id'])))
                if detail['doc'] != row['doc'] or detail['date'] != row['date']:
                    raise ValueError('MOFE_LIST_DETAIL_MISMATCH')
                payload = json.dumps(dict(id=row['id'], **detail), ensure_ascii=False, indent=1).encode('utf-8')
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
                entry.update(status='SUCCESS', sha256=hashlib.sha256(payload).hexdigest(), collected_at=stamp(), reused=False)
            except Budget:
                entry.update(status='PENDING', reason='COLLECTION_BUDGET_EXHAUSTED')
                manifest['documents'].append(entry)
                raise
            except ValueError as exc:
                entry.update(status='FAILED', reason=str(exc))
            manifest['documents'].append(entry)
        manifest['status'] = 'COMPLETE' if all(e['status'] == 'SUCCESS' for e in manifest['documents']) \
            and manifest['documents'] else 'PARTIAL'
    except Budget:
        manifest['status'] = 'INCOMPLETE'
        manifest['stop_reason'] = 'COLLECTION_BUDGET_EXHAUSTED'
        # Entries not reached this run stay reusable next run.
        reached = {e['id'] for e in manifest['documents']}
        manifest['documents'].extend(dict(e, status='SUCCESS') for i, e in old.items() if i not in reached)
    except ValueError as exc:
        manifest['status'] = 'FAILED'
        manifest['stop_reason'] = str(exc)
        manifest['documents'] = previous.get('documents', [])
    manifest['requests'] = client.calls
    manifest['status_counts'] = {s: sum(e['status'] == s for e in manifest['documents']) for s in ('SUCCESS', 'FAILED', 'PENDING')}
    manifest['finished_at'] = stamp()
    save(manifest_path, manifest)
    return manifest['status'] == 'COMPLETE'


def export(out, dest):
    """One JSON file of every SUCCESS document; verifies each cached file against its manifest hash."""
    out = Path(out)
    manifest = strict_json((out / 'manifest.json').read_bytes())
    if manifest.get('schema') != SCHEMA or manifest.get('status') != 'COMPLETE':
        raise ValueError('MOFE_MANIFEST_NOT_COMPLETE')
    items = []
    for entry in sorted(manifest['documents'], key=lambda e: e['id']):
        raw = (out / entry['path']).read_bytes()
        if hashlib.sha256(raw).hexdigest() != entry['sha256']:
            raise ValueError('MOFE_CACHE_HASH_MISMATCH')
        detail = strict_json(raw)
        if detail['id'] != entry['id'] or set(detail['sec']) != set(SECTIONS):
            raise ValueError('MOFE_CACHE_INVALID')
        items.append(dict(id=detail['id'], title=detail['title'], doc=detail['doc'], date=detail['date'],
                          sec=detail['sec'], url=DETAIL_URL.format(id=detail['id'])))
    payload = dict(source=manifest['source'], policy=POLICY, collected_at=manifest['finished_at'],
                   collection_date=manifest['date'], count=len(items), items=items)
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / 'mofe-export.json').write_text(json.dumps(payload, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
    (dest / 'mofe-manifest.json').write_text(json.dumps(
        {k: v for k, v in manifest.items() if k != 'documents'} | {'documents': len(manifest['documents'])},
        ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return len(items)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('command', choices=('collect', 'export'))
    ap.add_argument('--out', type=Path, default=Path('auto/mofe'))
    ap.add_argument('--dest', type=Path, default=Path('release'))
    ap.add_argument('--seconds', type=int, default=3300)
    ap.add_argument('--full', action='store_true')
    ap.add_argument('--partial-ok', action='store_true')
    args = ap.parse_args()
    if args.command == 'export':
        print(f'MOFE_EXPORT_DOCUMENTS={export(args.out, args.dest)}')
        return 0
    complete = collect(Client(seconds=args.seconds), args.out, today(), full=args.full)
    write_step_output(complete)
    manifest = strict_json((args.out / 'manifest.json').read_bytes())
    print(f"MOFE_COLLECT status={manifest['status']} documents={len(manifest['documents'])} "
          f"counts={manifest['status_counts']} pages={manifest['pages']} requests={manifest['requests']}")
    if complete:
        return 0
    return 0 if args.partial_ok and manifest.get('stop_reason') == 'COLLECTION_BUDGET_EXHAUSTED' else 1


if __name__ == '__main__':
    raise SystemExit(main())
