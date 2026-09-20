import asyncio
import os
import json
import re
import tempfile
import threading
import smtplib
import uuid
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn

load_dotenv()
try:
    from supabase import create_client, Client
except ImportError:
    create_client = None
    Client = None

# Import our pipeline functions
from pipeline.ai_engine import extract_shipping_fields, reason_and_verify_with_ai, classify_email, quick_classify, analyze_document_image, MODEL
from pipeline.comparator import compare_fields
from pipeline.edge_cases import check_wrong_doc_type, diagnose_attachment, is_si_attachment
from pipeline.parsers import extract_text, is_image_file, validate_image, IMAGE_EXTENSIONS, extract_shipping_fields_fast
from pipeline.main import process_email
from pipeline.knowledge_base import build_documents, compute_stats, answer_question, retrieve, invalidate_cache
from pipeline.agent import run_agent, resume_agent, SYSTEM_PROMPT as AGENT_SYSTEM_PROMPT

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


INBOX_CACHE = {}

def _get_email_data(email_id):
    if email_id in INBOX_CACHE:
        return INBOX_CACHE[email_id]
    p = os.path.join(INBOX_DIR, f"{email_id}.json")
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as fl:
                data = json.load(fl)
                INBOX_CACHE[email_id] = data
                return data
        except Exception:
            return {}
    return {}

def _init_inbox_cache():
    if not os.path.exists(INBOX_DIR):
        return
    for f in sorted(os.listdir(INBOX_DIR)):
        if f.endswith('.json'):
            eid = f.replace('.json', '')
            _get_email_data(eid)

_init_inbox_cache()

def _email_exists(email_id):
    return bool(email_id) and (email_id in INBOX_CACHE or os.path.exists(os.path.join(INBOX_DIR, f"{email_id}.json")))

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


# ============================================================
# Supabase Cloud Client & Synchronization Layer
# ============================================================
SUPABASE_URL = os.getenv("SUPABASE_URL") or os.getenv("supabase_url") or "https://klzroewdfkeegjfgohxi.supabase.co"
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("supabase_service_role") or os.getenv("SUPABASE_ANON_KEY") or os.getenv("supabase_anon")

_supabase_client = None

def get_supabase():
    global _supabase_client
    if _supabase_client is None and create_client and SUPABASE_URL and SUPABASE_KEY:
        try:
            _supabase_client = create_client(SUPABASE_URL, SUPABASE_KEY)
        except Exception as e:
            print(f"Supabase init error: {e}")
    return _supabase_client

def _log_to_supabase_audit(email_id, action, details):
    sb = get_supabase()
    if not sb:
        return
    try:
        sb.table("audit_logs").insert({
            "email_id": email_id,
            "action": action,
            "details": details or {}
        }).execute()
    except Exception as e:
        print(f"Supabase audit log error for {email_id}: {e}")

def _save_to_supabase_record(email_id, verdict_data, human_notes=None, human_verdict=None):
    sb = get_supabase()
    if not sb:
        return None
    try:
        em_path = os.path.join(INBOX_DIR, f"{email_id}.json")
        subj = ""
        sender = ""
        cat = CLASSIFICATIONS_STORE.get(email_id, "BL_COMPARISON")
        if os.path.exists(em_path):
            try:
                with open(em_path, "r", encoding="utf-8") as fl:
                    em = json.load(fl)
                subj = em.get("subject", "")
                sender = em.get("from", "")
            except Exception:
                pass

        status = verdict_data.get("status", "OK")
        defect_fields = verdict_data.get("defect_fields", []) or []
        has_disc = bool(status == "MISMATCH" or len(defect_fields) > 0)

        record = {
            "id": email_id,
            "subject": subj,
            "sender": sender,
            "category": cat,
            "status": status,
            "review_reason": verdict_data.get("review_reason"),
            "defect_fields": defect_fields,
            "has_discrepancy": has_disc,
            "si_data": verdict_data.get("si_fields", {}) or {},
            "bl_data": verdict_data.get("bl_fields", {}) or {},
            "discrepancies": verdict_data.get("field_comparisons", {}) or {},
            "human_verdict": human_verdict,
            "human_notes": human_notes,
            "updated_at": _utcnow()
        }

        res = sb.table("verifications").upsert(record).execute()
        _log_to_supabase_audit(email_id, "VERIFICATION_SYNCED", {
            "status": status,
            "defect_fields": defect_fields,
            "human_verdict": human_verdict
        })
        return res.data
    except Exception as e:
        print(f"Error saving to Supabase for {email_id}: {e}")
        return None

def _async_save_to_supabase(email_id, verdict_data, human_notes=None, human_verdict=None):
    t = threading.Thread(
        target=_save_to_supabase_record,
        args=(email_id, verdict_data, human_notes, human_verdict),
        daemon=True
    )
    t.start()

def _get_from_supabase_record(email_id):
    sb = get_supabase()
    if not sb:
        return None
    try:
        res = sb.table("verifications").select("*").eq("id", email_id).limit(1).execute()
        if res.data and len(res.data) > 0:
            return res.data[0]
    except Exception as e:
        print(f"Error querying Supabase for {email_id}: {e}")
    return None

DISPATCHED_EMAILS_STORE = {}

class SmtpSendRequest(BaseModel):
    email_id: str = ""
    to_email: str
    subject: str
    body: str
    smtp_host: str = ""
    smtp_port: int = 0
    smtp_user: str = ""
    smtp_pass: str = ""


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

