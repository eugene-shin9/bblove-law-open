"""Allowlisted Actions diagnostics; never print raw exceptions or API payloads."""
import json
import os
from pathlib import Path
import re
from bblove_storage import strict_json
from bblove_network import RequestFailure, safe_network


def error_code(message):
    if isinstance(message, RequestFailure):
        return message.code
    if not isinstance(message, str):
        return 'PROCESSING_ERROR'
    if message.startswith('LAW_OC secret missing;'):
        return 'AUTH_CONFIGURATION_MISSING'
    if 'output folder is not empty' in message:
        return 'OUTPUT_NOT_EMPTY'
    if re.fullmatch(r'.*: HTTP (401|403)', message):
        return 'AUTH_OR_ACCESS_DENIED'
    if re.fullmatch(r'.*: HTTP [0-9]{3}', message):
        return 'API_HTTP_ERROR'
    if message.endswith(': network failure'):
        return 'NETWORK_ERROR'
    if message.endswith(': non-JSON response'):
        return 'API_NON_JSON'
    if message.endswith((': unexpected JSON root', ': unexpected response size')):
        return 'API_RESPONSE_INVALID'
    if message.startswith('storage:') or message == 'document storage failed':
        return 'STORAGE_INTEGRITY_ERROR'
    if message == 'single lookup: no current law for requested ID':
        return 'LAW_NOT_FOUND_OR_NOT_CURRENT'
    if message == 'single lookup: current status missing or unsupported':
        return 'CURRENT_STATUS_UNVERIFIED'
    if message.startswith('single lookup effective date:') or message == 'single lookup: title missing':
        return 'DATA_VALIDATION_ERROR'
    if message.startswith('single lookup:'):
        return 'SINGLE_LOOKUP_INVALID'
    if message == 'document exceeds project 1MB gate; needs sharding':
        return 'DOCUMENT_SIZE_GATE'
    if message.startswith(('detail:', 'detail effective date:')):
        return 'DATA_VALIDATION_ERROR'
    if message.startswith(('catalog:', 'catalog page ', 'catalog effective date:')):
        return 'CATALOG_ERROR'
    return 'PROCESSING_ERROR'


def publish(out, requested, fallback_code='RUN_ABORTED', use_manifest=True,
            write_artifact=True, allow_pass=True, fallback_status='FAIL'):
    """Read an existing manifest, emit a safe summary and a detailed JSON artifact."""
    out = Path(out)
    diagnostic = {'schema_version': 1, 'status': fallback_status, 'requested': requested,
                  'verified': 0, 'failed': 0 if fallback_status == 'NOT_RUN' else requested,
                  'errors': [{'id': '-', 'stage': 'startup', 'code': fallback_code}]}
    if fallback_status == 'NOT_RUN':
        diagnostic['not_attempted'] = requested
    try:
        if not use_manifest:
            raise ValueError('manifest not from this run')
        report = strict_json((out / 'manifest.json').read_bytes())
        counts = [report.get(k) for k in ('requested', 'verified', 'failed')]
        if (any(type(n) is not int or n < 0 for n in counts)
                or counts[0] != requested or counts[1] + counts[2] != requested
                or not isinstance(report.get('errors'), list)):
            raise ValueError('invalid manifest')
        errors = []
        for row in report['errors']:
            lid = row.get('id', '-')
            stage = row.get('stage', 'detail')
            errors.append({'id': lid if isinstance(lid, str) and re.fullmatch(r'[0-9]+', lid) else '-',
                           'stage': stage if stage in ('catalog', 'single_lookup', 'lookup_validation',
                                                      'lookup_storage', 'fetch', 'validation', 'storage', 'detail') else 'detail',
                           'code': error_code(row.get('error'))})
            network = safe_network(row.get('network'))
            if network is not None:
                errors[-1]['network'] = network
                if network['code'] != 'RESPONSE_RECEIVED':
                    errors[-1]['code'] = network['code']
        if ((report.get('phase') != 'catalog' and len(errors) != counts[2])
                or (counts[2] and not errors) or (not allow_pass and counts[2] == 0)):
            raise ValueError('invalid error count')
        diagnostic.update(requested=counts[0], verified=counts[1], failed=counts[2], errors=errors,
                          status='PASS' if counts[2] == 0 else 'FAIL')
    except (OSError, ValueError, TypeError, AttributeError, UnicodeError):
        pass
    lines = ['### BBLOVE V1.07 collection diagnosis', '',
             f"**{diagnostic['status']}** — requested={diagnostic['requested']}, "
             f"verified={diagnostic['verified']}, failed={diagnostic['failed']}", '',
             'Official original comparison: REVIEW_REQUIRED.', '']
    if 'not_attempted' in diagnostic:
        lines += [f"Not attempted: {diagnostic['not_attempted']}.", '']
    if diagnostic['errors']:
        lines += ['| Law ID | Stage | Error code |', '| --- | --- | --- |']
        lines += [f"| {r['id']} | {r['stage']} | {r['code']} |" for r in diagnostic['errors']]
        for row in diagnostic['errors']:
            n = row.get('network')
            if n:
                lines += ['', f"Transport: {n['scheme']}://{n['host']}{n['path']} (no query)",
                          f"Exception: {n['exception_type']}; reason: {n['reason_type']}; phase: {n['transport_phase']}",
                          f"Observed stdlib operations: {', '.join(n['stdlib_operations']) or 'unavailable'}",
                          f"HTTP: {n['http_status']}; content type: {n['content_type']}; errno: {n.get('os_error', '-')}; TLS verify code: {n.get('tls_verify_code', '-')}",
                          f"Timeout: {n['timeout_seconds']}s; elapsed: {n.get('elapsed_ms', 0)}ms; attempt: {n['attempt']}",
                          f"Proxy variable presence only: {json.dumps(n['proxy_environment'], sort_keys=True)}; automatic retry: disabled"]
    summary = '\n'.join(lines) + '\n'
    print(summary)
    # Never overwrite a previous run's output or create a folder for a rejected run.
    if write_artifact:
        try:
            with (out / 'diagnostics.json').open('x', encoding='utf-8') as stream:
                json.dump(diagnostic, stream, ensure_ascii=False, indent=2)
        except OSError:
            print('DIAGNOSTICS_ARTIFACT_WRITE_FAILED')
    target = os.getenv('GITHUB_STEP_SUMMARY')
    if target:
        try:
            with Path(target).open('a', encoding='utf-8') as stream:
                stream.write(summary)
        except OSError:
            print('STEP_SUMMARY_WRITE_FAILED')
    return diagnostic


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--requested', type=int, default=150)
    parser.add_argument('--outcome', choices=('success', 'failure', 'skipped', 'cancelled'), required=True)
    args = parser.parse_args(argv)
    if (args.output / 'diagnostics.json').exists():
        # The collector already wrote its own summary and detailed diagnostic.
        return 0
    try:
        args.output.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    skipped = args.outcome in ('skipped', 'cancelled')
    publish(args.output, args.requested, 'COLLECTION_NOT_RUN' if skipped else 'RUN_ABORTED',
            use_manifest=not skipped, allow_pass=args.outcome == 'success',
            fallback_status='NOT_RUN' if skipped else 'FAIL')
    return 0  # diagnostics must never convert a failed collection into success


if __name__ == '__main__':
    raise SystemExit(main())
