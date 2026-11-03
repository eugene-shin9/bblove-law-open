#!/usr/bin/env python3
"""Build a gated committee release, compare the last published manifest, or verify locally."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
sys.path.insert(0,str(Path(__file__).resolve().parent))
from bblove_committee_release import export, verify, KIND
from release_info import build, sha256


def prepare(source, dest):
    export(source,dest)
    info=build(dest,Path(dest)/'committee-manifest.json',KIND)
    info['collection_status']='COMPLETE'
    (Path(dest)/'release-info.json').write_text(json.dumps(info,ensure_ascii=False,indent=2)+'\n')
    (Path(dest)/'SHA256SUMS').write_text(''.join(f'{sha256(p)}  {p.name}\n' for p in sorted(Path(dest).iterdir()) if p.is_file()))


def gh(args):
    result=subprocess.run(['gh',*args],check=True,capture_output=True,text=True,timeout=180)
    return result.stdout


def last_manifest(execute=gh):
    pages=json.loads(execute(['api','--paginate','--slurp','repos/{owner}/{repo}/releases?per_page=100']))
    releases=[r for page in pages for r in page if not r['draft'] and not r['prerelease'] and __import__('re').fullmatch(r'decc-\d{8}',r['tag_name'])]
    if not releases:
        return None
    latest=max(releases,key=lambda r:r['published_at'])
    with tempfile.TemporaryDirectory() as t:
        execute(['release','download',latest['tag_name'],'--pattern','release-info.json','--dir',t])
        info=json.loads((Path(t)/'release-info.json').read_text())
        if info.get('kind') != KIND or info.get('format_version') != 1:
            raise ValueError('COMMITTEE_RELEASE_KIND')
        return info['files']['committee-manifest.json']['sha256']


def needs_publication(dest, previous):
    verify(dest)
    return sha256(Path(dest)/'committee-manifest.json') != previous


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--source',type=Path)
    p.add_argument('--dest',type=Path,default=Path('release-decc'))
    p.add_argument('--verify',action='store_true')
    p.add_argument('--decide',action='store_true')
    a=p.parse_args()
    try:
        if a.verify: verify(a.dest)
        elif a.decide:
            needed=needs_publication(a.dest,last_manifest())
            with open(os.environ['GITHUB_OUTPUT'],'a') as f: f.write(f'needed={str(needed).lower()}\n')
        else: prepare(a.source,a.dest)
    except Exception:
        print('::error::COMMITTEE_RELEASE_FAILED')
        return 1
    return 0


if __name__=='__main__':
    sys.exit(main())
