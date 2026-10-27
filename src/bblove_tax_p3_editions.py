"""TAX P3: prior 국세기본법 editions for 지정납부기한 elapsed by 2026-06-30.

Exact public www.law.go.kr response bytes (no API key, no LAW_OC) are stored as
immutable tax objects. They are official web HTML, not DRF JSON originals, and
stay original_complete=False. Article text is extracted deterministically into
a bound DERIVED object; legal applicability remains REVIEW_REQUIRED.
"""
import argparse
import html
import json
from pathlib import Path
import re
from bblove_storage import strict_json
from bblove_tax_archive import TaxArchive, canonical, sha

RECORD = "tax/p3/prior-editions.json"
ARTICLES = ("제21조", "제47조의4", "제47조의5")
SUPPLEMENT_SENTENCES = (
    "제47조의4 및 제47조의5의 개정규정은 2026년 7월 1일부터 시행",
    "2026년 6월 30일 이전에 지정납부기한이 경과한 경우의 납부지연가산세의 세액 계산에 관하여는 제47조의4제1항 및 제8항의 개정규정에도 불구하고 종전의 규정에 따른다",
)


def page_text(raw):
    text = raw.decode("utf-8", errors="strict")
    text = re.sub(r"<script.*?</script>", "", text, flags=re.S)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    return re.sub(r"\s+", " ", text).strip()


def extract(raw):
    """Edition header and the full text of the late-payment articles."""
    text = page_text(raw)
    header = re.search(r"\[시행 (\d{4})\. (\d{1,2})\. (\d{1,2})\.\] \[법률 제(\d+)호, (\d{4})\. (\d{1,2})\. (\d{1,2})\., ([^\]]+)\]", text)
    if not header:
        raise ValueError("EDITION_HEADER_NOT_FOUND")
    y, m, d, no, py, pm, pd, kind = header.groups()
    articles = {}
    for start, end in (("제21조(납세의무의 성립시기)", "제22조("), ("제47조의4(납부지연가산세)", "제47조의5("),
                       ("제47조의5(원천징수 등 납부지연가산세)", "제48조(")):
        i = text.find(start)
        j = text.find(end, i + len(start)) if i >= 0 else -1
        if i < 0 or j < 0:
            raise ValueError("ARTICLE_NOT_FOUND:" + start)
        articles[start.split("(")[0]] = text[i:j].strip()
    supplements = {s: s in text for s in SUPPLEMENT_SENTENCES}
    return {"effective_date": f"{y}-{int(m):02d}-{int(d):02d}", "promulgation_no": no,
            "promulgation_date": f"{py}-{int(pm):02d}-{int(pd):02d}", "amendment_kind": kind.strip(),
            "articles": articles, "supplement_21212_sentences_present": supplements,
            "extraction": "html-tags-stripped, whitespace-collapsed; not an API JSON original"}


