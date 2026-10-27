"""Allowlisted transport observations. Never serialize exception messages or URLs."""
import errno
import http.client
import os
import re
import socket
import ssl
from urllib.error import HTTPError, URLError, ContentTooShortError

TIMEOUT_SECONDS = 35
CODES = {'NETWORK_ERROR', 'DNS_ERROR', 'CONNECTION_ERROR', 'TLS_ERROR',
         'TLS_CERTIFICATE_ERROR', 'PROXY_ERROR', 'PROXY_AUTH_REQUIRED',
         'NETWORK_TIMEOUT', 'HTTP_BAD_REQUEST', 'AUTH_OR_ACCESS_DENIED',
         'HTTP_RATE_LIMITED', 'HTTP_SERVER_ERROR', 'API_HTTP_ERROR',
         'API_NON_JSON', 'API_RESPONSE_INVALID', 'API_RESPONSE_INCOMPLETE',
         'API_PROTOCOL_ERROR', 'RESPONSE_RECEIVED'}
TYPES = {'HTTPError', 'URLError', 'ContentTooShortError', 'gaierror',
         'TimeoutError', 'ConnectionError', 'ConnectionRefusedError', 'ConnectionResetError',
         'ConnectionAbortedError', 'BrokenPipeError', 'PermissionError', 'OSError', 'SSLError',
         'SSLCertVerificationError', 'SSLWantReadError', 'IncompleteRead', 'RemoteDisconnected',
         'BadStatusLine', 'HTTPException', 'JSONDecodeError', 'UnicodeDecodeError',
         'UnicodeError', 'ValueError', 'str', 'OTHER', 'NONE'}
OS_NAMES = set(errno.errorcode.values()) | {
    'EAI_AGAIN', 'EAI_NONAME', 'EAI_FAIL', 'EAI_NODATA', 'EAI_OTHER'}
CONTENT_TYPES = {'JSON', 'HTML', 'XML', 'TEXT', 'OTHER', 'MISSING'}
OPERATIONS = {('socket','getaddrinfo'):'DNS_LOOKUP', ('socket','create_connection'):'TCP_CONNECT',
              ('ssl','do_handshake'):'TLS_HANDSHAKE', ('http.client','_tunnel'):'PROXY_CONNECT',
              ('http.client','_read_status'):'HTTP_HEADERS', ('http.client','begin'):'HTTP_HEADERS',
              ('http.client','read'):'HTTP_BODY', ('http.client','_read_chunked'):'HTTP_BODY',
              ('urllib.request','do_open'):'URL_OPEN'}


class RequestFailure(RuntimeError):
    def __init__(self, message, diagnostic):
        super().__init__(message)
        self.network = safe_network(diagnostic)
        self.code = self.network['code']


class ObservedResponse(dict):
    """Keep transport metadata outside the API JSON and its stored document hash."""
    def __init__(self, doc, diagnostic):
        super().__init__(doc)
        self.network = safe_network(diagnostic)


def request_context(endpoint):
    # Presence only; no environment values, proxy URLs, credentials or bypass lists.
    return {'schema_version':1, 'host':'www.law.go.kr', 'scheme':'https', 'port':443,
            'path':'/DRF/'+endpoint, 'timeout_seconds':TIMEOUT_SECONDS,
            'attempt':1, 'transport_phase':'open', 'http_status':None,
            'content_type':'MISSING', 'proxy_environment':{
                key:any(name in os.environ for name in (key, key.upper()))
                for key in ('http_proxy', 'https_proxy', 'no_proxy')}}


def content_type(headers):
    value = headers.get('Content-Type') if hasattr(headers, 'get') else None
    if not isinstance(value, str) or not value.strip():
        return 'MISSING'
    mime = value.split(';', 1)[0].strip().lower()
    if mime == 'application/json' or mime.endswith('+json'): return 'JSON'
    if mime in ('text/html', 'application/xhtml+xml'): return 'HTML'
    if mime in ('text/xml', 'application/xml') or mime.endswith('+xml'): return 'XML'
    if mime.startswith('text/'): return 'TEXT'
    return 'OTHER'


def type_name(exc):
    name = type(exc).__name__
    return name if name in TYPES else 'OTHER'


