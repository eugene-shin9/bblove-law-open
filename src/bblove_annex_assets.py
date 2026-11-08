"""Offline annex extraction. Raw text is authoritative; tables are optional views."""
from collections import Counter, defaultdict
import hashlib
import json
import re
import unicodedata
from urllib.parse import unquote, urljoin, urlsplit, quote

from bblove_quality import credential_markers
from bblove_storage import strict_json
from bblove_v107_pilot import text

LIMIT = 8 * 1024 * 1024
KINDS = {'별표', '별도', '부록', '도식'}
# Real 2026-10-15 data also has '별지', '부도' and may gain new labels: never fail the law build on a label;
# anything outside KINDS is stored like a form (title·number·official links only, no body).
BOX = re.compile('[\u2500-\u257f]')
VERTICAL = set('│┃║┆┊┇┋')
HORIZONTAL = set('─━═┄┅┈┉')


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def guard(value):
    # No secret environment access. Decode URL encodings before checking markers;
    # even a redacted OC parameter must never become a public asset link.
    raw = encode(value)
    decoded = raw.decode('utf-8')
    for _ in range(3):
        decoded = unquote(decoded).replace('&amp;', '&')
    if (credential_markers(decoded.encode()) or re.search(r'(?i)\bOC\s*=', decoded) or
            re.search(r'(?i)"(?:OC|LAW_OC|authorization|client_secret|secret|token|password|api[_-]?key|access[_-]?token)"\s*:\s*"[^"]+"', decoded)):
        raise ValueError('ANNEX_AUTH_MARKER')


def original(value):
    """Source fragments verbatim. DRF sends a table as a list of display lines, so list items are
    joined with a line break (the line boundary is part of the source layout); characters are untouched."""
    if value is None:
        return ''
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return '\n'.join(original(v) for v in value)
    if isinstance(value, dict):
        return '\n'.join(original(v) for v in value.values())
    raise ValueError('ANNEX_UNSUPPORTED_TEXT_SHAPE')


def official_link(value):
    if not value:
        return ''
    value = str(value).strip()
    guard(value)
    if any(ord(c) < 32 for c in value):
        raise ValueError('ANNEX_NONOFFICIAL_LINK')
    result = urljoin('https://www.law.go.kr/', value)
    parts = urlsplit(result)
    if (parts.scheme != 'https' or parts.netloc != 'www.law.go.kr' or
            parts.username or parts.password or any(ord(c) < 32 for c in result)):
        raise ValueError('ANNEX_NONOFFICIAL_LINK')
    return result


def characters(value):
    return Counter(c for c in value if not c.isspace() and not BOX.fullmatch(c))


def lossless(raw, table):
    return characters(raw) == characters(''.join(c for row in table['rows'] for c in row))


WIDE = ({'W', 'F', 'A'}, {'W', 'F'})  # CJK layout (real 법제처 text) first, then narrow ambiguous width


def positions(line, wide=WIDE[0]):
    out, x = [], 0
    for c in line:
        if c == '\t' or unicodedata.combining(c):
            raise ValueError('unsupported_width')
        # 법제처 tables are drawn for CJK fonts: ambiguous-width box lines take two columns there.
        width = 2 if unicodedata.east_asian_width(c) in wide else 1
        out.append((x, c))
        x += width
    return out


def parse_block(lines):
    """Try each deterministic width model; the first geometry that parses is used (lossless check follows).
    The strict full-rule grid goes first so every table it accepts keeps its exact earlier view; the
    segment grid only handles what it rejects (partial rules, open frames, one-cell border drift)."""
    error = None
    for parser in (parse_grid, parse_segments):
        for wide in WIDE:
            try:
                return parser([positions(s, wide) for s in lines])
            except ValueError as exc:
                error = error or exc
    raise error


