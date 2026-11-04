"""재정경제부 세법해석 사례 collector: synthetic HTML, fake transport, no network."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
import bblove_mofe_collect as M


def ntt(i):
    return f'MOSF_{i:015d}'


def list_html(rows):
    items = ''.join(f"""
  <li>
    <h3><a href="javascript:fn_egov_select('{r['id']}');" title="상세보기">{r['title']}</a></h3>
    <div class="boardInfo"><div class="infoLeft"><span class="depart">{r['doc']}</span>
      <span class="date">회신일자 : {r['date']}</span></div><div class="infoRight"><span>조회 4,459</span></div></div>
  </li>""" for r in rows)
    # The real site leaves menu <li> tags unclosed; the parser must not depend on nesting.
    return f"""<html><body><ul class="menu"><li><a href="/x">뉴스</a><li><a href="/y">정책</a></ul>
<main><div class="subContainer"><div class="body"><ul class="boardType3 explnList">{items}
</ul><div class="paging"><a href="?pageIndex=2">2</a></div></div></div></main></body></html>""".encode('utf-8')


def detail_html(row, sections=None, extra=''):
    sections = sections or {'질문요지': '□ 질문 &amp; 요지', '사실관계': '□ 사실', '회신': '<p>□ 회신 내용</p>', '관계법령': '□ 소득세법 제57조'}
    parts = ''.join(f'<h4 class="taxLawCase">{k}</h4>\n<div class="editorCont taxLawCase">{v}</div>\n' for k, v in sections.items())
    return f"""<html><body><main><div class="subContainer"><div class="body"><div class="detailBoard">
