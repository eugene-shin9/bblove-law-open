#!/usr/bin/env python3
"""Public-repository guard: only collector code and public law data tooling may be committed.

1. Path allowlist: every tracked file must live under an allowed top-level entry.
2. Personal-data patterns in tracked text files (resident registration numbers, phone numbers,
   e-mail addresses). Match text is never printed, only file paths and pattern names.
"""
import re
import subprocess
import sys
from pathlib import Path

ALLOWED = {'src', 'scripts', 'tests', '.github', 'README.md', 'AGENTS.md', '.gitignore', 'LICENSE'}
PATTERNS = {
    'resident_number': re.compile(rb'(?<!\d)\d{6}\s?-\s?[1-8]\d{6}(?!\d)'),
    'mobile_phone': re.compile(rb'(?<!\d)01[016789][- .]?\d{3,4}[- .]?\d{4}(?!\d)'),
    'email': re.compile(rb'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}'),
}
# Synthetic addresses used by tests/docs are allowed; real personal mailboxes are not.
EMAIL_OK = re.compile(rb'@(?:example\.(?:com|org|net|invalid)|users\.noreply\.github\.com)$', re.I)


def tracked(root):
    out = subprocess.run(['git', '-C', str(root), 'ls-files', '-z'], check=True, capture_output=True).stdout
    return [p for p in out.decode('utf-8').split('\0') if p]


def check(root, paths):
    problems = []
    for rel in paths:
        top = rel.split('/', 1)[0]
        if top not in ALLOWED:
            problems.append((rel, 'path_not_allowed'))
            continue
        data = (Path(root) / rel).read_bytes()
        if b'\0' in data[:4096]:
            continue
        for name, pattern in PATTERNS.items():
            hits = pattern.findall(data)
            if name == 'email':
                hits = [h for h in hits if not EMAIL_OK.search(h)]
            if hits:
                problems.append((rel, name))
    return problems


def main(argv):
    root = Path(argv[0]) if argv else Path('.')
    problems = check(root, tracked(root))
    for rel, kind in problems:
        print(f'REPO_GUARD {kind}: {rel}')
    print(f'REPO_GUARD problems={len(problems)}')
    return 1 if problems else 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
