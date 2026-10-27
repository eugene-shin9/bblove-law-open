#!/usr/bin/env python3
"""BBLOVE V1.07 150-document trial; never commits and never stores API credentials."""
import argparse
import datetime as dt
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlencode, quote, quote_plus
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from zoneinfo import ZoneInfo
from bblove_storage import MAX_FILE_BYTES, write_document
from bblove_diagnostics import error_code, publish
from bblove_network import RequestFailure, ObservedResponse, TIMEOUT_SECONDS, request_context, classify, content_type, type_name

API_BASE = "https://www.law.go.kr/DRF/"
MAX_RESPONSE_BYTES = 20_000_000
MAX_DOCUMENT_BYTES = MAX_FILE_BYTES  # per-file gate, including shard parts
CATEGORY_NAMES = ("법률", "대통령령", "총리령·부령", "기타")
SINGLE_LOOKUP_METHOD = "eflaw-LID-nw3-v1"
CURRENT_LABELS = {"현행"}  # official metadata example; unknown/missing codes fail closed

def text(obj, *keys):
    if not isinstance(obj, dict):
        return ""
    for key in keys:
        value = obj.get(key)
        if value is not None and not isinstance(value, (dict, list)):
            s = str(value).strip()
            if s:
                return s
    return ""

def records(obj):
    if isinstance(obj, list):
        return [v for v in obj if isinstance(v, dict)]
    if isinstance(obj, dict):
        return [obj]
    return []

def check_date(value, label, today=None):
    """Reject impossible dates, not merely bad 8-digit date strings."""
    if not re.fullmatch(r"\d{8}", value or ""):
        raise ValueError(f"{label}: invalid date format")
    try:
        date = dt.datetime.strptime(value, "%Y%m%d").date()
    except ValueError:
        raise ValueError(f"{label}: impossible calendar date") from None
    if today is not None:
        today_date = dt.datetime.strptime(today, "%Y%m%d").date()
        if date > today_date:
            raise ValueError(f"{label}: future-effective law excluded")
    return date

def category(item):
    kind = text(item, "법령구분명")
    if kind == "법률":
        return "법률"
    if kind == "대통령령":
        return "대통령령"
    if "총리령" in kind or "부령" in kind or kind.endswith("령"):
        return "총리령·부령"
    return "기타"

