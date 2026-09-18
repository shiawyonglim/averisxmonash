import os
import json
import re
import threading
from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn

# Import our pipeline functions
from pipeline.ai_engine import extract_shipping_fields, reason_and_verify_with_ai, classify_email, quick_classify, MODEL
from pipeline.comparator import compare_fields
from pipeline.edge_cases import check_wrong_doc_type, diagnose_attachment, is_si_attachment
from pipeline.parsers import extract_text
from pipeline.main import process_email

app = FastAPI(title="Shipping Document Verification API")

# Enable CORS for the React frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Adjust in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BUNDLE_DIR = os.path.join(BASE_DIR, "sdoc-hackathon-bundle")
INBOX_DIR = os.path.join(BUNDLE_DIR, "inbox")
SUBMISSION_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "submission.json")

# Word-boundary relevance check for BL-related emails. \bSI\b / \bBL\b will NOT
# match SINGAPORE, SIN525534192, MISSING, BILLING, etc.
BL_RELEVANT_RX = re.compile(r'\bBL\b|\bDRAFT\b|\bDOCS\b|\bSI\b|\bCONFIRM\b')

CARRIERS = ["MSC", "CMA", "HAPAG", "OOCL", "EVERGREEN", "ONE", "PIL", "YANG MING", "MONTER"]

# Carrier name -> regex matched against the SUBJECT only. Subjects encode
# carriers as CODE(ref) e.g. ONE(SINF07365118), EVER(EGLV...), YM(YMJAI...).
# Word boundaries prevent "ONE" matching PAPERONE/ZONE and "EVER" must not
# match "EVERY".
CARRIER_PATTERNS = {
    "MSC": r'\bMSC\b',
    "CMA": r'\bCMA\b',
    "HAPAG": r'\bHAPAG\b|\bHLCU\b',
    "OOCL": r'\bOOCL\b|\bOOLU\b',
    "EVERGREEN": r'\bEVER\s*\(|\bEVERGREEN\b|\bEGLV\b',
    "ONE": r'\bONE\s*\(',
    "PIL": r'\bPIL\b',
    "YANG MING": r'\bYM\s*\(|\bYANG\s*MING\b',
    "MONTER": r'\bMONTER\b|\bMCLS\b',
}

DEFECT_FIELDS = [
    "shipper", "consignee", "notify_party",
    "port_of_loading", "port_of_discharge",
    "container_count", "gross_weight_kg",
]


def _utcnow():
    return datetime.now(timezone.utc).isoformat()


def _email_exists(email_id):
    return bool(email_id) and os.path.exists(os.path.join(INBOX_DIR, f"{email_id}.json"))


def _is_bl_relevant_subject(subject):
    return bool(BL_RELEVANT_RX.search((subject or "").upper()))


def _detect_carrier(subj, body):
    text = (subj or "").upper()
    for c in CARRIERS:
        if re.search(CARRIER_PATTERNS[c], text):
            return c
    return "Shipping Line"


def _has_bl_attachment(atts):
    return any('BL' in a.upper() for a in atts)


@app.get("/api/emails")
def get_emails():
    try:
        files = sorted([f.replace('.json', '') for f in os.listdir(INBOX_DIR) if f.endswith('.json')])
        return {"emails": files}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/inbox")
def get_inbox(search: str = "", limit: int = 1000, page: int = 1):
    items = []
    files = sorted([f for f in os.listdir(INBOX_DIR) if f.endswith('.json')])
    for f in files:
        eid = f.replace('.json', '')
        path = os.path.join(INBOX_DIR, f)
        try:
            with open(path, 'r', encoding='utf-8') as fl:
                d = json.load(fl)
            subj = d.get("subject", "")
            sender = d.get("from", "")
            if search and (search.lower() not in subj.lower() and search.lower() not in sender.lower() and search.lower() not in eid.lower()):
                continue
            items.append({
                "email_id": eid,
                "subject": subj,
                "from": sender,
                "attachments_count": len(d.get("attachments", []))
            })
        except Exception:
            pass

    start = (page - 1) * limit
    end = start + limit
    return {
        "total": len(items),
        "page": page,
        "emails": items[start:end]
    }

VERDICTS_STORE = {}
CLASSIFICATIONS_STORE = {}

