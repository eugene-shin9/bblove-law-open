#!/usr/bin/env python3
"""Read-only replay of failed/warning RAW; never fetches or reads credentials."""
import argparse
from collections import Counter
import hashlib
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from bblove_law_all import safe_detail, save, today, validate_raw, failure_distribution
from bblove_storage import strict_json
from bblove_v107_pilot import validate_detail


def diagnose(source, date=None):
    manifest = strict_json((source / 'manifest.json').read_bytes())
    date = date or today()
    details = []
    for entry in manifest['documents']:
        if entry.get('reason') != 'DETAIL_VALIDATION_FAILED' and not entry.get('warnings'):
            continue
        result = {'title': entry['title'], 'mst': entry['mst'], 'status': 'FAILED'}
        path = (source / entry['path']).resolve()
        if not path.is_relative_to(source.resolve()):
            result.update(detail='UNSAFE_RAW_PATH', replay='UNAVAILABLE')
        elif not path.is_file():
            result.update(detail='RAW_CACHE_UNAVAILABLE', replay='UNAVAILABLE')
        else:
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != entry.get('sha256'):
                result.update(detail='RAW_HASH_UNVERIFIED', replay='UNAVAILABLE')
            else:
                try:
                    basic = strict_json(raw).get('법령', {}).get('기본정보', {})
                    row = entry['row']
                    diff = {}
                    for key, rk, dk in (('title', '법령명한글', '법령명_한글'), ('id', '법령ID', '법령ID'),
                                        ('effective', '시행일자', '시행일자')):
                        cv, dv = str(row.get(rk, '')), str(basic.get(dk, ''))
                        if cv != dv:
                            diff[key] = {'catalog': cv[:80], 'detail': dv[:80]}
                    if diff:
                        result['field_diff'] = diff
                except Exception:
                    pass
                try:
                    validate_detail(strict_json(raw), entry['row'], date)
                    result['detail'] = 'V107_PASS'
                except ValueError as exc:
                    result['detail'] = safe_detail(exc)
                try:
                    _, _, warnings = validate_raw(raw, entry['row'], date, with_warnings=True)
                    result.update(replay='SUCCESS', warnings=warnings)
                except (ValueError, KeyError, TypeError) as exc:
                    result.update(replay='FAILED', relaxed_detail=safe_detail(exc))
        details.append(result)
    return {'date': date, 'candidates': len(details),
            'replay_counts': dict(Counter(r['replay'] for r in details)),
            'strict_failure_distribution': failure_distribution(details), 'documents': details}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, default=Path('auto/official-all'))
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    if (args.source / 'manifest.json').exists():
        result = diagnose(args.source)
    else:
        result = {'status': 'RAW_CACHE_UNAVAILABLE', 'candidates': 0}
    save(args.report, result)
    print({k: v for k, v in result.items() if k != 'documents'})