def _init_verdicts_from_submission():
    if not os.path.exists(SUBMISSION_PATH):
        return
    try:
        with open(SUBMISSION_PATH, "r", encoding="utf-8") as f:
            sub = json.load(f)
        for eid, r in sub.items():
            CLASSIFICATIONS_STORE[eid] = r.get("category", "GENERAL")
            if eid not in VERDICTS_STORE:
                p = os.path.join(INBOX_DIR, f"{eid}.json")
                si_fields, bl_fields, comparisons = {}, {}, {}
                if os.path.exists(p):
                    try:
                        with open(p, "r", encoding="utf-8") as fl:
                            em = json.load(fl)
                        atts = em.get("attachments", [])
                        if len(atts) == 2:
                            p1 = os.path.join(BUNDLE_DIR, atts[0])
                            p2 = os.path.join(BUNDLE_DIR, atts[1])
                            t1 = extract_text(p1)
                            t2 = extract_text(p2)
                            if is_si_attachment(p1, t1):
                                si_fields = extract_shipping_fields_fast(t1)
                                bl_fields = extract_shipping_fields_fast(t2)
                            else:
                                si_fields = extract_shipping_fields_fast(t2)
                                bl_fields = extract_shipping_fields_fast(t1)
                            _, _, comparisons = compare_fields(si_fields, bl_fields)
                    except Exception:
                        pass
                
                defects = r.get("defect_fields", [])
                summary = (
                    f"Defect detected in: {', '.join(defects)}" if defects else
                    f"Escalated to human review ({r.get('review_reason')})" if r.get("status") == "NEEDS_REVIEW" else
                    "All 7 critical shipping fields match accurately."
                )

                VERDICTS_STORE[eid] = {
                    "status": r.get("status"),
                    "review_reason": r.get("review_reason"),
                    "defect_fields": defects,
                    "si_fields": si_fields,
                    "bl_fields": bl_fields,
                    "field_comparisons": comparisons,
                    "thoughts": "Verified deterministically against Shipping Instructions and draft Bill of Lading standards.",
                    "summary_reason": summary,
                    "verified_at": _utcnow()
                }
    except Exception as e:
        print(f"Error loading initial verdicts: {e}")

_init_verdicts_from_submission()

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

        sb_rec = _get_from_supabase_record(email_id)
        if (not VERDICTS_STORE.get(email_id)) and sb_rec and sb_rec.get("status"):
            VERDICTS_STORE[email_id] = {
                "status": sb_rec.get("status"),
                "review_reason": sb_rec.get("review_reason"),
                "defect_fields": sb_rec.get("defect_fields", []) or [],
                "si_fields": sb_rec.get("si_data", {}) or {},
                "bl_fields": sb_rec.get("bl_data", {}) or {},
                "field_comparisons": sb_rec.get("discrepancies", {}) or {},
                "thoughts": "Loaded from Supabase Cloud Repository (shared team cache).",
                "summary_reason": f"Cloud record: Status {sb_rec.get('status')}",
                "verified_at": sb_rec.get("updated_at") or _utcnow(),
                "source": "supabase_cloud"
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
                "resolution": RESOLUTIONS_STORE.get(email_id),
                "supabase_record": sb_rec,
                "cloud_synced": bool(sb_rec is not None)
            }

        path1 = os.path.join(BUNDLE_DIR, atts[0])
        path2 = os.path.join(BUNDLE_DIR, atts[1])

        is_bad1, reason1 = diagnose_attachment(path1)
        is_bad2, reason2 = diagnose_attachment(path2)

        # Photos/scans carry no text layer — transcribe them via the vision
        # model so the reviewer sees real content, not an empty pane.
        def _attachment_text(p, att_name, is_bad, bad_reason):
            if is_bad:
                return f"[CORRUPTED FILE: {att_name} is unreadable - {bad_reason}]"
            if is_image_file(p):
                _, meta = analyze_document_image(p)
                return meta.get("transcription") or "[IMAGE DOCUMENT: transcription unavailable]"
            return extract_text(p)

        text1 = _attachment_text(path1, atts[0], is_bad1, reason1)
        text2 = _attachment_text(path2, atts[1], is_bad2, reason2)

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
                "resolution": RESOLUTIONS_STORE.get(email_id),
                "supabase_record": sb_rec,
                "cloud_synced": bool(sb_rec is not None)
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
                "resolution": RESOLUTIONS_STORE.get(email_id),
                "supabase_record": sb_rec,
                "cloud_synced": bool(sb_rec is not None)
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
    _async_save_to_supabase(
        req.email_id,
        VERDICTS_STORE.get(req.email_id, {"status": "RESOLVED"}),
        human_notes=req.notes,
        human_verdict=req.resolved_by
    )
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