@app.get("/api/email/{email_id}")
def get_email_content(email_id: str):
    try:
        path = os.path.join(INBOX_DIR, f"{email_id}.json")
        if not os.path.exists(path):
            raise HTTPException(status_code=404, detail="Email not found")

        with open(path, 'r', encoding='utf-8') as f:
            email_data = json.load(f)

        atts = email_data.get("attachments", [])

        # Build attachment diagnostics
        att_meta = []
        for a in atts:
            full_a = os.path.join(BUNDLE_DIR, a)
            sz = os.path.getsize(full_a) if os.path.exists(full_a) else 0
            is_bad, reason = diagnose_attachment(full_a)
            att_meta.append({
                "path": a,
                "size": sz,
                "is_corrupt": is_bad,
                "reason": reason
            })

        category = classify_email(email_data.get("subject", ""), email_data.get("body", ""), len(atts) > 0)
        CLASSIFICATIONS_STORE[email_id] = category
        email_info = {
            "email_id": email_id,
            "from": email_data.get("from", "Unknown"),
            "subject": email_data.get("subject", "No subject"),
            "body": email_data.get("body", ""),
            "attachments": att_meta,
            "category": category
        }

        if len(atts) < 2:
            return {
                "email": email_info,
                "si_text": "[MISSING ATTACHMENT: Email does not carry the required 2 shipping attachments]",
                "bl_text": "[MISSING ATTACHMENT: Bill of Lading draft is absent from this request]",
                "is_corrupted": True,
                "corrupt_reason": "missing_attachment",
                "issue_details": f"Only {len(atts)} attachment(s) provided.",
                "verdict": VERDICTS_STORE.get(email_id),
                "resolution": RESOLUTIONS_STORE.get(email_id)
            }

        path1 = os.path.join(BUNDLE_DIR, atts[0])
        path2 = os.path.join(BUNDLE_DIR, atts[1])

        is_bad1, reason1 = diagnose_attachment(path1)
        is_bad2, reason2 = diagnose_attachment(path2)

        text1 = extract_text(path1) if not is_bad1 else f"[CORRUPTED FILE: {atts[0]} is unreadable - {reason1}]"
        text2 = extract_text(path2) if not is_bad2 else f"[CORRUPTED FILE: {atts[1]} is unreadable - {reason2}]"

        is_corrupt = is_bad1 or is_bad2
        corrupt_reason = reason1 or reason2
        issue_detail = f"{atts[1] if is_bad2 else atts[0]}: {corrupt_reason}" if is_corrupt else None

        if is_si_attachment(path1, text1):
            return {
                "email": email_info,
                "si_text": text1,
                "bl_text": text2,
                "is_corrupted": is_corrupt,
                "corrupt_reason": "unreadable" if is_corrupt else None,
                "issue_details": issue_detail,
                "verdict": VERDICTS_STORE.get(email_id),
                "resolution": RESOLUTIONS_STORE.get(email_id)
            }
        else:
            return {
                "email": email_info,
                "si_text": text2,
                "bl_text": text1,
                "is_corrupted": is_corrupt,
                "corrupt_reason": "unreadable" if is_corrupt else None,
                "issue_details": issue_detail,
                "verdict": VERDICTS_STORE.get(email_id),
                "resolution": RESOLUTIONS_STORE.get(email_id)
            }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class ResolveRequest(BaseModel):
    email_id: str
    resolutions: dict
    notes: str = ""
    resolved_by: str = "Shipping Operator"

RESOLUTIONS_STORE = {}

@app.post("/api/resolve")
def resolve_mismatch(req: ResolveRequest):
    if not _email_exists(req.email_id):
        raise HTTPException(status_code=404, detail=f"Email {req.email_id} not found")
    RESOLUTIONS_STORE[req.email_id] = {
        "email_id": req.email_id,
        "resolutions": req.resolutions,
        "notes": req.notes,
        "resolved_by": req.resolved_by,
        "timestamp": _utcnow(),
        "status": "RESOLVED"
    }
    return {
        "status": "SUCCESS",
        "message": f"Discrepancies for {req.email_id} resolved successfully.",
        "record": RESOLUTIONS_STORE[req.email_id]
    }

@app.get("/api/resolutions")
def get_resolutions():
    return {"resolutions": RESOLUTIONS_STORE}

CORRUPTED_STORE = {}

@app.get("/api/corrupted")
def get_corrupted_files():
    # Issue detection is single-sourced in _ensure_corrupted_cache /
    # _email_corrupt_issue (defined below; resolved at call time).
    _ensure_corrupted_cache()
    corrupted_list = []
    for f in sorted(os.listdir(INBOX_DIR)):
        if not f.endswith('.json'):
            continue
        eid = f.replace('.json', '')
        issue = CORRUPTED_CACHE.get(eid)
        if not issue:
            continue

        try:
            with open(os.path.join(INBOX_DIR, f), 'r', encoding='utf-8') as fl:
                email_data = json.load(fl)
        except Exception:
            email_data = {}

        atts = email_data.get("attachments", [])
        corrupted_list.append({
            "email_id": eid,
            "subject": email_data.get("subject", "No subject"),
            "from": email_data.get("from", "Unknown"),
            "attachments": [
                {
                    "path": a,
                    "size": os.path.getsize(os.path.join(BUNDLE_DIR, a)) if os.path.exists(os.path.join(BUNDLE_DIR, a)) else 0
                } for a in atts
            ],
            "issue": issue,
            "status": CORRUPTED_STORE.get(eid, {}).get("status", "NEEDS_REVIEW"),
            "resolution": CORRUPTED_STORE.get(eid, {}).get("notes", None)
        })

    return {"corrupted": corrupted_list}

