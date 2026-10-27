"""Offline SYNTHETIC retrieval benchmark and repeatable SQLite/D1-local probe.

The synonym rules are query-side legal vocabulary, never invented case facts.
No API calls, credentials, remote D1, or production datasets are used here.
"""
import argparse
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile

from build_precedent_d1 import COLUMNS, insert_sql, schema, wrangler_json

# Synthetic case numbers and summaries: NOT official precedents or legal advice.
EXAMPLES = [
    ('명의만 빌려준 주식 증여', '명의신탁 주식의 증여의제', '조세회피목적 명의신탁 증여의제 요건'),
    ('세무조사 두 번 받았어요', '중복세무조사 금지', '재조사 허용 사유와 중복세무조사 판단'),
    ('가산세 깎아주는 정당한 사유', '가산세 감면의 정당한 사유', '납세의무 해태에 정당한 사유가 있는지'),
    ('부정행위면 10년', '부정행위 부과제척기간', '사기 기타 부정한 행위와 십년의 부과제척기간'),
    ('세금 매길 수 있는 기간 지났나요', '국세 부과제척기간', '국세 부과제척기간의 기산일과 만료'),
    ('세금 돌려달라고 고치고 싶어요', '경정청구 기간', '과다신고 경정청구의 요건과 청구기간'),
    ('가짜 세금계산서인 줄 몰랐어요', '사실과 다른 세금계산서', '매입세액공제 선의 무과실 입증책임'),
    ('사업이랑 상관없는 돈 썼어요', '업무무관 비용 손금불산입', '업무무관 지출과 손금불산입 요건'),
    ('가족에게 너무 싸게 팔았어요', '특수관계인 부당행위계산부인', '특수관계인 저가양도 시가와 부당행위계산부인'),
    ('차명 계좌에 매출 넣었어요', '차명계좌 매출누락', '매출누락과 부정행위의 입증'),
    ('물려받은 집 얼마로 평가하나요', '상속재산 시가평가', '상속재산 평가기준일과 매매사례가액'),
    ('집 한 채 팔면 세금 없나요', '일세대 일주택 비과세', '일세대 일주택 보유기간과 거주요건'),
    ('돈 빌려주고 못 받았어요', '대손금 손금산입', '회수불능 채권 대손금 손금산입 시기'),
    ('영수증 없이 경비 인정되나요', '필요경비 입증책임', '필요경비 지출 사실과 납세자 입증책임'),
    ('세금 안내 믿고 신고했어요', '신뢰보호 원칙', '과세관청 공적 견해표명 신뢰보호 요건'),
    ('처분 전에 의견 낼 기회 없었어요', '과세전적부심사 절차', '과세예고 통지와 과세전적부심사 청구권'),
    ('명의만 사장인데 세금 내래요', '실질과세 원칙', '명의상 대표자와 실질귀속자 실질과세'),
    ('세금 대신 낸 돈 돌려받나요', '국세환급금 청구권', '오납 국세환급금 청구권의 귀속'),
    ('계약 깨져서 받은 돈도 소득인가요', '위약금 기타소득', '계약 해약 위약금 기타소득 과세대상'),
    ('해외에서 낸 세금 빼주나요', '외국납부세액공제', '외국납부세액공제의 범위와 한도'),
]

# Synthetic tribunal questions and relevant summaries; not official decisions.
TRIBUNAL_EXAMPLES = [
    ('경정청구 거부 심판 인용', '경정청구 거부처분 취소',
     '과다신고 경정청구 거부처분의 적법성과 청구기간', '인용', '법인', '국세기본법'),
    ('가산세 감면 정당한 사유 심판례', '가산세 감면의 정당한 사유',
     '가산세 면제에 필요한 정당한 사유와 납세의무 해태', '기각', '부가', '국세기본법'),
    ('명의신탁 증여의제 조세회피 목적 심판', '명의신탁 증여의제 요건',
     '명의신탁 주식 증여의제와 조세회피 목적의 입증', '인용', '상증', '상속세 및 증여세법'),
    ('매입세액공제 허위 세금계산서 심판', '사실과 다른 세금계산서 매입세액공제',
     '허위 세금계산서의 거래 사실과 매입세액공제 증빙', '기각', '부가', '부가가치세법'),
    ('취득세 감면 유예기간 직접 사용 심판', '취득세 감면 유예기간',
     '유예기간 내 직접 사용하지 못한 취득세 감면 추징 사유', '인용', '취득', '지방세특례제한법'),
    ('과점주주 제2차 납세의무 심판 재조사', '과점주주 제2차 납세의무',
     '과점주주의 실질 주주권과 제2차 납세의무 지정', '재조사', '기타', '국세기본법'),
    ('상속재산 시가 매매사례가액 심판', '상속재산 시가 평가',
     '상속재산 평가기준일의 매매사례가액과 시가 증빙', '기각', '상증', '상속세 및 증여세법'),
    ('중복세무조사 재조사 금지 심판', '중복세무조사 금지',
     '중복세무조사 해당 여부와 예외적인 재조사 사유', '인용', '소득', '국세기본법'),
    ('부과제척기간 부정행위 십년 심판', '부정행위 부과제척기간',
     '부정행위 해당 여부와 십년 부과제척기간 기산일', '기각', '법인', '국세기본법'),
    ('부당행위계산부인 특수관계인 시가 심판', '특수관계인 부당행위계산부인',
     '특수관계인 저가양도의 시가와 부당행위계산부인', '재조사', '양도', '소득세법'),
]