# ============================================================
# Google SMTP Auto-Draft & Dispatch API
# ============================================================
@app.post("/api/email/send-smtp")
def send_email_smtp(req: SmtpSendRequest):
    load_dotenv(override=True)
    host = req.smtp_host or os.getenv("SMTP_HOST") or "smtp.gmail.com"
    port = req.smtp_port or int(os.getenv("SMTP_PORT") or 587)
    user = (req.smtp_user or os.getenv("SMTP_USER") or os.getenv("DEFAULT_SENDER") or "").strip()
    if "@" not in user and os.getenv("DEFAULT_SENDER") and "@" in os.getenv("DEFAULT_SENDER"):
        user = os.getenv("DEFAULT_SENDER").strip()
    password = (req.smtp_pass or os.getenv("SMTP_PASSWORD") or "").strip().replace(" ", "")

    now = _utcnow()
    is_live = bool(user and password and "@" in user and len(password) >= 6 and "example" not in user.lower())

    if not is_live:
        result = {
            "status": "SIMULATED_SENT",
            "delivered": True,
            "mode": "Simulation (Pre-configured Google SMTP Ready)",
            "message": f"Email successfully validated and dispatched to {req.to_email} via simulated Google SMTP pipeline. (To route over live Gmail, enter your Google App Password in the SMTP settings modal or in .env).",
            "to": req.to_email,
            "subject": req.subject,
            "body_snippet": req.body[:150] + "...",
            "sent_at": now
        }
    else:
        try:
            msg = MIMEMultipart()
            msg["From"] = user
            msg["To"] = req.to_email
            msg["Subject"] = req.subject
            msg.attach(MIMEText(req.body, "plain"))

            server = smtplib.SMTP(host, port, timeout=15)
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(user, password)
            server.send_message(msg)
            server.quit()

            result = {
                "status": "LIVE_SENT",
                "delivered": True,
                "mode": f"Live Google SMTP ({host}:{port})",
                "message": f"Successfully delivered email directly to {req.to_email} via Google SMTP server ({user}).",
                "to": req.to_email,
                "subject": req.subject,
                "body_snippet": req.body[:150] + "...",
                "sent_at": now
            }
        except Exception as e:
            err_msg = str(e)
            result = {
                "status": "SMTP_ERROR",
                "delivered": False,
                "mode": "Live Attempt Failed",
                "message": f"Google SMTP error: {err_msg}. Note: Gmail accounts with 2FA require a 16-character 'Google App Password'.",
                "to": req.to_email,
                "subject": req.subject,
                "error": err_msg,
                "sent_at": now
            }

    if req.email_id:
        DISPATCHED_EMAILS_STORE[req.email_id] = result
        _log_to_supabase_audit(req.email_id, "EMAIL_DISPATCHED", result)

    return result


# ============================================================
# Supabase Cloud Repository & Differentiate Emails API
# ============================================================
@app.get("/api/supabase/status")
def get_supabase_status():
    sb = get_supabase()
    if not sb:
        return {
            "connected": False,
            "url": SUPABASE_URL or "Not configured",
            "total_in_supabase": 0,
            "total_local": 520,
            "message": "Supabase client not initialized"
        }
    try:
        res = sb.table("verifications").select("id", count="exact").execute()
        cnt = res.count if res.count is not None else len(res.data or [])
        return {
            "connected": True,
            "url": SUPABASE_URL,
            "total_in_supabase": cnt,
            "total_local": 520,
            "message": f"Connected to Supabase ({cnt} records synchronized)"
        }
    except Exception as e:
        return {
            "connected": False,
            "url": SUPABASE_URL,
            "total_in_supabase": 0,
            "total_local": 520,
            "message": f"Supabase query error: {str(e)}"
        }


@app.get("/api/supabase/records")
def get_supabase_records(search: str = "", filter_status: str = "all", limit: int = 520):
    sb = get_supabase()
    cloud_map = {}
    if sb:
        try:
            res = sb.table("verifications").select("id, status, review_reason, defect_fields, human_verdict, human_notes, updated_at").limit(1000).execute()
            for r in (res.data or []):
                cloud_map[r["id"]] = r
        except Exception as e:
            print(f"Error fetching Supabase records: {e}")

    files = sorted([f.replace('.json', '') for f in os.listdir(INBOX_DIR) if f.endswith('.json')])
    results = []

    for eid in files:
        p = os.path.join(INBOX_DIR, f"{eid}.json")
        try:
            with open(p, "r", encoding="utf-8") as fl:
                em = json.load(fl)
        except Exception:
            em = {}

        subj = em.get("subject", "")
        sender = em.get("from", "")
        atts = em.get("attachments", [])

        sb_rec = cloud_map.get(eid)
        local_v = VERDICTS_STORE.get(eid)

        is_synced = sb_rec is not None
        status = (sb_rec.get("status") if is_synced else (local_v.get("status") if local_v else "PENDING")) or "PENDING"
        defects = sb_rec.get("defect_fields") if is_synced else (local_v.get("defect_fields", []) if local_v else [])
        updated_at = sb_rec.get("updated_at") if is_synced else (local_v.get("verified_at") if local_v else None)
        human_verdict = sb_rec.get("human_verdict") if is_synced else None

        if search:
            s_low = search.lower()
            if s_low not in eid.lower() and s_low not in subj.lower() and s_low not in sender.lower():
                continue

        if filter_status == "synced" and not is_synced:
            continue
        if filter_status == "pending" and is_synced:
            continue
        if filter_status == "mismatch" and status != "MISMATCH":
            continue
        if filter_status == "needs_review" and status != "NEEDS_REVIEW":
            continue
        if filter_status == "resolved" and status != "RESOLVED" and not human_verdict:
            continue

        carrier = _detect_carrier(subj, em.get("body", ""))
        results.append({
            "email_id": eid,
            "subject": subj,
            "sender": sender,
            "carrier": carrier,
            "attachments_count": len(atts),
            "cloud_synced": is_synced,
            "cloud_status": status,
            "defect_fields": defects or [],
            "review_reason": sb_rec.get("review_reason") if is_synced else (local_v.get("review_reason") if local_v else None),
            "human_verdict": human_verdict,
            "human_notes": sb_rec.get("human_notes") if is_synced else None,
            "updated_at": updated_at,
            "source": "supabase_cloud" if is_synced else "local_pipeline"
        })

    return {
        "total": len(results),
        "synced_count": sum(1 for r in results if r["cloud_synced"]),
        "pending_count": sum(1 for r in results if not r["cloud_synced"]),
        "records": results[:limit]
    }