def parse_grid(lines):
    """Infer rectangular cells from border geometry, including empty merge slots."""
    separators = [i for i, line in enumerate(lines)
                  if any(c in HORIZONTAL for _, c in line) and
                  all(c.isspace() or BOX.fullmatch(c) for _, c in line)]
    if len(separators) < 2 or separators[0] != 0 or separators[-1] != len(lines) - 1:
        raise ValueError('open_or_mixed_border')
    xs = sorted({x for i in separators for x, c in lines[i]
                 if BOX.fullmatch(c) and c not in HORIZONTAL and c not in VERTICAL})
    if len(xs) < 2:
        raise ValueError('missing_grid')
    bands = []
    for a, b in zip(separators, separators[1:]):
        body = lines[a + 1:b]
        if not body:
            raise ValueError('empty_band')
        borders = [{x for x, c in line if c in VERTICAL} for line in body]
        if any(v != borders[0] for v in borders) or not {xs[0], xs[-1]} <= borders[0]:
            raise ValueError('inconsistent_vertical_border')
        if not borders[0] <= set(xs):
            raise ValueError('unknown_column')
        bands.append((a, b, body, borders[0]))
    nr, nc = len(bands), len(xs) - 1
    parents = list(range(nr * nc))

    def root(n):
        while parents[n] != n:
            parents[n] = parents[parents[n]]
            n = parents[n]
        return n

    def union(a, b):
        parents[root(b)] = root(a)

    for r, (a, _, body, borders) in enumerate(bands):
        for c in range(nc - 1):
            if xs[c + 1] not in borders:
                union(r * nc + c, r * nc + c + 1)
        if r:
            boundary = dict(lines[a])
            for c in range(nc):
                interior = [boundary.get(x, ' ') for x in range(xs[c] + 1, xs[c + 1])]
                if not any(v in HORIZONTAL for v in interior):
                    union((r - 1) * nc + c, r * nc + c)
    components = defaultdict(list)
    for r in range(nr):
        for c in range(nc):
            components[root(r * nc + c)].append((r, c))
    rows, merges = [['' for _ in range(nc)] for _ in range(nr)], []
    anchors = {}
    for cells in components.values():
        top, left = min(r for r, c in cells), min(c for r, c in cells)
        height = max(r for r, c in cells) - top + 1
        width = max(c for r, c in cells) - left + 1
        if len(cells) != height * width:
            raise ValueError('nonrectangular_merge')
        for cell in cells:
            anchors[cell] = (top, left)
        if height > 1 or width > 1:
            merges.append({'row': top, 'col': left, 'rowspan': height, 'colspan': width})
    for r, (_, _, body, _) in enumerate(bands):
        for line in body:
            fragments = defaultdict(str)
            for x, ch in line:
                if ch.isspace() or BOX.fullmatch(ch):
                    # Preserve spaces inside cells; borders themselves are view markup.
                    if not ch.isspace():
                        continue
                if xs[0] < x < xs[-1]:
                    c = next((i for i in range(nc) if xs[i] < x < xs[i + 1]), None)
                    # A missing inner vertical border belongs to its merged cell.
                    if c is None and x in xs[1:-1]:
                        c = xs.index(x) - 1
                    if c is not None:
                        fragments[anchors[r, c]] += ch
                elif not ch.isspace() and not BOX.fullmatch(ch):
                    raise ValueError('text_outside_grid')
            for (rr, cc), value in fragments.items():
                if not value.strip():
                    continue
                rows[rr][cc] += ('\n' if rows[rr][cc] else '') + value.strip()
    return {'rows': rows, 'merges': merges}


SNAP = 2  # HWP export shifts a border by at most one character cell


def is_stop(c):
    return bool(BOX.fullmatch(c)) and c not in HORIZONTAL


def is_text(c):
    return not c.isspace() and not BOX.fullmatch(c)


STROKES = set('│┃║┆┇┊┋┌┍┎┏┐┑┒┓└┕┖┗┘┙┚┛├┝┞┟┠┡┢┣┤┥┦┧┨┩┪┫┬┭┮┯┰┱┲┳┴┵┶┷┸┹┺┻┼┽┾┿╀╁╂╃╄╅╆╇╈╉╊╋'
              '╒╓╔╕╖╗╘╙╚╛╜╝╞╟╠╡╢╣╤╥╦╧╨╩╪╫╬╭╮╯╰')


def strokes(c):
    """(up, down) strokes of a junction/border character, from its Unicode name."""
    name = unicodedata.name(c, '')
    vertical = 'VERTICAL' in name or c in VERTICAL
    up = vertical or ' UP' in name or c in '╯╰'
    down = vertical or ' DOWN' in name or c in '╭╮'
    return up, down