def sanitized(doc, oc):
    tokens = tuple(s for s in {oc, quote(oc, safe=""), quote_plus(oc)} if s)
    def scrub(x):
        if isinstance(x, list):
            return [scrub(v) for v in x]
        if isinstance(x, dict):
            return {scrub(k): scrub(v) for k,v in x.items()}
        if isinstance(x, str):
            x = re.sub(r"(?i)([?&]OC=)[^&\s\"<>]+", r"\1REDACTED", x)
            for t in tokens:
                x = x.replace(t, "REDACTED")
            return x
        return x
    clean = scrub(doc)
    encoded = json.dumps(clean, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if any(t.encode("utf-8") in encoded for t in tokens):
        raise ValueError("Credentials redaction verification failed")
    return clean, encoded

def get_json(endpoint, params, oc, label):
    payload = urlencode({**params, "OC": oc, "type": "JSON"})
    url = API_BASE + endpoint + "?" + payload
    context = request_context(endpoint)
    started = time.monotonic()
    safe_label = label if re.fullmatch(r'(single lookup [0-9]+|catalog page [0-9]+|law detail [0-9]+/[0-9]+)', label) else 'API request'
    try:
        req = Request(url, headers={"User-Agent": "BBLOVE-V1.07-Pilot/1.0",
                                    "Accept": "application/json"})
        with urlopen(req, timeout=TIMEOUT_SECONDS) as res:
            context['transport_phase'] = 'read'
            context['content_type'] = content_type(getattr(res, 'headers', None))
            status = getattr(res, 'status', None)
            if type(status) is int:
                context['http_status'] = status
            raw = res.read(MAX_RESPONSE_BYTES + 1)
        context['transport_phase'] = 'response'
        if not 2 <= len(raw) <= MAX_RESPONSE_BYTES:
            raise ValueError('unexpected response size')
        context['transport_phase'] = 'decode'
        obj = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(obj, dict):
            raise ValueError('unexpected JSON root')
    except (HTTPError, URLError, OSError, http.client.HTTPException, UnicodeError, ValueError) as exc:
        context['elapsed_ms'] = max(0, round((time.monotonic()-started)*1000))
        diagnostic = classify(exc, context)
        if isinstance(exc, (UnicodeError, json.JSONDecodeError)):
            diagnostic.update(code='API_NON_JSON', exception_type=type_name(exc))
            message = 'non-JSON response'
        elif isinstance(exc, ValueError) and not isinstance(exc, OSError):
            diagnostic.update(code='API_RESPONSE_INVALID', exception_type=type_name(exc))
            message = 'unexpected JSON root' if str(exc) == 'unexpected JSON root' else 'unexpected response size'
        else:
            message = f'HTTP {diagnostic["http_status"]}' if isinstance(exc, HTTPError) else 'network failure'
        raise RequestFailure(f'{safe_label}: {message}', diagnostic) from None
    context.update(code='RESPONSE_RECEIVED', exception_type='NONE', reason_type='NONE',
                   transport_phase='response', elapsed_ms=max(0, round((time.monotonic()-started)*1000)))
    return ObservedResponse(sanitized(obj, oc)[0], context)

def catalog(oc, today=None):
    """Enumerate ALL pages. If any count/page/ID is suspect, abort."""
    all_items = []
    seen = set()
    total = None
    today = today or dt.datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d")
    for page in range(1, 501):  # defensive upper bound
        doc = get_json("lawSearch.do",
                       {"target": "eflaw", "nw": "3", "display": "100",
                        "page": str(page), "sort": "lasc"}, oc, f"catalog page {page}")
        search = doc.get("LawSearch")
        if not isinstance(search, dict):
            raise RuntimeError("catalog: LawSearch field missing")
        try:
            reported = int(text(search, "totalCnt"))
        except ValueError:
            raise RuntimeError("catalog: missing totalCnt") from None
        if reported <= 0 or (total is not None and reported != total):
            raise RuntimeError("catalog: totalCnt changed or invalid")
        total = reported
        raw_items = search.get("law")
        items = records(raw_items)
        if isinstance(raw_items, list) and len(items) != len(raw_items):
            raise RuntimeError("catalog: malformed record")
        if not items:
            raise RuntimeError("catalog: early empty page")
        for item in items:
            lid = text(item, "법령ID")
            effective = text(item, "시행일자")
            title = text(item, "법령명한글", "법령명_한글")
            if not lid.isdecimal() or not title:
                raise RuntimeError("catalog: invalid ID or title")
            try:
                check_date(effective, "catalog effective date", today=today)
            except ValueError as exc:
                raise RuntimeError(str(exc)) from None
            if lid in seen:
                raise RuntimeError(f"catalog: duplicate 법령ID {lid} — stop to prevent omissions")
            seen.add(lid)
            all_items.append(item)
        if len(all_items) >= total:
            if len(all_items) != total:
                raise RuntimeError("catalog: count exceeds official total")
            break
        if len(items) != 100:
            raise RuntimeError("catalog: short non-final page")
        time.sleep(0.3)
    else:
        raise RuntimeError("catalog: safety page limit exceeded")
    return all_items, total

def select_stratified(items, limit):
    buckets = {name: [] for name in CATEGORY_NAMES}
    for item in items:
        buckets[category(item)].append(item)
    for group in buckets.values():
        group.sort(key=lambda x: (text(x, "법령명한글"), text(x, "법령ID")))
    sample = []
    while len(sample) < limit and any(buckets.values()):
        for name in CATEGORY_NAMES:
            if buckets[name] and len(sample) < limit:
                sample.append(buckets[name].pop(0))
    return sample

def validate_single_lookup(doc, law_id, today):
    """Validate one official LID/nw=3 result, without enumerating other laws."""
    search = doc.get("LawSearch") if isinstance(doc, dict) else None
    if not isinstance(search, dict):
        raise ValueError("single lookup: LawSearch field missing")
    if "target" in search and text(search, "target") != "eflaw":
        raise ValueError("single lookup: unexpected target")
    if "page" in search and text(search, "page") != "1":
        raise ValueError("single lookup: unexpected page")
    try:
        total = int(text(search, "totalCnt"))
    except ValueError:
        raise ValueError("single lookup: totalCnt missing or invalid") from None
    raw_items = search.get("law")
    items = records(raw_items)
    if total == 0 and not items:
        raise ValueError("single lookup: no current law for requested ID")
    if total != 1 or len(items) != 1 or (isinstance(raw_items, list) and len(raw_items) != 1):
        raise ValueError("single lookup: ambiguous or incomplete result")
    item = items[0]
    if text(item, "법령ID") != law_id:
        raise ValueError("single lookup: requested ID mismatch")
    if not text(item, "법령명한글", "법령명_한글"):
        raise ValueError("single lookup: title missing")
    if text(item, "현행연혁코드") not in CURRENT_LABELS:
        raise ValueError("single lookup: current status missing or unsupported")
    check_date(text(item, "시행일자"), "single lookup effective date", today=today)
    if not text(item, "법령구분명") or not re.fullmatch(r"[0-9]+", text(item, "법령일련번호")):
        raise ValueError("single lookup: kind or official sequence missing")
    return item

def validate_detail(doc, item, today):
    law = doc.get("법령")
    basic = law.get("기본정보") if isinstance(law, dict) else None
    if not isinstance(basic, dict):
        raise ValueError("detail: 기본정보 missing")
    expected_title = text(item, "법령명한글", "법령명_한글")
    expected_id = text(item, "법령ID")
    expected_date = text(item, "시행일자")
    actual_title = text(basic, "법령명_한글", "법령명한글")
    actual_id = text(basic, "법령ID")
    actual_date = text(basic, "시행일자")
    if not expected_title or (actual_title, actual_id, actual_date) != (expected_title, expected_id, expected_date):
        raise ValueError("detail: title/ID/effective date mismatch")
    check_date(expected_date, "detail effective date", today=today)
    unit = law.get("조문", {}).get("조문단위") if isinstance(law.get("조문"), dict) else None
    units = records(unit)
    if isinstance(unit, list) and len(units) != len(unit):
        raise ValueError("detail: malformed article record")
    # Chapter headings (N or the official 전문 marker) may reuse numbering.
    articles = [v for v in units if text(v, "조문여부") not in {"N", "전문"}]
    if not articles:
        raise ValueError("detail: no article text")
    if any(not text(v, "조문내용") for v in articles):
        raise ValueError("detail: empty article text")
    seen_article_ids = set()
    for article in articles:
        no = text(article, "조문번호")
        sub_no = text(article, "조문가지번호") or "0"
        if (not re.fullmatch(r"[0-9]+", no) or not re.fullmatch(r"[0-9]+", sub_no)
                or isinstance(article.get("조문가지번호"), (dict, list))):
            raise ValueError("detail: missing or invalid article identifier")
        key = (no.lstrip("0") or "0", sub_no.lstrip("0") or "0")
        if key in seen_article_ids:
            raise ValueError("detail: duplicate article identifier")
        seen_article_ids.add(key)
    return len(articles)

def self_test():
    sample = [
        {"법령명한글": "샘플법", "법령ID": "100", "시행일자": "20200101", "법령구분명": "법률"},
        {"법령명한글": "샘플시행령", "법령ID": "200", "시행일자": "20200101", "법령구분명": "대통령령"},
        {"법령명한글": "샘플시행규칙", "법령ID": "300", "시행일자": "20200101", "법령구분명": "국토교통부령"}
    ]
    assert len(select_stratified(sample, 3)) == 3
    assert category(sample[2]) == "총리령·부령"
    detail = {"법령": {"기본정보": {"법령명_한글": "샘플법", "법령ID": "100", "시행일자": "20200101"},
                     "조문": {"조문단위": {"조문번호": "1", "조문내용": "제1조 샘플입니다."}}}}
    assert validate_detail(detail, sample[0], "20261009") == 1
    for bad in ("20261341", "20260230", "2026099a"):
        try:
            check_date(bad, "date")
            raise AssertionError("bad date accepted")
        except ValueError:
            pass
    bad_detail = json.loads(json.dumps(detail))
    bad_detail["법령"]["조문"]["조문단위"] = [
        {"조문번호":"1","조문가지번호":"0","조문내용":"조문 1"},
        {"조문번호":"1","조문가지번호":"0","조문내용":"조문 1 복제"}]
    try:
        validate_detail(bad_detail, sample[0], "20261009")
        raise AssertionError("duplicate article accepted")
    except ValueError as exc:
        assert "duplicate" in str(exc)
    clean, raw = sanitized({"url": "https://law.go.kr?a=1&OC=TOPSECRET&b=2"}, "TOPSECRET")
    assert b"TOPSECRET" not in raw and "REDACTED" in clean["url"]
    print("PASS: offline fixtures / calendar dates / duplicate articles / redaction")

def run(limit, out, law_id=None):
    if law_id is not None and not re.fullmatch(r"[0-9]+", law_id):
        raise RuntimeError("invalid law ID")
    if law_id is None and not 100 <= limit <= 200:
        raise RuntimeError("sample limit must be 100–200")
    if law_id is not None:
        limit = 1
    oc = os.getenv("LAW_OC", "").strip()
    if not oc:
        raise RuntimeError("LAW_OC secret missing; abort without live API requests")
    now = dt.datetime.now(ZoneInfo("Asia/Seoul"))
    today = now.strftime("%Y%m%d")
    out.mkdir(parents=True, exist_ok=True)
    if any(out.iterdir()):
        raise RuntimeError("pilot output folder is not empty; select a fresh output path")
    selection = None
    response_network = None
    stage = "catalog"
    try:
        if law_id is not None:
            stage = "single_lookup"
            # Official guide: LID filters the ID; nw=3 means currently in force.
            lookup = get_json("lawSearch.do", {"target":"eflaw", "LID":law_id,
                              "nw":"3", "display":"2", "page":"1"},
                              oc, f"single lookup {law_id}")
            response_network = getattr(lookup, 'network', None)
            stage = "lookup_validation"
            item = validate_single_lookup(lookup, law_id, today)
            _, lookup_raw = sanitized(lookup, oc)
            if len(lookup_raw) > MAX_DOCUMENT_BYTES:
                raise ValueError("single lookup: metadata exceeds file gate")
            stage = "lookup_storage"
            (out/"single_lookup.json").write_bytes(lookup_raw)
            selection = {"law_id":law_id, "method":SINGLE_LOOKUP_METHOD,
                         "lookup":{"path":"single_lookup.json", "bytes":len(lookup_raw),
                                   "sha256":hashlib.sha256(lookup_raw).hexdigest()}}
            choices, total = [item], None  # global official count was not queried
        else:
            items, total = catalog(oc, today=today)
            choices = select_stratified(items, limit)
        if len(choices) != limit:
            raise RuntimeError("not enough eligible official records for sample")
    except (RuntimeError, ValueError, OSError) as exc:
        # Record a failed start, without printing or storing OC, URLs, or raw error pages.
        message = "document storage failed" if isinstance(exc, OSError) else str(exc)
        failed = {"version":"V1.07", "mode":"trial-only / NOT COMMITTED",
                  "checked_at_kst":now.isoformat(), "requested":limit,
                  "verified":0, "failed":limit,
                  "phase":"single_lookup" if law_id is not None else "catalog",
                  "errors":[{"stage":stage,"code":error_code(exc) if isinstance(exc, RequestFailure) else error_code(message),"error":message}]}
        if isinstance(exc, RequestFailure):
            failed['errors'][0]['network'] = exc.network
        elif response_network is not None:
            failed['errors'][0]['network'] = response_network
        if law_id is not None:
            failed['laws'] = []
            failed['selection'] = {'law_id': law_id, 'method': SINGLE_LOOKUP_METHOD}
            failed['errors'][0]['id'] = law_id
        failed = sanitized(failed, oc)[0]
        (out/"manifest.json").write_text(
            json.dumps(failed,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
        raise RuntimeError(failed["errors"][0]["error"]) from None
    # Validate every requested record before success. Artifacts never go into the Git repo.
    (out/"laws").mkdir(parents=True, exist_ok=True)
    successes = []
    errors = []
    for n, item in enumerate(choices, 1):
        lid = text(item, "법령ID")
        response_network = None
        try:
            stage = "fetch"
            doc = get_json("lawService.do", {"target": "eflaw", "ID": lid},
                           oc, f"law detail {n}/{limit}")
            response_network = getattr(doc, 'network', None)
            stage = "validation"
            count = validate_detail(doc, item, today)
            _, payload = sanitized(doc, oc)
            digest = hashlib.sha256(payload).hexdigest()
            stage = "storage"
            sub = out/"laws"/lid[:2]
            sub.mkdir(parents=True, exist_ok=True)
            dest = sub/f"{lid}.json"
            storage = write_document(dest, payload)
            successes.append({"id":lid, "title":text(item,"법령명한글","법령명_한글"),
                "kind":text(item,"법령구분명"), "effective":text(item,"시행일자"),
                "articles":count, "sha256":digest, "bytes":len(payload),
                "path":str(dest.relative_to(out)), "source":f"https://www.law.go.kr/LSW/lsInfoP.do?lsiSeq={text(item,'법령일련번호')}"})
            if storage:
                successes[-1]["storage"] = storage
        except (RuntimeError, ValueError) as exc:
            errors.append({"id":lid, "stage":stage, "code":error_code(exc) if isinstance(exc, RequestFailure) else error_code(str(exc)), "error":str(exc)})
            if isinstance(exc, RequestFailure):
                errors[-1]['network'] = exc.network
            elif response_network is not None:
                errors[-1]['network'] = response_network
        except OSError:
            errors.append({"id":lid, "stage":stage, "code":"STORAGE_INTEGRITY_ERROR", "error":"document storage failed"})
        time.sleep(0.5)
    sizes = [x["bytes"] for x in successes]
    sample_categories = {key:0 for key in CATEGORY_NAMES}
    actual_categories = {key:0 for key in CATEGORY_NAMES}
    for item in choices:
        sample_categories[category(item)] += 1
    for row in successes:
        actual_categories[category({"법령구분명":row["kind"]})] += 1
    report = {"version":"V1.07", "mode":"trial-only / NOT COMMITTED",
        "checked_at_kst":now.isoformat(), "official_total":total, "requested":limit,
        "sample_by_category":sample_categories, "verified_by_category":actual_categories,
        "verified":len(successes), "failed":len(errors),
        "estimated_full_bytes_from_sample":round(sum(sizes)/len(sizes)*total) if sizes and total is not None else None,
        "max_document_bytes":max(sizes) if sizes else None, "laws":successes, "errors":errors,
        "warning":"A repository snapshot never guarantees law applicable to historical events."}
    if law_id is not None:
        report["selection"] = selection
        report["lookup_total"] = 1
    report = sanitized(report, oc)[0]
    (out/"manifest.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    if errors or len(successes) != limit:
        raise RuntimeError(f"trial INCOMPLETE: {len(successes)}/{limit}; see manifest.json")
    if law_id is not None:
        print(f"PASS: 1/1 validated, ID {law_id}, targeted current-law lookup, artifact only")
    else:
        print(f"PASS: {len(successes)}/{limit} validated, catalog total={total}, artifact only")

def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--limit", type=int, default=150)
    parser.add_argument("--law-id", help="With --run, verify one current law by LID and fetch its body by ID")
    parser.add_argument("--output", default="v107_pilot_artifact")
    args = parser.parse_args(argv)
    out = Path(args.output)
    # A rejected nonempty destination must remain untouched, including diagnostics.
    fresh = not out.exists() or (out.is_dir() and not any(out.iterdir()))
    result = 0
    fallback_code = 'RUN_ABORTED'
    try:
        if args.self_test:
            self_test()
        elif args.run:
            run(args.limit, out, args.law_id)
        else:
            parser.print_help()
    except (ValueError, RuntimeError, OSError) as exc:
        fallback_code = error_code(str(exc))
        print("FAIL: collection aborted; see safe diagnosis", file=sys.stderr)
        result = 1
    except Exception:
        print("FAIL: unexpected processing or storage error", file=sys.stderr)
        result = 1
    finally:
        if args.run:
            if fresh:
                try:
                    out.mkdir(parents=True, exist_ok=True)
                except OSError:
                    pass
                diagnostic = publish(out, 1 if args.law_id is not None else args.limit,
                                     fallback_code, allow_pass=result == 0)
                if diagnostic['status'] != 'PASS':
                    result = 1
            else:
                publish(out, 1 if args.law_id is not None else args.limit, 'OUTPUT_NOT_EMPTY',
                        use_manifest=False, write_artifact=False)
    return result


if __name__ == "__main__":
    sys.exit(main())