@app.post("/api/corrupted/resolve")
def resolve_corrupted(payload: dict):
    eid = payload.get("email_id")
    if not _email_exists(eid):
        raise HTTPException(status_code=404, detail=f"Email {eid} not found")
    action = payload.get("action", "ESCALATED")
    notes = payload.get("notes", "")
    CORRUPTED_STORE[eid] = {
        "status": action,
        "notes": notes,
        "updated_at": _utcnow()
    }
    return {"status": "SUCCESS", "message": f"{eid} marked as {action}"}

CHASERS_STORE = {}

@app.get("/api/missing-bills")
def get_missing_bills(carrier: str = "", search: str = ""):
    missing = []
    files = sorted([f for f in os.listdir(INBOX_DIR) if f.endswith('.json')])
    for f in files:
        eid = f.replace('.json', '')
        path = os.path.join(INBOX_DIR, f)
        try:
            with open(path, 'r', encoding='utf-8') as fl:
                d = json.load(fl)
            atts = d.get('attachments', [])
            has_bl = _has_bl_attachment(atts)
            subj = d.get('subject', '')
            body = d.get('body', '')

            is_bl_relevant = _is_bl_relevant_subject(subj)

            if not has_bl and is_bl_relevant:
                carrier_name = _detect_carrier(subj, body)

                ref_match = re.search(r'([0-9A-Z]{3,}-[0-9A-Z]{4,}|[A-Z]{3,}[0-9]{6,})', subj)
                ref_no = ref_match.group(1) if ref_match else "PO / Booking Ref"

                if search and (search.lower() not in subj.lower() and search.lower() not in d.get('from','').lower() and search.lower() not in eid.lower() and search.lower() not in ref_no.lower()):
                    continue
                if carrier and carrier.upper() not in carrier_name.upper():
                    continue

                missing_type = "Draft BL Missing (0 attachments)" if len(atts) == 0 else f"SI Received ({len(atts)} doc) - Draft BL Missing"

                missing.append({
                    "email_id": eid,
                    "subject": subj,
                    "from": d.get("from", "Unknown"),
                    "body": body,
                    "carrier": carrier_name,
                    "ref_no": ref_no,
                    "missing_type": missing_type,
                    "attachments": atts,
                    "status": CHASERS_STORE.get(eid, {}).get("status", "AWAITING_DRAFT_BL"),
                    "chaser_sent_at": CHASERS_STORE.get(eid, {}).get("chaser_sent_at", None),
                    "chaser_notes": CHASERS_STORE.get(eid, {}).get("notes", None)
                })
        except Exception:
            pass

    return {
        "total": len(missing),
        "missing_bills": missing
    }

@app.post("/api/missing-bills/chase")
def chase_missing_bill(payload: dict):
    eid = payload.get("email_id")
    if not _email_exists(eid):
        raise HTTPException(status_code=404, detail=f"Email {eid} not found")
    CHASERS_STORE[eid] = {
        "status": "CHASER_DISPATCHED",
        "chaser_sent_at": _utcnow(),
        "notes": payload.get("notes", "Urgent reminder sent to carrier requesting draft BL.")
    }
    return {
        "status": "SUCCESS",
        "message": f"Chaser dispatched to carrier for {eid}",
        "record": CHASERS_STORE[eid]
    }

@app.post("/api/missing-bills/batch-chase")
def batch_chase_missing_bills(payload: dict):
    eids = payload.get("email_ids", [])
    notes = payload.get("notes", "Urgent batch reminder sent to ocean carrier operations.")
    updated = []
    now = _utcnow()
    for eid in eids:
        if _email_exists(eid):
            CHASERS_STORE[eid] = {
                "status": "CHASER_DISPATCHED",
                "chaser_sent_at": now,
                "notes": notes
            }
            updated.append(eid)
    return {
        "status": "SUCCESS",
        "message": f"Dispatched chasers for {len(updated)} missing bills.",
        "updated_count": len(updated)
    }

class VerificationRequest(BaseModel):
    si_text: str
    bl_text: str
    email_id: str = ""

def _store_verdict(email_id, resp):
    if not email_id:
        return
    VERDICTS_STORE[email_id] = {
        "status": resp.get("status"),
        "review_reason": resp.get("review_reason"),
        "defect_fields": resp.get("defect_fields", []),
        "si_fields": resp.get("si_fields", {}),
        "bl_fields": resp.get("bl_fields", {}),
        "field_comparisons": resp.get("field_comparisons", {}),
        "thoughts": resp.get("thoughts"),
        "summary_reason": resp.get("summary_reason"),
        "verified_at": _utcnow()
    }

