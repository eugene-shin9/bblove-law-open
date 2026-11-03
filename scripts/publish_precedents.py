#!/usr/bin/env python3
"""Shared immutable publication sequence, invoked only after scan and attestation.

No call occurs on import. The workflow supplies the already scanned/attested directory.
"""
import argparse
import json
from pathlib import Path
import re
import subprocess


def publish(directory, tag, title, commit, execute=subprocess.run):
    directory = Path(directory)
    info = json.loads((directory / 'release-info.json').read_text())
    kind = info.get('kind')
    pattern = {'committee-summary-raw': r'decc-[0-9]{8}', 'precedent-summary-raw': r'prec-[0-9]{8}(?:-partial-(?:prec|ntsCgmExpc|ttSpecialDecc)(?:-(?:prec|ntsCgmExpc|ttSpecialDecc))*)?',
               'precedent-fulltext-raw': r'prec-fulltext-[0-9]{8}(?:-partial)?'}.get(kind)
    if not pattern or not re.fullmatch(pattern, tag) or info.get('format_version') != 1:
        raise ValueError('RELEASE_CONTRACT_INVALID')
    files = sorted(str(p) for p in directory.iterdir() if p.is_file())
    if not (directory / 'SHA256SUMS').is_file():
        raise ValueError('RELEASE_SUMS_MISSING')
    notes = 'law.go.kr DRF; ' + kind + '; verify release-info.json, SHA256SUMS and build provenance. Third-party materials: links only.'
    for args in [
        ['gh', 'release', 'create', tag, '--draft', '--target', commit, '--latest=false', '--title', title, '--notes', notes],
        ['gh', 'release', 'upload', tag, *files],
        ['gh', 'release', 'edit', tag, '--draft=false', '--latest=false'],
    ]:
        execute(args, check=True, capture_output=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--directory', type=Path, required=True)
    ap.add_argument('--tag', required=True)
    ap.add_argument('--title', required=True)
    ap.add_argument('--commit', required=True)
    a = ap.parse_args()
    publish(a.directory, a.tag, a.title, a.commit)


if __name__ == '__main__':
    main()