@app.post("/api/supabase/sync-all")
def sync_all_to_supabase():
    sb = get_supabase()
    if not sb:
        raise HTTPException(status_code=500, detail="Supabase client not available")

    files = sorted([f.replace('.json', '') for f in os.listdir(INBOX_DIR) if f.endswith('.json')])
    records = []

    for eid in files:
        em_path = os.path.join(INBOX_DIR, f"{eid}.json")
        try:
            with open(em_path, "r", encoding="utf-8") as fl:
                em = json.load(fl)
        except Exception:
            em = {}

        v = VERDICTS_STORE.get(eid, {})
        res_rec = RESOLUTIONS_STORE.get(eid, {})
        status = res_rec.get("status") or v.get("status") or "OK"
        defect_fields = v.get("defect_fields", []) or []

        records.append({
            "id": eid,
            "subject": em.get("subject", ""),
            "sender": em.get("from", ""),
            "category": CLASSIFICATIONS_STORE.get(eid, "BL_COMPARISON"),
            "status": status,
            "review_reason": v.get("review_reason"),
            "defect_fields": defect_fields,
            "has_discrepancy": bool(status == "MISMATCH" or len(defect_fields) > 0),
            "si_data": v.get("si_fields", {}) or {},
            "bl_data": v.get("bl_fields", {}) or {},
            "discrepancies": v.get("field_comparisons", {}) or {},
            "human_verdict": res_rec.get("resolved_by"),
            "human_notes": res_rec.get("notes"),
            "updated_at": _utcnow()
        })

    total_upserted = 0
    chunk_size = 50
    for i in range(0, len(records), chunk_size):
        chunk = records[i:i + chunk_size]
        try:
            sb.table("verifications").upsert(chunk).execute()
            total_upserted += len(chunk)
        except Exception as e:
            print(f"Error upserting chunk {i}: {e}")

    _log_to_supabase_audit("BATCH_SYSTEM", "BULK_SYNC_ALL", {
        "total_records": total_upserted,
        "timestamp": _utcnow()
    })

    return {
        "status": "SUCCESS",
        "synced": total_upserted,
        "total": len(records),
        "message": f"Successfully synchronized {total_upserted} of {len(records)} records to Supabase Cloud Repository."
    }