@app.post("/api/verify")
def verify_documents(req: VerificationRequest):
    si_text = req.si_text.strip()
    bl_text = req.bl_text.strip()

    if not si_text or not bl_text:
        raise HTTPException(status_code=400, detail="Both SI and BL text must be provided.")

    # Check for Corrupted or Missing Attachment markers
    if "[CORRUPTED" in si_text or "[CORRUPTED" in bl_text or "[MISSING ATTACHMENT" in si_text or "[MISSING ATTACHMENT" in bl_text:
        reason = "unreadable" if "[CORRUPTED" in (si_text + bl_text) else "missing_attachment"
        resp = {
            "status": "NEEDS_REVIEW",
            "review_reason": reason,
            "thoughts": f"Document integrity check detected an unreadable or corrupt attachment ({reason}). The verification engine deliberately halts automated comparison to prevent hallucinated audits.",
            "summary_reason": f"Escalated to human review: Attachment document is {reason}.",
            "si_fields": {},
            "bl_fields": {},
            "defect_fields": [],
            "field_comparisons": {}
        }
        _store_verdict(req.email_id, resp)
        return resp

    # Check Edge Case
    wrong_doc_err = check_wrong_doc_type(si_text, bl_text)
    if wrong_doc_err:
        resp = {
            "status": wrong_doc_err[0],
            "review_reason": wrong_doc_err[1],
            "thoughts": "Document header inspection detected an invalid document type (e.g. Commercial Invoice or Packing List instead of SI/BL). Escalated to human review.",
            "summary_reason": f"Escalated to human review due to {wrong_doc_err[1]}.",
            "si_fields": {},
            "bl_fields": {},
            "defect_fields": [],
            "field_comparisons": {}
        }
        _store_verdict(req.email_id, resp)
        return resp

    # Extract Fields via NVIDIA AI
    si_fields = extract_shipping_fields(si_text)
    bl_fields = extract_shipping_fields(bl_text)

    # Extraction failures return "ERROR" placeholders - never treat as a match
    # (two all-ERROR dicts would otherwise compare equal -> false OK)
    extracted = list(si_fields.values()) + list(bl_fields.values())
    if any(str(v).strip().upper() == "ERROR" for v in extracted):
        resp = {
            "status": "NEEDS_REVIEW",
            "review_reason": "unreadable",
            "thoughts": "AI field extraction failed and returned 'ERROR' placeholders. Halting automated comparison to prevent a false match.",
            "summary_reason": "Escalated to human review: document text could not be extracted.",
            "si_fields": si_fields,
            "bl_fields": bl_fields,
            "defect_fields": [],
            "field_comparisons": {}
        }
        _store_verdict(req.email_id, resp)
        return resp

    # Compare Fields with Smart Normalization
    defect_fields, is_missing_val, field_comparisons = compare_fields(si_fields, bl_fields)

    # Missing values are escalated deterministically - skip the AI reasoning call
    if is_missing_val:
        resp = {
            "status": "NEEDS_REVIEW",
            "review_reason": "missing_value",
            "thoughts": "Required shipping field contains blank or unreadable tokens.",
            "summary_reason": "Required field is missing or placeholder value.",
            "si_fields": si_fields,
            "bl_fields": bl_fields,
            "defect_fields": defect_fields,
            "field_comparisons": field_comparisons
        }
        _store_verdict(req.email_id, resp)
        return resp

    # Reason and think first using AI
    ai_reasoning = reason_and_verify_with_ai(
        si_fields, bl_fields, defect_fields, is_missing_val, field_comparisons
    )

    has_defect = len(defect_fields) > 0
    status = "MISMATCH" if has_defect else "OK"

    resp = {
        "status": status,
        "review_reason": None,
        "thoughts": ai_reasoning.get("thoughts", "Carefully analyzed field values across documents."),
        "summary_reason": ai_reasoning.get("summary_reason", "All fields verified successfully." if not has_defect else "Defect detected in specified fields."),
        "si_fields": si_fields,
        "bl_fields": bl_fields,
        "defect_fields": defect_fields,
        "field_comparisons": field_comparisons
    }
    _store_verdict(req.email_id, resp)
    return resp


# ---------------------------------------------------------------------------
# Corrupted-attachment cache (lazy full scan on first use), shared by
# /api/queue and /api/stats.
# ---------------------------------------------------------------------------
CORRUPTED_CACHE = {}
_CORRUPTED_SCANNED = False


def _email_corrupt_issue(email_data):
    """
    Returns an issue string or None.
    - Any attachment failing diagnose_attachment -> that reason.
    - Exactly 1 attachment -> 'missing_attachment' (e.g. email_507/509 carry
      only the SI; their draft BL is missing regardless of subject wording).
    - 0 attachments -> 'missing_attachment' ONLY when the email is a
      BL-comparison request whose documents were dropped, i.e. the body
      explicitly references both an SI and a BL as standalone words
      (e.g. 'compare the SI and draft BL' in email_506/508/510). Plain
      'please send the draft BL' requests are handled by the missing-bills
      queue instead.
    """
    atts = email_data.get("attachments", [])
    for a in atts:
        is_bad, reason = diagnose_attachment(os.path.join(BUNDLE_DIR, a))
        if is_bad:
            return reason
    if len(atts) == 1:
        return "missing_attachment"
    if len(atts) == 0:
        body = (email_data.get("body", "") or "").upper()
        if re.search(r'\bSI\b', body) and re.search(r'\bBL\b', body):
            return "missing_attachment"
    return None


