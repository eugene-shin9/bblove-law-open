"""One list request per DRF target through the configured egress.

Prints only status, HTTP code, content type and JSON root keys as annotations. The credential
never appears in output: any line that would contain it is replaced by REDACTED.
"""
import json
import os
import sys

sys.path[:0] = [os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src')]
from bblove_tax_p2_collect import BoundedClient, PilotBlocked  # noqa: E402
from bblove_law_transport import measured_transport  # noqa: E402

TARGETS = ('law', 'eflaw', 'prec', 'ntsCgmExpc', 'ttSpecialDecc', 'admrul')


def main():
    oc = os.environ.get('LAW_OC', '')
    if not oc:
        print('::error title=egress check::LAW_OC missing')
        return 1
    ok = 0
    for target in TARGETS:
        client = BoundedClient(oc, measured_transport, request_limit=1)
        keys, err = None, None
        try:
            doc, _raw, _ = client.json('lawSearch.do', {'target': target, 'display': 1, 'page': 1}, 'egress ' + target)
            root = next((v for v in doc.values() if isinstance(v, dict)), {})
            keys = {'root': sorted(doc)[:3], 'totalCnt': root.get('totalCnt'),
                    'resultCode': root.get('resultCode'), 'resultMsg': root.get('resultMsg')}
            ok += 1
        except PilotBlocked as exc:
            err = str(exc)
        r = client.receipts[-1] if client.receipts else {}
        msg = json.dumps({'target': target, 'status': r.get('status'), 'http': r.get('http_status'),
                          'type': r.get('content_type'), 'error': err, 'json': keys}, ensure_ascii=False)
        if oc in msg:
            msg = 'REDACTED'
        print(f'::notice title=egress {target}::{msg}')
    print(f'::notice title=egress summary::{ok}/{len(TARGETS)} targets answered with JSON')
    return 0 if ok == len(TARGETS) else 1


if __name__ == '__main__':
    sys.exit(main())
