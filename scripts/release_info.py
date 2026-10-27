#!/usr/bin/env python3
"""Write release-info.json: format version, origin and per-file SHA-256 for one published run.

Consumers must refuse an unknown format_version and verify every listed file hash.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path

FORMAT_VERSION = 1


def sha256(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def build(release_dir, manifest_path, kind):
    release_dir = Path(release_dir)
    manifest = json.loads(Path(manifest_path).read_text(encoding='utf-8'))
    files = {p.name: {'sha256': sha256(p), 'bytes': p.stat().st_size}
             for p in sorted(release_dir.iterdir())
             if p.is_file() and p.name not in {'release-info.json', 'SHA256SUMS'}}
    return {
        'format_version': FORMAT_VERSION,
        'kind': kind,
        'source': 'law.go.kr DRF (법제처 국가법령정보 공동활용)',
        'collected_date': manifest.get('date'),
        'collection_status': manifest.get('status'),
        'status_counts': manifest.get('status_counts'),
        'repository': os.environ.get('GITHUB_REPOSITORY', ''),
        'commit': os.environ.get('GITHUB_SHA', ''),
        'run_id': os.environ.get('GITHUB_RUN_ID', ''),
        'files': files,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--release', type=Path, default=Path('release'))
    ap.add_argument('--manifest', type=Path, required=True)
    ap.add_argument('--kind', required=True)
    args = ap.parse_args()
    info = build(args.release, args.manifest, args.kind)
    (args.release / 'release-info.json').write_text(json.dumps(info, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({k: info[k] for k in ('format_version', 'kind', 'collected_date', 'collection_status')}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