def classify(exc, context):
    """Use exception types/errno; inspect only a tunnel status, never persist text."""
    result = dict(context, exception_type=type_name(exc), code='NETWORK_ERROR')
    reason = exc
    for _ in range(4):
        if isinstance(reason, URLError) and not isinstance(reason, HTTPError):
            reason = reason.reason
        else:
            break
    result['reason_type'] = type_name(reason)
    operations = []
    for error in (exc, reason):
        tb = error.__traceback__ if isinstance(error, BaseException) else None
        for _ in range(64):
            if tb is None: break
            # No source paths, source lines, frame locals, repr or raw traceback.
            op = OPERATIONS.get((tb.tb_frame.f_globals.get('__name__'), tb.tb_frame.f_code.co_name))
            if op is not None and op not in operations: operations.append(op)
            tb = tb.tb_next
    result['stdlib_operations'] = operations
    if isinstance(exc, HTTPError):
        status = exc.code if type(exc.code) is int and 100 <= exc.code <= 599 else None
        result.update(http_status=status, content_type=content_type(exc.headers), transport_phase='response')
        result['code'] = ('PROXY_AUTH_REQUIRED' if status == 407 else
                          'HTTP_BAD_REQUEST' if status == 400 else
                          'AUTH_OR_ACCESS_DENIED' if status in (401, 403) else
                          'HTTP_RATE_LIMITED' if status == 429 else
                          'HTTP_SERVER_ERROR' if status is not None and status >= 500 else 'API_HTTP_ERROR')
    elif isinstance(exc, ContentTooShortError) or isinstance(reason, http.client.IncompleteRead):
        result['code'] = 'API_RESPONSE_INCOMPLETE'
    elif isinstance(reason, ssl.SSLCertVerificationError):
        result['code'] = 'TLS_CERTIFICATE_ERROR'
        if type(getattr(reason, 'verify_code', None)) is int:
            result['tls_verify_code'] = reason.verify_code
    elif isinstance(reason, ssl.SSLError):
        result['code'] = 'TLS_ERROR'
    elif isinstance(reason, socket.gaierror):
        result['code'] = 'DNS_ERROR'
        result['os_error'] = next((name for name in ('EAI_AGAIN', 'EAI_NONAME', 'EAI_FAIL', 'EAI_NODATA')
                                   if getattr(socket, name, None) == reason.errno), 'EAI_OTHER')
    elif isinstance(reason, TimeoutError):
        result['code'] = 'NETWORK_TIMEOUT'
    elif isinstance(reason, OSError):
        # urllib HTTPS proxy CONNECT rejects non-200 with this stdlib fixed prefix.
        tunnel = re.match(r'^Tunnel connection failed: ([1-5][0-9]{2})\b', str(reason))
        if tunnel:
            status = int(tunnel.group(1))
            result.update(code='PROXY_AUTH_REQUIRED' if status == 407 else 'PROXY_ERROR', http_status=status)
        else:
            result['os_error'] = errno.errorcode.get(reason.errno, None)
            if isinstance(reason, ConnectionError) or result['os_error'] in (
                    'ECONNREFUSED', 'ECONNRESET', 'ECONNABORTED', 'EHOSTUNREACH', 'ENETUNREACH', 'EPIPE'):
                result['code'] = 'CONNECTION_ERROR'
    elif isinstance(reason, http.client.HTTPException):
        result['code'] = 'API_PROTOCOL_ERROR'
    return safe_network(result)


def retry_design(value):
    """Proposal only: current diagnosis makes exactly one attempt, never sleeps."""
    code = value.get('code')
    eligible = (code == 'NETWORK_TIMEOUT'
                or (code == 'DNS_ERROR' and value.get('os_error') == 'EAI_AGAIN')
                or (code == 'CONNECTION_ERROR' and value.get('os_error') in ('ECONNRESET', 'ECONNABORTED'))
                or (code in ('HTTP_RATE_LIMITED', 'HTTP_SERVER_ERROR') and value.get('http_status') in (429, 500, 502, 503, 504)))
    return {'automatic_enabled':False, 'actual_max_attempts':1, 'transient_candidate':eligible,
            'proposed_max_attempts':2 if eligible else 1,
            'proposed_wait_seconds':(30 if code == 'HTTP_RATE_LIMITED' else 2) if eligible else 0}


def safe_network(value):
    """Filter again when reading an artifact; no arbitrary strings reach Summary."""
    if not isinstance(value, dict) or value.get('schema_version') != 1:
        return None
    code = value.get('code')
    if not isinstance(code, str) or code not in CODES:
        return None
    result = request_context('lawSearch.do')
    result['code'] = code
    result['path'] = value.get('path') if value.get('path') in ('/DRF/lawSearch.do', '/DRF/lawService.do') else '/DRF/unknown'
    result['transport_phase'] = value.get('transport_phase') if value.get('transport_phase') in ('open', 'read', 'decode', 'response') else 'open'
    result['proxy_environment'] = {key:value.get('proxy_environment', {}).get(key) is True
                                   for key in ('http_proxy', 'https_proxy', 'no_proxy')} if isinstance(value.get('proxy_environment'), dict) else {}
    for key in ('exception_type', 'reason_type'):
        v = value.get(key)
        result[key] = v if isinstance(v, str) and v in TYPES else 'OTHER'
    v = value.get('content_type')
    result['content_type'] = v if isinstance(v, str) and v in CONTENT_TYPES else 'OTHER'
    for key, maximum in (('http_status',599), ('tls_verify_code',255), ('elapsed_ms',2_147_483_647)):
        v = value.get(key)
        if type(v) is int and 0 <= v <= maximum and (key != 'http_status' or v >= 100):
            result[key] = v
    v = value.get('os_error')
    if isinstance(v, str) and v in OS_NAMES:
        result['os_error'] = v
    ops = value.get('stdlib_operations')
    result['stdlib_operations'] = list(dict.fromkeys(op for op in ops[:16]
                                                    if isinstance(op, str) and op in OPERATIONS.values())) if isinstance(ops, list) else []
    result['retry_design'] = retry_design(result)
    return result