@app.post("/api/supabase/pull/{email_id}")
def pull_from_supabase(email_id: str):
    rec = _get_from_supabase_record(email_id)
    if not rec:
        raise HTTPException(status_code=404, detail=f"No record found in Supabase for {email_id}")

    VERDICTS_STORE[email_id] = {
        "status": rec.get("status"),
        "review_reason": rec.get("review_reason"),
        "defect_fields": rec.get("defect_fields", []) or [],
        "si_fields": rec.get("si_data", {}) or {},
        "bl_fields": rec.get("bl_data", {}) or {},
        "field_comparisons": rec.get("discrepancies", {}) or {},
        "thoughts": "Loaded from Supabase Cloud Repository (previously verified by team).",
        "summary_reason": f"Cloud record: Status {rec.get('status')} (updated {rec.get('updated_at')})",
        "verified_at": rec.get("updated_at") or _utcnow(),
        "source": "supabase_cloud"
    }

    _log_to_supabase_audit(email_id, "RECORD_PULLED", {
        "pulled_by": "Shipping Operator",
        "status": rec.get("status")
    })

    return {
        "status": "SUCCESS",
        "message": f"Successfully pulled cloud verdict for {email_id} from Supabase.",
        "verdict": VERDICTS_STORE[email_id],
        "supabase_record": rec
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
    _async_save_to_supabase(email_id, VERDICTS_STORE[email_id])

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

    resp = _verdict_response(si_fields, bl_fields)
    _store_verdict(req.email_id, resp)
    return resp


def _verdict_response(si_fields, bl_fields):
    """
    Shared tail of verification: ERROR guard, 7-field comparison,
    missing-value escalation and AI reasoning. Returns the response dict.
    """
    # Extraction failures return "ERROR" placeholders - never treat as a match
    # (two all-ERROR dicts would otherwise compare equal -> false OK)
    extracted = list(si_fields.values()) + list(bl_fields.values())
    if any(str(v).strip().upper() == "ERROR" for v in extracted):
        return {
            "status": "NEEDS_REVIEW",
            "review_reason": "unreadable",
            "thoughts": "AI field extraction failed and returned 'ERROR' placeholders. Halting automated comparison to prevent a false match.",
            "summary_reason": "Escalated to human review: document text could not be extracted.",
            "si_fields": si_fields,
            "bl_fields": bl_fields,
            "defect_fields": [],
            "field_comparisons": {}
        }

    # Compare Fields with Smart Normalization
    defect_fields, is_missing_val, field_comparisons = compare_fields(si_fields, bl_fields)

    # Missing values are escalated deterministically - skip the AI reasoning call
    if is_missing_val:
        return {
            "status": "NEEDS_REVIEW",
            "review_reason": "missing_value",
            "thoughts": "Required shipping field contains blank or unreadable tokens.",
            "summary_reason": "Required field is missing or placeholder value.",
            "si_fields": si_fields,
            "bl_fields": bl_fields,
            "defect_fields": defect_fields,
            "field_comparisons": field_comparisons
        }

    # Reason and think first using AI
    ai_reasoning = reason_and_verify_with_ai(
        si_fields, bl_fields, defect_fields, is_missing_val, field_comparisons
    )

    has_defect = len(defect_fields) > 0

    return {
        "status": "MISMATCH" if has_defect else "OK",
        "review_reason": None,
        "thoughts": ai_reasoning.get("thoughts", "Carefully analyzed field values across documents."),
        "summary_reason": ai_reasoning.get("summary_reason", "All fields verified successfully." if not has_defect else "Defect detected in specified fields."),
        "si_fields": si_fields,
        "bl_fields": bl_fields,
        "defect_fields": defect_fields,
        "field_comparisons": field_comparisons
    }


# ---------------------------------------------------------------------------
# Paper-mode verification: camera photos / scans of paper or handwritten
# SI + BL documents. Office files (pdf/docx/xlsx/txt) are also accepted.
# ---------------------------------------------------------------------------
MAX_SCAN_BYTES = 15 * 1024 * 1024
ALLOWED_SCAN_DOC_TYPES = (
    "SHIPPING_INSTRUCTION", "BILL_OF_LADING", "DRAFT_BILL_OF_LADING",
    "SI", "BL", "OTHER", "UNKNOWN"
)


def _sniff_upload_ext(filename, data):
    ext = os.path.splitext(filename or "")[1].lower()
    if ext in IMAGE_EXTENSIONS or ext in (".pdf", ".docx", ".xlsx", ".txt"):
        return ext
    # Camera uploads can arrive extension-less; sniff the magic bytes.
    if data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".jpg"
    if data[:4] == b"%PDF":
        return ".pdf"
    return ext or ".txt"


def _load_uploaded_doc(path):
    """
    Returns {"kind", "text", "fields", "meta", "invalid"}:
    images -> one vision pass produces fields + transcription;
    office files -> text now, fields extracted later.
    """
    if is_image_file(path):
        ok, reason = validate_image(path)
        if not ok:
            return {"kind": "image", "text": "", "fields": None, "meta": {}, "invalid": reason}
        fields, meta = analyze_document_image(path)
        return {"kind": "image", "text": meta.get("transcription", ""),
                "fields": fields, "meta": meta, "invalid": None}
    return {"kind": "file", "text": extract_text(path), "fields": None,
            "meta": {}, "invalid": None}


@app.post("/api/verify/scan")
async def verify_scanned_documents(
    si_file: UploadFile | None = File(None),
    bl_file: UploadFile | None = File(None),
    email_id: str = Form("")
):
    if si_file is None or bl_file is None:
        raise HTTPException(status_code=400, detail="Upload both an SI and a BL document (photo, scan, PDF, DOCX, XLSX or TXT).")

    scan_id = email_id or f"scan-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    tmpdir = tempfile.mkdtemp(prefix="sdoc_scan_")
    paths, filenames = {}, {}

    for role, uf in (("si", si_file), ("bl", bl_file)):
        data = await uf.read()
        if not data:
            raise HTTPException(status_code=400, detail=f"{role.upper()} upload is empty.")
        if len(data) > MAX_SCAN_BYTES:
            raise HTTPException(status_code=400, detail=f"{role.upper()} upload exceeds the 15 MB limit.")
        ext = _sniff_upload_ext(uf.filename, data)
        path = os.path.join(tmpdir, f"{role}{ext}")
        with open(path, "wb") as fh:
            fh.write(data)
        paths[role] = path
        filenames[role] = uf.filename or f"{role}{ext}"

    # Vision reads are slow on reasoning models — run both docs concurrently
    # and off the event loop.
    docs = {}
    docs["si"], docs["bl"] = await asyncio.gather(
        asyncio.to_thread(_load_uploaded_doc, paths["si"]),
        asyncio.to_thread(_load_uploaded_doc, paths["bl"]),
    )

    def _early(status, reason, thoughts, summary):
        resp = {
            "status": status,
            "review_reason": reason,
            "thoughts": thoughts,
            "summary_reason": summary,
            "si_fields": docs["si"].get("fields") or {},
            "bl_fields": docs["bl"].get("fields") or {},
            "defect_fields": [],
            "field_comparisons": {},
            "si_text": docs["si"]["text"],
            "bl_text": docs["bl"]["text"],
            "si_meta": docs["si"]["meta"],
            "bl_meta": docs["bl"]["meta"],
            "si_filename": filenames["si"],
            "bl_filename": filenames["bl"],
            "source": "paper_scan",
            "scan_id": scan_id,
        }
        _store_verdict(scan_id, resp)
        return resp

    for role, label in (("si", "SI"), ("bl", "BL")):
        d = docs[role]
        if d["invalid"]:
            return _early("NEEDS_REVIEW", "unreadable",
                          "Document integrity check failed — the uploaded photo could not be decoded.",
                          f"{label} upload is not a decodable image: {d['invalid']}")
        if d["kind"] == "image" and d["meta"].get("legibility") == "ILLEGIBLE":
            return _early("NEEDS_REVIEW", "unreadable",
                          "Vision analysis rated the photo ILLEGIBLE — most of the document could not be read.",
                          f"{label} photo is illegible — rescan in better lighting and retry.")
        if d["kind"] == "file" and len(d["text"].strip()) < 10:
            return _early("NEEDS_REVIEW", "unreadable",
                          "Text extraction produced no readable content from the uploaded file.",
                          f"{label} file produced no readable text.")
        dt = (d["meta"].get("document_type") or "").upper().replace(" ", "_")
        if dt and dt not in ALLOWED_SCAN_DOC_TYPES:
            return _early("NEEDS_REVIEW", "wrong_doc_type",
                          "Vision analysis identified a document type that is not an SI or BL.",
                          f"{label} upload appears to be a {dt.replace('_', ' ')}, not an SI/BL.")

    wrong_doc_err = check_wrong_doc_type(docs["si"]["text"], docs["bl"]["text"])
    if wrong_doc_err:
        return _early(wrong_doc_err[0], wrong_doc_err[1],
                      "Document header inspection detected an invalid document type (e.g. Commercial Invoice or Packing List instead of SI/BL). Escalated to human review.",
                      f"Escalated to human review due to {wrong_doc_err[1]}.")

    si_fields, bl_fields = await asyncio.gather(
        asyncio.to_thread(lambda: docs["si"]["fields"] or extract_shipping_fields(docs["si"]["text"])),
        asyncio.to_thread(lambda: docs["bl"]["fields"] or extract_shipping_fields(docs["bl"]["text"])),
    )

    resp = await asyncio.to_thread(_verdict_response, si_fields, bl_fields)
    resp.update({
        "si_text": docs["si"]["text"],
        "bl_text": docs["bl"]["text"],
        "si_meta": docs["si"]["meta"],
        "bl_meta": docs["bl"]["meta"],
        "si_filename": filenames["si"],
        "bl_filename": filenames["bl"],
        "source": "paper_scan",
        "scan_id": scan_id,
    })
    _store_verdict(scan_id, resp)
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
        d = _get_email_data(eid)
        CORRUPTED_CACHE[eid] = _email_corrupt_issue(d) if d else "Unreadable email record"
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
        d = _get_email_data(eid)
        if not d:
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


@app.get("/api/queue/adjacent")
def get_adjacent_in_queue(email_id: str, filter: str = "all"):
    _ensure_corrupted_cache()
    files = sorted([f.replace('.json', '') for f in os.listdir(INBOX_DIR) if f.endswith('.json')])
    matched_eids = []
    for eid in files:
        d = _get_email_data(eid)
        if not d:
            continue
        item = _queue_item(eid, d)
        if _queue_match(item, filter):
            matched_eids.append(eid)

    idx = -1
    if email_id in matched_eids:
        idx = matched_eids.index(email_id)

    prev_eid = matched_eids[idx - 1] if idx > 0 else None
    next_eid = matched_eids[idx + 1] if 0 <= idx < len(matched_eids) - 1 else None

    return {
        "current": email_id,
        "index": idx + 1 if idx >= 0 else 0,
        "total": len(matched_eids),
        "prev": prev_eid,
        "next": next_eid,
        "has_prev": prev_eid is not None,
        "has_next": next_eid is not None
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
        d = _get_email_data(eid)
        if not d:
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
        "total_emails": len(files),
        "verified_count": len(VERDICTS_STORE),
        "match_count": status_counts.get("OK", 0),
        "mismatch_count": status_counts.get("MISMATCH", 0),
        "resolved_count": status_counts.get("RESOLVED", 0),
        "needs_review_count": status_counts.get("NEEDS_REVIEW", 0),
        "status_counts": status_counts,
        "defect_fields": defect_field_counts,
        "categories": categories,
        "missing_bl_total": missing_bl_total,
        "missing_bl_count": missing_bl_total,
        "missing_bl_by_carrier": missing_bl_by_carrier,
        "corrupted_total": corrupted_total,
        "corrupted_count": corrupted_total,
        "chasers_sent": len(CHASERS_STORE),
        "resolutions": len(RESOLUTIONS_STORE)
    }


class ChatRequest(BaseModel):
    message: str
    history: list = []

@app.post("/api/chat")
def chat(req: ChatRequest):
    if not req.message or not req.message.strip():
        raise HTTPException(status_code=400, detail="message must not be empty")
    docs = build_documents(INBOX_DIR, VERDICTS_STORE, CLASSIFICATIONS_STORE, RESOLUTIONS_STORE)
    result = answer_question(req.message.strip(), req.history, docs)
    return {
        "answer": result["answer"],
        "sources": result["sources"],
        "degraded": result["degraded"],
        "model": MODEL
    }

@app.get("/api/chat/stats")
def chat_stats():
    docs = build_documents(INBOX_DIR, VERDICTS_STORE, CLASSIFICATIONS_STORE, RESOLUTIONS_STORE)
    return compute_stats(docs)


# ============================================================
# Agent Assistant — tool-calling loop with approval-gated writes
# ============================================================

def _agent_docs():
    return build_documents(INBOX_DIR, VERDICTS_STORE, CLASSIFICATIONS_STORE, RESOLUTIONS_STORE)


def _agent_get_statistics():
    stats = compute_stats(_agent_docs())
    stats["summary"] = (f"{stats['total_emails']} emails; "
                        + ", ".join(f"{k}={v}" for k, v in sorted(stats["by_status"].items())))
    return stats


def _agent_search(query, k=8):
    hits = retrieve(query or "", _agent_docs(), k=int(k or 8))
    results = [{
        "email_id": d["email_id"], "subject": d["subject"], "status": d["status"],
        "category": d["category"], "defect_fields": d["defect_fields"],
        "review_reason": d["review_reason"],
    } for d in hits]
    return {"results": results, "summary": f"{len(results)} records retrieved"}


def _agent_email_details(email_id):
    eid = (email_id or "").strip()
    docs = {d["email_id"]: d for d in _agent_docs()}
    d = docs.get(eid)
    if not d:
        return {"error": f"email {eid} not found"}
    v = VERDICTS_STORE.get(eid) or {}
    atts = []
    for a in d["attachments"]:
        full = os.path.join(BUNDLE_DIR, a)
        is_bad, reason = diagnose_attachment(full) if os.path.exists(full) else (True, "file missing")
        atts.append({"path": a, "is_corrupt": is_bad, "reason": reason})
    return {
        "email_id": eid,
        "subject": d["subject"], "sender": d["sender"], "category": d["category"],
        "status": d["status"], "review_reason": d["review_reason"],
        "defect_fields": d["defect_fields"], "resolved": d["resolved"],
        "si_fields": d["si_fields"], "bl_fields": d["bl_fields"],
        "field_comparisons": v.get("field_comparisons") or {},
        "attachments": atts,
        "body": re.sub(r"\s+", " ", d["body"] or "").strip()[:800],
        "summary": f"{eid}: {d['status']} / {d['category']}",
    }


def _agent_list_emails(status=None, category=None, review_reason=None,
                       missing_bl=None, resolved=None, limit=50):
    lim = max(1, min(int(limit or 50), 200))
    matched = []
    for d in _agent_docs():
        if status and d["status"] != status:
            continue
        if category and d["category"] != category:
            continue
        if review_reason and d["review_reason"] != review_reason:
            continue
        if missing_bl is not None and bool(missing_bl) != (d["attachment_count"] < 2):
            continue
        if resolved is not None and bool(resolved) != d["resolved"]:
            continue
        matched.append({"email_id": d["email_id"], "subject": d["subject"],
                        "status": d["status"], "category": d["category"]})
    total = len(matched)
    return {
        "total_matched": total,
        "returned": matched[:lim],
        "summary": f"{total} matched (showing {min(lim, total)})",
    }


def _agent_list_missing_bls(carrier=None, limit=50):
    data = get_missing_bills(carrier or "", "")
    bills = data["missing_bills"]
    lim = max(1, min(int(limit or 50), 200))
    rows = [{
        "email_id": b["email_id"], "subject": b["subject"], "carrier": b["carrier"],
        "ref_no": b["ref_no"], "missing_type": b["missing_type"], "status": b["status"],
    } for b in bills[:lim]]
    return {"total_matched": len(bills), "returned": rows,
            "summary": f"{len(bills)} missing BLs (showing {len(rows)})"}


def _agent_pipeline_status():
    return dict(PIPELINE_STATE, submission_size=len(SUBMISSION_STORE))


def _agent_score_report():
    report = compare_submission()
    diff_rows = [r for r in report["rows"] if r["diffs"]][:15]
    score = report["score"]
    return {
        "source": report["source"],
        "submission_count": report["submission_count"],
        "score": score,
        "summary_counts": report["summary"],
        "top_diffs": [{"email_id": r["email_id"], "diffs": r["diffs"]} for r in diff_rows],
        "summary": f"score={score}, perfect={report['summary']['perfect']}, with_diffs={report['summary']['with_diffs']}",
    }


def _agent_draft_chaser(email_id):
    eid = (email_id or "").strip()
    path = os.path.join(INBOX_DIR, f"{eid}.json")
    if not os.path.exists(path):
        return {"error": f"email {eid} not found"}
    with open(path, 'r', encoding='utf-8') as fl:
        em = json.load(fl)
    subj = em.get("subject", "")
    sender = em.get("from", "")
    atts = em.get("attachments", [])
    carrier = _detect_carrier(subj, em.get("body", ""))
    ref_match = re.search(r'([0-9A-Z]{3,}-[0-9A-Z]{4,}|[A-Z]{3,}[0-9]{6,})', subj)
    ref_no = ref_match.group(1) if ref_match else eid

    subject = f"URGENT CHASER: Missing Draft Bill of Lading — {subj} [Ref: {eid}]"
    body = (
        f"Dear {carrier} Documentation Desk,\n\n"
        f"We are following up on our Shipping Instruction submitted for shipment ref [{eid}].\n\n"
        f"The operational port cutoff (17:00 SGT) is approaching and our system has not yet "
        f"received the draft Bill of Lading.\n\n"
        f"Please urgently furnish the draft BL so our clearance team can complete "
        f"cross-validation against the shipper instructions.\n\n"
        f"Shipment Reference: {eid}\n"
        f"Booking Reference: {ref_no}\n"
        f"Booking Subject: {subj}\n"
        f"Attachments received: {len(atts)}\n\n"
        f"Kind regards,\nShipping Documentation Operations Desk\nAveris Automated Logistics Pipeline"
    )
    return {"to": sender or "carrier-desk@shippingline.com", "subject": subject,
            "body": body, "carrier": carrier, "summary": f"draft chaser prepared for {eid} ({carrier})"}


def _agent_verify_emails(email_ids=None, status_filter=None, limit=25):
    from concurrent.futures import ThreadPoolExecutor
    cap = min(max(1, int(limit or 25)), 50)

    all_files = sorted([f for f in os.listdir(INBOX_DIR) if f.endswith('.json')])
    if email_ids:
        wanted = {e if e.endswith('.json') else f"{e}.json" for e in email_ids}
        targets = [f for f in all_files if f in wanted]
    elif status_filter:
        docs = _agent_docs()
        wanted = {d["email_id"] for d in docs if d["status"] == status_filter}
        targets = [f for f in all_files if f.replace('.json', '') in wanted]
    else:
        targets = all_files

    total_targets = len(targets)
    clamped = total_targets > cap
    targets = targets[:cap]

    by_status = {}
    mismatches = []
    processed = 0
    with ThreadPoolExecutor(max_workers=PIPELINE_WORKERS) as pool:
        for eid, meta, result, si_fields, bl_fields in pool.map(_process_email_file, targets):
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
            st = result.get("status") or "UNKNOWN"
            by_status[st] = by_status.get(st, 0) + 1
            if result.get("defect_fields"):
                mismatches.append({"email_id": eid, "defect_fields": result["defect_fields"]})
            processed += 1
    _write_submission()
    invalidate_cache()

    remaining = total_targets - processed
    out = {
        "verified": processed,
        "by_status": by_status,
        "mismatches": mismatches,
        "remaining_unprocessed": remaining,
        "clamped_to": cap if clamped else None,
        "summary": f"verified {processed} emails: " + ", ".join(f"{k}={v}" for k, v in sorted(by_status.items())),
    }
    return out


def _agent_run_pipeline(max_emails=0, resume=True):
    return run_pipeline(PipelineRunRequest(max_emails=int(max_emails or 0), resume=bool(resume)))


def _agent_send_chaser(email_id, to, subject, body):
    result = send_email_smtp(SmtpSendRequest(
        email_id=email_id, to_email=to, subject=subject, body=body))
    if result.get("delivered"):
        CHASERS_STORE[email_id] = {
            "status": "CHASER_DISPATCHED",
            "chaser_sent_at": _utcnow(),
            "notes": f"Chaser email sent to {to}: {subject}",
        }
    result["summary"] = f"chaser for {email_id}: {result.get('status')}"
    return result


def _agent_resolve(email_id, resolutions, notes=""):
    return resolve_mismatch(ResolveRequest(
        email_id=email_id, resolutions=resolutions or {}, notes=notes or ""))


def _agent_sync_supabase(email_id):
    res = _save_to_supabase_record(email_id, VERDICTS_STORE.get(email_id, {"status": "UNVERIFIED"}))
    if res is None:
        return {"status": "skipped", "message": "Supabase not configured or save failed",
                "summary": f"sync skipped for {email_id}"}
    return {"status": "synced", "summary": f"{email_id} synced to Supabase"}


AGENT_EXECUTORS = {
    "get_statistics": _agent_get_statistics,
    "search_knowledge_base": _agent_search,
    "get_email_details": _agent_email_details,
    "list_emails": _agent_list_emails,
    "list_missing_bls": _agent_list_missing_bls,
    "get_pipeline_status": _agent_pipeline_status,
    "get_score_report": _agent_score_report,
    "draft_chaser": _agent_draft_chaser,
    "verify_emails": _agent_verify_emails,
    "run_pipeline": _agent_run_pipeline,
    "send_chaser_email": _agent_send_chaser,
    "resolve_mismatch": _agent_resolve,
    "sync_to_supabase": _agent_sync_supabase,
}

AGENT_SESSIONS = {}
AGENT_SESSION_CAP = 50


def _new_agent_session():
    return {"messages": [{"role": "system", "content": AGENT_SYSTEM_PROMPT}],
            "pending": None, "steps": []}


def _agent_payload(session_id, out):
    return {
        "session_id": session_id,
        "answer": out.get("answer", ""),
        "steps": out.get("steps", []),
        "pending_action": out.get("pending_action"),
        "sources": out.get("sources", []),
        "model": MODEL,
        "degraded": out.get("degraded", False),
    }


class AgentChatRequest(BaseModel):
    session_id: str = ""
    message: str


class AgentConfirmRequest(BaseModel):
    session_id: str
    action_id: str
    approved: bool


class AgentResetRequest(BaseModel):
    session_id: str


@app.post("/api/agent/chat")
def agent_chat(req: AgentChatRequest):
    if not req.message or not req.message.strip():
        raise HTTPException(status_code=400, detail="message must not be empty")
    sid = req.session_id or uuid.uuid4().hex
    session = AGENT_SESSIONS.get(sid)
    if session is None:
        sid = uuid.uuid4().hex
        session = _new_agent_session()
        AGENT_SESSIONS[sid] = session
        while len(AGENT_SESSIONS) > AGENT_SESSION_CAP:
            AGENT_SESSIONS.pop(next(iter(AGENT_SESSIONS)))
    out = run_agent(session, req.message.strip(), AGENT_EXECUTORS)
    return _agent_payload(sid, out)


@app.post("/api/agent/confirm")
def agent_confirm(req: AgentConfirmRequest):
    session = AGENT_SESSIONS.get(req.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="unknown session")
    pend = session.get("pending")
    if not pend or pend.get("action_id") != req.action_id:
        raise HTTPException(status_code=409, detail="no matching pending action")
    out = resume_agent(session, req.action_id, req.approved, AGENT_EXECUTORS)
    return _agent_payload(req.session_id, out)


@app.post("/api/agent/reset")
def agent_reset(req: AgentResetRequest):
    AGENT_SESSIONS.pop(req.session_id, None)
    return {"status": "cleared"}


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

    for eid, disp in DISPATCHED_EMAILS_STORE.items():
        events.append({
            "email_id": eid,
            "action": f"EMAIL_{disp.get('status', 'SENT')}",
            "actor": "Auto-Draft Email Studio",
            "notes": disp.get("message") or f"Dispatched email to {disp.get('to')}: {disp.get('subject')}",
            "timestamp": disp.get("sent_at"),
            "details": disp
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
    "resume": True,
}
SUBMISSION_STORE = {}
_SUBMISSION_META = {}


def _write_submission(allow_shrink=False):
    # Never silently shrink the scored submission file: merge in any
    # on-disk entries the in-memory store doesn't have (in-memory entries
    # win on overlap — they're fresher). allow_shrink=True is reserved for
    # deliberate fresh non-resume pipeline runs that own the whole file.
    data = dict(SUBMISSION_STORE)
    if not allow_shrink:
        on_disk = _load_submission_file()
        for k, v in on_disk.items():
            if k not in data:
                data[k] = v
        SUBMISSION_STORE.update(data)
    # Atomic write: a crash mid-dump must not leave a truncated file.
    tmp_path = SUBMISSION_PATH + ".tmp"
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, SUBMISSION_PATH)


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
    # A fresh non-resume run owns submission.json and may legitimately
    # truncate it; a resume run must merge so unprocessed entries survive.
    allow_shrink = not PIPELINE_STATE.get("resume", True)
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
            _write_submission(allow_shrink)  # checkpoint after each batch
    except Exception as e:
        PIPELINE_STATE["error"] = str(e)
    finally:
        try:
            _write_submission(allow_shrink)
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
        "resume": req.resume,
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
