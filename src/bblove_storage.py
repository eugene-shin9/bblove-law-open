"""Versioned lossless JSON storage. No network or environment access."""
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile

FORMAT = 'bblove-json-shards-v1'
MAX_FILE_BYTES = 1_000_000
MAX_TOTAL_BYTES = 100_000_000  # bounded offline reassembly, not a per-file exemption


def strict_json(raw):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError('duplicate JSON key')
            result[key] = value
        return result
    def invalid_constant(_):
        raise ValueError('non-finite JSON number')
    return json.loads(raw.decode('utf-8-sig'), object_pairs_hook=pairs,
                      parse_constant=invalid_constant)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def bounded_read(path):
    with path.open('rb') as stream:
        raw = stream.read(MAX_FILE_BYTES + 1)
    if not raw or len(raw) > MAX_FILE_BYTES:
        raise ValueError('storage: file size gate')
    return raw


def read_document(path, storage=None):
    """Return (JSON object, original bytes, all storage paths); fail closed."""
    path = Path(path)
    raw = bounded_read(path)
    doc = strict_json(raw)
    if not isinstance(doc, dict):
        raise ValueError('storage: invalid JSON root')
    if storage is None:
        if 'storage_format' in doc:
            raise ValueError('storage: manifest format missing')
        return doc, raw, {path.resolve()}
    if not isinstance(storage, dict) or storage.get('format') != FORMAT:
        raise ValueError('storage: unsupported format')
    if storage.get('index_sha256') != digest(raw) or storage.get('index_bytes') != len(raw):
        raise ValueError('storage: index integrity mismatch')
    if doc.get('storage_format') != FORMAT or doc.get('law_id') != path.stem:
        raise ValueError('storage: index identity mismatch')
    total = doc.get('document_bytes')
    parts = doc.get('parts')
    if (type(total) is not int or not MAX_FILE_BYTES < total <= MAX_TOTAL_BYTES
            or not isinstance(parts, list) or not 2 <= len(parts) <= 102):
        raise ValueError('storage: invalid shard inventory')
    folder = path.parent / (path.stem + '.parts')
    if folder.is_symlink() or not folder.resolve().is_relative_to(path.parent.resolve()):
        raise ValueError('storage: unsafe shard path')
    files = {path.resolve()}
    chunks = []
    accumulated = 0
    for n, row in enumerate(parts, 1):
        expected = f'{path.stem}.parts/{n:06d}.utf8'
        if (not isinstance(row, dict) or type(row.get('sequence')) is not int
                or row['sequence'] != n or row.get('path') != expected):
            raise ValueError('storage: shard order or path mismatch')
        part = path.parent / expected
        if part.is_symlink() or not part.resolve().is_relative_to(folder.resolve()):
            raise ValueError('storage: unsafe shard path')
        chunk = bounded_read(part)
        if type(row.get('bytes')) is not int or row['bytes'] != len(chunk) or row.get('sha256') != digest(chunk):
            raise ValueError('storage: shard integrity mismatch')
        chunk.decode('utf-8')  # every part must end at a complete character
        accumulated += len(chunk)
        if accumulated > total:
            raise ValueError('storage: document size mismatch')
        chunks.append(chunk)
        files.add(part.resolve())
    if {p.resolve() for p in folder.rglob('*') if p.is_file()} != files - {path.resolve()}:
        raise ValueError('storage: unlisted shard file')
    payload = b''.join(chunks)
    if len(payload) != total or digest(payload) != doc.get('document_sha256'):
        raise ValueError('storage: document integrity mismatch')
    document = strict_json(payload)
    if not isinstance(document, dict):
        raise ValueError('storage: invalid JSON root')
    return document, payload, files


def write_document(path, payload):
    """Commit an index last; verify bytes before returning a successful law."""
    path = Path(path)
    if not re.fullmatch(r'[0-9]+', path.stem) or path.suffix != '.json':
        raise ValueError('storage: invalid law path')
    if not 0 < len(payload) <= MAX_TOTAL_BYTES or not isinstance(strict_json(payload), dict):
        raise ValueError('storage: invalid document')
    folder = path.parent / (path.stem + '.parts')
    if path.exists() or folder.exists():
        raise ValueError('storage: existing document preserved')
    path.parent.mkdir(parents=True, exist_ok=True)
    committed_folder = committed_index = False
    try:
        with tempfile.TemporaryDirectory(prefix='.staging-', dir=path.parent) as temp:
            stage = Path(temp)
            index_path = stage / path.name
            storage = None
            if len(payload) <= MAX_FILE_BYTES:
                index_path.write_bytes(payload)
            else:
                staged_folder = stage / folder.name
                staged_folder.mkdir()
                parts = []
                offset = 0
                while offset < len(payload):
                    end = min(offset + MAX_FILE_BYTES, len(payload))
                    while end < len(payload) and payload[end] & 0xC0 == 0x80:
                        end -= 1
                    chunk = payload[offset:end]
                    chunk.decode('utf-8')
                    n = len(parts) + 1
                    name = f'{n:06d}.utf8'
                    (staged_folder / name).write_bytes(chunk)
                    parts.append({'sequence': n, 'path': f'{folder.name}/{name}',
                                  'bytes': len(chunk), 'sha256': digest(chunk)})
                    offset = end
                index = {'storage_format': FORMAT, 'law_id': path.stem,
                         'document_bytes': len(payload), 'document_sha256': digest(payload),
                         'parts': parts}
                index_raw = json.dumps(index, ensure_ascii=False, sort_keys=True,
                                       separators=(',', ':')).encode('utf-8')
                index_path.write_bytes(index_raw)
                storage = {'format': FORMAT, 'index_bytes': len(index_raw),
                           'index_sha256': digest(index_raw)}
            if read_document(index_path, storage)[1] != payload:
                raise ValueError('storage: roundtrip mismatch')
            if storage:
                staged_folder.rename(folder)
                committed_folder = True
            index_path.rename(path)
            committed_index = True
            if read_document(path, storage)[1] != payload:
                raise ValueError('storage: roundtrip mismatch')
            return storage
    except Exception:
        if committed_index:
            path.unlink(missing_ok=True)
        if committed_folder:
            shutil.rmtree(folder)
        raise