def parse_segments(lines):
    """Cells from line×column segments: a rule may span only some columns (a cell spanning rows
    continues through it), the outer frame may be missing, and a border drawn one cell off in
    other rows is snapped. Anything that would need guessing raises instead."""
    n = len(lines)
    if any(BOX.fullmatch(c) and c not in HORIZONTAL and c not in STROKES for line in lines for _, c in line):
        raise ValueError('unsupported_box_shape')  # diagonals, dashes, half lines: not guessed
    stop_sets = [{x for x, c in line if is_stop(c)} for line in lines]
    stops = sorted(set().union(*stop_sets))
    if not stops:
        raise ValueError('missing_grid')
    # A border drawn one cell off in some rows is the same column boundary, provided the two
    # positions never appear together on one line.
    clusters = []
    for x in stops:
        last = clusters[-1] if clusters else None
        if (last and x - last[0] <= SNAP and
                not any(x in s and any(y in s for y in last) for s in stop_sets)):
            last.append(x)
        else:
            clusters.append([x])
    texts = [x for line in lines for x, c in line if is_text(c)]
    lo, hi = min(texts, default=stops[0]), max(texts, default=stops[-1])
    vleft, vright = lo < clusters[0][0], hi > clusters[-1][-1]
    bounds, present = [], []
    for i in range(n):
        b, p = [], []
        if vleft:
            b.append(-1); p.append(True)
        for cl in clusters:
            hit = [x for x in cl if x in stop_sets[i]]
            b.append(hit[0] if hit else cl[0]); p.append(bool(hit))
        if vright:
            b.append(hi + 2); p.append(True)
        bounds.append(b); present.append(p)
    nc = len(bounds[0]) - 1
    if nc < 1:
        raise ValueError('missing_grid')

    def col(i, x):
        b = bounds[i]
        for c in range(nc):
            if b[c] < x < b[c + 1]:
                return c
        k = next((k for k in range(1, nc) if b[k] == x and not present[i][k]), None)
        return None if k is None else k - 1  # text on an absent inner border: merged cell

    kind, inside = [], [set() for _ in range(nc)]
    for i, line in enumerate(lines):
        segs = [[] for _ in range(nc)]
        for x, ch in line:
            b = bounds[i]
            c = next((c for c in range(nc) if b[c] < x < b[c + 1]), None)
            if c is not None:
                segs[c].append(ch)
                if not ch.isspace():
                    inside[c].add(i)
        row = []
        for c, s in enumerate(segs):
            # A rule fills its whole span; a fraction bar inside a cell has spaces around it.
            body = ''.join(s)
            if vleft and c == 0:
                body = body.lstrip()
            if vright and c == nc - 1:
                body = body.rstrip()
            if body and all(ch in HORIZONTAL for ch in body):
                row.append('h')
                continue
            # Otherwise a horizontal run is only a fraction bar when spaces surround it; a run that
            # touches a border or a character is a broken rule and is not guessed.
            for run in re.finditer('[%s]+' % ''.join(sorted(HORIZONTAL)), body):
                if (run.start() == 0 or run.end() == len(body) or
                        not body[run.start() - 1].isspace() or not body[run.end()].isspace()):
                    raise ValueError('ambiguous_rule')
            row.append('o')
        kind.append(row)
    for c in range(nc):
        # An empty sliver column is only a double border if both edges are drawn on every line.
        if not inside[c] and not all(present[i][c] and present[i][c + 1] for i in range(n)):
            raise ValueError('misaligned_border')
    rule = [any(k == 'h' for k in row) for row in kind]
    # A vertical border can only start or stop at a rule line (junction). Between two text lines
    # the drawn borders must match; otherwise character widths drifted and cells would be guessed.
    for i in range(n - 1):
        a, b = present[i], present[i + 1]
        if not rule[i] and not rule[i + 1] and a != b:
            raise ValueError('inconsistent_vertical_border')
        for x, y, r in ((a, b, rule[i + 1]), (b, a, rule[i])):
            if r and any(x[k] and not y[k] for k in range(len(x))):
                raise ValueError('inconsistent_vertical_border')
        for j, t, way in ((i, i + 1, 1), (i + 1, i, 0)):
            # A junction drawn with a stroke toward a text line means that line has the border too.
            if rule[j] and not rule[t]:
                for k, x in enumerate(bounds[j]):
                    ch = dict(lines[j]).get(x)
                    if present[j][k] and ch and strokes(ch)[way] and not present[t][k]:
                        raise ValueError('inconsistent_vertical_border')
    # A fraction bar counts as cell content, so numerator and denominator stay in one row.
    blank = [not rule[i] and not any(is_text(ch) or ch in HORIZONTAL for _, ch in line)
             for i, line in enumerate(lines)]
    filled = [[any(is_text(ch) or ch in HORIZONTAL for x, ch in lines[i]
                   if bounds[i][c] < x < bounds[i][c + 1]) for c in range(nc)] for i in range(n)]
    # Rows: runs of lines between rules. A band is cut further at blank lines only when every
    # piece fills every column (borderless rate schedules); otherwise a blank line is padding
    # inside multi-line cells and row spans.
    band, cut, b, i = [None] * n, [False] * n, -1, 0
    while i < n:
        if rule[i]:
            i += 1
            continue
        j = i
        while j < n and not rule[j]:
            j += 1
        pieces, current = [], []
        for t in range(i, j):
            if blank[t]:
                if current:
                    pieces.append(current); current = []
            else:
                current.append(t)
        if current:
            pieces.append(current)
        if len(pieces) > 1 and all(all(any(filled[t][c] for t in piece) for c in range(nc)) for piece in pieces):
            for t in range(i, j):
                cut[t] = blank[t]
            for piece in pieces:
                b += 1
                for t in piece:
                    band[t] = b
        else:
            b += 1
            for t in range(i, j):
                band[t] = b
        i = j
    nr = b + 1
    if nr == 0:
        raise ValueError('empty_band')
    parent = list(range(n * nc))

    def root(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        ra, rb = root(a), root(b)
        if ra != rb:
            parent[rb] = ra

    for i in range(n):
        for c in range(nc - 1):
            if kind[i][c] == 'o' and kind[i][c + 1] == 'o' and not present[i][c + 1]:
                union(i * nc + c, i * nc + c + 1)
        if i + 1 < n and not cut[i] and not cut[i + 1]:
            for c in range(nc):
                if kind[i][c] == 'o' and kind[i + 1][c] == 'o':
                    union(i * nc + c, (i + 1) * nc + c)
    comps = defaultdict(set)
    for i in range(n):
        if band[i] is None:
            continue
        for c in range(nc):
            if kind[i][c] == 'o':
                comps[root(i * nc + c)].add((band[i], c))
    anchor, merges = {}, []
    for key, cells in comps.items():
        top, left = min(r for r, _ in cells), min(c for _, c in cells)
        h = max(r for r, _ in cells) - top + 1
        w = max(c for _, c in cells) - left + 1
        if len(cells) != h * w:
            raise ValueError('nonrectangular_merge')
        anchor[key] = (top, left)
        if h > 1 or w > 1:
            merges.append({'row': top, 'col': left, 'rowspan': h, 'colspan': w})
    rows = [['' for _ in range(nc)] for _ in range(nr)]
    for i, line in enumerate(lines):
        frag = defaultdict(str)
        for x, ch in line:
            c = col(i, x)
            if BOX.fullmatch(ch):
                # Keep a fraction bar inside a cell (numerator above, denominator below); all
                # other box characters are view markup and excluded from the lossless check.
                b = bounds[i]
                if not (ch in HORIZONTAL and c is not None and kind[i][c] == 'o' and b[c] < x < b[c + 1]):
                    continue
            if c is None:
                if is_text(ch):
                    raise ValueError('text_outside_grid')
                continue
            if kind[i][c] != 'o':
                if is_text(ch):
                    raise ValueError('text_on_rule')
                continue
            key = root(i * nc + c)
            if key in anchor:
                frag[anchor[key]] += ch
            elif is_text(ch):
                raise ValueError('text_outside_grid')
        for (r, c), v in frag.items():
            if v.strip():
                rows[r][c] += ('\n' if rows[r][c] else '') + v.strip()
    return {'rows': rows, 'merges': sorted(merges, key=lambda m: (m['row'], m['col']))}


def parse_table(raw):
    """Real annexes mix title/notes text with one or more box tables. Each contiguous block of box
    lines is parsed on its own and must pass the character-multiset check against that block;
    text lines outside the boxes stay only in the verbatim raw text."""
    if not BOX.search(raw):
        return None, 'not_table'
    try:
        blocks, current = [], []
        for line in raw.splitlines():
            if BOX.search(line):
                current.append(line)
            elif current:
                blocks.append(current); current = []
        if current:
            blocks.append(current)
        tables = []
        for block in blocks:
            # Consecutive pages may touch without blank lines: bottom then top border.
            pages, start = [], 0
            for i in range(1, len(block)):
                if re.search('[┌╔┏]', block[i]) and re.search('[┘╝┛]', block[i - 1]):
                    pages.append(block[start:i]); start = i
            pages.append(block[start:])
            for page in pages:
                part = parse_block(page)
                if not part['rows'] or not lossless('\n'.join(page), part):
                    return None, 'character_multiset_mismatch'
                tables.append(part)
        if not tables:
            return None, 'missing_grid'
        return {'tables': tables}, 'accepted'
    except ValueError as exc:
        return None, str(exc)


def extract(doc, entry):
    guard(doc)  # Includes forms and unused fields, before any projection.
    section = doc['법령'].get('별표')
    if section is None:
        return []
    if not isinstance(section, dict):
        raise ValueError('ANNEX_INVALID_SECTION')
    units = section.get('별표단위')
    if units is not None and not isinstance(units, (dict, list)):
        raise ValueError('ANNEX_INVALID_UNITS')
    items = [units] if isinstance(units, dict) else (units or [])
    result = []
    for position, unit in enumerate(items):
        invalid_unit = not isinstance(unit, dict)
        if invalid_unit:
            unit = {}  # No statutory identity/body can be inferred from this shape.
        category = text(unit, '별표구분')
        errors = ['ANNEX_INVALID_UNIT'] if invalid_unit else []
        if not category:
            category = '구분미상'
            errors.append('ANNEX_UNKNOWN_KIND')
        links = []
        for field in ('별표서식PDF파일링크', '별표서식파일링크'):
            try:
                links.append(official_link(text(unit, field)))
            except ValueError as exc:
                if str(exc) == 'ANNEX_AUTH_MARKER':
                    raise
                links.append('')
                errors.append('ANNEX_NONOFFICIAL_LINK')
        pdf, file = links
        item = {'law_id': entry['id'], 'law': entry['title'], 'kind': entry['kind'],
                'effective': entry['effective'], 'number': text(unit, '별표번호'),
                'subnumber': text(unit, '별표가지번호'), 'type': category,
                'title': text(unit, '별표제목'), 'pdf': pdf, 'file': file}
        if invalid_unit:
            item['source_position'] = position
        if not pdf:
            item['pdf_missing_reason'] = 'source_pdf_link_absent'
        if not file and not pdf:
            item['link_missing_reason'] = 'source_official_links_absent'
        if category in KINDS:
            try:
                raw = original(unit.get('별표내용'))
            except ValueError:
                raw = ''  # Unknown source shapes are not guessed or partially flattened.
                errors.append('ANNEX_UNSUPPORTED_TEXT_SHAPE')
            item['raw'] = raw
            item['raw_sha256'] = digest(raw.encode('utf-8'))
            table, reason = parse_table(raw)
            item['parse_status'] = reason
            if table is not None:
                item['table'] = table
        if errors:
            item.pop('table', None)
            item['extraction_errors'] = sorted(set(errors))
            item['parse_status'] = 'extraction_failed'
        result.append(item)
    return result


def size_fallback(item):
    """Keep identities and official links, never truncate an authoritative body."""
    return {k: v for k, v in item.items() if k in {
        'law_id', 'law', 'kind', 'effective', 'number', 'subnumber', 'type',
        'title', 'pdf', 'file', 'pdf_missing_reason', 'link_missing_reason', 'source_position'}} | {
        'fallback': 'size_exceeded',
        'source_url': 'https://www.law.go.kr/법령/' + quote(item['law'], safe='')}


def number_key(item):
    # All laws in a family sharing a displayed number stay in one part. A number
    # request consequently never needs to fetch multiple files on a cold call.
    no = lambda s: str(int(s)) if s and s.isdigit() else s
    return f"{item['type']} {no(item['number'])}" + (f"의{no(item['subnumber'])}" if item['subnumber'] and no(item['subnumber']) != '0' else '')


def index_row(item, file):
    return [number_key(item), item['title'], file, item['law_id']]


def write_assets(stage, grouped, identifiers, limit=LIMIT):
    if not 0 < limit <= LIMIT:
        raise ValueError('ANNEX_PART_LIMIT_INVALID')
    folder = stage / 'annex'
    folder.mkdir()
    index, files, failures = {}, {}, Counter()
    oversized = []
    stats = Counter(annexes=0, forms=0, texts=0, tables_accepted=0, tables_failed=0,
                    raw_bytes=0, form_body_bytes=0, missing_pdf=0, missing_links=0)
    for group, items in grouped.items():
        if not items:
            continue
        units = defaultdict(list)
        for item in items:
            units[number_key(item).split(' ', 1)[1]].append(item)
            stats['annexes' if item['type'] in KINDS else 'forms'] += 1
            stats['missing_pdf'] += not bool(item['pdf'])
            stats['missing_links'] += not bool(item['pdf'] or item['file'])
            if item.get('extraction_errors'):
                stats['tables_failed'] += 1
                failures.update(item['extraction_errors'])
            if 'raw' in item:
                stats['texts'] += bool(item['raw'])
                stats['raw_bytes'] += len(item['raw'].encode('utf-8'))
                reason = item['parse_status']
                if reason == 'accepted':
                    stats['tables_accepted'] += 1
                elif reason not in {'not_table', 'extraction_failed'}:
                    stats['tables_failed'] += 1; failures[reason] += 1
        parts, current, current_bytes = [], [], 2
        for same_number in units.values():
            unit_bytes = len(encode(same_number))
            if unit_bytes > limit:
                oversized.append({'group': group, 'number': number_key(same_number[0]),
                                  'bytes': unit_bytes, 'entries': len(same_number)})
                for item in same_number:
                    if 'raw' in item:
                        stats['texts'] -= bool(item['raw'])
                        stats['raw_bytes'] -= len(item['raw'].encode('utf-8'))
                        stats['tables_accepted'] -= item['parse_status'] == 'accepted'
                        if item['parse_status'] in {'accepted', 'not_table'}:
                            stats['tables_failed'] += 1
                    failures['size_exceeded'] += 1
                same_number = [size_fallback(item) for item in same_number]
                unit_bytes = len(encode(same_number))
                if unit_bytes > limit:
                    raise ValueError('ANNEX_LINK_METADATA_EXCEEDS_PART_LIMIT')
            combined_bytes = current_bytes + unit_bytes - 1 if current else unit_bytes
            if current and combined_bytes > limit:
                parts.append(current); current = []
                combined_bytes = unit_bytes
            current.extend(same_number)
            current_bytes = combined_bytes
        parts.append(current)
        rows = []
        for n, part in enumerate(parts):
            name = f'{identifiers[group]}' + (f'/{n}' if len(parts) > 1 else '') + '.json'
            path = folder / name
            path.parent.mkdir(parents=True, exist_ok=True)
            raw = encode(part)
            path.write_bytes(raw)
            files[name] = {'bytes': len(raw), 'sha256': digest(raw)}
            rows.extend(index_row(item, name) for item in part)
        index[group] = rows
    return {'schema': 1, 'groups': index, 'files': files}, {
        **stats, 'groups': len(index), 'files': len(files),
        'asset_bytes': sum(f['bytes'] for f in files.values()),
        'max_file_bytes': max((f['bytes'] for f in files.values()), default=0),
        'parse_failures': dict(failures.most_common()),
        'oversized_groups': oversized, 'size_fallback_entries': sum(x['entries'] for x in oversized)}


def verify_assets(assets, index, groups, documents=None):
    folder = assets / 'annex'
    if index is None:
        if folder.exists():
            raise ValueError('ANNEX_INDEX_MISSING')
        return {}  # Legacy Releases remain usable before the first annex build.
    if index.get('schema') != 1 or set(index['groups']) - set(groups):
        raise ValueError('ANNEX_INDEX_INVALID')
    expected, seen, number_files = defaultdict(list), set(), {}
    if not folder.is_dir() or folder.is_symlink() or any(p.is_symlink() for p in folder.rglob('*')):
        raise ValueError('ANNEX_FILE_SIZE_OR_PATH_INVALID')
    for group, rows in index['groups'].items():
        for row in rows:
            if not isinstance(row, list) or len(row) != 4:
                raise ValueError('ANNEX_INDEX_INVALID')
            name = row[2]
            if not re.fullmatch(re.escape(groups[group]) + r'(?:/\d+)?\.json', name):
                raise ValueError('ANNEX_PATH_INVALID')
            if documents is not None and documents.get(row[3], {}).get('group') != group:
                raise ValueError('ANNEX_LAW_GROUP_MISMATCH')
            expected[name].append(row)
            key = (group, row[0].split(' ', 1)[-1])
            if number_files.setdefault(key, name) != name:
                raise ValueError('ANNEX_NUMBER_SPLIT_ACROSS_FILES')
    if set(expected) != set(index['files']):
        raise ValueError('ANNEX_FILE_INDEX_MISMATCH')
    for name, info in index['files'].items():
        path = folder / name
        if (not path.is_file() or any(p.is_symlink() for p in [folder, path, path.parent]) or
                not 0 < path.stat().st_size <= LIMIT):
            raise ValueError('ANNEX_FILE_SIZE_OR_PATH_INVALID')
        raw = path.read_bytes()
        if len(raw) != info['bytes'] or digest(raw) != info['sha256']:
            raise ValueError('ANNEX_FILE_HASH_MISMATCH')
        values = strict_json(raw)
        guard(values)
        if not values or [index_row(v, name) for v in values] != expected[name]:
            raise ValueError('ANNEX_ENTRY_INDEX_MISMATCH')
        for item in values:
            if documents is not None:
                law = documents.get(item['law_id'])
                if law is None or (item['law'], item['kind'], item['effective']) != (law['t'], law['k'], law['eff'].replace('-', '')):
                    raise ValueError('ANNEX_LAW_METADATA_MISMATCH')
            identity = (item['law_id'], item['type'], item['number'], item['subnumber'], item.get('source_position'))
            if identity in seen:
                raise ValueError('ANNEX_DUPLICATE_IDENTITY')
            seen.add(identity)
            if not item['type'] or (item['type'] not in KINDS and 'raw' in item):
                raise ValueError('ANNEX_UNKNOWN_KIND')
            for key in ('pdf', 'file'):
                if official_link(item[key]) != item[key]:
                    raise ValueError('ANNEX_NONABSOLUTE_LINK')
            if (not item['pdf'] and item.get('pdf_missing_reason') != 'source_pdf_link_absent' or
                    not (item['pdf'] or item['file']) and
                    item.get('link_missing_reason') != 'source_official_links_absent'):
                raise ValueError('ANNEX_LINK_REASON_MISSING')
            if 'source_position' in item and (type(item['source_position']) is not int or item['source_position'] < 0):
                raise ValueError('ANNEX_SOURCE_POSITION_INVALID')
            if item.get('fallback') == 'size_exceeded':
                if item != size_fallback(item):
                    raise ValueError('ANNEX_SIZE_FALLBACK_INVALID')
                continue
            if 'fallback' in item or 'source_url' in item:
                raise ValueError('ANNEX_SIZE_FALLBACK_INVALID')
            errors = item.get('extraction_errors')
            if errors is not None:
                if (not isinstance(errors, list) or not errors or
                        any(e not in {'ANNEX_UNKNOWN_KIND', 'ANNEX_NONOFFICIAL_LINK',
                                      'ANNEX_UNSUPPORTED_TEXT_SHAPE', 'ANNEX_INVALID_UNIT'} for e in errors) or
                        item.get('parse_status') != 'extraction_failed' or 'table' in item):
                    raise ValueError('ANNEX_EXTRACTION_FALLBACK_INVALID')
            if item['type'] not in KINDS:  # forms and other link-only labels
                if set(item) - {'law_id', 'law', 'kind', 'effective', 'number', 'subnumber',
                                'type', 'title', 'pdf', 'file', 'pdf_missing_reason', 'link_missing_reason',
                                *({'extraction_errors', 'parse_status', 'source_position'} if errors else set())}:
                    raise ValueError('ANNEX_FORM_BODY_FORBIDDEN')
            else:
                if digest(item['raw'].encode('utf-8')) != item['raw_sha256']:
                    raise ValueError('ANNEX_RAW_HASH_MISMATCH')
                if errors:
                    continue
                table, reason = parse_table(item['raw'])
                if item.get('table') != table or item['parse_status'] != reason:
                    raise ValueError('ANNEX_TABLE_VERIFICATION_FAILED')
    actual = {p.relative_to(folder).as_posix() for p in folder.rglob('*') if p.is_file()}
    if actual != set(expected):
        raise ValueError('ANNEX_UNLISTED_FILE')
    return {'annex_files': len(actual), 'annex_entries': len(seen)}
