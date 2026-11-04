#!/usr/bin/env python3
"""Offline packaging shared by summary and opt-in fulltext releases.

Attest release/* using the existing pinned attest-build-provenance step after
secret_scan, then use the existing immutable draft/upload/finalize sequence.
This command does not publish or attest anything by itself.
"""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from release_info import build, sha256

PART_BYTES = 1900 * 1024 * 1024
KINDS = {'precedent-summary-raw': ('precedent-collection', 'manifest.json', 'prec-'),
         'precedent-fulltext-raw': ('fulltext-delta', 'fulltext-delta-manifest.json', 'prec-fulltext-')}


def release_tag(kind, day, partial=False, run_id=None, run_attempt=None):
    import re
    if kind not in KINDS or not re.fullmatch(r'[0-9]{8}', day):
        raise ValueError('RELEASE_ARGUMENT_INVALID')
    if kind == 'precedent-fulltext-raw':
        if partial or not re.fullmatch(r'[0-9]+',str(run_id)) or not re.fullmatch(r'[0-9]+',str(run_attempt)):
            raise ValueError('RELEASE_ARGUMENT_INVALID')
        return f'prec-fulltext-{day}-r{run_id}-a{run_attempt}'
    return KINDS[kind][2] + day + ('-partial' if partial else '')


def package(source, dest, kind, part_bytes=PART_BYTES):
    if kind not in KINDS or not 0 < part_bytes <= PART_BYTES:
        raise ValueError('RELEASE_ARGUMENT_INVALID')
    source, dest = Path(source), Path(dest)
    folder, manifest_name, _ = KINDS[kind]
    if source.name != folder or dest.exists():
        raise ValueError('RELEASE_DIRECTORY_INVALID')
    manifest = source / manifest_name
    data = json.loads(manifest.read_text())
    if kind == 'precedent-fulltext-raw' and (data.get('kind') != kind or data.get('format_version') != 2 or data.get('delta') is not True):
        raise ValueError('RELEASE_KIND_INVALID')
    if kind == 'precedent-fulltext-raw':
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
        from bblove_fulltext_delta import verify_delta, validate_manifest
        verify_delta(source)
        snapshot = validate_manifest(json.loads((source/'fulltext-manifest.json').read_text()))
        if snapshot['sequence'] != data['sequence'] or not snapshot.get('previous_tag'):
            raise ValueError('RELEASE_SNAPSHOT_INVALID')
        for sid, meta in data['documents'].items():
            current = snapshot['documents'].get(sid, {})
            if current.get('body_sha256') != meta['body_sha256'] or current.get('status') != 'PUBLISHED' or current.get('release') != snapshot['previous_tag']:
                raise ValueError('RELEASE_SNAPSHOT_INVALID')
        for item in data['withdrawn']:
            if snapshot['documents'].get(item['id'], {}).get('status') != 'EXCLUDED':
                raise ValueError('RELEASE_SNAPSHOT_INVALID')
    if any(p.is_symlink() for p in source.rglob('*')):
        raise ValueError('RELEASE_SYMLINK')
    dest.mkdir(parents=True)
    with tempfile.TemporaryFile() as stream:
        subprocess.run(['tar', '-C', str(source.parent), '-czf', '-', folder], stdout=stream, check=True)
        stream.seek(0)
        index = 0
        while True:
            chunk = stream.read(min(part_bytes, 1024 * 1024))
            if not chunk:
                break
            with (dest / f'{folder}.tar.gz.part-{index:02d}').open('wb') as output:
                output.write(chunk)
                left = part_bytes - len(chunk)
                while left:
                    chunk = stream.read(min(left, 1024 * 1024))
                    if not chunk:
                        break
                    output.write(chunk)
                    left -= len(chunk)
            index += 1
    shutil.copyfile(manifest, dest / ('precedent-manifest.json' if kind == 'precedent-summary-raw' else manifest_name))
    if kind == 'precedent-summary-raw':
        from collect_precedents import summary
        (dest / 'precedent-summary.json').write_text(json.dumps(summary(source), ensure_ascii=False, indent=2))
    if kind == 'precedent-fulltext-raw':
        shutil.copyfile(source/'fulltext-manifest.json', dest/'fulltext-manifest.json')
    info = build(dest, manifest, kind)
    if kind == 'precedent-fulltext-raw':
        info.update(format_version=2, delta=True, sequence=data['sequence'], previous_tag=data['previous_tag'])
    (dest / 'release-info.json').write_text(json.dumps(info, ensure_ascii=False, indent=2)+'\n')
    (dest / 'SHA256SUMS').write_text(''.join(f'{sha256(p)}  {p.name}\n' for p in sorted(dest.iterdir()) if p.is_file()))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--source', type=Path, required=True)
    ap.add_argument('--dest', type=Path, required=True)
    ap.add_argument('--kind', choices=KINDS, required=True)
    args = ap.parse_args()
    package(args.source, args.dest, args.kind)


if __name__ == '__main__':
    main()