def _ensure_corrupted_cache():
    global _CORRUPTED_SCANNED
    if _CORRUPTED_SCANNED:
        return
    for f in sorted(os.listdir(INBOX_DIR)):
        if not f.endswith('.json'):
            continue
        eid = f.replace('.json', '')
        try:
            with open(os.path.join(INBOX_DIR, f), 'r', encoding='utf-8') as fl:
                d = json.load(fl)
            CORRUPTED_CACHE[eid] = _email_corrupt_issue(d)
        except Exception:
            CORRUPTED_CACHE[eid] = "Unreadable email record"
    _CORRUPTED_SCANNED = True


def _queue_item(eid, d):
    atts = d.get("attachments", [])
    subj = d.get("subject", "")
    body = d.get("body", "")
    verdict = VERDICTS_STORE.get(eid)
    verdict_status = verdict.get("status") if verdict else None
    resolved = eid in RESOLUTIONS_STORE
    corrupt_issue = CORRUPTED_CACHE.get(eid)
    corrupted = corrupt_issue is not None
    has_bl = _has_bl_attachment(atts)
    missing_bl = (not has_bl) and _is_bl_relevant_subject(subj)
    category = CLASSIFICATIONS_STORE.get(eid) or quick_classify(subj, body, len(atts) > 0)
    chaser_status = CHASERS_STORE.get(eid, {}).get("status")
    corrupt_action = CORRUPTED_STORE.get(eid, {}).get("status")

    if resolved:
        queue_status = "RESOLVED"
    elif verdict_status:
        queue_status = verdict_status
    elif corrupted:
        queue_status = "CORRUPTED"
    elif missing_bl:
        queue_status = "MISSING_BL"
    else:
        queue_status = "UNVERIFIED"

    return {
        "email_id": eid,
        "subject": subj,
        "from": d.get("from", "Unknown"),
        "attachments_count": len(atts),
        "category": category,
        "has_bl": has_bl,
        "corrupted": corrupted,
        "corrupt_issue": corrupt_issue,
        "verdict_status": verdict_status,
        "resolved": resolved,
        "chaser_status": chaser_status,
        "corrupt_action": corrupt_action,
        "queue_status": queue_status,
        "_missing_bl": missing_bl,
    }


def _queue_match(item, flt):
    if flt == "all":
        return True
    if flt == "mismatch":
        return item["verdict_status"] == "MISMATCH" and not item["resolved"]
    if flt == "needs_review":
        return item["verdict_status"] == "NEEDS_REVIEW" or item["corrupted"]
    if flt == "missing_bl":
        return item["_missing_bl"]
    if flt == "corrupted":
        return item["corrupted"]
    if flt == "resolved":
        return item["resolved"]
    if flt == "chaser_sent":
        return item["chaser_status"] is not None
    return True


@app.get("/api/queue")
def get_queue(filter: str = "all", search: str = "", page: int = 1, limit: int = 50):
    _ensure_corrupted_cache()
    items = []
    files = sorted([f for f in os.listdir(INBOX_DIR) if f.endswith('.json')])
    for f in files:
        eid = f.replace('.json', '')
        try:
            with open(os.path.join(INBOX_DIR, f), 'r', encoding='utf-8') as fl:
                d = json.load(fl)
        except Exception:
            continue
        items.append(_queue_item(eid, d))

    # Counts are computed over ALL emails (not affected by page/search),
    # while 'mismatch' still respects the "not resolved" rule.
    counts = {
        "all": len(items),
        "mismatch": sum(1 for i in items if _queue_match(i, "mismatch")),
        "needs_review": sum(1 for i in items if _queue_match(i, "needs_review")),
        "missing_bl": sum(1 for i in items if _queue_match(i, "missing_bl")),
        "corrupted": sum(1 for i in items if _queue_match(i, "corrupted")),
        "resolved": sum(1 for i in items if _queue_match(i, "resolved")),
        "chaser_sent": sum(1 for i in items if _queue_match(i, "chaser_sent")),
    }

    filtered = [i for i in items if _queue_match(i, filter)]
    if search:
        s = search.lower()
        filtered = [i for i in filtered if s in i["email_id"].lower()
                    or s in i["subject"].lower() or s in i["from"].lower()]

    total = len(filtered)
    start = (page - 1) * limit
    page_items = filtered[start:start + limit]
    for i in page_items:
        i.pop("_missing_bl", None)

    return {
        "total": total,
        "page": page,
        "limit": limit,
        "counts": counts,
        "emails": page_items
    }


