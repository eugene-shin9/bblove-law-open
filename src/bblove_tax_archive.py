"""Offline, immutable tax objects. A byte match is never legal verification.

No credentials, HTTP, Drive dependency, or collection is implemented here.
An authenticated release/LFS backend is deliberately not selected by this PR.
"""
import hashlib
import json
import re
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

MAX_OBJECT_BYTES = 20 * 1024 * 1024  # conservative Git budget, not GitHub's limit
OFFICIAL_HOSTS = {"www.law.go.kr", "law.go.kr", "www.nts.go.kr", "taxlaw.nts.go.kr",
                  "www.mois.go.kr", "www.wetax.go.kr", "www.scourt.go.kr",
                  "www.tt.go.kr", "www.customs.go.kr"}
FORMATS = {"JSON", "XML", "HWP", "HWPX", "PDF", "PNG", "JPEG", "MD", "TEXT"}
KINDS = {"RAW_ORIGINAL", "DERIVED", "CONNECTOR_CAPTURE"}


class ArchiveBlocked(ValueError):
    pass


def sha(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ArchiveBlocked("SHA256_INVALID")
    return value


def iso(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ArchiveBlocked("DATE_INVALID")
    try:
        date.fromisoformat(value)
    except ValueError:
        raise ArchiveBlocked("DATE_INVALID") from None
    return value


def official_url(value):
    if not isinstance(value, str):
        raise ArchiveBlocked("OFFICIAL_URL_REQUIRED")
    p = urlsplit(value)
    if p.scheme != "https" or p.hostname not in OFFICIAL_HOSTS or p.username or p.password or p.port not in (None, 443):
        raise ArchiveBlocked("OFFICIAL_URL_REQUIRED")
    if p.query and any(k in p.query.lower() for k in ("token=", "secret=", "oc=")):
        raise ArchiveBlocked("CREDENTIAL_QUERY_FORBIDDEN")
    return value


def identity(value):
    if not isinstance(value, dict) or set(value) != {"law_id", "edition", "effective_date", "promulgation_no"}:
        raise ArchiveBlocked("IDENTITY_FIELDS_REQUIRED")
    if not re.fullmatch(r"\d{6}", value["law_id"] or ""):
        raise ArchiveBlocked("LAW_ID_REQUIRED")
    for key in ("edition", "promulgation_no"):
        if not isinstance(value[key], str) or not value[key] or len(value[key]) > 120:
            raise ArchiveBlocked("EDITION_REQUIRED")
    iso(value["effective_date"])
    return dict(value)


class TaxArchive:
    """Trusted local Private Git root; hash coordinates only, never user paths.

    Atomic create and immutable manifests protect against accidental overwrite.
    The checkout and its parent directories must remain administrator-owned.
    A deployment must authenticate manifest hashes independently of these bytes.
    """
    def __init__(self, root):
        self.root = Path(root)

    def _path(self, area, key):
        digest(key)
        path = self.root / area / key[:2] / key
        # Refuse existing symlink ancestors or files; no extraction to disk.
        ancestors = [self.root]
        cursor = self.root
        for component in (*Path(area).parts, key[:2], key):
            cursor = cursor / component
            ancestors.append(cursor)
        for part in ancestors:
            if part.is_symlink():
                raise ArchiveBlocked("SYMLINK_FORBIDDEN")
        return path

    def _create(self, path, raw):
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("xb") as f:
                f.write(raw)
        except FileExistsError:
            if path.read_bytes() != raw:
                raise ArchiveBlocked("IMMUTABLE_OBJECT_CORRUPTED") from None

    def put(self, raw, *, kind, format, source_url, parent_sha256=None):
        if not isinstance(raw, bytes) or not raw or len(raw) > MAX_OBJECT_BYTES:
            raise ArchiveBlocked("OBJECT_SIZE_OR_TYPE_INVALID")
        if kind not in KINDS or format not in FORMATS:
            raise ArchiveBlocked("OBJECT_KIND_OR_FORMAT_INVALID")
        official_url(source_url)
        if kind == "DERIVED":
            digest(parent_sha256)
            self.read(parent_sha256)
        elif parent_sha256 is not None:
            raise ArchiveBlocked("RAW_PARENT_FORBIDDEN")
        # Captures can only be text; an extracted screen cannot become a PDF.
        if kind == "CONNECTOR_CAPTURE" and format not in {"TEXT", "JSON"}:
            raise ArchiveBlocked("CAPTURE_NOT_ORIGINAL")
        key = sha(raw)
        self._create(self._path("objects/sha256", key), raw)
        return {"sha256": key, "bytes": len(raw), "kind": kind, "format": format,
                "source_url": source_url, "parent_sha256": parent_sha256,
                "stored_integrity": "PASS", "official_verification": "REVIEW_REQUIRED"}

    def read(self, key):
        path = self._path("objects/sha256", key)
        try:
            if path.stat().st_size > MAX_OBJECT_BYTES:
                raise ArchiveBlocked("OBJECT_SIZE_INVALID")
            raw = path.read_bytes()
        except FileNotFoundError:
            raise ArchiveBlocked("ORIGINAL_MISSING") from None
        if sha(raw) != key:
            raise ArchiveBlocked("OBJECT_HASH_MISMATCH")
        return raw

    def bind(self, law, objects, *, dependencies, collected_at, original_complete=False):
        law = identity(law)
        iso(collected_at)
        if type(original_complete) is not bool or not isinstance(dependencies, list) or not objects:
            raise ArchiveBlocked("MANIFEST_CONTRACT_REQUIRED")
        if len(objects) > 2048 or len(dependencies) > 2048:
            raise ArchiveBlocked("MANIFEST_LIMIT")
        keys = set()
        for obj in objects:
            if obj.get("kind") not in KINDS or obj.get("format") not in FORMATS:
                raise ArchiveBlocked("OBJECT_DESCRIPTOR_INVALID")
            raw = self.read(obj["sha256"])
            if len(raw) != obj["bytes"] or obj["sha256"] in keys:
                raise ArchiveBlocked("OBJECT_SIZE_OR_DUPLICATE")
            official_url(obj["source_url"])
            keys.add(obj["sha256"])
        for obj in objects:
            if obj["kind"] == "DERIVED" and not any(
                    p["sha256"] == obj["parent_sha256"] and p["kind"] == "RAW_ORIGINAL" for p in objects):
                raise ArchiveBlocked("DERIVATIVE_ORIGINAL_UNBOUND")
        for dep in dependencies:
            if set(dep) != {"kind", "identity", "manifest_sha256"} or dep["kind"] not in {"decree", "rule", "supplement", "annex", "form", "delegation"}:
                raise ArchiveBlocked("DEPENDENCY_CONTRACT_REQUIRED")
            identity(dep["identity"])
            digest(dep["manifest_sha256"])
        if original_complete and not any(o["kind"] == "RAW_ORIGINAL" for o in objects):
            raise ArchiveBlocked("CAPTURE_CANNOT_COMPLETE_ORIGINAL")
        manifest = {"schema": "bblove-tax-manifest-1", "identity": law, "objects": objects,
                    "dependencies": dependencies, "collected_at": collected_at,
                    "original_complete": original_complete,
                    "official_verification": "REVIEW_REQUIRED"}
        raw = canonical(manifest)
        key = sha(raw)
        self._create(self._path("manifests/sha256", key), raw)
        return key

    def restore(self, key, expected_identity, *, require_original=True, _seen=None):
        seen = set() if _seen is None else set(_seen)
        if key in seen or len(seen) > 32:
            raise ArchiveBlocked("DEPENDENCY_CYCLE_OR_DEPTH")
        seen.add(key)
        try:
            raw = self._path("manifests/sha256", key).read_bytes()
        except FileNotFoundError:
            raise ArchiveBlocked("MANIFEST_MISSING") from None
        if sha(raw) != key:
            raise ArchiveBlocked("MANIFEST_HASH_MISMATCH")
        manifest = json.loads(raw)
        if manifest["identity"] != identity(expected_identity):
            raise ArchiveBlocked("LAW_EDITION_MISMATCH")
        if require_original and not manifest["original_complete"]:
            raise ArchiveBlocked("FULL_ORIGINAL_NOT_AVAILABLE")
        bodies = {o["sha256"]: self.read(o["sha256"]) for o in manifest["objects"]}
        for dep in manifest["dependencies"]:
            self.restore(dep["manifest_sha256"], dep["identity"], _seen=seen)
        return {"manifest": manifest, "bodies": bodies, "stored_integrity": "PASS",
                "official_verification": "REVIEW_REQUIRED", "event_applicability": "REVIEW_REQUIRED"}


def collection_status(catalog):
    """No candidate list or empty dynamic table can certify full collection."""
    rows = catalog.get("targets", [])
    checks = catalog.get("official_enumeration", {})
    errors = []
    if checks.get("independently_verified") is not True or not rows:
        errors.append("OFFICIAL_ENUMERATION_INCOMPLETE")
    if checks.get("row_count") != len(rows):
        errors.append("OFFICIAL_COUNT_UNCONFIRMED")
    keys = []
    for row in rows:
        try:
            value = identity({k: row.get(k) for k in ("law_id", "edition", "effective_date", "promulgation_no")})
            keys.append(canonical(value))
            digest(row.get("manifest_sha256"))
            if type(row.get("attachment_count")) is not int or row["attachment_count"] < 0 or row.get("collection_status") != "ORIGINAL_AND_ATTACHMENTS_VERIFIED":
                raise ArchiveBlocked("TARGET_INCOMPLETE")
        except (ArchiveBlocked, TypeError):
            errors.append("TARGET_INCOMPLETE")
    if len(keys) != len(set(keys)):
        errors.append("DUPLICATE_VERSION")
    # This is an inventory gate only, never legal validity or event application.
    return {"inventory_status": "REVIEW_REQUIRED" if errors else "PASS_INVENTORY_ONLY",
            "tax_entire_collection_complete": False,
            "full_collection_audit": "NOT_IMPLEMENTED",
            "errors": sorted(set(errors)), "official_legal_status": "REVIEW_REQUIRED"}