# Phrase-to-concept vocabulary; phrase fragments also cover unseen paraphrases.
ALIASES = {
    '명의만 빌려': '명의신탁 증여의제', '명의신탁': '명의신탁 증여의제',
    '두 번': '중복세무조사 재조사', '재조사': '중복세무조사',
    '가산세': '가산세 정당한 사유', '10년': '부과제척기간 부정행위',
    '부정행위': '부정행위 부과제척기간', '기간 지났': '부과제척기간',
    '고치고': '경정청구', '가짜 세금계산서': '세금계산서 매입세액공제 선의',
    '사업이랑 상관없': '업무무관 손금불산입', '가족에게': '특수관계인 부당행위계산부인',
    '차명 계좌': '차명계좌 매출누락', '물려받': '상속재산 시가평가',
    '집 한 채': '일주택 비과세', '못 받': '대손금 회수불능',
    '영수증': '필요경비 입증책임', '안내 믿': '신뢰보호 견해표명',
    '의견 낼': '과세전적부심사 과세예고', '명의만 사장': '실질과세 실질귀속자',
    '대신 낸': '국세환급금 오납', '계약 깨': '위약금 기타소득',
    '해외에서 낸': '외국납부세액공제',
}


def match_query(query, tokenizer='trigram', expand=True):
    terms = re.findall(r'[가-힣A-Za-z0-9]+', query)
    if expand:
        for phrase, concepts in ALIASES.items():
            if phrase in query:
                terms.extend(concepts.split())
    minimum = 3 if tokenizer == 'trigram' else 1
    terms = sorted({s for s in terms if len(s) >= minimum})
    # Token punctuation never reaches MATCH syntax. Parameterize this result.
    return ' OR '.join('"' + s + '"' for s in terms)


def fixtures():
    rows = []
    for n, (_, title, summary) in enumerate(EXAMPLES, 1):
        row = dict(id=str(n), case_no=f'2099두{n}', court='합성대법원', date='20260101',
                   title=title, issues=summary, holding='합성 fixture: 실제 판결 아님',
                   refs='합성 참조조문', url=f'https://www.law.go.kr/LSW/precInfoP.do?precSeq={n}')
        rows.append(('precedents', row))
    # Topic-overlap distractors make top-five ranking meaningful (80 documents).
    for n in range(21, 81):
        row = dict(rows[(n - 21) % 20][1])
        row.update(id=str(n), case_no=f'2099두{n}', title='합성 일반 절차',
                   issues='과세처분 취소 일반 절차 입증책임 신고 납부', refs='')
        rows.append(('precedents', row))
    rows.append(('interpretations', dict(id='1', number='합성-001', title='명의신탁 증여의제 질의',
        date='20260101', question='명의신탁 주식 질의', answer='합성 회신', laws='합성법',
        url='https://www.law.go.kr/LSW/expcInfoP.do?expcSeq=1')))
    tribunals = []
    for n, (_, title, summary, kind, tax_type, laws) in enumerate(TRIBUNAL_EXAMPLES, 1):
        tribunals.append(('tribunal_decisions', dict(id=str(n), number=f'조심2026서{n:04d}', date='20260101',
            decision_type=kind, tax_type=tax_type, title=title, summary='합성 요지: ' + summary,
            laws=laws, url=f'https://www.law.go.kr/LSW/deccInfoP.do?deccSeq={n}')))
    rows.extend(tribunals)
    for n in range(11, 41):
        # Shared tax category, references and limited topic overlap; the relevant
        # document has the full issue rather than this generic procedure text.
        original = tribunals[(n - 11) % len(TRIBUNAL_EXAMPLES)][1]
        rows.append(('tribunal_decisions', {**original, 'id': str(n), 'number': f'조심2026서{n:04d}',
            'title': '합성 일반 심판청구 절차', 'summary': '청구기간 증빙자료 과세처분 심리 ' + original['tax_type'],
            'url': f'https://www.law.go.kr/LSW/deccInfoP.do?deccSeq={n}'}))
    return rows