@app.get("/api/config")
def get_config():
    return {"provider": "NVIDIA NIM", "model": MODEL}

@app.get("/api/stats")
def get_stats():
    _ensure_corrupted_cache()
    files = sorted([f for f in os.listdir(INBOX_DIR) if f.endswith('.json')])

    status_counts = {"OK": 0, "MISMATCH": 0, "NEEDS_REVIEW": 0, "RESOLVED": 0, "UNVERIFIED": 0}
    defect_field_counts = {k: 0 for k in DEFECT_FIELDS}
    categories = {}
    missing_bl_total = 0
    missing_bl_by_carrier = {}
    corrupted_total = 0

    for f in files:
        eid = f.replace('.json', '')
        try:
            with open(os.path.join(INBOX_DIR, f), 'r', encoding='utf-8') as fl:
                d = json.load(fl)
        except Exception:
            continue

        # Status rollup: RESOLVED > verdict status > UNVERIFIED
        if eid in RESOLUTIONS_STORE:
            status_counts["RESOLVED"] += 1
        elif eid in VERDICTS_STORE:
            st = VERDICTS_STORE[eid].get("status")
            status_counts[st if st in status_counts else "UNVERIFIED"] += 1
        else:
            status_counts["UNVERIFIED"] += 1

        category = CLASSIFICATIONS_STORE.get(eid) or quick_classify(
            d.get("subject", ""), d.get("body", ""), len(d.get("attachments", [])) > 0)
        categories[category] = categories.get(category, 0) + 1

        atts = d.get("attachments", [])
        if not _has_bl_attachment(atts) and _is_bl_relevant_subject(d.get("subject", "")):
            missing_bl_total += 1
            c = _detect_carrier(d.get("subject", ""), d.get("body", ""))
            missing_bl_by_carrier[c] = missing_bl_by_carrier.get(c, 0) + 1

        if CORRUPTED_CACHE.get(eid):
            corrupted_total += 1

    for v in VERDICTS_STORE.values():
        for fld in v.get("defect_fields", []) or []:
            if fld in defect_field_counts:
                defect_field_counts[fld] += 1
            else:
                defect_field_counts[fld] = defect_field_counts.get(fld, 0) + 1

    return {
        "emails_total": len(files),
        "verified_count": len(VERDICTS_STORE),
        "status_counts": status_counts,
        "defect_fields": defect_field_counts,
        "categories": categories,
        "missing_bl_total": missing_bl_total,
        "missing_bl_by_carrier": missing_bl_by_carrier,
        "corrupted_total": corrupted_total,
        "chasers_sent": len(CHASERS_STORE),
        "resolutions": len(RESOLUTIONS_STORE)
    }


@app.get("/api/audit")
def get_audit():
    events = []

    for eid, v in VERDICTS_STORE.items():
        events.append({
            "email_id": eid,
            "action": "AUTO_VERIFIED",
            "actor": "AI Verification Engine",
            "notes": v.get("summary_reason"),
            "timestamp": v.get("verified_at"),
            "details": {
                "status": v.get("status"),
                "review_reason": v.get("review_reason"),
                "defect_fields": v.get("defect_fields")
            }
        })

    for eid, r in RESOLUTIONS_STORE.items():
        events.append({
            "email_id": eid,
            "action": "RESOLVED",
            "actor": r.get("resolved_by", "Shipping Operator"),
            "notes": r.get("notes"),
            "timestamp": r.get("timestamp"),
            "details": {"resolutions": r.get("resolutions")}
        })

    for eid, c in CORRUPTED_STORE.items():
        events.append({
            "email_id": eid,
            "action": c.get("status"),
            "actor": "Shipping Operator",
            "notes": c.get("notes"),
            "timestamp": c.get("updated_at"),
            "details": {"status": c.get("status")}
        })

    for eid, ch in CHASERS_STORE.items():
        events.append({
            "email_id": eid,
            "action": "CHASER_DISPATCHED",
            "actor": "Shipping Operator",
            "notes": ch.get("notes"),
            "timestamp": ch.get("chaser_sent_at"),
            "details": {"status": ch.get("status")}
        })

    events.sort(key=lambda e: e.get("timestamp") or "", reverse=True)
    return {"events": events}


# ---------------------------------------------------------------------------
# Batch pipeline runner (submission generation)
# ---------------------------------------------------------------------------
PIPELINE_STATE = {
    "running": False, "processed": 0, "total": 0, "current": None,
    "done": False, "error": None, "skipped": 0,
    "finished_at": None, "supabase": None, "cancel_requested": False,
}
SUBMISSION_STORE = {}
_SUBMISSION_META = {}


def _write_submission():
    with open(SUBMISSION_PATH, 'w', encoding='utf-8') as f:
        json.dump(SUBMISSION_STORE, f, indent=2)


