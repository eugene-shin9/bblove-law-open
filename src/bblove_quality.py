#!/usr/bin/env python3
"""Offline structural/integrity checks. No network, credentials or data writes."""
import argparse
from collections import Counter
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

# Resolve only this trusted sibling directory, including under python -I.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import bblove_v107_pilot as pilot
from bblove_storage import read_document, strict_json

OFFICIAL_NAMES = {'민법', '형법', '근로기준법', '민사소송법', '소액사건심판법'}
CASE_NUMBERS = {'2012도14516', '2015두41401', '2017다226605'}
PROTECTED = ('.github/workflows/bblove-law-sync.yml',
             '.github/workflows/bblove-precedent-sync.yml',
             '.github/workflows/bblove-v107-pilot.yml', 'BBLOVE_AUDIT_PROTOCOL.md')

# This task authorizes only these additive, read-only pilot diagnostics changes.
# All original collection commands, permissions, secret bindings and uploads stay frozen.
PILOT_DIAGNOSTIC_STEP = '''      - name: Diagnose collection outcome without API or secrets
        if: always()
        env:
          BBLOVE_COLLECTION_OUTCOME: ${{ steps.collection.outcome }}
        run: python src/bblove_diagnostics.py --output v107_pilot_artifact --requested 150 --outcome "$BBLOVE_COLLECTION_OUTCOME"
'''


def credential_markers(raw):
    """Detect credential-shaped values without reading any environment secret."""
    text = raw.decode('utf-8', errors='replace')
    for match in re.finditer(r'(?i)(?:[?&]|%3[fF]|%26)OC(?:=|%3[dD])([^&\s"<>]+)', text):
        if match.group(1) not in {'REDACTED', 'test'}:
            return True
    return bool(re.search(r'(?i)"(?:LAW_OC|access_token|api_key|password)"\s*:\s*"(?!REDACTED")[^"\s]+"', text))


def official_url(value, kind, identifier=None):
    if not isinstance(value, str):
        return False
    try:
        u = urlsplit(value)
        key, path = ('lsiSeq', '/LSW/lsInfoP.do') if kind == 'law' else ('precSeq', '/LSW/precInfoP.do')
        query = parse_qs(u.query)
        seq = query.get(key, [])
        return (u.scheme == 'https' and u.netloc in {'www.law.go.kr', 'law.go.kr'}
                and u.path == path and not u.fragment and len(seq) == 1
                and bool(re.fullmatch(r'[0-9]+', seq[0]))
                and (identifier is None or seq[0] == str(identifier)))
    except ValueError:
        return False