def evaluate(tokenizer, expand=True):
    db = sqlite3.connect(':memory:')
    db.executescript(schema(tokenizer))
    before = db.total_changes
    for table, row in fixtures():
        db.execute(insert_sql(table, row))
    writes = db.total_changes - before
    results = []
    for n, (query, _, _) in enumerate(EXAMPLES, 1):
        match = match_query(query, tokenizer, expand)
        found = [str(r[0]) for r in db.execute('SELECT p.id FROM precedents_fts '
            'JOIN precedents p ON p.rowid=precedents_fts.rowid WHERE precedents_fts MATCH ? '
            'ORDER BY bm25(precedents_fts), p.id LIMIT 5', (match,))] if match else []
        results.append(dict(query=query, expected=str(n), top5=found, hit=str(n) in found))
    tribunal_results = []
    for n, (query, *_) in enumerate(TRIBUNAL_EXAMPLES, 1):
        match = match_query(query, tokenizer, expand)
        found = [str(r[0]) for r in db.execute('SELECT t.id FROM tribunal_decisions_fts '
            'JOIN tribunal_decisions t ON t.rowid=tribunal_decisions_fts.rowid WHERE tribunal_decisions_fts MATCH ? '
            'ORDER BY bm25(tribunal_decisions_fts), t.id LIMIT 5', (match,))] if match else []
        tribunal_results.append(dict(query=query, expected=str(n), top5=found, hit=str(n) in found))
    size = db.execute('PRAGMA page_count').fetchone()[0] * db.execute('PRAGMA page_size').fetchone()[0]
    db.close()
    return dict(tokenizer=tokenizer, query_expansion=expand, synthetic=True, total=20,
                hits=sum(r['hit'] for r in results), documents=len(fixtures()),
                database_bytes=size, total_changes=writes, results=results,
                tribunal=dict(total=len(TRIBUNAL_EXAMPLES), hits=sum(r['hit'] for r in tribunal_results),
                              results=tribunal_results))


def probe(wrangler):
    observations = []
    with tempfile.TemporaryDirectory(prefix='precedent-tokenizer-') as tmp:
        folder = Path(tmp)
        config = folder / 'wrangler.toml'
        config.write_text('name="precedent-tokenizer-probe"\ncompatibility_date="2026-09-01"\n'
            '[[d1_databases]]\nbinding="PRECEDENT_DB"\ndatabase_name="precedent-probe"\n'
            'database_id="00000000-0000-0000-0000-000000000008"\n')
        for tokenizer in ('trigram', 'unicode61'):
            sql = (f"CREATE VIRTUAL TABLE probe_{tokenizer} USING fts5(body, tokenize='{tokenizer}');\n"
                f"INSERT INTO probe_{tokenizer} VALUES ('명의신탁 주식 증여의제');\n"
                f"SELECT rowid FROM probe_{tokenizer} WHERE probe_{tokenizer} MATCH '\"명의신\"';\n")
            db = sqlite3.connect(':memory:')
            statements = sql.strip().split(';')
            for stmt in statements[:2]:
                db.execute(stmt)
            local = db.execute(statements[2]).fetchall()
            db.close()
            file = folder / f'{tokenizer}.sql'
            file.write_text(sql, encoding='utf-8')
            command = [str(Path(wrangler).resolve()), 'd1', 'execute', 'precedent-probe', '--local',
                       '--config', str(config), '--persist-to', str(folder / 'state'), '--file', str(file)]
            try:
                result = wrangler_json(command)
                observations.append(dict(tokenizer=tokenizer, sqlite_supported=True,
                    sqlite_substring_hits=len(local), d1_local_supported=True,
                    d1_local_substring_hits=len(result[-1]['results'])))
            except ValueError:
                observations.append(dict(tokenizer=tokenizer, sqlite_supported=True,
                    sqlite_substring_hits=len(local), d1_local_supported=False))
    return observations


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--report', type=Path)
    ap.add_argument('--wrangler', help='If set, run D1 LOCAL tokenizer probe too')
    args = ap.parse_args()
    reports = [evaluate(t, expand) for t in ('trigram', 'unicode61') for expand in (False, True)]
    result = dict(sqlite_version=sqlite3.sqlite_version, evaluations=reports)
    if args.wrangler:
        result['d1_local_probe'] = probe(args.wrangler)
    raw = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(raw, encoding='utf-8')
    print(json.dumps({t: dict(precedent_hits=evaluate(t)['hits'], tribunal_hits=evaluate(t)['tribunal']['hits'])
                      for t in ('trigram', 'unicode61')}))
    measured = evaluate('trigram')
    return 0 if measured['hits'] >= 16 and measured['tribunal']['hits'] >= 8 else 1


if __name__ == '__main__':
    raise SystemExit(main())