def _load_submission_file():
    if not os.path.exists(SUBMISSION_PATH):
        return {}
    try:
        with open(SUBMISSION_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_to_supabase():
    """
    Best-effort upsert of the submission results into the `verifications`
    table via the PostgREST API. Status mapping:
    OK -> AUTO_VERIFIED, MISMATCH -> FLAGGED_MISMATCH, NEEDS_REVIEW stays.
    """
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_ANON_KEY")
    if not url or not key or not SUBMISSION_STORE:
        return "skipped (no credentials or empty submission)"

    status_map = {"OK": "AUTO_VERIFIED", "MISMATCH": "FLAGGED_MISMATCH", "NEEDS_REVIEW": "NEEDS_REVIEW"}
    rows = []
    for eid, r in SUBMISSION_STORE.items():
        meta = _SUBMISSION_META.get(eid, {})
        verdict = VERDICTS_STORE.get(eid, {})
        rows.append({
            "id": eid,
            "subject": meta.get("subject"),
            "sender": meta.get("sender"),
            "category": r.get("category") or "GENERAL",
            "status": status_map.get(r.get("status"), "PENDING"),
            "review_reason": r.get("review_reason"),
            "defect_fields": r.get("defect_fields") or [],
            "has_discrepancy": bool(r.get("has_defect")),
            "si_data": verdict.get("si_fields") or {},
            "bl_data": verdict.get("bl_fields") or {},
            "discrepancies": verdict.get("field_comparisons") or {},
            "updated_at": _utcnow(),
        })

    try:
        import httpx
        endpoint = f"{url.rstrip('/')}/rest/v1/verifications"
        headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates",
        }
        for i in range(0, len(rows), 100):
            resp = httpx.post(endpoint, headers=headers, json=rows[i:i + 100], timeout=30.0)
            if resp.status_code >= 300:
                return f"error (HTTP {resp.status_code}: {resp.text[:200]})"
        return f"saved ({len(rows)} rows)"
    except Exception as e:
        return f"error ({e})"


PIPELINE_BATCH_SIZE = 10   # emails per batch
PIPELINE_WORKERS = 4       # concurrent LLM calls within a batch


def _process_email_file(f):
    """Process a single inbox file. Runs inside the batch thread pool."""
    eid = f.replace('.json', '')
    try:
        with open(os.path.join(INBOX_DIR, f), 'r', encoding='utf-8') as fl:
            email_data = json.load(fl)
        meta = {
            "subject": email_data.get("subject", ""),
            "sender": email_data.get("from", ""),
        }
        try:
            result, si_fields, bl_fields = process_email(email_data)
        except Exception:
            result, si_fields, bl_fields = {
                "category": "GENERAL",
                "status": "NEEDS_REVIEW",
                "review_reason": "unreadable",
                "has_defect": False,
                "defect_fields": []
            }, {}, {}
        return eid, meta, result, si_fields, bl_fields
    except Exception:
        return eid, {}, {
            "category": "GENERAL",
            "status": "NEEDS_REVIEW",
            "review_reason": "unreadable",
            "has_defect": False,
            "defect_fields": []
        }, {}, {}


def _pipeline_worker(files):
    from concurrent.futures import ThreadPoolExecutor
    try:
        num_batches = (len(files) + PIPELINE_BATCH_SIZE - 1) // PIPELINE_BATCH_SIZE
        for i in range(0, len(files), PIPELINE_BATCH_SIZE):
            if PIPELINE_STATE["cancel_requested"]:
                PIPELINE_STATE["error"] = "cancelled"
                break
            batch = files[i:i + PIPELINE_BATCH_SIZE]
            PIPELINE_STATE["current"] = f"batch {i // PIPELINE_BATCH_SIZE + 1}/{num_batches}"
            with ThreadPoolExecutor(max_workers=PIPELINE_WORKERS) as pool:
                for eid, meta, result, si_fields, bl_fields in pool.map(_process_email_file, batch):
                    _SUBMISSION_META[eid] = meta
                    SUBMISSION_STORE[eid] = result
                    if result.get("category"):
                        CLASSIFICATIONS_STORE[eid] = result["category"]
                    VERDICTS_STORE[eid] = {
                        "status": result.get("status"),
                        "review_reason": result.get("review_reason"),
                        "defect_fields": result.get("defect_fields", []),
                        "si_fields": si_fields or {},
                        "bl_fields": bl_fields or {},
                        "field_comparisons": {},
                        "thoughts": None,
                        "summary_reason": None,
                        "verified_at": _utcnow(),
                    }
                    PIPELINE_STATE["processed"] += 1
            _write_submission()  # checkpoint after each batch
    except Exception as e:
        PIPELINE_STATE["error"] = str(e)
    finally:
        try:
            _write_submission()
            PIPELINE_STATE["supabase"] = _save_to_supabase()
        except Exception as e:
            PIPELINE_STATE["error"] = PIPELINE_STATE["error"] or str(e)
        PIPELINE_STATE["done"] = True
        PIPELINE_STATE["running"] = False
        PIPELINE_STATE["current"] = None
        PIPELINE_STATE["finished_at"] = _utcnow()