def natural(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


class Quality:
    def __init__(self, as_of=None):
        self.as_of = as_of or dt.datetime.now(ZoneInfo('Asia/Seoul')).strftime('%Y%m%d')
        pilot.check_date(self.as_of, 'as_of')
        self.findings = []
        self.counts = {}

    def issue(self, code, subject, status='FAIL'):
        # Never include raw payloads, URLs, exception messages or credential values.
        self.findings.append({'status': status, 'code': code, 'subject': subject})

    def require(self, condition, code, subject):
        if not condition:
            self.issue(code, subject)
        return bool(condition)

    def read(self, path, subject):
        try:
            raw = path.read_bytes()
            if credential_markers(raw):
                self.issue('credential_marker', subject)
            value = strict_json(raw)
            if not isinstance(value, dict):
                raise ValueError('object expected')
            return value, raw
        except (OSError, ValueError, UnicodeError):
            self.issue('missing_or_invalid_json', subject)
            return {}, b''

    def date(self, value, subject):
        try:
            pilot.check_date(value, 'effective', self.as_of)
            return True
        except (ValueError, TypeError):
            self.issue('invalid_or_future_date', subject)
            return False

    def checked_date(self, manifest, subject):
        try:
            value = dt.datetime.fromisoformat(manifest['checked_at_kst'])
            if value.utcoffset() != dt.timedelta(hours=9):
                raise ValueError('KST offset required')
            date = value.strftime('%Y%m%d')
            if not self.date(date, subject):
                return self.as_of
            return date
        except (KeyError, ValueError, TypeError, AttributeError):
            self.issue('invalid_collection_timestamp', subject)
            return self.as_of

    def resolve(self, root, name, subject):
        if not isinstance(name, str) or not name or Path(name).is_absolute():
            self.issue('unsafe_or_missing_path', subject)
            return None
        path = root / name
        if '..' in Path(name).parts or not path.resolve().is_relative_to(root.resolve()):
            self.issue('unsafe_or_missing_path', subject)
            return None
        return path

    def rows(self, manifest, key, subject):
        rows = manifest.get(key)
        if not isinstance(rows, list) or any(not isinstance(x, dict) for x in rows):
            self.issue('invalid_manifest_rows', subject)
            return []
        return rows

    def hash(self, raw, digest, subject):
        self.require(bool(raw) and isinstance(digest, str)
                     and hashlib.sha256(raw).hexdigest() == digest, 'sha256_mismatch', subject)

    def law(self, doc, row, checked, subject, legacy=False):
        item = {'법령ID': row.get('law_id' if legacy else 'id'),
                '법령명한글': row.get('name' if legacy else 'title'),
                '시행일자': row.get('effective')}
        self.require(bool(re.fullmatch(r'[0-9]+', str(item['법령ID'] or ''))), 'invalid_law_id', subject)
        self.require(isinstance(item['법령명한글'], str) and bool(item['법령명한글'].strip()),
                     'invalid_or_missing_law_title', subject)
        try:
            articles = pilot.validate_detail(doc, item, checked)
        except (ValueError, TypeError, AttributeError):
            self.issue('law_identity_date_or_articles_invalid', subject)
            articles = None
        basic = doc.get('법령', {}).get('기본정보', {}) if isinstance(doc.get('법령'), dict) else {}
        kind = basic.get('법종구분', '') if isinstance(basic, dict) else ''
        if isinstance(kind, dict):
            kind = pilot.text(kind, 'content')
        self.require(isinstance(kind, str) and bool(kind.strip()), 'missing_law_kind', subject)
        if not legacy:
            self.require(kind == row.get('kind'), 'law_kind_mismatch', subject)
            self.require(natural(row.get('articles')) and articles == row.get('articles'), 'article_count_mismatch', subject)
        else:
            # V1.05 counts nonempty stored units, including chapter headings.
            law = doc.get('법령', {})
            section = law.get('조문', {}) if isinstance(law, dict) else {}
            units = pilot.records(section.get('조문단위')) if isinstance(section, dict) else []
            stored_count = sum(bool(pilot.text(x, '조문내용')) for x in units)
            self.require(natural(row.get('articles')) and row['articles'] == stored_count,
                         'legacy_stored_unit_count_mismatch', subject)
        return pilot.category({'법령구분명': kind})

    def legacy(self, root):
        official = root / 'auto/official'
        manifest, _ = self.read(official / 'manifest.json', 'official_manifest')
        checked = self.checked_date(manifest, 'official_manifest')
        rows = self.rows(manifest, 'laws', 'official_manifest')
        names = [r.get('name') for r in rows]
        self.require(len(rows) == 5 and set(str(x) for x in names) == OFFICIAL_NAMES,
                     'official_expected_set_or_duplicate', 'official_manifest')
        referenced = set()
        categories = Counter()
        ids = []
        for i, row in enumerate(rows):
            subject = f'official[{i}]'
            path = self.resolve(official, row.get('json'), subject)
            if path is None:
                continue
            self.require(path.name == str(row.get('name')) + '.json', 'law_filename_mismatch', subject)
            referenced.add(path.resolve())
            doc, raw = self.read(path, subject)
            self.hash(raw, row.get('sha256_json'), subject)
            self.require(official_url(row.get('official_url'), 'law', row.get('mst')), 'invalid_official_source', subject)
            ids.append(str(row.get('law_id')))
            categories[self.law(doc, row, checked, subject, legacy=True)] += 1
            md = self.resolve(official, row.get('markdown'), subject)
            if md is None or not md.is_file():
                self.issue('missing_markdown', subject)
            elif credential_markers(md.read_bytes()):
                self.issue('credential_marker', subject)
        self.require(len(ids) == len(set(ids)), 'duplicate_law_id', 'official_manifest')
        self.require({p.resolve() for p in official.glob('*.json') if p.name != 'manifest.json'} == referenced,
                     'unlisted_or_missing_law_file', 'official_manifest')
        self.counts['official_by_category'] = dict(categories)
        self.counts['official_laws'] = len(rows)
        self.precedents(root)

    def precedents(self, root):
        folder = root / 'auto/precedents'
        manifest, _ = self.read(folder / 'manifest.json', 'precedent_manifest')
        checked = self.checked_date(manifest, 'precedent_manifest')
        rows = self.rows(manifest, 'cases', 'precedent_manifest')
        self.require(len(rows) == 3 and {str(r.get('case_no')) for r in rows} == CASE_NUMBERS,
                     'precedent_expected_set_or_duplicate', 'precedent_manifest')
        self.require(manifest.get('not_collected') == [], 'precedent_incomplete', 'precedent_manifest')
        referenced, ids = set(), []
        for i, row in enumerate(rows):
            subject = f'precedent[{i}]'
            no = row.get('case_no')
            if not isinstance(no, str) or not re.fullmatch(r'[0-9]{4}[가-힣]+[0-9]+', no):
                self.issue('invalid_case_number', subject)
                continue
            path = self.resolve(folder, no + '.json', subject)
            if path is None:
                continue
            referenced.add(path.resolve())
            doc, raw = self.read(path, subject)
            self.hash(raw, row.get('sha256_json'), subject)
            for key in ('case_no', 'id', 'date', 'official_url'):
                self.require(doc.get(key) == row.get(key), 'precedent_manifest_mismatch', subject)
            self.require(bool(re.fullmatch(r'[0-9]+', str(row.get('id') or ''))), 'invalid_precedent_id', subject)
            ids.append(str(row.get('id')))
            self.require(official_url(doc.get('official_url'), 'case', row.get('id')), 'invalid_official_source', subject)
            self.date(doc.get('date'), subject)
            if isinstance(doc.get('date'), str):
                self.require(doc['date'] <= checked, 'date_after_collection', subject)
            court = doc.get('court')
            self.require(isinstance(court, str) and bool(re.fullmatch(r'(?:대법원|헌법재판소|[가-힣]+(?:법원|지원))', court)), 'invalid_or_missing_court', subject)
            self.require(bool(pilot.text(doc, 'title')) and bool(pilot.text(doc, 'issues') or pilot.text(doc, 'holding')),
                         'missing_precedent_content', subject)
            md = folder / (no + '.md')
            self.require(md.is_file(), 'missing_markdown', subject)
            if md.is_file() and credential_markers(md.read_bytes()):
                self.issue('credential_marker', subject)
        self.require(len(ids) == len(set(ids)), 'duplicate_precedent_id', 'precedent_manifest')
        self.require({p.resolve() for p in folder.glob('*.json') if p.name != 'manifest.json'} == referenced,
                     'unlisted_or_missing_precedent_file', 'precedent_manifest')
        self.counts['precedents'] = len(rows)

    def artifact(self, root):
        manifest, _ = self.read(root / 'manifest.json', 'pilot_manifest')
        checked = self.checked_date(manifest, 'pilot_manifest')
        rows = [] if manifest.get('phase') == 'catalog' and 'laws' not in manifest else self.rows(manifest, 'laws', 'pilot_manifest')
        errors = self.rows(manifest, 'errors', 'pilot_manifest')
        requested, verified, failed = (manifest.get(k) for k in ('requested', 'verified', 'failed'))
        self.require(all(natural(x) for x in (requested, verified, failed))
                     and (100 <= requested <= 200 or (requested == 1
                          and isinstance(manifest.get('selection'), dict)
                          and bool(re.fullmatch(r'[0-9]+', str(manifest['selection'].get('law_id') or '')))))
                     and verified == len(rows)
                     and verified + failed == requested,
                     'pilot_counter_mismatch', 'pilot_manifest')
        selection = manifest.get('selection')
        direct = isinstance(selection, dict) and selection.get('method') == pilot.SINGLE_LOOKUP_METHOD
        if isinstance(selection, dict) and 'method' in selection:
            self.require(direct and requested == 1, 'unsupported_single_lookup_method', 'pilot_manifest')
        if manifest.get('phase') in ('catalog', 'single_lookup'):
            self.require(verified == 0 and failed == requested and bool(errors) and not rows,
                         'missing_failure_record', 'pilot_manifest')
        else:
            self.require(failed == len(errors), 'missing_failure_record', 'pilot_manifest')
        self.require(failed == 0 and verified == requested, 'collection_incomplete', 'pilot_manifest')
        for error in errors:
            self.require(bool(pilot.text(error, 'error')), 'empty_failure_reason', 'pilot_manifest')
        if manifest.get('phase') in ('catalog', 'single_lookup'):
            if manifest.get('phase') == 'single_lookup':
                self.require(direct and requested == 1, 'invalid_single_lookup_failure', 'pilot_manifest')
            self.counts['pilot_laws'] = len(rows)
            return
        current_item = None
        if direct:
            proof = selection.get('lookup')
            if not isinstance(proof, dict):
                self.issue('single_lookup_evidence_missing', 'pilot_manifest')
            else:
                path = self.resolve(root, proof.get('path'), 'single_lookup')
                if path is not None:
                    self.require(proof.get('path') == 'single_lookup.json', 'single_lookup_evidence_path', 'single_lookup')
                    lookup, raw = self.read(path, 'single_lookup')
                    self.hash(raw, proof.get('sha256'), 'single_lookup')
                    self.require(natural(proof.get('bytes')) and proof['bytes'] == len(raw)
                                 and len(raw) <= pilot.MAX_DOCUMENT_BYTES, 'size_mismatch_or_gate', 'single_lookup')
                    try:
                        current_item = pilot.validate_single_lookup(lookup, selection['law_id'], checked)
                    except (ValueError, TypeError, KeyError):
                        self.issue('single_lookup_identity_date_or_current_invalid', 'single_lookup')
        ids, paths, categories = [], set(), Counter()
        storage_files = set()
        for i, row in enumerate(rows):
            subject = f'pilot[{i}]'
            path = self.resolve(root, row.get('path'), subject)
            if path is None:
                continue
            ids.append(str(row.get('id')))
            paths.add(path.resolve())
            self.require(path.name == str(row.get('id')) + '.json', 'law_filename_mismatch', subject)
            if 'storage' in row:
                try:
                    if not isinstance(row['storage'], dict):
                        raise ValueError('invalid storage metadata')
                    doc, raw, files = read_document(path, row['storage'])
                    storage_files.update(files)
                    if credential_markers(raw):
                        self.issue('credential_marker', subject)
                except (OSError, ValueError, TypeError, UnicodeError):
                    self.issue('storage_integrity_invalid', subject)
                    doc, raw = {}, b''
                    storage_files.add(path.resolve())
            else:
                doc, raw = self.read(path, subject)
                storage_files.add(path.resolve())
                self.require(len(raw) <= pilot.MAX_DOCUMENT_BYTES, 'size_mismatch_or_gate', subject)
            self.hash(raw, row.get('sha256'), subject)
            self.require(natural(row.get('bytes')) and row['bytes'] == len(raw),
                         'size_mismatch_or_gate', subject)
            self.require(official_url(row.get('source'), 'law'), 'invalid_official_source', subject)
            if direct and current_item is not None:
                self.require((row.get('id'), row.get('title'), row.get('effective'), row.get('kind')) ==
                             (pilot.text(current_item, '법령ID'), pilot.text(current_item, '법령명한글', '법령명_한글'),
                              pilot.text(current_item, '시행일자'), pilot.text(current_item, '법령구분명')),
                             'single_lookup_manifest_mismatch', subject)
                self.require(official_url(row.get('source'), 'law', pilot.text(current_item, '법령일련번호')),
                             'single_lookup_source_mismatch', subject)
            categories[self.law(doc, row, checked, subject)] += 1
        self.require(len(ids) == len(set(ids)) and len(paths) == len(rows), 'duplicate_law_id_or_path', 'pilot_manifest')
        if manifest.get('phase') != 'catalog':
            failed_ids = [str(e.get('id') or '') for e in errors]
            self.require(all(re.fullmatch(r'[0-9]+', value) for value in failed_ids)
                         and len(failed_ids) == len(set(failed_ids))
                         and not set(failed_ids).intersection(ids),
                         'invalid_or_duplicate_failure_id', 'pilot_manifest')
        self.require({p.resolve() for p in (root / 'laws').rglob('*') if p.is_file()} == storage_files,
                     'unlisted_or_missing_law_file', 'pilot_manifest')
        expected = {k: categories[k] for k in pilot.CATEGORY_NAMES}
        distribution = manifest.get('verified_by_category')
        self.require(isinstance(distribution, dict) and all(natural(v) for v in distribution.values())
                     and distribution == expected, 'category_count_mismatch', 'pilot_manifest')
        sample = manifest.get('sample_by_category')
        self.require(isinstance(sample, dict) and set(sample) == set(pilot.CATEGORY_NAMES)
                     and all(natural(v) for v in sample.values()) and sum(sample.values()) == requested,
                     'sample_count_mismatch', 'pilot_manifest')
        if failed == 0:
            self.require(sample == expected, 'category_count_mismatch', 'pilot_manifest')
        if direct:
            self.require(manifest.get('lookup_total') == 1 and manifest.get('official_total') is None
                         and manifest.get('estimated_full_bytes_from_sample') is None,
                         'invalid_single_lookup_total', 'pilot_manifest')
        else:
            self.require(natural(requested) and natural(manifest.get('official_total'))
                         and manifest['official_total'] >= requested,
                         'invalid_official_total', 'pilot_manifest')
        if requested == 1:
            self.require(ids + failed_ids == [manifest.get('selection', {}).get('law_id')],
                         'single_law_selection_mismatch', 'pilot_manifest')
        self.counts['pilot_verified_by_category'] = expected
        self.counts['pilot_laws'] = len(rows)

    def preservation(self, root, base_ref):
        if not re.fullmatch(r'[0-9a-fA-F]{40}', base_ref or ''):
            self.issue('invalid_base_ref', 'repository')
            return
        try:
            changed = subprocess.check_output(['git', 'diff', '--name-only', '-z', base_ref, '--'],
                                              cwd=root, stderr=subprocess.DEVNULL).decode().split('\0')
            protected = [p for p in changed if p in PROTECTED or p.startswith(('auto/official/', 'auto/precedents/'))]
            workflow = '.github/workflows/bblove-v107-pilot.yml'
            if workflow in protected:
                previous = subprocess.check_output(['git', 'show', f'{base_ref}:{workflow}'],
                                                   cwd=root, stderr=subprocess.DEVNULL).decode()
                current = (root / workflow).read_text(encoding='utf-8')
                if (current.count(PILOT_DIAGNOSTIC_STEP) == 1 and current.count('        id: collection\n') == 1
                        and current.replace(PILOT_DIAGNOSTIC_STEP, '', 1).replace('        id: collection\n', '', 1) == previous):
                    protected.remove(workflow)
            self.require(not protected, 'protected_file_changed', 'repository')
        except (OSError, subprocess.CalledProcessError, UnicodeError):
            self.issue('preservation_not_verifiable', 'repository')

    def report(self):
        deterministic = 'FAIL' if any(f['status'] == 'FAIL' for f in self.findings) else 'PASS'
        review = [{'status': 'REVIEW_REQUIRED', 'code': 'official_original_not_compared', 'subject': 'scope'},
                  {'status': 'REVIEW_REQUIRED', 'code': 'event_time_applicability_not_evaluated', 'subject': 'scope'}]
        return {'schema_version': 1, 'deterministic_status': deterministic,
                'status': 'FAIL' if deterministic == 'FAIL' else 'REVIEW_REQUIRED',
                'as_of': self.as_of, 'counts': self.counts, 'findings': self.findings + review,
                'scope': 'offline structure/integrity; no API or independent original comparison'}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--root', type=Path, default=Path('.'))
    ap.add_argument('--pilot-artifact', type=Path)
    ap.add_argument('--base-ref')
    ap.add_argument('--as-of')
    ap.add_argument('--report', type=Path)
    args = ap.parse_args()
    try:
        q = Quality(args.as_of)
        q.legacy(args.root)
        if args.pilot_artifact:
            q.artifact(args.pilot_artifact)
        if args.base_ref:
            q.preservation(args.root, args.base_ref)
        else:
            q.issue('preservation_base_not_supplied', 'repository', 'REVIEW_REQUIRED')
        report = q.report()
        # Data paths remain read-only; keep generated reports outside the data roots.
        if args.report:
            target = args.report.resolve()
            protected_roots = [args.root.resolve() / 'auto']
            if args.pilot_artifact:
                protected_roots.append(args.pilot_artifact.resolve())
            if any(target.is_relative_to(p) for p in protected_roots) or target.exists():
                raise ValueError('report destination must be fresh and outside data roots')
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open('x', encoding='utf-8') as stream:
                json.dump(report, stream, ensure_ascii=False, indent=2)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1 if report['deterministic_status'] == 'FAIL' else 0
    except Exception:
        print(json.dumps({'status': 'FAIL', 'code': 'validation_execution_error'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
