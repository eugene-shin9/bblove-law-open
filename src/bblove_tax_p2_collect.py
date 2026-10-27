"""One user-authorized TAX P2 pilot on GitHub-hosted Actions, <=20 requests.

Only runtime uses the existing LAW_OC for API authentication. No credentials,
authenticated request URL, exception text, or credential-bearing raw bytes are
persisted. Exact unmodified official body/file bytes are preserved; search/list
responses are sanitized selection evidence and never counted as law originals.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import multiprocessing
import os
from pathlib import Path
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from bblove_storage import strict_json
from bblove_v107_pilot import validate_detail, records, text, sanitized, check_date
from bblove_tax_archive import TaxArchive, canonical, sha

API = "https://www.law.go.kr/DRF/"
NAMES = ("국세기본법", "국세기본법 시행령", "국세기본법 시행규칙", "부가가치세법")
MAX_REQUESTS = 20
MAX_BYTES = 20 * 1024 * 1024
MAX_TOTAL = 80 * 1024 * 1024
TIMEOUT = 30


class PilotBlocked(ValueError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class RemoteNetworkError(Exception):
    def __init__(self, exception_type, reason_type):
        self.exception_type=exception_type;self.reason_type=reason_type


def _network_worker(connection,url,headers,timeout,read_limit):
    """No exception text or authenticated URL leaves the isolated worker."""
    try:
        req=urllib.request.Request(url,headers=headers)
        with urllib.request.build_opener(NoRedirect()).open(req,timeout=timeout) as response:
            connection.send(('OK',response.status,response.headers.get('Content-Type',''),response.read(read_limit)))
    except urllib.error.HTTPError as exc:connection.send(('HTTP',exc.code))
    except (urllib.error.URLError,socket.timeout,TimeoutError,OSError) as exc:
        connection.send(('NETWORK',type(exc).__name__,type(getattr(exc,'reason',exc)).__name__))
    except Exception:connection.send(('NETWORK','WorkerError','Unclassified'))
    finally:connection.close()


def bounded_transport(request,timeout,*,worker=_network_worker):
    """Hard wall-clock deadline includes DNS, all address attempts and body read.

    Killing a single worker never retries the HTTP request. Credential-bearing
    URLs remain process memory, never command-line arguments or log messages.
    """
    context=multiprocessing.get_context('spawn')
    reader,writer=context.Pipe(duplex=False)
    read_limit=min(MAX_BYTES+1,getattr(request,'bblove_read_limit',MAX_BYTES+1))
    if not 1<=read_limit<=MAX_BYTES+1:raise PilotBlocked('RESPONSE_READ_LIMIT_INVALID')
    process=context.Process(target=worker,args=(writer,request.full_url,dict(request.header_items()),timeout,read_limit))
    process.start();writer.close()
    try:
        if not reader.poll(timeout):raise RemoteNetworkError('DeadlineExceeded','AbsoluteDeadline')
        try:message=reader.recv()
        except EOFError:raise RemoteNetworkError('WorkerError','NoResponse') from None
    finally:
        reader.close()
        if process.is_alive():process.terminate()
        process.join(timeout=1)
        if process.is_alive():process.kill();process.join(timeout=1)
    if message[0]=='HTTP':raise urllib.error.HTTPError('',message[1],'HTTP_ERROR',{},None)
    if message[0]=='NETWORK':raise RemoteNetworkError(message[1],message[2])
    response=io.BytesIO(message[3]);response.status=message[1]
    response.headers={'Content-Type':message[2]}
    return response


class BoundedClient:
    def __init__(self, credential, transport=None, *, request_limit=MAX_REQUESTS, api_origin=API):
        if api_origin not in {API,'https://law.go.kr/DRF/'} or not 1<=request_limit<=MAX_REQUESTS:
            raise PilotBlocked('ORIGIN_OR_REQUEST_BUDGET_INVALID')
        self.request_limit=request_limit
        self.api_origin=api_origin
        self._credential = credential
        self._transport = transport or bounded_transport
        self.receipts = []
        self.total_bytes = 0

    def get(self, path, params=None, *, label):
        if len(self.receipts) >= self.request_limit or self.total_bytes >= MAX_TOTAL:
            raise PilotBlocked("REQUEST_OR_TOTAL_SIZE_BUDGET")
        if params is not None:
            if path not in {"lawSearch.do", "lawService.do"}:
                raise PilotBlocked("ENDPOINT_NOT_APPROVED")
            safe_url = self.api_origin + path + "?" + urllib.parse.urlencode(params)
            url = self.api_origin + path + "?" + urllib.parse.urlencode({**params, "OC": self._credential})
        else:
            url = urllib.parse.urljoin("https://www.law.go.kr", path)
            p = urllib.parse.urlsplit(url)
            if p.scheme != "https" or p.hostname not in {"www.law.go.kr", "law.go.kr", "taxlaw.nts.go.kr"} or p.username or p.password or p.port not in (None, 443):
                raise PilotBlocked("DOWNLOAD_ORIGIN_NOT_APPROVED")
            if any(k in p.query.lower() for k in ("oc=", "token=", "secret=")):
                raise PilotBlocked("CREDENTIAL_LINK_REJECTED")
            safe_url = url
        receipt = {"request_no": len(self.receipts)+1, "label": label,
                   "source_url": safe_url, "started_at_utc": now(), "status": "STARTED",
                   "http_status": None, "bytes": 0, "sha256": None, "retry_count": 0}
        self.receipts.append(receipt)
        if os.getenv('GITHUB_ACTIONS')=='true':
            # Safe count before DNS/connect: preserves a lower bound in job logs
            # if the workflow itself times out before a final receipt exists.
            print(json.dumps({'request_started':receipt['request_no'],'label':label},ensure_ascii=False),flush=True)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "BBLOVE-TAX-P2/1.0", "Accept": "*/*"})
            req.bblove_read_limit=min(MAX_BYTES,MAX_TOTAL-self.total_bytes)+1
            with self._transport(req, timeout=TIMEOUT) as response:
                receipt["http_status"] = response.status
                receipt["content_type"] = response.headers.get("Content-Type", "").split(";", 1)[0]
                if response.status != 200:
                    raise PilotBlocked("HTTP_NOT_200")
                raw = response.read(min(MAX_BYTES, MAX_TOTAL-self.total_bytes)+1)
            self.total_bytes += len(raw)
            receipt["bytes"] = len(raw)
            if not 2 <= len(raw) <= MAX_BYTES or self.total_bytes > MAX_TOTAL:
                raise PilotBlocked("RESPONSE_SIZE_INVALID")
            receipt["sha256"] = sha(raw)
            receipt["status"] = "HTTP_BYTES_RECEIVED"
            return raw, receipt
        except urllib.error.HTTPError as exc:
            receipt.update(status="HTTP_ERROR", http_status=exc.code)
            raise PilotBlocked("HTTP_ERROR") from None
        except RemoteNetworkError as exc:
            receipt.update(status='NETWORK_OR_TIMEOUT_ERROR',exception_type=exc.exception_type,reason_type=exc.reason_type)
            raise PilotBlocked('NETWORK_OR_TIMEOUT_ERROR') from None
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
            reason=getattr(exc,'reason',exc)
            receipt.update(status="NETWORK_OR_TIMEOUT_ERROR",exception_type=type(exc).__name__,
                           reason_type=type(reason).__name__)
            raise PilotBlocked("NETWORK_OR_TIMEOUT_ERROR") from None
        except PilotBlocked as exc:
            receipt["status"] = str(exc)
            raise
        finally:
            receipt["completed_at_utc"] = now()
            if os.getenv('GITHUB_ACTIONS')=='true':
                print(json.dumps({'request_completed':receipt['request_no'],'status':receipt['status'],
                                  'http_status':receipt['http_status'],'bytes':receipt['bytes']},ensure_ascii=False),flush=True)

    def original_safe(self, raw):
        # Some list links echo OC. Never silently alter a claimed original.
        variants = {self._credential, urllib.parse.quote(self._credential, safe=""),
                    urllib.parse.quote_plus(self._credential)}
        if any(v and v.encode() in raw for v in variants) or re.search(rb"(?i)[?&]OC=", raw):
            raise PilotBlocked("CREDENTIAL_BEARING_ORIGINAL_NOT_PERSISTED")

    def json(self, endpoint, params, label):
        raw, receipt = self.get(endpoint, {**params, "type": "JSON"}, label=label)
        try:
            doc = strict_json(raw)
        except (ValueError, UnicodeError):
            receipt["status"] = "NON_JSON_OR_ERROR_PAGE"
            raise PilotBlocked("NON_JSON_OR_ERROR_PAGE") from None
        if not isinstance(doc, dict):
            raise PilotBlocked("JSON_OBJECT_REQUIRED")
        return doc, raw, receipt


def choose(doc, name, today, *, historical=False):
    root = doc.get("LawSearch", {})
    if not isinstance(root, dict):
        raise PilotBlocked("LIST_NOT_LAWSEARCH")
    found = []
    for row in records(root.get("law")):
        effective = text(row, "시행일자")
        if text(row, "법령명한글", "법령명_한글") != name:
            continue
        try:
            check_date(effective, "tax pilot effective", today=today)
        except ValueError:
            continue
        if not re.fullmatch(r"\d{6}", text(row, "법령ID")) or not text(row, "법령일련번호").isdigit():
            continue
        status = text(row, "현행연혁코드")
        if historical and status not in {"연혁", "1"}:
            continue
        if not historical and status not in {"현행", "3"}:
            continue
        found.append(row)
    if not found:
        raise PilotBlocked("EXACT_NAME_EDITION_NOT_FOUND")
    return max(found, key=lambda x:(text(x,"시행일자"),int(text(x,"법령일련번호"))))


def units(law, section, key):
    section = law.get(section, {})
    return records(section.get(key)) if isinstance(section, dict) else []


def detail_links(value):
    if isinstance(value,dict):
        for key,item in value.items():
            if '상세링크' in key and isinstance(item,str):yield item
            elif isinstance(item,(dict,list)):yield from detail_links(item)
    elif isinstance(value,list):
        for item in value:yield from detail_links(item)


def inspect_body(doc, selected, today):
    articles = validate_detail(doc, selected, today)  # existing quality contract
    law = doc["법령"]; basic = law["기본정보"]
    for key in ("공포번호", "공포일자"):
        value = text(basic, key)
        if not value or value != text(selected, key):
            raise PilotBlocked("PROMULGATION_LIST_BODY_MISMATCH")
    check_date(text(basic, "공포일자"), "tax publication", today=today)
    supplementary = units(law, "부칙", "부칙단위")
    annexes = units(law, "별표", "별표단위")
    law_id = text(basic, "법령ID")
    return {"name": text(basic, "법령명_한글", "법령명한글"), "law_id": law_id,
            "mst": text(selected,"법령일련번호"), "law_key": text(law,"법령키"),
            "effective_date": text(basic,"시행일자"), "promulgation_no": text(basic,"공포번호"),
            "promulgation_date": text(basic,"공포일자"), "article_count": articles,
            "supplementary_units": len(supplementary), "annex_units": len(annexes),
            "identity_check": "LIST_AND_BODY_FIELDS_MATCH",
            "mst_check": "LIST_AND_REQUEST_SELECTOR_MATCH_NOT_SEPARATE_BODY_FIELD",
            "supplementary_legal_applicability": "REVIEW_REQUIRED",
            "official_independent_crosscheck": "REVIEW_REQUIRED"}


def identity_from(row):
    ef=row["effective_date"]
    return {"law_id":row["law_id"],"edition":row["mst"]+":"+ef,
            "effective_date":ef[:4]+"-"+ef[4:6]+"-"+ef[6:],"promulgation_no":row["promulgation_no"]}


def pilot(output, client, *, run_id, head_sha):
    output=Path(output);output.mkdir(parents=True,exist_ok=False)
    archive=TaxArchive(output/"tax")
    result={"schema":"bblove-tax-p2-pilot-1","run_id":run_id,"head_sha":head_sha,
            "started_at_utc":now(),"request_limit":client.request_limit,"automatic_retries":0,
            "law_originals":[],"file_originals":[],"selection_evidence":[],"reviews":[],
            "approved_rules":0,"review_required_rules":2,"independent_audit":"NOT_EXECUTED"}
    today=datetime.now(timezone.utc).strftime("%Y%m%d");saved={}
    def review(label, error):
        result["reviews"].append({"label":label,"status":"REVIEW_REQUIRED","reason":str(error) if isinstance(error,PilotBlocked) else "STRUCTURAL_VALIDATION_FAILED"})
    def fetch_law(selected, label):
        params={"target":"eflaw","MST":text(selected,"법령일련번호"),"efYd":text(selected,"시행일자")}
        doc,raw,receipt=client.json("lawService.do",params,label)
        row=inspect_body(doc,selected,today);client.original_safe(raw)
        obj=archive.put(raw,kind="RAW_ORIGINAL",format="JSON",source_url=receipt["source_url"])
        # Body including returned supplements is exact; all annex files and
        # dependency closure are NOT complete yet. Never claim full manifest.
        key=archive.bind(identity_from(row),[obj],dependencies=[],collected_at=today[:4]+"-"+today[4:6]+"-"+today[6:],original_complete=False)
        row.update(object_sha256=obj["sha256"],bytes=len(raw),manifest_sha256=key,
                   source_url=receipt["source_url"],collected_at_utc=receipt["completed_at_utc"],
                   provenance="EXACT_HTTP_RESPONSE_BYTES",annex_files_complete=False,
                   dependency_inventory_status="REVIEW_REQUIRED",label=label)
        result["law_originals"].append(row);saved[label]=(doc,row,selected)
        receipt["status"]="VALIDATED_ORIGINAL_JSON"
        return doc,row,selected
    try:
        for name in NAMES:
            try:
                doc,raw,receipt=client.json("lawSearch.do",{"target":"eflaw","query":name,"nw":"3","display":"20","page":"1"},"current list "+name)
                clean,encoded=sanitized(doc,client._credential)
                evidence={"label":name,"type":"SANITIZED_LIST_NOT_ORIGINAL","raw_response_sha256":sha(raw),"safe_selection":choose(clean,name,today)}
                # sanitization is explicit; only a derived selection receipt
                result["selection_evidence"].append(evidence)
                fetch_law(evidence["safe_selection"],name)
            except (PilotBlocked,ValueError,KeyError,TypeError) as exc:
                review(name,exc)
        if "국세기본법 시행령" in saved:
            try:
                current=saved["국세기본법 시행령"][1]
                doc,_,_=client.json("lawSearch.do",{"target":"eflaw","LID":current["law_id"],"nw":"1","display":"20","page":"1"},"historical decree list")
                clean,_=sanitized(doc,client._credential)
                selected=choose(clean,"국세기본법 시행령",today,historical=True)
                if text(selected,"시행일자")>=current["effective_date"]:
                    raise PilotBlocked("HISTORICAL_EDITION_NOT_EARLIER")
                fetch_law(selected,"historical decree")
            except (PilotBlocked,ValueError,KeyError,TypeError) as exc:review("historical decree",exc)
        if "국세기본법" in saved:
            try:
                _,current,selected=saved["국세기본법"]
                doc,raw,receipt=client.json("lawService.do",{"target":"eflaw","ID":current["law_id"]},"ID current semantics")
                inspect_body(doc,selected,today);client.original_safe(raw)
                obj=archive.put(raw,kind="RAW_ORIGINAL",format="JSON",source_url=receipt["source_url"])
                result["id_semantics"]={"status":"ID_CURRENT_FIELDS_MATCH","efYd_used":False,"object_sha256":obj["sha256"],"bytes":len(raw)}
                raw,receipt=client.get("lawService.do",{"target":"eflaw","MST":current["mst"],"efYd":current["effective_date"],"type":"XML"},label="XML current base law")
                client.original_safe(raw)
                if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():raise PilotBlocked("UNSAFE_XML")
                xml=ET.fromstring(raw)
                basic=xml.find(".//기본정보")
                if basic is None or basic.findtext("법령ID")!=current["law_id"] or basic.findtext("시행일자")!=current["effective_date"] or basic.findtext("공포번호")!=current["promulgation_no"] or not xml.findall(".//조문단위"):
                    raise PilotBlocked("XML_IDENTITY_OR_BODY_MISMATCH")
                obj=archive.put(raw,kind="RAW_ORIGINAL",format="XML",source_url=receipt["source_url"])
                result["xml_original"]={**obj,"law_id":current["law_id"],"mst":current["mst"],"effective_date":current["effective_date"],"scope":"SAME_API_FORMAT_PARITY_ONLY_NOT_INDEPENDENT_LEGAL_REVIEW"}
            except (PilotBlocked,ValueError,KeyError,TypeError,ET.ParseError) as exc:review("ID_XML_semantics",exc)
        # At most two physical attachments discovered in the fetched law body.
        if "국세기본법 시행규칙" in saved:
            doc,parent,_=saved["국세기본법 시행규칙"]
            annexes=units(doc["법령"],"별표","별표단위")
            annexes.sort(key=lambda a:0 if re.search("가산|세액|납부|신고",text(a,"별표제목")) else 1)
            if annexes:
                annex=annexes[0]
                for field,fmt in (("별표서식PDF파일링크","PDF"),("별표서식파일링크","HWP")):
                    link=text(annex,field)
                    if not link:continue
                    try:
                        raw,receipt=client.get(link,label="official annex "+fmt)
                        client.original_safe(raw)
                        if fmt=="PDF" and not raw.startswith(b"%PDF-"):raise PilotBlocked("NOT_A_PDF")
                        if fmt=="HWP" and not raw.startswith(bytes.fromhex("d0cf11e0a1b11ae1")):
                            if raw.startswith(b"PK"):fmt="HWPX"
                            else:raise PilotBlocked("NOT_HWP_OR_HWPX")
                        obj=archive.put(raw,kind="RAW_ORIGINAL",format=fmt,source_url=receipt["source_url"])
                        result["file_originals"].append({**obj,"name":text(annex,"별표제목"),"annex_key":text(annex,"별표키"),"annex_effective_date":text(annex,"별표시행일자"),"parent_identity":identity_from(parent),"parent_manifest":parent["manifest_sha256"],"edition_link_status":"API_METADATA_ONLY_REVIEW_REQUIRED","collected_at_utc":receipt["completed_at_utc"]})
                    except (PilotBlocked,ValueError,KeyError,TypeError) as exc:review("annex "+fmt,exc)
        # Interpretation lists are separate legal-status evidence, not statutes.
        for target in ("ntsCgmExpc","moefCgmExpc"):
            try:
                doc,raw,receipt=client.json("lawSearch.do",{"target":target,"query":"가산세","display":"2","page":"1"},"interpretation list "+target)
                clean,encoded=sanitized(doc,client._credential)
                result["selection_evidence"].append({"target":target,"legal_status":"administrative_interpretation","type":"SANITIZED_LIST_NOT_LAW_ORIGINAL","raw_response_sha256":sha(raw),"data":clean,"body_status":"REVIEW_REQUIRED"})
                link=next(detail_links(clean),None)
                if link:
                    try:
                        # Only public official links without OC; no guessed
                        # undocumented JSON body endpoint or account login.
                        page,access=client.get(link,label="interpretation public detail "+target)
                        client.original_safe(page)
                        result.setdefault("interpretation_access",[]).append({"target":target,"source_url":access['source_url'],"http_status":access['http_status'],"bytes":len(page),"sha256":sha(page),"status":"REVIEW_REQUIRED","reason":"PUBLIC_PAGE_BYTES_ONLY_BODY_COMPLETENESS_UNVERIFIED"})
                    except (PilotBlocked,ValueError,KeyError,TypeError) as exc:review(target+" detail",exc)
                else:review(target+" detail",PilotBlocked("PUBLIC_DETAIL_LINK_UNAVAILABLE"))
            except (PilotBlocked,ValueError,KeyError,TypeError) as exc:review(target,exc)
    finally:
        original_keys={x["object_sha256"] for x in result["law_originals"]}
        original_keys.update(x["sha256"] for x in result["file_originals"])
        if "xml_original" in result:original_keys.add(result["xml_original"]["sha256"])
        if "id_semantics" in result:original_keys.add(result["id_semantics"]["object_sha256"])
        result.update(requests=client.receipts,actual_requests=len(client.receipts),download_bytes=client.total_bytes,
                      completed_at_utc=now(),status="PARTIAL_OFFICIAL_ORIGINALS" if result["law_originals"] else "NO_LAW_ORIGINALS_ACQUIRED",
                      original_files_count=len(original_keys),
                      official_independent_comparisons_completed=0,production_calculation="BLOCKED")
        (output/"pilot-result.json").write_bytes(canonical(result))
    return result


def main():
    p=argparse.ArgumentParser();p.add_argument("--output",type=Path,required=True)
    p.add_argument("--run-id",required=True);p.add_argument("--head-sha",required=True)
    p.add_argument("--confirmation",required=True)
    p.add_argument('--remaining-budget',type=int,default=20)
    p.add_argument('--api-origin',choices=[API,'https://law.go.kr/DRF/'],default=API)
    a=p.parse_args()
    if a.confirmation!="RUN_TAX_P2_20" or os.getenv("GITHUB_ACTIONS")!="true":
        raise SystemExit("LIVE_PILOT_GITHUB_ACTIONS_AND_CONFIRMATION_REQUIRED")
    credential=os.getenv("LAW_OC","").strip()
    if not credential:raise SystemExit("LAW_OC_NOT_INJECTED_NO_REQUESTS")
    try:r=pilot(a.output,BoundedClient(credential,request_limit=a.remaining_budget,api_origin=a.api_origin),run_id=a.run_id,head_sha=a.head_sha)
    except Exception:raise SystemExit("PILOT_INTERNAL_FAILURE_SEE_SAFE_ARTIFACT") from None
    print(json.dumps({k:r[k] for k in ("status","actual_requests","download_bytes","original_files_count","approved_rules")},ensure_ascii=False))
    if not r["law_originals"]:raise SystemExit(1)


if __name__=="__main__":main()