class PipelineRunRequest(BaseModel):
    max_emails: int = 0
    resume: bool = True


@app.post("/api/pipeline/run")
def run_pipeline(req: PipelineRunRequest):
    if PIPELINE_STATE["running"]:
        return {"started": False, "message": "already running"}

    files = sorted([f for f in os.listdir(INBOX_DIR) if f.endswith('.json')])
    if req.max_emails and req.max_emails > 0:
        files = files[:req.max_emails]

    # Resume: skip emails already present in submission.json so re-runs
    # don't re-spend API calls. Send resume=false (or delete the file)
    # to force a fresh run.
    skipped = 0
    if req.resume:
        existing = _load_submission_file()
        SUBMISSION_STORE.update(existing)
        before = len(files)
        files = [f for f in files if f.replace('.json', '') not in existing]
        skipped = before - len(files)

    PIPELINE_STATE.update({
        "running": True,
        "processed": 0,
        "total": len(files),
        "current": None,
        "done": False,
        "error": None,
        "skipped": skipped,
        "finished_at": None,
        "supabase": None,
        "cancel_requested": False,
    })
    threading.Thread(target=_pipeline_worker, args=(files,), daemon=True).start()
    msg = f"started ({len(files)} to process"
    if skipped:
        msg += f", resumed with {skipped} already done"
    return {"started": True, "total": len(files), "skipped": skipped, "message": msg + ")"}


@app.post("/api/pipeline/cancel")
def cancel_pipeline():
    if not PIPELINE_STATE["running"]:
        return {"cancelled": False, "message": "no run in progress"}
    PIPELINE_STATE["cancel_requested"] = True
    return {"cancelled": True, "message": "cancel requested — run stops after the current email"}


@app.get("/api/pipeline/status")
def pipeline_status():
    return dict(PIPELINE_STATE, submission_size=len(SUBMISSION_STORE))


@app.get("/api/pipeline/submission")
def pipeline_submission():
    if SUBMISSION_STORE:
        return {"submission": SUBMISSION_STORE}
    return {"submission": _load_submission_file()}


# ---------------------------------------------------------------------------
# Submission vs ground-truth scoring
# ---------------------------------------------------------------------------
GROUND_TRUTH_PATH = os.path.join(BASE_DIR, "sdoc-hackathon-docker", "data_v2", "ground_truth.json")
SCORING_PATH = os.path.join(BASE_DIR, "sdoc-hackathon-docker", "server", "scoring.py")
COMPARE_KEYS = ["category", "status", "review_reason", "has_defect", "defect_fields"]

_SCORING_MOD = None


def _scoring():
    # The folder name contains hyphens, so it cannot be a normal package import.
    global _SCORING_MOD
    if _SCORING_MOD is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("sdoc_scoring", SCORING_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _SCORING_MOD = mod
    return _SCORING_MOD


@app.get("/api/compare")
def compare_submission():
    if not os.path.exists(GROUND_TRUTH_PATH):
        raise HTTPException(status_code=404, detail=f"ground_truth.json not found at {GROUND_TRUTH_PATH}")
    with open(GROUND_TRUTH_PATH, 'r', encoding='utf-8') as f:
        truth = json.load(f)

    # Prefer the freshest submission: in-memory pipeline results, else the file.
    if SUBMISSION_STORE:
        sub, source = dict(SUBMISSION_STORE), "pipeline_store"
    elif os.path.exists(SUBMISSION_PATH):
        sub = _load_submission_file()
        source = "submission.json"
    else:
        raise HTTPException(
            status_code=404,
            detail="No submission found — run the pipeline or place submission.json next to server.py."
        )

    score = _scoring().score_all(truth, sub)

    rows = []
    for eid in sorted(set(truth) | set(sub)):
        t, s = truth.get(eid), sub.get(eid)
        diffs = []
        if s is None:
            diffs.append("missing_submission")
        elif t is None:
            diffs.append("extra_submission")
        else:
            for k in COMPARE_KEYS:
                tv, sv = t.get(k), s.get(k)
                if k == "has_defect":
                    tv, sv = bool(tv), bool(sv)
                elif k == "defect_fields":
                    tv, sv = sorted(tv or []), sorted(sv or [])
                if sv != tv:
                    diffs.append(k)
        rows.append({"email_id": eid, "truth": t, "submission": s, "diffs": diffs})

    return {
        "source": source,
        "submission_count": len(sub),
        "ground_truth_count": len(truth),
        "score": score,
        "summary": {
            "perfect": sum(1 for r in rows if not r["diffs"]),
            "with_diffs": sum(1 for r in rows if r["diffs"]),
            "missing": sum(1 for r in rows if "missing_submission" in r["diffs"]),
        },
        "rows": rows,
    }


if __name__ == "__main__":
    print("Starting FastAPI Backend on http://localhost:8000")
    uvicorn.run(app, host="127.0.0.1", port=8000)
