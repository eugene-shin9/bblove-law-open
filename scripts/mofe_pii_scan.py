#!/usr/bin/env python3
"""Fail if collected MOFE reply text matches a personal-data pattern.

The replies are official, anonymised publications; a hit means the source published something it
should not have, and we must not republish it. Only file names and pattern names are printed.
"""
import json
from pathlib import Path
import re
import sys

PATTERNS = {
    'resident_number': re.compile(r'(?<!\d)\d{6}\s?-\s?[1-8]\d{6}(?!\d)'),
    'mobile_phone': re.compile(r'(?<!\d)01[016789][- .]?\d{3,4}[- .]?\d{4}(?!\d)'),
    'email': re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}'),
    'account_context': re.compile(r'(?:계좌|account)\s*(?:번호)?\s*[:：]?\s*\d{10,16}(?!\d)', re.I),
}
TEXT_KEYS = ('title', '질문요지', '사실관계', '회신', '관계법령')


def scan(root):
    problems = []
    for path in sorted(Path(root).rglob('*.json')):
        if path.name == 'manifest.json':
            continue
        try:
            doc = json.loads(path.read_text(encoding='utf-8'))
        except (ValueError, UnicodeDecodeError):
            problems.append((path.name, 'unreadable_json'))
            continue
        texts = [doc.get('title', '')] + [doc.get('sec', {}).get(k, '') for k in TEXT_KEYS[1:]]
        for name, pattern in PATTERNS.items():
            if any(pattern.search(t or '') for t in texts):
                problems.append((path.name, name))
    return problems


def main(argv):
    problems = scan(argv[0] if argv else 'auto/mofe')
    for name, kind in problems:
        print(f'MOFE_PII {kind}: {name}')
    print(f'MOFE_PII problems={len(problems)}')
    return 1 if problems else 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
