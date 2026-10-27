#!/usr/bin/env python3
"""Fail if the DRF credential (LAW_OC) appears in any file under the given paths.

The credential is read from the environment only. Its value, variants and match locations
are never printed; only a count of affected files is reported. Run before every cache save,
artifact upload and Release upload: GitHub masks secrets in logs only, not in published files.
"""
import os
from pathlib import Path
import sys
from urllib.parse import quote, quote_plus

CHUNK = 1 << 20


def variants(secret):
    forms = {secret, quote(secret, safe=''), quote_plus(secret, safe='')}
    return sorted({f.encode('utf-8') for f in forms if f}, key=len, reverse=True)


def files(paths):
    for raw in paths:
        p = Path(raw)
        if p.is_file():
            yield p
        elif p.is_dir():
            yield from (q for q in p.rglob('*') if q.is_file() and not q.is_symlink())


def contains(path, needles):
    """Case-insensitive streaming search with overlap so matches across chunks are found."""
    lowered = [n.lower() for n in needles]
    keep = max(len(n) for n in lowered) - 1
    tail = b''
    with path.open('rb') as stream:
        while chunk := stream.read(CHUNK):
            window = (tail + chunk).lower()
            if any(n in window for n in lowered):
                return True
            tail = window[-keep:] if keep else b''
    return False


def scan(paths, secret):
    if len(secret) < 4:
        raise ValueError('credential too short to scan safely')
    needles = variants(secret)
    return sum(1 for p in files(paths) if contains(p, needles))


def main(argv):
    secret = os.environ.get('LAW_OC', '')
    if not secret:
        print(('::error title=secret scan::' if os.environ.get('GITHUB_ACTIONS') == 'true' else '') + 'SECRET_SCAN_NO_CREDENTIAL')
        return 1
    if not argv:
        print('SECRET_SCAN_NO_PATHS')
        return 1
    hits = scan(argv, secret)
    prefix = ('::error title=secret scan::' if hits else '::notice title=secret scan::') if os.environ.get('GITHUB_ACTIONS') == 'true' else ''
    print(f'{prefix}SECRET_SCAN files_with_credential={hits}')
    return 1 if hits else 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
