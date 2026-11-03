#!/usr/bin/env python3
"""Prepare/finalize deltas and recover attested metadata; never dispatch or publish."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
sys.path[:0] = [str(Path(__file__).resolve().parents[1]/'src'), str(Path(__file__).resolve().parent)]
from bblove_fulltext_delta import KIND, TAG_PATTERN, validate_manifest, verify_delta, committed_snapshot
from bblove_law_all import save
from bblove_storage import strict_json, digest
from package_precedents import package, release_tag

REPO='eugene-shin9/bblove-law-open'


def output(**values):
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'],'a') as stream:
            for key,value in values.items(): stream.write(f'{key}={value}\n')


def prepare(source, cache, dest, run_id, attempt):
    delta=verify_delta(source)
    manifest=validate_manifest(strict_json(Path(cache).read_bytes()))
    tag=release_tag(KIND,delta['date'],run_id=run_id,run_attempt=attempt)
    save(Path(source)/'fulltext-manifest.json', committed_snapshot(manifest,delta,tag))
    package(source,dest,KIND)
    output(tag=tag)
    return tag


def finalize(cache, source, published, count, collection, tag):
    state='FAILED'
    if collection in ('COMPLETE','PAUSED','INCOMPLETE'):
        if published=='success':
            delta=verify_delta(source)
            if count != len(delta['documents'])+len(delta['withdrawn']) or count<=0:
                raise ValueError('FULLTEXT_FINALIZE_COUNT')
            manifest=validate_manifest(strict_json(Path(cache).read_bytes()))
            save(Path(cache),committed_snapshot(manifest,delta,tag));state='PUBLISHED'
        elif count==0:
            delta=verify_delta(source)
            if delta['documents'] or delta['withdrawn']:
                raise ValueError('FULLTEXT_FINALIZE_COUNT')
            validate_manifest(strict_json(Path(cache).read_bytes()))
            state='NO_DELTA'
    output(state=state)
    return state


def gh(args):
    p=subprocess.run(['gh',*args],capture_output=True,text=True,timeout=600)
    if p.returncode: raise ValueError('FULLTEXT_RECOVERY_REQUEST_FAILED')
    return p.stdout


def recover(cache, execute=gh):
    cache=Path(cache)
    if cache.exists():
        m=strict_json(cache.read_bytes())
        if m.get('format_version')==1:
            cache.unlink()  # v1 has never been operational; intentionally start afresh.
            return 'EMPTY'
        validate_manifest(m)
        return 'CACHE'
    pages=json.loads(execute(['api','--paginate','--slurp',f'repos/{REPO}/releases?per_page=100']))
    rows=[r for page in pages for r in page]
    rows=[r for r in rows if not r.get('draft') and not r.get('prerelease') and re.fullmatch(TAG_PATTERN,r.get('tag_name',''))]
    if not rows: return 'EMPTY'
    tag=max(rows,key=lambda r:r.get('published_at') or r.get('created_at',''))['tag_name']
    with tempfile.TemporaryDirectory() as folder:
        stage=Path(folder)
        for name in ('fulltext-manifest.json','release-info.json'):
            execute(['release','download',tag,'-R',REPO,'--pattern',name,'--dir',folder])
            execute(['attestation','verify',str(stage/name),'-R',REPO,'--signer-workflow',REPO+'/.github/workflows/collect-precedents.yml'])
        info=strict_json((stage/'release-info.json').read_bytes())
        raw=(stage/'fulltext-manifest.json').read_bytes()
        if info.get('kind')!=KIND or info.get('format_version')!=2 or info.get('delta') is not True or info.get('files',{}).get('fulltext-manifest.json',{}).get('sha256')!=digest(raw):
            raise ValueError('FULLTEXT_RECOVERY_HASH')
        manifest=validate_manifest(strict_json(raw))
        if manifest['previous_tag']!=tag: raise ValueError('FULLTEXT_RECOVERY_TAG')
        save(cache,manifest)
    return 'RELEASE'


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('mode',choices=('recover','prepare','finalize'))
    parser.add_argument('--cache',type=Path,default=Path('auto/fulltext-collection/fulltext-manifest.json'))
    parser.add_argument('--source',type=Path,default=Path('auto/fulltext-delta/fulltext-delta'))
    parser.add_argument('--dest',type=Path,default=Path('release-fulltext'))
    a=parser.parse_args()
    try:
        if a.mode=='recover': recover(a.cache)
        elif a.mode=='prepare': prepare(a.source,a.cache,a.dest,os.environ['GITHUB_RUN_ID'],os.environ['GITHUB_RUN_ATTEMPT'])
        else:
            if os.environ.get('SCAN_OUTCOME') != 'success':
                output(state='FAILED'); return 0
            finalize(a.cache,a.source,os.environ.get('PUBLISH_OUTCOME',''),int(os.environ.get('FULLTEXT_DELTA') or '0'),os.environ.get('FULLTEXT_OUTCOME',''),os.environ.get('FULLTEXT_TAG',''))
    except Exception:
        if a.mode=='finalize': output(state='FAILED')
        print('::error::FULLTEXT_RELEASE_OPERATION_FAILED')
        return 1
    return 0


if __name__=='__main__': raise SystemExit(main())