<h3>{row['title']}</h3>
<div class="boardInfo"><div class="infoLeft"><span class="depart mr80">문서번호 : {row['doc']}</span>
<span class="date">회신일자 : {row['date']}</span></div></div>
{parts}{extra}<div class="btnArea"><a href="list">목록</a></div></div></div></div></main></body></html>""".encode('utf-8')


def row(i, **changes):
    base = dict(id=ntt(i), title=f'합성 안건 {i}', doc=f'재산세제과-{i}', date='2026.03.0%d.' % (i % 9 + 1))
    base.update(changes)
    return base


class FakeTransport:
    def __init__(self, pages, details, fail=()):
        self.pages, self.details, self.fail, self.urls = pages, details, set(fail), []

    def __call__(self, url, timeout):
        self.urls.append(url)
        if 'TaxLawIntrPrtCaseList.do' in url:
            page = int(url.rsplit('pageIndex=', 1)[1])
            return list_html(self.pages.get(page, []))
        ident = url.rsplit('searchNttId1=', 1)[1]
        if ident in self.fail:
            raise ValueError('MOFE_HTTP_STATUS')
        return self.details[ident]


def client(transport):
    c = M.Client(seconds=600, interval=1.0, transport=transport)
    c.interval = 0.0  # no pacing in tests; production minimum is 1.0 s
    return c


class ParserTests(unittest.TestCase):
    def test_list_rows_and_detail_sections(self):
        rows = [row(1), row(2, doc='국제조세제도과-104', date='2026.07.22.')]
        parsed = M.parse_list(list_html(rows))
        self.assertEqual(parsed, [dict(r) for r in rows])
        detail = M.parse_detail(detail_html(rows[1]))
        self.assertEqual(detail['doc'], '국제조세제도과-104')
        self.assertEqual(detail['date'], '2026.07.22.')
        self.assertEqual(detail['sec']['질문요지'], '□ 질문 & 요지')
        self.assertEqual(detail['sec']['회신'], '□ 회신 내용')
        self.assertEqual(list(detail['sec']), list(M.SECTIONS))

    def test_malformed_pages_fail_closed(self):
        with self.assertRaises(ValueError):
            M.parse_list(list_html([row(1, doc='문서번호없음')]))
        with self.assertRaises(ValueError):
            M.parse_list(list_html([row(1), row(1)]))
        with self.assertRaises(ValueError):
            M.parse_detail(detail_html(row(1), sections={'회신': 'x'}))
        with self.assertRaises(ValueError):
            M.parse_detail(b'<html><body>502 Bad Gateway</body></html>')
        self.assertEqual(M.parse_list(list_html([])), [])
        with self.assertRaises(ValueError):
            M.parse_list(b'<html><body>blocked</body></html>')


class CollectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.out = Path(self.temp.name) / 'auto' / 'mofe'

    def tearDown(self):
        self.temp.cleanup()

    def world(self, n=13, fail=()):
        rows = [row(i) for i in range(n, 0, -1)]  # newest first
        pages = {p: rows[(p - 1) * 10:p * 10] for p in range(1, (n + 9) // 10 + 1)}
        details = {r['id']: detail_html(r) for r in rows}
        return rows, FakeTransport(pages, details, fail)

    def test_full_walk_then_incremental_stop_and_export(self):
        rows, transport = self.world()
        self.assertTrue(M.collect(client(transport), self.out, '20261105'))
        manifest = json.loads((self.out / 'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual((manifest['status'], manifest['pages'], len(manifest['documents'])), ('COMPLETE', 3, 13))  # page 3 is the empty end marker
        self.assertEqual(sum('CaseView' in u for u in transport.urls), 13)
        # A second run: page 1 unchanged → stops after one list request, re-fetches nothing.
        before = len(transport.urls)
        self.assertTrue(M.collect(client(transport), self.out, '20261112'))
        self.assertEqual(transport.urls[before:], [M.LIST_URL.format(page=1)])
        manifest = json.loads((self.out / 'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(len(manifest['documents']), 13)
        self.assertTrue(all(e['reused'] for e in manifest['documents']))
        # A new reply appears on top → only it is fetched; the rest is kept.
        new = row(14, date='2026.08.01.')
        transport.pages = {1: [new] + rows[:9], 2: rows[9:]}
        transport.details[new['id']] = detail_html(new)
        before = len(transport.urls)
        self.assertTrue(M.collect(client(transport), self.out, '20261119'))
        self.assertEqual([u for u in transport.urls[before:] if 'CaseView' in u], [M.DETAIL_URL.format(id=new['id'])])
        manifest = json.loads((self.out / 'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(len(manifest['documents']), 14)
        count = M.export(self.out, Path(self.temp.name) / 'release')
        self.assertEqual(count, 14)
        payload = json.loads((Path(self.temp.name) / 'release' / 'mofe-export.json').read_text(encoding='utf-8'))
        self.assertEqual(payload['count'], 14)
        self.assertEqual(set(payload['items'][0]), {'id', 'title', 'doc', 'date', 'sec', 'url'})
        self.assertEqual([i['id'] for i in payload['items']], sorted(i['id'] for i in payload['items']))
        self.assertNotIn('<', json.dumps(payload, ensure_ascii=False))

    def test_detail_failure_marks_entry_and_blocks_export(self):
        rows, transport = self.world(n=3, fail=[ntt(2)])
        self.assertFalse(M.collect(client(transport), self.out, '20261105'))
        manifest = json.loads((self.out / 'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['status'], 'PARTIAL')
        self.assertEqual([e['status'] for e in manifest['documents']], ['SUCCESS', 'FAILED', 'SUCCESS'])
        self.assertEqual(manifest['documents'][1]['reason'], 'MOFE_REQUEST_FAILED')
        with self.assertRaises(ValueError):
            M.export(self.out, Path(self.temp.name) / 'release')
        # Next run retries only the failed one.
        transport.fail.clear()
        before = len(transport.urls)
        self.assertTrue(M.collect(client(transport), self.out, '20261106', full=True))
        self.assertEqual([u for u in transport.urls[before:] if 'CaseView' in u], [M.DETAIL_URL.format(id=ntt(2))])

    def test_empty_first_page_is_a_failure(self):
        transport = FakeTransport({}, {})
        self.assertFalse(M.collect(client(transport), self.out, '20261105'))
        manifest = json.loads((self.out / 'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual((manifest['status'], manifest['stop_reason']), ('FAILED', 'MOFE_LIST_EMPTY'))

    def test_budget_exhaustion_keeps_a_resumable_manifest(self):
        rows, transport = self.world(n=5)
        c = client(transport)
        c.deadline = 0  # every request is over budget
        self.assertFalse(M.collect(c, self.out, '20261105'))
        manifest = json.loads((self.out / 'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual((manifest['status'], manifest['stop_reason']), ('INCOMPLETE', 'COLLECTION_BUDGET_EXHAUSTED'))

    def test_list_detail_mismatch_is_a_failure_not_a_record(self):
        rows, transport = self.world(n=1)
        transport.details[ntt(1)] = detail_html(row(1, doc='조세정책과-999'))
        self.assertFalse(M.collect(client(transport), self.out, '20261105'))
        manifest = json.loads((self.out / 'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['documents'][0]['reason'], 'MOFE_LIST_DETAIL_MISMATCH')
        self.assertFalse((self.out / 'raw' / f'{ntt(1)}.json').exists())


class PiiScanTests(unittest.TestCase):
    def test_scan_flags_patterns_only_in_text_fields(self):
        sys.path.insert(0, str(ROOT / 'scripts'))
        import mofe_pii_scan
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            (root / 'raw').mkdir()
            clean = dict(id=ntt(1), title='합성', doc='재산세제과-1', date='2026.01.01.',
                         sec={'질문요지': '□ 2026.01.01. 양도', '사실관계': '사업자번호 123-45-67890', '회신': '소득세법 제12조', '관계법령': ''})
            (root / 'raw' / 'a.json').write_text(json.dumps(clean, ensure_ascii=False), encoding='utf-8')
            phone = '010' + '-1234-' + '5678'  # assembled so the repository guard never sees a literal number
            dirty = dict(clean, sec=dict(clean['sec'], 회신='연락처 ' + phone))
            (root / 'raw' / 'b.json').write_text(json.dumps(dirty, ensure_ascii=False), encoding='utf-8')
            (root / 'manifest.json').write_text(json.dumps({'documents': [{'title': phone}]}), encoding='utf-8')
            self.assertEqual(mofe_pii_scan.scan(root), [('b.json', 'mobile_phone')])


if __name__ == '__main__':
    unittest.main()