def ingest(repo, sources, fetched_at):
    """sources: [{path, url, law_id, mst, role}] of exact response bytes."""
    repo = Path(repo); archive = TaxArchive(repo / "tax"); rows = []
    for s in sources:
        raw = Path(s["path"]).read_bytes()
        if re.search(rb"(?i)[?&]OC=[^&\"'\s]", raw):
            raise ValueError("CREDENTIAL_ECHO_REJECTED")
        original = archive.put(raw, kind="RAW_ORIGINAL", format="TEXT", source_url=s["url"])
        row = {"role": s["role"], "law_id": s["law_id"], "mst": s["mst"], "source_url": s["url"],
               "media_type": "text/html;charset=UTF-8", "provenance": "PUBLIC_WEB_HTML_NOT_DRF_JSON",
               "object_sha256": original["sha256"], "bytes": original["bytes"], "fetched_at_utc": fetched_at}
        if s["role"] == "edition_body":
            info = extract(raw)
            derived = archive.put(canonical(info), kind="DERIVED", format="JSON", source_url=s["url"], parent_sha256=original["sha256"])
            identity = {"law_id": s["law_id"], "edition": f"{s['mst']}:{info['effective_date'].replace('-', '')}",
                        "effective_date": info["effective_date"], "promulgation_no": info["promulgation_no"]}
            manifest = archive.bind(identity, [original, {**derived}], dependencies=[],
                                    collected_at=fetched_at[:10], original_complete=False)
            row.update(identity=identity, promulgation_date=info["promulgation_date"],
                       derived_sha256=derived["sha256"], manifest_sha256=manifest)
        rows.append(row)
    bodies = [r for r in rows if r["role"] == "edition_body"]
    texts = {r["identity"]["edition"]: strict_json(archive.read(r["derived_sha256"]))["articles"]["제47조의4"] for r in bodies}
    record = {"schema": "bblove-tax-p3-prior-editions-1", "purpose": "종전 제47조의4 for 지정납부기한 elapsed by 2026-06-30 (법률 제21212호 부칙 제3조)",
              "sources": rows, "article_47_4_identical_across_bodies": len(set(texts.values())) == 1,
              "law_text_coverage": {"from": min(r["identity"]["effective_date"] for r in bodies), "to": "2026-06-30"},
              "decree_rate_coverage": {"edition": "283623:20260227", "from": "2026-02-27", "to": "2026-06-30",
                                       "note": "시행령 제27조의4 single paragraph (1일 10만분의 22); earlier decree editions NOT_ARCHIVED"},
              "api_json_original": "NOT_ACQUIRED_LAW_OC_WORKFLOW_NOT_AUTHORIZED",
              "legal_verification": "REVIEW_REQUIRED", "event_applicability": "REVIEW_REQUIRED"}
    (repo / RECORD).parent.mkdir(parents=True, exist_ok=True)
    (repo / RECORD).write_bytes(canonical(record))
    return record


def verify(repo):
    repo = Path(repo); archive = TaxArchive(repo / "tax")
    rec = strict_json((repo / RECORD).read_bytes())
    texts = []
    for row in rec["sources"]:
        raw = archive.read(row["object_sha256"])
        if len(raw) != row["bytes"]:
            raise ValueError("PRIOR_EDITION_SIZE_MISMATCH")
        if row["role"] != "edition_body":
            continue
        info = extract(raw)
        if canonical(info) != archive.read(row["derived_sha256"]):
            raise ValueError("PRIOR_EDITION_EXTRACTION_REPLAY_MISMATCH")
        if (info["effective_date"], info["promulgation_no"]) != (row["identity"]["effective_date"], row["identity"]["promulgation_no"]):
            raise ValueError("PRIOR_EDITION_IDENTITY_MISMATCH")
        if not all(info["supplement_21212_sentences_present"].values()):
            raise ValueError("TRANSITION_SUPPLEMENT_MISSING")
        restored = archive.restore(row["manifest_sha256"], row["identity"], require_original=False)
        if row["object_sha256"] not in restored["bodies"]:
            raise ValueError("PRIOR_EDITION_MANIFEST_UNBOUND")
        texts.append(info["articles"]["제47조의4"])
    if not texts or len(set(texts)) != 1 or not rec["article_47_4_identical_across_bodies"]:
        raise ValueError("PRIOR_47_4_TEXT_NOT_STABLE")
    if rec["legal_verification"] != "REVIEW_REQUIRED":
        raise ValueError("PRIOR_EDITION_RECORD_PROMOTED")
    return {"status": "PASS_STORED_BYTES_AND_EXTRACTION", "editions": [r["identity"]["edition"] for r in rec["sources"] if r["role"] == "edition_body"],
            "law_text_coverage": rec["law_text_coverage"], "legal_verification": "REVIEW_REQUIRED"}


def prior_article_47_4(repo):
    repo = Path(repo); archive = TaxArchive(repo / "tax")
    rec = strict_json((repo / RECORD).read_bytes())
    row = max((r for r in rec["sources"] if r["role"] == "edition_body"), key=lambda r: r["identity"]["effective_date"])
    return strict_json(archive.read(row["derived_sha256"]))["articles"]["제47조의4"], row


if __name__ == "__main__":
    p = argparse.ArgumentParser(); p.add_argument("--repo", type=Path, default=Path("."))
    a = p.parse_args()
    print(json.dumps(verify(a.repo), ensure_ascii=False))
