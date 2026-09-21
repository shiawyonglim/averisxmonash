import asyncio
import os
import json
import time
import re
import shutil
import tempfile
import threading
import smtplib
import imaplib
import email
from concurrent.futures import ThreadPoolExecutor
from email.header import decode_header
import uuid
from typing import Optional, Dict, List, Any
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
from pipeline.ai_engine import extract_shipping_fields, extract_fields_tiered, reason_and_verify_with_ai, classify_email, quick_classify, classify_email_detailed, analyze_document_image, MODEL
from pipeline.comparator import compare_fields
from pipeline.edge_cases import check_wrong_doc_type, diagnose_attachment, is_si_attachment
from pipeline.parsers import extract_text, is_image_file, validate_image, IMAGE_EXTENSIONS, extract_shipping_fields_fast, extract_inline_si_fields
from pipeline.main import process_email, submission_row
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
        details_sidecar = {}
        details_path = os.path.join(os.path.dirname(SUBMISSION_PATH), "verification_details.json")
        if os.path.exists(details_path):
            try:
                with open(details_path, "r", encoding="utf-8") as df:
                    details_sidecar = json.load(df)
            except Exception:
                details_sidecar = {}
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
                
                det = details_sidecar.get(eid, {})
                eff_si = det.get("si_fields") or si_fields
                eff_bl = det.get("bl_fields") or bl_fields
                eff_comp = det.get("field_comparisons") or comparisons

                defects = r.get("defect_fields", [])
                if defects:
                    summary = f"Defect detected in: {', '.join(defects)}"
                    thoughts = "Verified deterministically against Shipping Instructions and draft Bill of Lading standards."
                elif r.get("status") == "NEEDS_REVIEW":
                    summary = f"Escalated to human review ({r.get('review_reason')})"
                    thoughts = "Verified deterministically against Shipping Instructions and draft Bill of Lading standards."
                elif not eff_comp:
                    # OK verdict but no field audit ever ran — never claim a match.
                    if r.get("category") == "BL_COMPARISON":
                        summary = ("No field comparison performed — the email did not carry "
                                   "the required SI and draft BL attachments.")
                        thoughts = ("Automated audit did not run: fewer than 2 shipping "
                                    "attachments were provided. Handled via the missing-BL / "
                                    "carrier-chaser workflow, not field comparison.")
                    else:
                        summary = "No SI/BL comparison required for this email."
                        thoughts = (f"Classified as {r.get('category', 'GENERAL')} — not an SI vs "
                                    "draft BL comparison request, so no field audit was performed.")
                else:
                    summary = "All 7 critical shipping fields match accurately."
                    thoughts = "Verified deterministically against Shipping Instructions and draft Bill of Lading standards."

                VERDICTS_STORE[eid] = {
                    "status": r.get("status"),
                    "review_reason": r.get("review_reason"),
                    "defect_fields": defects,
                    "si_fields": eff_si,
                    "bl_fields": eff_bl,
                    "field_comparisons": eff_comp,
                    "details": det,
                    "evidence": det.get("evidence", []),
                    "si_provenance": det.get("si_provenance") or {k: "regex" for k in (eff_si or {})},
                    "bl_provenance": det.get("bl_provenance") or {k: "regex" for k in (eff_bl or {})},
                    "thoughts": thoughts,
                    "summary_reason": summary,
                    "verified_at": _utcnow()
                }
    except Exception as e:
        print(f"Error loading initial verdicts: {e}")

_init_verdicts_from_submission()

SI_FIELD_LABELS = {
    "shipper": "Shipper",
    "consignee": "Consignee",
    "notify_party": "Notify Party",
    "port_of_loading": "Port of Loading",
    "port_of_discharge": "Port of Discharge",
    "container_count": "Container Count",
    "gross_weight_kg": "Gross Weight (KG)",
}

def _materialize_inline_si(email_id, email_data):
    """
    When a Shipping Instruction lives inside the email body rather than an
    attachment, persist it as a real document file
    (attachments/{eid}_SI_from_body.txt) so it behaves like a document
    downstream — shown in the SI pane, editable via save-documents,
    diffable, auditable.

    The pointer is stored under 'generated_si' in the email JSON —
    deliberately NOT in attachments[] — so classification, corrupt-scan and
    submission verdicts are unaffected. Idempotent: reuses the file once
    written (operator edits to it are preserved).
    Returns the bundle-relative path, or None when nothing was written.
    """
    gen = email_data.get("generated_si")
    if gen and os.path.exists(os.path.join(BUNDLE_DIR, gen)):
        return gen

    body = email_data.get("body", "")
    if not extract_inline_si_fields(body):
        return None

    fields, prov = extract_fields_tiered(body)
    if not any(str(v).strip() and str(v).strip().upper() != "ERROR"
               for v in (fields or {}).values()):
        return None

    lines = [
        "SHIPPING INSTRUCTION",
        f"(Auto-extracted from email body - {email_id})",
        "",
    ]
    for f in DEFECT_FIELDS:
        val = str((fields or {}).get(f) or "").strip()
        if not val or val.upper() == "ERROR":
            val = "MISSING_VALUE"
        lines.append(f"{SI_FIELD_LABELS[f]}: {val}")
    lines += [
        "",
        f"--- Source: email body of {email_id} ---",
        body.strip(),
        "",
    ]

    rel = f"attachments/{email_id}_SI_from_body.txt"
    try:
        os.makedirs(os.path.join(BUNDLE_DIR, "attachments"), exist_ok=True)
        _backup_email_files(email_id)  # snapshot the JSON before adding the pointer
        with open(os.path.join(BUNDLE_DIR, rel), "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
        email_data["generated_si"] = rel
        email_data["generated_si_fields"] = fields
        email_data["generated_si_provenance"] = prov
        with open(os.path.join(INBOX_DIR, f"{email_id}.json"), "w", encoding="utf-8") as fh:
            json.dump(email_data, fh, indent=2, ensure_ascii=False)
        INBOX_CACHE[email_id] = email_data
        _append_backup_manifest({"timestamp": _utcnow(), "email_id": email_id,
                                 "action": "materialize_si", "files": {"si": rel}})
        _log_to_supabase_audit(email_id, "SI_MATERIALIZED", {"file": rel})
        return rel
    except Exception as e:
        print(f"Inline SI materialization failed for {email_id}: {e}")
        return None


def _si_conflict(att_si_text, body):
    """
    An email may carry an attached SI AND a second SI written in the body.
    When they disagree on the 7 core fields that is a real operational
    issue (one version was edited). Returns a conflict descriptor or None.
    The attached SI stays authoritative — this only surfaces the diff.
    """
    body_fields = extract_inline_si_fields(body)
    if not body_fields or not att_si_text or _is_placeholder_text(att_si_text):
        return None
    att_fields, _ = extract_fields_tiered(att_si_text)
    defect_fields, _missing, comparisons = compare_fields(att_fields, body_fields)
    if not defect_fields:
        return None
    return {
        "fields": defect_fields,
        "attachment_values": {f: att_fields.get(f) for f in defect_fields},
        "body_values": {f: body_fields.get(f) for f in defect_fields},
        "comparisons": {f: comparisons.get(f) for f in defect_fields},
    }


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

        det = classify_email_detailed(email_data.get("subject", ""), email_data.get("body", ""), len(atts) > 0)
        category = det["category"]
        CLASSIFICATIONS_STORE[email_id] = category
        email_info = {
            "email_id": email_id,
            "from": email_data.get("from", "Unknown"),
            "subject": email_data.get("subject", "No subject"),
            "body": email_data.get("body", ""),
            "attachments": att_meta,
            "category": category,
            "subcategory": det["subcategory"],
            "display_tag": det["display_tag"],
            "category_description": det["description"]
        }

        threads = EMAIL_THREADS_STORE.get(email_id, [])
        inbound_replies = [m for m in threads if m.get("direction") == "INBOUND"]
        email_info["has_reply"] = len(inbound_replies) > 0
        email_info["reply_count"] = len(inbound_replies)
        email_info["thread_count"] = len(threads)
        email_info["threads"] = threads

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
            verdict = VERDICTS_STORE.get(email_id)
            is_corrupted = bool(verdict and verdict.get("status") == "NEEDS_REVIEW" and verdict.get("review_reason") == "missing_attachment")
            missing_doc = None
            si_conflict = None
            si_source = None

            # A Shipping Instruction written directly into the email body is
            # a real document — materialize it into a file so the SI pane
            # shows actual content and it becomes editable/auditable.
            generated_rel = None
            generated_si_text = None
            if extract_inline_si_fields(email_data.get("body", "")):
                generated_rel = _materialize_inline_si(email_id, email_data)
                if generated_rel:
                    generated_si_text = extract_text(os.path.join(BUNDLE_DIR, generated_rel))
                    email_info["si_fields"] = email_data.get("generated_si_fields") or {}
                    email_info["si_provenance"] = email_data.get("generated_si_provenance") or {}
                    email_info["generated_si"] = generated_rel

            # Work out which document is missing from the one attachment
            # that did arrive — filename convention first, then content.
            present_text = None
            present_side = None
            present_bad = False
            present_reason = None
            if len(atts) == 1:
                p1 = os.path.join(BUNDLE_DIR, atts[0])
                present_bad, present_reason = diagnose_attachment(p1)
                present_text = (f"[CORRUPTED FILE: {atts[0]} is unreadable - {present_reason}]"
                                if present_bad else extract_text(p1))
                present_side = "si" if is_si_attachment(p1, "" if present_bad else present_text) else "bl"

            if generated_rel:
                # The body carried the SI — the draft BL is the open item.
                si_text = generated_si_text
                si_source = "email_body"
                issue_detail = None
                corrupt_reason = None
                if present_side == "si" and not present_bad:
                    # An attached SI AND a body SI: the attachment is
                    # authoritative, but surface disagreements.
                    si_text = present_text
                    si_source = "attachment"
                    si_conflict = _si_conflict(present_text, email_data.get("body", ""))
                if present_side == "bl" and not present_bad:
                    bl_text = present_text
                    missing_doc = None  # draft BL arrived; inline SI completes the pair
                else:
                    bl_text = "[AWAITING CARRIER DRAFT BL]\nShipping Instruction received (inline in the email body) — the carrier has not yet issued the draft Bill of Lading."
                    missing_doc = "bl"
                    if present_bad:
                        issue_detail = f"{atts[0]}: {present_reason}"
            elif category != "BL_COMPARISON":
                si_text = f"[NON-COMPARISON CATEGORY: {category}]\nThis email is classified as {category} ({det['description']}). It does not carry or require a Shipping Instruction (SI) document."
                bl_text = f"[NON-COMPARISON CATEGORY: {category}]\nThis email is classified as {category} ({det['description']}). It does not carry or require a draft Bill of Lading (BL)."
                issue_detail = None
                corrupt_reason = None
            else:
                if len(atts) == 1:
                    missing_doc = "bl" if present_side == "si" else "si"
                elif len(atts) == 0:
                    missing_doc = "both"

                if is_corrupted:
                    si_text = "[MISSING ATTACHMENT: Shipping Instruction is absent from this comparison request]"
                    bl_text = "[MISSING ATTACHMENT: Draft Bill of Lading is absent from this comparison request]"
                    if present_side == "si":
                        si_text = present_text
                    elif present_side == "bl":
                        bl_text = present_text
                    issue_detail = f"Comparison requested but only {len(atts)} attachment(s) found. Escalated to NEEDS_REVIEW."
                    corrupt_reason = "missing_attachment"
                else:
                    si_text = "[INBOUND CHASER / STATUS REQUEST]\nNo Shipping Instruction attached. Inbound request regarding draft BL issuance."
                    bl_text = "[AWAITING CARRIER DRAFT BL]\nNo draft Bill of Lading attached. Carrier has not yet issued the draft document."
                    if present_side == "si":
                        si_text = present_text
                        bl_text = "[AWAITING CARRIER DRAFT BL]\nShipping Instruction received — the carrier has not yet issued the draft Bill of Lading."
                    elif present_side == "bl":
                        bl_text = present_text
                        si_text = "[MISSING ATTACHMENT: Shipping Instruction is absent — request it from the shipper]"
                    issue_detail = None
                    corrupt_reason = None

            return {
                "email": email_info,
                "threads": threads,
                "si_text": si_text,
                "bl_text": bl_text,
                "si_source": si_source,
                "si_conflict": si_conflict,
                "si_inline": bool(generated_rel),
                "generated_si": generated_rel,
                "is_corrupted": is_corrupted,
                "corrupt_reason": corrupt_reason,
                "issue_details": issue_detail,
                "missing_doc": missing_doc,
                "verdict": verdict,
                "resolution": RESOLUTIONS_STORE.get(email_id),
                "supabase_record": sb_rec,
                "cloud_synced": bool(sb_rec is not None),
                "backups": _list_backups(email_id)
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
            si_doc_text, bl_doc_text = text1, text2
        else:
            si_doc_text, bl_doc_text = text2, text1

        # A second SI written in the email body may disagree with the
        # attached one — surface the diff; the attachment stays authoritative.
        body_inline = extract_inline_si_fields(email_data.get("body", ""))
        si_conflict = _si_conflict(si_doc_text, email_data.get("body", "")) if body_inline else None

        return {
            "email": email_info,
            "threads": threads,
            "si_text": si_doc_text,
            "bl_text": bl_doc_text,
            "si_source": "attachment",
            "si_conflict": si_conflict,
            "si_inline": bool(body_inline),
            "generated_si": None,
            "is_corrupted": is_corrupt,
            "corrupt_reason": "unreadable" if is_corrupt else None,
            "issue_details": issue_detail,
            "missing_doc": None,
            "verdict": VERDICTS_STORE.get(email_id),
            "resolution": RESOLUTIONS_STORE.get(email_id),
            "supabase_record": sb_rec,
            "cloud_synced": bool(sb_rec is not None),
            "backups": _list_backups(email_id)
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ---------------------------------------------------------------------------
# Document editing + backup layer
# SI/BL textarea edits are written back to the attachment files (and the email
# JSON is repointed when a binary format can't be rewritten). Every write is
# preceded by a snapshot under sdoc-hackathon-bundle/_backups/:
#   _backups/original/<eid>/            pristine first version (demo rollback)
#   _backups/<eid>/<utc-timestamp>/     full save history
#   _backups/manifest.json              append-only log of saves/restores
# ---------------------------------------------------------------------------
BACKUP_ROOT = os.path.join(BUNDLE_DIR, "_backups")
BACKUP_MANIFEST = os.path.join(BACKUP_ROOT, "manifest.json")
DOC_EDITS_STORE = {}

# UI-generated placeholder texts — never persisted into real attachments.
_PLACEHOLDER_RX = re.compile(
    r'^\s*\[(MISSING ATTACHMENT|CORRUPTED FILE|NON-COMPARISON|'
    r'INBOUND CHASER|AWAITING CARRIER|IMAGE DOCUMENT)', re.IGNORECASE)


def _is_placeholder_text(text):
    return bool(_PLACEHOLDER_RX.match(text or ""))


def _backup_timestamp():
    return _utcnow().replace(":", "-").replace(".", "_")


def _append_backup_manifest(entry):
    try:
        os.makedirs(BACKUP_ROOT, exist_ok=True)
        manifest = []
        if os.path.exists(BACKUP_MANIFEST):
            with open(BACKUP_MANIFEST, "r", encoding="utf-8") as f:
                manifest = json.load(f)
        manifest.append(entry)
        with open(BACKUP_MANIFEST, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)
    except Exception as e:
        print(f"Backup manifest error: {e}")


def _backup_email_files(email_id, extra_files=None):
    """
    Snapshot the inbox JSON + every referenced attachment before modification.
    The 'original' dir is written only once — it stays pristine for rollback.
    Returns the list of files that were snapshotted this call.
    """
    email_path = os.path.join(INBOX_DIR, f"{email_id}.json")
    d = _get_email_data(email_id)
    att_paths = [os.path.join(BUNDLE_DIR, a) for a in d.get("attachments", [])]
    att_paths += list(extra_files or [])

    snapped = []

    orig_dir = os.path.join(BACKUP_ROOT, "original", email_id)
    if not os.path.exists(os.path.join(orig_dir, f"{email_id}.json")):
        os.makedirs(os.path.join(orig_dir, "attachments"), exist_ok=True)
        if os.path.exists(email_path):
            shutil.copy2(email_path, os.path.join(orig_dir, f"{email_id}.json"))
            snapped.append(email_path)
        for p in att_paths:
            if os.path.exists(p):
                shutil.copy2(p, os.path.join(orig_dir, "attachments", os.path.basename(p)))

    hist_dir = os.path.join(BACKUP_ROOT, email_id, _backup_timestamp())
    os.makedirs(os.path.join(hist_dir, "attachments"), exist_ok=True)
    if os.path.exists(email_path):
        shutil.copy2(email_path, os.path.join(hist_dir, f"{email_id}.json"))
    for p in att_paths:
        if os.path.exists(p):
            shutil.copy2(p, os.path.join(hist_dir, "attachments", os.path.basename(p)))

    return snapped


def _list_backups(email_id):
    orig = os.path.exists(os.path.join(BACKUP_ROOT, "original", email_id, f"{email_id}.json"))
    hist_dir = os.path.join(BACKUP_ROOT, email_id)
    history = sorted(os.listdir(hist_dir)) if os.path.isdir(hist_dir) else []
    return {"has_original": orig, "snapshots": history}


def _write_document_text(path, text):
    """
    Persist edited document text back to disk, preserving format where
    possible. Returns (written_path, remapped_from_or_None) — binary formats
    (pdf/images) can't be safely rewritten, so a .txt twin is created and the
    caller repoints the email's attachment entry.
    """
    ext = os.path.splitext(path)[1].lower()
    if ext == ".docx":
        import docx
        doc = docx.Document()
        for line in text.split("\n"):
            doc.add_paragraph(line)
        doc.save(path)
        return path, None
    if ext == ".xlsx":
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Document"
        for i, line in enumerate(text.split("\n"), start=1):
            ws.cell(row=i, column=1, value=line)
        wb.save(path)
        return path, None
    if ext == ".pdf" or ext in IMAGE_EXTENSIONS:
        txt_path = os.path.splitext(path)[0] + ".txt"
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(text)
        return txt_path, path
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path, None


def _attachment_entry_for(path):
    """Convert an absolute file path back to the 'attachments/xxx' entry style
    used in the email JSON."""
    return "attachments/" + os.path.basename(path)


class SaveDocumentsRequest(BaseModel):
    si_text: str = ""
    bl_text: str = ""


@app.post("/api/email/{email_id}/save-documents")
def save_email_documents(email_id: str, req: SaveDocumentsRequest):
    if not _email_exists(email_id):
        raise HTTPException(status_code=404, detail=f"Email {email_id} not found")

    email_path = os.path.join(INBOX_DIR, f"{email_id}.json")
    email_data = _get_email_data(email_id)
    atts = list(email_data.get("attachments", []))
    gen_si = email_data.get("generated_si")
    if len(atts) < 2 and not gen_si:
        raise HTTPException(
            status_code=400,
            detail="This email does not carry two document attachments — nothing to overwrite.")

    # Map SI/BL texts to the right files (same logic as get_email_content).
    # A materialized body-SI file stands in for the SI attachment.
    si_att = bl_att = None
    if len(atts) >= 2:
        path1 = os.path.join(BUNDLE_DIR, atts[0])
        path2 = os.path.join(BUNDLE_DIR, atts[1])
        t1 = extract_text(path1)
        si_att, bl_att = (atts[0], atts[1]) if is_si_attachment(path1, t1) else (atts[1], atts[0])
    elif len(atts) == 1:
        p1 = os.path.join(BUNDLE_DIR, atts[0])
        if is_si_attachment(p1, extract_text(p1)):
            si_att = atts[0]  # attached SI stays authoritative over the body copy
        else:
            bl_att, si_att = atts[0], gen_si
    else:
        si_att = gen_si

    sides = [("si", si_att, req.si_text), ("bl", bl_att, req.bl_text)]
    skipped = []
    for side, att, txt in sides:
        if _is_placeholder_text(txt):
            skipped.append(f"{side}_text is a placeholder — nothing to save")
        elif att is None:
            skipped.append(f"no {side.upper()} document exists to write to")
    if len(skipped) == 2:
        raise HTTPException(status_code=400, detail="Both documents are placeholders — nothing to save.")

    # Snapshot before touching anything (original + timestamped history)
    _backup_email_files(email_id, extra_files=[os.path.join(BUNDLE_DIR, gen_si)] if gen_si else None)

    saved, remapped = {}, []
    for side, att, txt in sides:
        if att is None or _is_placeholder_text(txt):
            continue
        src_path = os.path.join(BUNDLE_DIR, att)
        written_path, was_remapped = _write_document_text(src_path, txt)
        saved[side] = os.path.basename(written_path)
        if was_remapped:
            new_entry = _attachment_entry_for(written_path)
            if att in atts:
                idx = atts.index(att)
                atts[idx] = new_entry
            elif att == gen_si:
                email_data["generated_si"] = new_entry
                gen_si = new_entry
            remapped.append({"from": att, "to": new_entry})

    # Repoint attachment entries when a binary file was swapped for its .txt twin
    if remapped:
        email_data["attachments"] = atts
        with open(email_path, "w", encoding="utf-8") as f:
            json.dump(email_data, f, indent=2, ensure_ascii=False)

    record = {
        "timestamp": _utcnow(),
        "action": "DOCUMENT_EDITED",
        "files": saved,
        "remapped": remapped,
        "skipped": skipped,
        "notes": f"Operator edited document text: {', '.join(saved.values())}",
    }
    DOC_EDITS_STORE.setdefault(email_id, []).append(record)
    _persist_audit_state()
    _append_backup_manifest({"timestamp": record["timestamp"], "email_id": email_id,
                             "action": "save", "files": saved, "remapped": remapped})
    _log_to_supabase_audit(email_id, "DOCUMENT_EDITED", record)

    return {
        "status": "SUCCESS",
        "saved": saved,
        "skipped": skipped,
        "remapped": remapped,
        "backups": _list_backups(email_id),
        "message": f"Saved edits for {email_id} ({', '.join(saved.values())}). "
                   f"Originals backed up under _backups/.",
    }


@app.post("/api/email/{email_id}/restore-documents")
def restore_email_documents(email_id: str):
    """Restore the pristine email JSON + attachments captured before the first edit."""
    if not _email_exists(email_id):
        raise HTTPException(status_code=404, detail=f"Email {email_id} not found")
    orig_dir = os.path.join(BACKUP_ROOT, "original", email_id)
    orig_json = os.path.join(orig_dir, f"{email_id}.json")
    if not os.path.exists(orig_json):
        raise HTTPException(status_code=400, detail="No backup exists for this email.")

    gen_rel = _get_email_data(email_id).get("generated_si")
    _backup_email_files(email_id, extra_files=[os.path.join(BUNDLE_DIR, gen_rel)] if gen_rel else None)

    shutil.copy2(orig_json, os.path.join(INBOX_DIR, f"{email_id}.json"))
    att_dir = os.path.join(orig_dir, "attachments")
    restored_files = []
    if os.path.isdir(att_dir):
        for fn in os.listdir(att_dir):
            shutil.copy2(os.path.join(att_dir, fn), os.path.join(BUNDLE_DIR, "attachments", fn))
            restored_files.append(fn)

    # The pristine JSON carries no generated_si pointer — drop the stale
    # materialized file; it is regenerated below if the body still holds an SI.
    if gen_rel:
        gp = os.path.join(BUNDLE_DIR, gen_rel)
        if os.path.exists(gp):
            try:
                os.remove(gp)
            except OSError:
                pass

    INBOX_CACHE.pop(email_id, None)
    email_data = _get_email_data(email_id)

    si_text = bl_text = ""
    atts = email_data.get("attachments", [])
    if len(atts) >= 2:
        p1 = os.path.join(BUNDLE_DIR, atts[0])
        p2 = os.path.join(BUNDLE_DIR, atts[1])
        t1, t2 = extract_text(p1), extract_text(p2)
        if is_si_attachment(p1, t1):
            si_text, bl_text = t1, t2
        else:
            si_text, bl_text = t2, t1
    elif extract_inline_si_fields(email_data.get("body", "")):
        regen = _materialize_inline_si(email_id, email_data)
        if regen:
            si_text = extract_text(os.path.join(BUNDLE_DIR, regen))

    record = {
        "timestamp": _utcnow(),
        "action": "DOCUMENT_RESTORED",
        "files": restored_files,
        "notes": "Restored pristine documents from _backups/original/.",
    }
    DOC_EDITS_STORE.setdefault(email_id, []).append(record)
    _persist_audit_state()
    _append_backup_manifest({"timestamp": record["timestamp"], "email_id": email_id,
                             "action": "restore", "files": restored_files})
    _log_to_supabase_audit(email_id, "DOCUMENT_RESTORED", record)

    return {
        "status": "SUCCESS",
        "si_text": si_text,
        "bl_text": bl_text,
        "backups": _list_backups(email_id),
        "message": f"Restored {email_id} from the pristine backup "
                   f"({len(restored_files)} attachment(s) + email JSON).",
    }


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
    _persist_audit_state()
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
    _persist_audit_state()
    return {"status": "SUCCESS", "message": f"{eid} marked as {action}"}

CARRIER_DESK_EMAILS = {
    "EVERGREEN": "doc.desk@evergreen-marine.com",
    "MSC": "bl.documentation@msc.com",
    "MAERSK": "import-export.docs@maersk.com",
    "CMA": "liner.docs@cma-cgm.com",
    "CMA CGM": "liner.docs@cma-cgm.com",
    "HAPAG": "doc.service@hlag.com",
    "HAPAG-LLOYD": "doc.service@hlag.com",
    "ONE": "ocean.docs@one-line.com",
    "OOCL": "liner.docs@oocl.com",
    "PIL": "bl.desk@pilship.com",
    "YANG MING": "doc.desk@yangming.com",
    "MONTER": "documentation@monter-lines.com"
}

def get_carrier_desk_email(carrier):
    if not carrier:
        return "carrier-desk@shippingline.com"
    c_upper = str(carrier).upper()
    for k, v in CARRIER_DESK_EMAILS.items():
        if k in c_upper:
            return v
    clean = re.sub(r'[^a-zA-Z0-9]', '', str(carrier)).lower()
    return f"doc.desk@{clean or 'carrier'}.com"

AUDIT_PERSIST_PATH = os.path.join(BASE_DIR, ".cache", "audit_state.json")

def _persist_audit_state():
    try:
        os.makedirs(os.path.dirname(AUDIT_PERSIST_PATH), exist_ok=True)
        payload = {
            "resolutions": RESOLUTIONS_STORE,
            "chasers": CHASERS_STORE,
            "corrupted": CORRUPTED_STORE,
            "dispatched": DISPATCHED_EMAILS_STORE,
            "doc_edits": DOC_EDITS_STORE
        }
        with open(AUDIT_PERSIST_PATH, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
    except Exception as e:
        print(f"Error saving audit state: {e}")

def _load_audit_state():
    if not os.path.exists(AUDIT_PERSIST_PATH):
        return
    try:
        with open(AUDIT_PERSIST_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        RESOLUTIONS_STORE.update(d.get("resolutions", {}))
        CHASERS_STORE.update(d.get("chasers", {}))
        CORRUPTED_STORE.update(d.get("corrupted", {}))
        DISPATCHED_EMAILS_STORE.update(d.get("dispatched", {}))
        DOC_EDITS_STORE.update(d.get("doc_edits", {}))
    except Exception as e:
        print(f"Error loading audit state: {e}")

CHASERS_STORE = {}
_load_audit_state()

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

                si_inline = len(atts) == 0 and bool(extract_inline_si_fields(body))
                if si_inline:
                    missing_type = "SI Received Inline (in body) - Draft BL Awaited"
                elif len(atts) == 0:
                    missing_type = "Draft BL Missing (0 attachments)"
                else:
                    missing_type = f"SI Received ({len(atts)} doc) - Draft BL Missing"

                missing.append({
                    "email_id": eid,
                    "subject": subj,
                    "from": d.get("from", "Unknown"),
                    "body": body,
                    "carrier": carrier_name,
                    "ref_no": ref_no,
                    "missing_type": missing_type,
                    "si_inline": si_inline,
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
    _persist_audit_state()
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
    _persist_audit_state()
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

    # Ensure subject carries thread tracking reference
    ref_tag = f"[REF: {req.email_id}]" if req.email_id else ""
    if ref_tag and ref_tag.lower() not in req.subject.lower():
        req.subject = f"{req.subject} {ref_tag}"
    msg_id = f"<averis-ops-{req.email_id or 'gen'}-{uuid.uuid4().hex[:8]}@averis-freight.com>"

    if not is_live:
        result = {
            "status": "SIMULATED_SENT",
            "delivered": True,
            "mode": "Simulation (Pre-configured Google SMTP Ready)",
            "message": f"Email successfully validated and dispatched to {req.to_email} via simulated Google SMTP pipeline. (To route over live Gmail, enter your Google App Password in the SMTP settings modal or in .env).",
            "to": req.to_email,
            "subject": req.subject,
            "message_id": msg_id,
            "body_snippet": req.body[:150] + "...",
            "sent_at": now
        }
    else:
        try:
            msg = MIMEMultipart()
            msg["From"] = user
            msg["To"] = req.to_email
            msg["Subject"] = req.subject
            msg["Message-ID"] = msg_id
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
                "message_id": msg_id,
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
                "message_id": msg_id,
                "error": err_msg,
                "sent_at": now
            }

    if req.email_id:
        outbound_msg = {
            "id": f"msg_out_{uuid.uuid4().hex[:8]}",
            "direction": "OUTBOUND",
            "message_id": msg_id,
            "from_addr": user or "ops@averis-freight.com",
            "to_addr": req.to_email,
            "subject": req.subject,
            "body": req.body,
            "sent_at": now,
            "status": "DELIVERED",
            "mode": result.get("mode", "SMTP")
        }
        _add_thread_message(req.email_id, outbound_msg)
        DISPATCHED_EMAILS_STORE[req.email_id] = result
        _persist_audit_state()
        _log_to_supabase_audit(req.email_id, "EMAIL_DISPATCHED", result)

    return result


# ============================================================
# Automated Inbound Thread Tracking & Reply Ingestion Engine
# ============================================================

EMAIL_THREADS_FILE = os.path.join(BASE_DIR, "email_threads.json")

def _load_email_threads() -> dict:
    if os.path.exists(EMAIL_THREADS_FILE):
        try:
            with open(EMAIL_THREADS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def _save_email_threads(threads: dict):
    try:
        with open(EMAIL_THREADS_FILE, "w", encoding="utf-8") as f:
            json.dump(threads, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"Error saving email threads: {e}")

EMAIL_THREADS_STORE: dict = _load_email_threads()

def _add_thread_message(email_id: str, msg: dict):
    if email_id not in EMAIL_THREADS_STORE:
        EMAIL_THREADS_STORE[email_id] = []
    EMAIL_THREADS_STORE[email_id].append(msg)
    _save_email_threads(EMAIL_THREADS_STORE)

def _find_email_id_for_reply(subject: str = "", body: str = "", in_reply_to: str = "", references: str = "") -> Optional[str]:
    for txt in [subject, body, in_reply_to, references]:
        if not txt:
            continue
        m = re.search(r'\[REF:\s*(email_\d+)\]', txt, re.IGNORECASE)
        if m:
            return m.group(1).lower()
        m2 = re.search(r'\b(email_\d+)\b', txt, re.IGNORECASE)
        if m2 and ("averis" in txt.lower() or "chaser" in txt.lower() or "draft" in txt.lower() or "bl" in txt.lower() or "booking" in txt.lower()):
            return m2.group(1).lower()

    for eid, msgs in EMAIL_THREADS_STORE.items():
        for msg in msgs:
            orig_subj = (msg.get("subject") or "").lower().replace("re:", "").strip()
            if orig_subj and orig_subj in (subject or "").lower():
                return eid
    return None

def process_inbound_reply(
    email_id: Optional[str] = None,
    from_addr: str = "carrier-ops@shipping-line.com",
    subject: str = "Re: Documentation Update",
    body: str = "",
    attachments: Optional[list] = None,
    in_reply_to: Optional[str] = None
) -> dict:
    matched_id = email_id or _find_email_id_for_reply(subject, body, in_reply_to or "")
    target_id = matched_id or "unmatched_replies"

    now = _utcnow()
    reply_msg = {
        "id": f"msg_in_{uuid.uuid4().hex[:8]}",
        "direction": "INBOUND",
        "from_addr": from_addr,
        "to_addr": "ops@averis-freight.com",
        "subject": subject,
        "body": body,
        "received_at": now,
        "status": "RECEIVED",
        "in_reply_to": in_reply_to,
        "attachments": attachments or []
    }
    _add_thread_message(target_id, reply_msg)

    # Auto-update status of chasers
    if target_id in CHASERS_STORE:
        CHASERS_STORE[target_id]["status"] = "REPLY_RECEIVED"
        CHASERS_STORE[target_id]["reply_received_at"] = now
        CHASERS_STORE[target_id]["reply_sender"] = from_addr
        CHASERS_STORE[target_id]["reply_preview"] = body[:200]

    _log_to_supabase_audit(target_id, "INBOUND_REPLY_RECEIVED", {
        "from": from_addr,
        "subject": subject,
        "preview": body[:120],
        "has_attachments": bool(attachments)
    })

    return {
        "status": "SUCCESS",
        "email_id": target_id,
        "message": f"Inbound reply successfully captured and linked to thread {target_id}",
        "record": reply_msg,
        "thread_length": len(EMAIL_THREADS_STORE.get(target_id, []))
    }

@app.get("/api/email/{email_id}/thread")
def get_email_thread(email_id: str):
    if not _email_exists(email_id) and email_id not in EMAIL_THREADS_STORE:
        raise HTTPException(status_code=404, detail="Email thread not found")
    threads = EMAIL_THREADS_STORE.get(email_id, [])
    initial = _get_email_data(email_id)
    return {
        "email_id": email_id,
        "thread_count": len(threads),
        "messages": threads,
        "initial_email": initial
    }

class InboundWebhookRequest(BaseModel):
    email_id: Optional[str] = None
    from_email: str = "carrier@liner.com"
    subject: str = ""
    body: str = ""
    attachments: Optional[list] = None
    in_reply_to: Optional[str] = None

@app.post("/api/email/inbound-webhook")
def receive_inbound_webhook(req: InboundWebhookRequest):
    return process_inbound_reply(
        email_id=req.email_id,
        from_addr=req.from_email,
        subject=req.subject,
        body=req.body,
        attachments=req.attachments,
        in_reply_to=req.in_reply_to
    )

class SimulateReplyRequest(BaseModel):
    sender: Optional[str] = None
    subject: Optional[str] = None
    body: Optional[str] = None
    has_attachment: bool = True

@app.post("/api/email/{email_id}/simulate-reply")
def simulate_carrier_reply(email_id: str, req: SimulateReplyRequest = None):
    if not _email_exists(email_id):
        raise HTTPException(status_code=404, detail=f"Email {email_id} not found")

    d = _get_email_data(email_id) or {}
    subj = d.get("subject", "Shipping Instructions")
    body_text = d.get("body", "")
    atts = d.get("attachments", [])
    has_bl = _has_bl_attachment(atts)
    det = classify_email_detailed(subj, body_text, len(atts) > 0)
    cat = det["category"]

    sender = (req.sender if req and req.sender else "Ocean Carrier Documentation Desk <ops@msc-oceanline.com>")
    reply_subj = (req.subject if req and req.subject else f"Re: {subj} [REF: {email_id}]")

    if req and req.body:
        reply_body = req.body
    elif not has_bl or "missing" in subj.lower():
        reply_body = (
            f"Dear Averis Freight Documentation Team,\n\n"
            f"Thank you for following up regarding reference {email_id}.\n"
            f"Please find attached the officially issued draft Bill of Lading (Draft BL) for your booking.\n"
            f"All shipping instructions, container manifests, and cargo weight declarations have been confirmed with terminal operations.\n\n"
            f"Please review and reply with your final confirmation so we can issue the Original Sea Waybill.\n\n"
            f"Best regards,\n"
            f"Carrier Export Documentation Services\n"
            f"Documentation Team Ref #{email_id.upper()}"
        )
    elif cat == "INVOICE_QUERY":
        reply_body = (
            f"Dear Accounting / Freight Operations,\n\n"
            f"We have re-examined demurrage calculation dispute for {email_id}.\n"
            f"Upon reviewing port terminal equipment interchange logs, the 4-day free-time extension was verified.\n"
            f"Credit Adjustment Note CR-89410 has been issued for USD 380.00. Revised account balance has been updated.\n\n"
            f"Sincerely,\n"
            f"Port Billing & Disbursements Team"
        )
    elif cat == "SI_REQUEST":
        reply_body = (
            f"Dear Shipper / Freight Forwarder,\n\n"
            f"Shipping Instructions submission for {email_id} has been accepted into our liner booking system.\n"
            f"Vessel cutoff schedule confirmed. Draft BL generation is currently in progress.\n\n"
            f"Best regards,\n"
            f"Global Booking Office"
        )
    else:
        reply_body = (
            f"Dear Averis Team,\n\n"
            f"Thank you for your message regarding {email_id}. We have received your request and updated the booking notes accordingly.\n\n"
            f"Kind regards,\n"
            f"Carrier Customer Service Support"
        )

    attachments = []
    if req and req.has_attachment:
        attachments = [{
            "filename": f"Revised_Draft_BL_{email_id.upper()}.pdf",
            "type": "application/pdf",
            "size_kb": 184,
            "description": "Carrier Revised Draft Bill of Lading document"
        }]

    return process_inbound_reply(
        email_id=email_id,
        from_addr=sender,
        subject=reply_subj,
        body=reply_body,
        attachments=attachments,
        in_reply_to=f"<averis-ops-{email_id}@averis-freight.com>"
    )

class ImapPollRequest(BaseModel):
    imap_host: str = ""
    imap_port: int = 0
    imap_user: str = ""
    imap_pass: str = ""
    limit: int = 10

@app.post("/api/email/imap-poll")
def poll_imap_inbox(req: ImapPollRequest):
    load_dotenv(override=True)
    host = req.imap_host or os.getenv("IMAP_HOST") or "imap.gmail.com"
    port = req.imap_port or int(os.getenv("IMAP_PORT") or 993)
    user = (req.imap_user or os.getenv("IMAP_USER") or os.getenv("DEFAULT_SENDER") or "").strip()
    password = (req.imap_pass or os.getenv("IMAP_PASSWORD") or os.getenv("SMTP_PASSWORD") or "").strip().replace(" ", "")

    is_live = bool(user and password and "@" in user and len(password) >= 6 and "example" not in user.lower())

    if not is_live:
        return {
            "status": "SIMULATED_POLL",
            "connected": False,
            "mode": "Simulation (Pre-configured Google IMAP Ready)",
            "message": f"IMAP listener verified for {host}:{port}. Enter your live Google App Password in .env (IMAP_PASSWORD) or provide it in request to poll live inbox.",
            "messages_checked": 0,
            "replies_matched": 0,
            "active_threads": len(EMAIL_THREADS_STORE)
        }

    try:
        mail = imaplib.IMAP4_SSL(host, port, timeout=15)
        mail.login(user, password)
        mail.select("INBOX")
        status, messages = mail.search(None, "UNSEEN")
        if status != "OK":
            return {"status": "ERROR", "message": "Failed to search inbox"}

        msg_ids = messages[0].split()
        matched = []
        for mid in msg_ids[-req.limit:]:
            res, data = mail.fetch(mid, "(RFC822)")
            if res != "OK":
                continue
            raw_email = data[0][1]
            msg = email.message_from_bytes(raw_email)

            subject_header = decode_header(msg.get("Subject", ""))[0]
            subject_text = subject_header[0]
            if isinstance(subject_text, bytes):
                subject_text = subject_text.decode(subject_header[1] or "utf-8", errors="ignore")

            from_header = msg.get("From", "Unknown")
            in_reply_to = msg.get("In-Reply-To", "")
            references = msg.get("References", "")

            # Extract body
            body_text = ""
            if msg.is_multipart():
                for part in msg.walk():
                    content_type = part.get_content_type()
                    content_disposition = str(part.get("Content-Disposition"))
                    if content_type == "text/plain" and "attachment" not in content_disposition:
                        payload = part.get_payload(decode=True)
                        if payload:
                            body_text += payload.decode("utf-8", errors="ignore")
            else:
                payload = msg.get_payload(decode=True)
                if payload:
                    body_text = payload.decode("utf-8", errors="ignore")

            reply_res = process_inbound_reply(
                email_id=None,
                from_addr=from_header,
                subject=subject_text,
                body=body_text,
                in_reply_to=in_reply_to
            )
            if reply_res.get("email_id") != "unmatched_replies":
                matched.append({
                    "email_id": reply_res.get("email_id"),
                    "subject": subject_text,
                    "from": from_header
                })

        mail.close()
        mail.logout()

        return {
            "status": "LIVE_POLL_SUCCESS",
            "connected": True,
            "messages_checked": min(len(msg_ids), req.limit),
            "unseen_total": len(msg_ids),
            "replies_matched": len(matched),
            "matched_details": matched
        }
    except Exception as e:
        return {
            "status": "IMAP_ERROR",
            "connected": False,
            "error": str(e),
            "message": f"IMAP connection failed: {e}. If using Gmail, make sure IMAP is enabled in Gmail settings and you are using a 16-character App Password."
        }


# ============================================================
# AUTO EMAIL GETTER & LAYA DECISION CLASSIFIER ENGINE
# (Connected to REAL Gmail via IMAP SSL & Google SMTP)
# ============================================================
try:
    import laya
    LAYA_AVAILABLE = True
except ImportError:
    laya = None
    LAYA_AVAILABLE = False

# Off by default: these endpoints read a real personal inbox and can send mail
# via stored SMTP creds — they must never be live on the public deployment.
# Set REAL_GMAIL_ENABLED=1 in .env for local demo use only.
REAL_GMAIL_ENABLED = os.getenv("REAL_GMAIL_ENABLED", "0") == "1"

class CustomIngestRequest(BaseModel):
    from_addr: str = "liner.desk@evergreen-marine.com"
    to_addr: str = "shipping.docs@aprilasia.com"
    subject: str = "DRAFT BL READY _ 5AKR-61849 _ PORT KLANG _ SIN832764835"
    body: str = "Dear Shiaw Yong Lim,\n\nPlease find attached draft Bill of Lading for verification before vessel cutoff.\n\nBest regards,\nEvergreen Marine Operations Desk"
    attachments: list = []

class FetchBatchRequest(BaseModel):
    count: int = 10

class PollGmailRequest(BaseModel):
    limit: int = 20
    only_unread: bool = False

class RealTestEmailRequest(BaseModel):
    subject: str = "URGENT DRAFT BL READY _ 5AKR-61849 _ PORT KLANG _ SIN832764835"
    body: str = "Dear Shiaw Yong Lim,\n\nPlease find attached draft Bill of Lading for verification before vessel cutoff at Port Klang.\n\nBest regards,\nEvergreen Marine Operations Desk"

GETTER_INGESTED_IDS = []
CUSTOM_INGESTED_ITEMS = []
REAL_GMAIL_ITEMS = []
_DOSSIER_CACHE = {}

LAYA_AGENT = None
_LAYA_LOAD_LOCK = threading.Lock()
_LAYA_STATS = {"calls": 0, "total_ms": 0.0}
_NEURAL_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="laya")

# Maritime label space for Laya's category question — real probabilities over
# the hackathon's categories instead of the default generic departments.
MARITIME_CATEGORIES = {
    "BL_COMPARISON": "draft Bill of Lading to check or compare against a Shipping Instruction",
    "SI_REQUEST": "prepare, submit or send a Shipping Instruction",
    "INVOICE_QUERY": "charges, freight, billing, invoices, demurrage or detention",
    "GENERAL": "operational updates, schedules, internal admin, automated reports",
    "SPAM": "unsolicited marketing, phishing or scams",
}

def _get_laya_agent():
    global LAYA_AGENT
    if not LAYA_AVAILABLE:
        return None
    if LAYA_AGENT is None:
        with _LAYA_LOAD_LOCK:  # pool workers race here on first enrich — load once
            if LAYA_AGENT is None:
                try:
                    print("[LAYA] Loading neural agent (convaiinnovations/laya)...", flush=True)
                    LAYA_AGENT = laya.load("convaiinnovations/laya")
                    print(f"[LAYA] ModernBERT neural agent loaded: {LAYA_AGENT}", flush=True)
                except Exception as e:
                    print(f"[LAYA] Neural agent unavailable, rules-only mode: {e}", flush=True)
    return LAYA_AGENT

def _clean_mime_header(raw_header):
    if not raw_header:
        return ""
    try:
        parts = decode_header(raw_header)
        res = []
        for content, enc in parts:
            if isinstance(content, bytes):
                res.append(content.decode(enc or 'utf-8', errors='ignore'))
            else:
                res.append(str(content))
        return "".join(res).strip()
    except Exception:
        return str(raw_header)

def _extract_shipping_entities(subject: str, body: str, attachments: list = None):
    text = f"{subject}\n{body}"
    booking_match = re.search(r'\b(5[A-Z0-9]{3}-\d{5}|OC\s*5[A-Z0-9]{3}-\d{5})\b', text, re.IGNORECASE)
    booking_ref = booking_match.group(1).replace("OC ", "").strip() if booking_match else None
    
    bl_match = re.search(r'\b(SIN\d{6,10}|MEDU[A-Z0-9]{6,12}|EGLV\d{8,12}|MAEU\d{8,12}|ONE[A-Z0-9]{8,12}|[A-Z]{4}\d{8,12})\b', text, re.IGNORECASE)
    bl_ref = bl_match.group(1).strip() if bl_match else None
    
    inv_match = re.search(r'\b(5\d{9}|INV[- ]?\d{6,10})\b', text, re.IGNORECASE)
    invoice_no = inv_match.group(1).strip() if inv_match else None
    
    ports_detected = []
    for port in ["PORT KLANG", "CALLAO", "AQABA", "SINGAPORE", "JAKARTA", "BELAWAN", "DUBAI", "JEBEL ALI", "SHANGHAI", "ROTTERDAM", "NEW YORK"]:
        if port in text.upper():
            ports_detected.append(port)
            
    carrier = _detect_carrier(subject, body)
    
    return {
        "booking_ref": booking_ref or "N/A",
        "bl_ref": bl_ref or "N/A",
        "invoice_no": invoice_no or "N/A",
        "carrier": carrier,
        "ports": ports_detected if ports_detected else ["UNDISCLOSED"],
    }

def _heuristic_laya_answers(cat, subcat, subj, body):
    """Rule-derived placeholder answers in the Laya typed-decision schema.
    Every answer is marked source='rules' — the neural forward pass in
    _enrich_dossier_neural replaces them with real model outputs."""
    urgency_val = 2.0 if any(k in f"{subj} {body}".upper() for k in ["URGENT", "ASAP", "CUTOFF", "PORT CUTOFF", "EMERGENCY"]) else (1.0 if cat in ["BL_COMPARISON", "SI_REQUEST"] else 0.2)
    needs_reply_prob = 0.95 if cat in ["BL_COMPARISON", "INVOICE_QUERY", "SI_REQUEST"] else 0.15
    is_spam_prob = 0.96 if cat == "SPAM" else 0.02
    cat_probs = {c: 0.03 for c in MARITIME_CATEGORIES}
    cat_probs[cat] = 0.88
    return {
        "category": {
            "type": "choice", "choice": cat,
            "confidence": 0.99 if "UNMATCHED" not in subcat else 0.92,
            "probabilities": cat_probs, "source": "rules"
        },
        "urgency": {
            "type": "score", "score": urgency_val,
            "level": "CRITICAL CUTOFF" if urgency_val >= 1.4 else ("HIGH ATTENTION" if urgency_val >= 0.8 else "ROUTINE"),
            "source": "rules"
        },
        "needs_reply": {"type": "noul", "probability": needs_reply_prob, "expected": needs_reply_prob > 0.5, "source": "rules"},
        "is_spam": {"type": "noul", "probability": is_spam_prob, "flagged": is_spam_prob > 0.5, "source": "rules"},
    }

def _enrich_dossier_neural(dossier):
    """Real Laya System-1 forward pass (~1s/email on CPU). Only call inline for
    bounded batches — for lists use _enrich_dossier_async so pages stay fast."""
    ld = dossier.get("laya_decision")
    if not LAYA_AVAILABLE or not ld or ld.get("inference") == "real":
        return
    agent = _get_laya_agent()
    if agent is None:
        ld["inference"] = "unavailable"
        return
    try:
        questions = laya.email_questions(categories=MARITIME_CATEGORIES)
        t0 = time.time()
        res = agent.system_one(ld["clean_state"], questions)
        _LAYA_STATS["calls"] += 1
        _LAYA_STATS["total_ms"] += (time.time() - t0) * 1000
        ld["neural_raw"] = res
        ld["inference"] = "real"
        answers, n_ans = ld["answers"], res.get("answers", {})
        cls = dossier["classification"]

        if "category" in n_ans:
            nc = n_ans["category"]
            answers["category"].update({
                "choice": nc.get("choice"), "confidence": nc.get("confidence"),
                "probabilities": nc.get("probabilities"), "source": "laya-neural"
            })
            # Rules tier owns routing; Laya may claim emails the rules missed.
            if cls["subcategory"] == "GENERAL_UNMATCHED" and nc.get("choice") in MARITIME_CATEGORIES and (nc.get("confidence") or 0) >= 0.6:
                cls["category"] = nc["choice"]
                cls["provenance"] = "Laya System-1 neural (rules unmatched)"
        if "urgency" in n_ans and "score" in n_ans["urgency"]:
            s = float(n_ans["urgency"]["score"])
            answers["urgency"].update({
                "score": round(s, 2),
                "level": "CRITICAL CUTOFF" if s >= 1.4 else ("HIGH ATTENTION" if s >= 0.8 else "ROUTINE"),
                "confidence": n_ans["urgency"].get("confidence"), "source": "laya-neural"
            })
        for key in ("needs_reply", "is_spam"):
            if key in n_ans and "noul" in n_ans[key]:
                p = float(n_ans[key]["noul"])
                answers[key].update({
                    "probability": round(p, 4), "confidence": n_ans[key].get("confidence"),
                    "source": "laya-neural"
                })
                answers[key]["expected" if key == "needs_reply" else "flagged"] = p > 0.5

        if cls["provenance"].startswith("Deterministic rules"):
            cls["provenance"] = "Deterministic rules + Laya System-1 neural"
    except Exception as ex:
        print(f"[LAYA] Forward pass warning: {ex}")
        ld["inference"] = "error"

def _enrich_dossier_async(dossier):
    """Schedule the neural pass on the worker pool — list pages return instantly
    and dossiers fill in over the next seconds as the frontend polls."""
    if not LAYA_AVAILABLE:
        return
    ld = dossier.get("laya_decision")
    if not ld or ld.get("inference") != "pending" or dossier.get("_neural_started"):
        return
    dossier["_neural_started"] = True
    _NEURAL_POOL.submit(_enrich_dossier_neural, dossier)

def _build_email_dossier(email_id: str, email_data: dict, run_neural: bool = False):
    subj = email_data.get("subject", "")
    body = email_data.get("body", "")
    from_addr = email_data.get("from", "operations@shippingline.com")
    to_addr = email_data.get("to", "shipping.docs@aprilasia.com")
    date_str = email_data.get("date", time.strftime("%Y-%m-%d %H:%M:%S"))
    atts = email_data.get("attachments", [])
    has_atts = len(atts) > 0

    # LAYA state construction (cleans body, strips signatures & disclaimers)
    if LAYA_AVAILABLE:
        clean_body = laya.clean_email_body(body)
        laya_state = laya.email_state(subject=subj, body=clean_body, sender=from_addr)
    else:
        clean_body, laya_state = body, None

    det = classify_email_detailed(subj, body, has_atts)
    entities = _extract_shipping_entities(subj, body, atts)

    cat = det.get("category", "GENERAL")
    subcat = det.get("subcategory", "")
    display_tag = det.get("display_tag", cat)
    desc = det.get("description", "")

    laya_decision = {
        "model": "laya-rl-agent (Convai Innovations ModernBERT)",
        "type": "non-autoregressive typed decision",
        "inference": "pending" if LAYA_AVAILABLE else "unavailable",
        "clean_state": laya_state,
        "neural_raw": None,
        "answers": _heuristic_laya_answers(cat, subcat, subj, body)
    }

    signals = []
    signals.append("Deterministic rules tier (Laya neural pass pending)" if LAYA_AVAILABLE else "Deterministic rules tier")
    if entities["booking_ref"] != "N/A":
        signals.append(f"Booking reference identified: {entities['booking_ref']}")
    if entities["bl_ref"] != "N/A":
        signals.append(f"Carrier B/L identifier identified: {entities['bl_ref']}")
    if entities["invoice_no"] != "N/A":
        signals.append(f"Financial invoice number parsed: {entities['invoice_no']}")
    if entities["carrier"] != "UNKNOWN":
        signals.append(f"Liner carrier recognized: {entities['carrier']}")
    if len(atts) == 2:
        signals.append("Paired documents detected: Shipping Instruction (SI) + Draft Bill of Lading (BL)")
    elif len(atts) == 1:
        signals.append("Single document attached")
    else:
        signals.append("0 attachments (Textual operational message / inquiry)")
        
    if cat == "INVOICE_QUERY":
        signals.append("Intent markers detected: Charges, THC, Demurrage, or Billing breakdown inquiry")
        target_queue = "Billing & THC Inquiries Desk"
        recommended_action = "Auto-draft itemized local charges and THC fee breakdown"
        priority = "MEDIUM"
    elif cat == "SI_REQUEST":
        signals.append("Intent markers detected: Shipping Instruction deadline or booking request")
        target_queue = "Shipping Instruction Operations Desk"
        recommended_action = "Transmit verified Shipping Instruction particulars to ocean carrier"
        priority = "HIGH"
    elif cat == "BL_COMPARISON":
        if len(atts) >= 2:
            signals.append("Ready for automated 7-field cross-validation against DCSA & IMO SOLAS")
            target_queue = "7-Field Verification Studio"
            recommended_action = "Execute automated 7-field document cross-audit"
            priority = "URGENT"
        else:
            signals.append("Inbound request awaiting carrier draft issuance")
            target_queue = "Carrier Draft BL Monitor"
            recommended_action = "Monitor vessel cutoff & dispatch carrier chaser if pending"
            priority = "HIGH"
    elif cat == "SPAM":
        signals.append("Spam/Phishing heuristics triggered")
        target_queue = "Quarantine Filter"
        recommended_action = "Discard unsolicited correspondence"
        priority = "LOW"
    else:
        signals.append("Operational vessel notice or administrative schedule")
        target_queue = "General Operations Log"
        recommended_action = "File operational advisory to audit archive"
        priority = "NORMAL"

    dossier = {
        "email_id": email_id,
        "from": from_addr,
        "to": to_addr,
        "date": date_str,
        "subject": subj,
        "body_preview": (clean_body[:220] + "...") if len(clean_body) > 220 else clean_body,
        "full_body": body,
        "clean_body": clean_body,
        "attachments_count": len(atts),
        "attachments": atts,
        "entities": entities,
        "laya_decision": laya_decision,
        "classification": {
            "category": cat,
            "subcategory": subcat,
            "display_tag": display_tag,
            "description": desc,
            "confidence": 0.99 if "UNMATCHED" not in subcat else 0.92,
            "provenance": ("Deterministic rules (Laya neural pending)" if LAYA_AVAILABLE else "Deterministic rules (Laya not installed)"),
            "signals": signals,
            "target_queue": target_queue,
            "recommended_action": recommended_action,
            "priority": priority
        }
    }
    if run_neural:
        _enrich_dossier_neural(dossier)
    return dossier

def _fetch_from_personal_gmail(limit: int = 20, only_unread: bool = False):
    """
    Connects to a real Gmail account via IMAP SSL (Port 993) and fetches
    incoming emails, attachment names, and headers. Requires
    REAL_GMAIL_ENABLED=1 plus SMTP_USER/SMTP_PASSWORD (or IMAP_*) env vars —
    disabled by default so this never runs on the public deployment.
    """
    if not REAL_GMAIL_ENABLED:
        return {"error": "Real Gmail ingestion is disabled on this deployment (REAL_GMAIL_ENABLED=0)", "emails": []}
    user = os.getenv("SMTP_USER") or os.getenv("IMAP_USER")
    password = (os.getenv("SMTP_PASSWORD") or os.getenv("IMAP_PASSWORD") or "").replace(" ", "")
    host = os.getenv("IMAP_HOST") or "imap.gmail.com"
    port = int(os.getenv("IMAP_PORT") or 993)
    if not user or not password:
        return {"error": "Missing Gmail credentials — set SMTP_USER/SMTP_PASSWORD (or IMAP_USER/IMAP_PASSWORD) in .env", "emails": []}

    real_emails = []
    try:
        mail = imaplib.IMAP4_SSL(host, port, timeout=15)
        mail.login(user, password)
        status, data = mail.select("INBOX", readonly=True)
        if status != "OK":
            return {"error": "Failed to select INBOX", "emails": []}
            
        search_criteria = "UNSEEN" if only_unread else "ALL"
        # UID search/fetch — UIDs are stable across sessions, unlike IMAP
        # sequence numbers which shift when mail is deleted or expunged.
        status, messages = mail.uid('search', None, search_criteria)
        total_inbox_count = int(data[0].decode() if data and data[0] else 0)

        if status != "OK" or not messages[0]:
            mail.close()
            mail.logout()
            return {"error": None, "emails": [], "total_inbox": total_inbox_count, "mailbox": user}

        msg_ids = messages[0].split()
        target_ids = msg_ids[-limit:]
        
        for mid in reversed(target_ids):
            try:
                res, fetch_data = mail.uid('fetch', mid, "(RFC822)")
                if res != "OK" or not fetch_data or not fetch_data[0]:
                    continue
                raw_email = fetch_data[0][1]
                msg = email.message_from_bytes(raw_email)
                
                mid_str = mid.decode(errors='ignore')
                eid = f"gmail_{mid_str}"
                
                subject_text = _clean_mime_header(msg.get("Subject", "No Subject"))
                from_text = _clean_mime_header(msg.get("From", "Unknown Sender"))
                to_text = _clean_mime_header(msg.get("To", user))
                date_text = msg.get("Date", "")
                
                body_plain = ""
                body_html = ""
                attachments = []
                
                if msg.is_multipart():
                    for part in msg.walk():
                        content_type = part.get_content_type()
                        content_disposition = str(part.get("Content-Disposition") or "")
                        filename = part.get_filename()
                        
                        if filename:
                            clean_fn = _clean_mime_header(filename)
                            attachments.append(clean_fn)
                        elif content_type == "text/plain" and "attachment" not in content_disposition:
                            payload = part.get_payload(decode=True)
                            if payload:
                                body_plain += payload.decode("utf-8", errors="ignore")
                        elif content_type == "text/html" and "attachment" not in content_disposition:
                            payload = part.get_payload(decode=True)
                            if payload:
                                body_html += payload.decode("utf-8", errors="ignore")
                else:
                    payload = msg.get_payload(decode=True)
                    if payload:
                        body_plain = payload.decode("utf-8", errors="ignore")
                        
                final_body = body_plain.strip()
                if not final_body and body_html:
                    final_body = re.sub(r'<[^>]+>', ' ', body_html)
                    final_body = re.sub(r'\s+', ' ', final_body).strip()
                    
                if not final_body:
                    final_body = "(Empty email body / Rich HTML only)"
                    
                email_data = {
                    "email_id": eid,
                    "from": from_text,
                    "to": to_text,
                    "date": date_text,
                    "subject": subject_text,
                    "body": final_body,
                    "attachments": attachments,
                    "source": "REAL_GMAIL_INBOX",
                    "mailbox": user
                }
                
                dossier = _build_email_dossier(eid, email_data)
                dossier["is_real_personal_email"] = True
                dossier["gmail_msg_id"] = mid_str
                real_emails.append(dossier)
                _DOSSIER_CACHE[eid] = dossier
                _enrich_dossier_async(dossier)
            except Exception as fe:
                print(f"Error parsing mid {mid}: {fe}")
                continue
                
        mail.close()
        mail.logout()
        return {
            "error": None,
            "emails": real_emails,
            "total_inbox": total_inbox_count,
            "mailbox": user,
            "host": f"{host}:{port}"
        }
    except Exception as e:
        print(f"IMAP connection failed: {e}")
        return {
            "error": str(e),
            "emails": [],
            "total_inbox": 0,
            "mailbox": user,
            "host": f"{host}:{port}"
        }

_INITIALIZING_GMAIL = False
_GMAIL_TOTAL_INBOX = 0

def _init_real_gmail_if_empty():
    """Kick off the initial real-inbox fetch in a background thread so status/
    list endpoints never block on IMAP."""
    global REAL_GMAIL_ITEMS, _INITIALIZING_GMAIL, _GMAIL_TOTAL_INBOX
    if not REAL_GMAIL_ENABLED or REAL_GMAIL_ITEMS or _INITIALIZING_GMAIL:
        return
    _INITIALIZING_GMAIL = True
    def _bg():
        global REAL_GMAIL_ITEMS, _INITIALIZING_GMAIL, _GMAIL_TOTAL_INBOX
        try:
            res = _fetch_from_personal_gmail(limit=5)
            if res.get("emails"):
                REAL_GMAIL_ITEMS = res["emails"]
                _GMAIL_TOTAL_INBOX = res.get("total_inbox") or 0
                print(f"[GMAIL] Indexed {len(REAL_GMAIL_ITEMS)} real emails", flush=True)
        finally:
            _INITIALIZING_GMAIL = False
    threading.Thread(target=_bg, daemon=True).start()

def _get_all_available_email_ids():
    if not os.path.exists(INBOX_DIR):
        return []
    return sorted([f.replace('.json', '') for f in os.listdir(INBOX_DIR) if f.endswith('.json')])

def _init_getter_store_if_needed():
    global GETTER_INGESTED_IDS
    if not GETTER_INGESTED_IDS:
        all_ids = _get_all_available_email_ids()
        GETTER_INGESTED_IDS = list(all_ids)

@app.get("/api/getter/status")
def get_getter_status():
    _init_real_gmail_if_empty()
    user_email = os.getenv("SMTP_USER") or os.getenv("IMAP_USER") or "not configured"
    
    cat_counts = {"BL_COMPARISON": 0, "INVOICE_QUERY": 0, "SI_REQUEST": 0, "GENERAL": 0, "SPAM": 0}
    queue_counts = {
        "comparator_ready": 0,
        "awaiting_draft_bl": 0,
        "billing_desk": 0,
        "si_operations": 0,
        "general_ops": 0,
        "quarantine": 0
    }
    
    for item in REAL_GMAIL_ITEMS:
        cat = item["classification"]["category"]
        cat_counts[cat] = cat_counts.get(cat, 0) + 1
        t_queue = item["classification"]["target_queue"]
        if "Verification Studio" in t_queue:
            queue_counts["comparator_ready"] += 1
        elif "Carrier Draft" in t_queue:
            queue_counts["awaiting_draft_bl"] += 1
        elif "Billing" in t_queue:
            queue_counts["billing_desk"] += 1
        elif "Shipping Instruction" in t_queue:
            queue_counts["si_operations"] += 1
        elif "Quarantine" in t_queue:
            queue_counts["quarantine"] += 1
        else:
            queue_counts["general_ops"] += 1

    return {
        "status": "ONLINE",
        "connection_mode": "Real Gmail IMAP (SSL)" if REAL_GMAIL_ENABLED else "disabled (REAL_GMAIL_ENABLED=0)",
        "mailbox": user_email if REAL_GMAIL_ENABLED else None,
        "server_host": "imap.gmail.com:993" if REAL_GMAIL_ENABLED else None,
        "total_available": _GMAIL_TOTAL_INBOX,
        "ingested_count": len(REAL_GMAIL_ITEMS),
        "is_fully_ingested": len(REAL_GMAIL_ITEMS) > 0,
        "initializing": _INITIALIZING_GMAIL,
        # Check LAYA_AGENT without triggering the ~15s model load in a GET
        "classifier_engine": ("Rules + Laya System-1 (neural loaded)" if LAYA_AGENT is not None
                              else ("Rules + Laya System-1 (loading/unavailable)" if LAYA_AVAILABLE
                                    else "Deterministic rules (laya not installed)")),
        "categories": cat_counts,
        "queues": queue_counts,
        "avg_latency_ms": round(_LAYA_STATS["total_ms"] / _LAYA_STATS["calls"], 1) if _LAYA_STATS["calls"] else None,
        "neural_calls": _LAYA_STATS["calls"],
        "last_updated": time.strftime("%Y-%m-%d %H:%M:%S")
    }

@app.post("/api/getter/poll-gmail")
def poll_real_gmail(req: PollGmailRequest):
    global REAL_GMAIL_ITEMS, _GMAIL_TOTAL_INBOX
    if not REAL_GMAIL_ENABLED:
        raise HTTPException(status_code=403, detail="Real Gmail ingestion is disabled on this deployment (REAL_GMAIL_ENABLED=0)")
    result = _fetch_from_personal_gmail(limit=req.limit, only_unread=req.only_unread)
    if result.get("error"):
        raise HTTPException(status_code=500, detail=result["error"])

    new_emails = result.get("emails", [])
    _GMAIL_TOTAL_INBOX = result.get("total_inbox") or _GMAIL_TOTAL_INBOX

    existing_ids = {e["email_id"] for e in REAL_GMAIL_ITEMS}
    added = 0
    for em in new_emails:
        if em["email_id"] not in existing_ids:
            REAL_GMAIL_ITEMS.insert(0, em)
            existing_ids.add(em["email_id"])
            added += 1
            
    _log_to_supabase_audit(
        "GMAIL_INBOX",
        "REAL_GMAIL_POLL",
        {
            "mailbox": result.get("mailbox"),
            "new_emails_fetched": added,
            "total_inbox": result.get("total_inbox"),
            "model": "laya-decision-engine"
        }
    )
    
    return {
        "status": "GMAIL_POLL_SUCCESS",
        "mailbox": result.get("mailbox"),
        "total_inbox": result.get("total_inbox"),
        "fetched_count": len(new_emails),
        "newly_added": added,
        "total_active_feed": len(REAL_GMAIL_ITEMS),
        "emails": REAL_GMAIL_ITEMS[:req.limit]
    }

@app.post("/api/getter/send-real-test")
def send_real_test_email_to_gmail(req: RealTestEmailRequest):
    """
    Sends a REAL email via Google SMTP to the configured Gmail inbox, waits for
    delivery, and polls it back via IMAP. Local demo only — disabled unless
    REAL_GMAIL_ENABLED=1.
    """
    if not REAL_GMAIL_ENABLED:
        raise HTTPException(status_code=403, detail="Real Gmail ingestion is disabled on this deployment (REAL_GMAIL_ENABLED=0)")
    load_dotenv(override=True)
    user = os.getenv("SMTP_USER") or os.getenv("IMAP_USER")
    password = (os.getenv("SMTP_PASSWORD") or os.getenv("IMAP_PASSWORD") or "").strip().replace(" ", "")
    host = os.getenv("SMTP_HOST") or "smtp.gmail.com"
    port = int(os.getenv("SMTP_PORT") or 587)
    
    if not user or not password:
        raise HTTPException(status_code=500, detail="Missing SMTP credentials in .env")
        
    msg = MIMEMultipart()
    msg["From"] = f"Ocean Carrier Operations <{user}>"
    msg["To"] = user
    msg["Subject"] = req.subject
    msg.attach(MIMEText(req.body, "plain"))
    
    try:
        server = smtplib.SMTP(host, port, timeout=15)
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(user, password)
        server.send_message(msg)
        server.quit()
    except Exception as se:
        raise HTTPException(status_code=500, detail=f"Failed to send email via Google SMTP: {se}")
        
    time.sleep(3)
    
    poll_res = _fetch_from_personal_gmail(limit=5)
    global REAL_GMAIL_ITEMS
    if poll_res.get("emails"):
        existing_ids = {e["email_id"] for e in REAL_GMAIL_ITEMS}
        for em in poll_res["emails"]:
            if em["email_id"] not in existing_ids:
                REAL_GMAIL_ITEMS.insert(0, em)
                existing_ids.add(em["email_id"])

    return {
        "status": "SENT_AND_RECEIVED",
        "message": f"Test email delivered to {user} via SMTP and ingested back via IMAP (Laya neural enrichment runs in the background).",
        "sent_to": user,
        "subject": req.subject,
        "latest_email": REAL_GMAIL_ITEMS[0] if REAL_GMAIL_ITEMS else None
    }

@app.get("/api/getter/emails")
def get_getter_emails(
    source: str = "real",
    category: str = "all",
    queue_filter: str = "all",
    search: str = "",
    page: int = 1,
    limit: int = 25
):
    _init_real_gmail_if_empty()
    _init_getter_store_if_needed()
    
    if source == "dataset":
        combined_eids = list(_get_all_available_email_ids())
        items = []
        for eid in combined_eids:
            if eid not in _DOSSIER_CACHE:
                p = os.path.join(INBOX_DIR, f"{eid}.json")
                if os.path.exists(p):
                    try:
                        with open(p, "r", encoding="utf-8") as f:
                            d = json.load(f)
                        _DOSSIER_CACHE[eid] = _build_email_dossier(eid, d)
                    except Exception:
                        pass
            dos = _DOSSIER_CACHE.get(eid)
            if dos:
                items.append(dos)
    else:
        # Default: Real personal Gmail emails!
        items = list(REAL_GMAIL_ITEMS)
        # Also include any custom ingested test items
        for c_item in reversed(CUSTOM_INGESTED_ITEMS):
            items.insert(0, c_item)

    filtered = []
    search_lower = search.strip().lower()
    
    for it in items:
        if category != "all" and it["classification"]["category"].upper() != category.upper():
            continue
            
        if queue_filter != "all":
            t_queue = it["classification"]["target_queue"].lower()
            if queue_filter == "comparator" and "verification studio" not in t_queue:
                continue
            if queue_filter == "chaser" and "carrier draft" not in t_queue:
                continue
            if queue_filter == "billing" and "billing" not in t_queue:
                continue
            if queue_filter == "si" and "shipping instruction" not in t_queue:
                continue
            if queue_filter == "general" and "general" not in t_queue:
                continue
                
        if search_lower:
            text_corpus = f"{it['email_id']} {it['subject']} {it['from']} {it.get('entities', {}).get('booking_ref', '')} {it.get('entities', {}).get('bl_ref', '')} {it.get('entities', {}).get('carrier', '')}".lower()
            if search_lower not in text_corpus:
                continue
                
        filtered.append(it)
        
    total_count = len(filtered)
    start_idx = (page - 1) * limit
    end_idx = start_idx + limit
    paginated = filtered[start_idx:end_idx]

    # Neural enrichment (~1s/email on CPU) runs in the background for the
    # returned page only — the dossier's laya_decision.inference flips to
    # "real" on the next poll. Never block the list endpoint on it.
    for it in paginated:
        _enrich_dossier_async(it)

    return {
        "source": source,
        "mailbox": (os.getenv("SMTP_USER") or os.getenv("IMAP_USER")) if REAL_GMAIL_ENABLED else None,
        "total": total_count,
        "page": page,
        "limit": limit,
        "total_pages": max(1, (total_count + limit - 1) // limit),
        "emails": paginated
    }

@app.post("/api/getter/fetch")
def fetch_more_getter_emails(req: FetchBatchRequest):
    _init_getter_store_if_needed()
    all_available = _get_all_available_email_ids()
    cur_len = len(GETTER_INGESTED_IDS)
    target_count = min(len(all_available), cur_len + req.count)
    new_ids = all_available[cur_len:target_count]
    GETTER_INGESTED_IDS.extend(new_ids)
    
    for eid in new_ids:
        if eid not in _DOSSIER_CACHE:
            p = os.path.join(INBOX_DIR, f"{eid}.json")
            if os.path.exists(p):
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        d = json.load(f)
                    _DOSSIER_CACHE[eid] = _build_email_dossier(eid, d)
                except Exception:
                    pass
                    
    return {
        "status": "FETCH_SUCCESS",
        "added_count": len(new_ids),
        "total_ingested": len(GETTER_INGESTED_IDS) + len(CUSTOM_INGESTED_ITEMS),
        "total_available": len(all_available),
        "newly_fetched": new_ids
    }

@app.post("/api/getter/ingest-custom")
def ingest_custom_email(req: CustomIngestRequest):
    custom_id = f"custom_inbound_{int(time.time() * 1000) % 1000000}"
    email_data = {
        "email_id": custom_id,
        "from": req.from_addr,
        "to": req.to_addr,
        "subject": req.subject,
        "body": req.body,
        "attachments": req.attachments
    }
    
    dossier = _build_email_dossier(custom_id, email_data)
    dossier["is_custom_simulation"] = True
    CUSTOM_INGESTED_ITEMS.append(dossier)
    _DOSSIER_CACHE[custom_id] = dossier
    _enrich_dossier_async(dossier)
    
    _log_to_supabase_audit(
        custom_id,
        "AUTO_GETTER_INGEST",
        {
            "description": "Auto Email Getter ingested and classified custom inbound correspondence (rules tier; Laya neural enrichment queued)",
            "category": dossier["classification"]["category"],
            "target_queue": dossier["classification"]["target_queue"],
            "entities": dossier["entities"]
        }
    )
    
    return {
        "status": "INGESTED_SUCCESSFULLY",
        "email_id": custom_id,
        "dossier": dossier
    }

@app.post("/api/getter/reset")
def reset_getter_stream(initial_count: int = 50):
    global GETTER_INGESTED_IDS, CUSTOM_INGESTED_ITEMS, REAL_GMAIL_ITEMS
    all_available = _get_all_available_email_ids()
    cnt = max(0, min(len(all_available), initial_count))
    GETTER_INGESTED_IDS = all_available[:cnt]
    CUSTOM_INGESTED_ITEMS = []
    # Refresh real gmail only when the feature is enabled on this deployment
    if REAL_GMAIL_ENABLED:
        res = _fetch_from_personal_gmail(limit=15)
        if res.get("emails"):
            REAL_GMAIL_ITEMS = res["emails"]
    return {
        "status": "RESET_COMPLETED",
        "ingested_count": len(REAL_GMAIL_ITEMS),
        "mailbox": (os.getenv("SMTP_USER") or os.getenv("IMAP_USER")) if REAL_GMAIL_ENABLED else None
    }

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

    # Check for any UI placeholder marker — a placeholder is never a real
    # document, so it must halt automated comparison (missing attachment,
    # corrupted file, non-comparison category, awaiting carrier draft, ...)
    if _is_placeholder_text(si_text) or _is_placeholder_text(bl_text):
        joined = si_text + bl_text
        reason = ("unreadable" if "[CORRUPTED" in joined
                  else "missing_attachment")
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

    # Extract fields with the same regex->LLM tiering the batch pipeline uses
    si_fields, si_prov = extract_fields_tiered(si_text)
    bl_fields, bl_prov = extract_fields_tiered(bl_text)

    resp = _verdict_response(si_fields, bl_fields)
    resp["si_provenance"] = si_prov
    resp["bl_provenance"] = bl_prov
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

    evidence = [
        {
            "field": f,
            "si_value": si_fields.get(f),
            "bl_value": bl_fields.get(f),
            "reason": c.get("reason"),
            **({"confidence": c["confidence"]} if c.get("confidence") else {}),
        }
        for f, c in field_comparisons.items() if not c.get("match")
    ]

    # Genuine value-vs-value defects are never swallowed by an escalation;
    # blanks (one-sided or both) surface as evidence instead.
    if defect_fields:
        ai_reasoning = reason_and_verify_with_ai(
            si_fields, bl_fields, defect_fields, is_missing_val, field_comparisons
        )
        return {
            "status": "MISMATCH",
            "review_reason": None,
            "thoughts": ai_reasoning.get("thoughts", "Carefully analyzed field values across documents."),
            "summary_reason": ai_reasoning.get("summary_reason", "Defect detected in specified fields."),
            "si_fields": si_fields,
            "bl_fields": bl_fields,
            "defect_fields": defect_fields,
            "field_comparisons": field_comparisons,
            "evidence": evidence
        }

    # Missing values (blank on exactly one side) escalate deterministically
    if is_missing_val:
        return {
            "status": "NEEDS_REVIEW",
            "review_reason": "missing_value",
            "thoughts": "Required shipping field contains blank or unreadable tokens.",
            "summary_reason": "Required field is missing or placeholder value.",
            "si_fields": si_fields,
            "bl_fields": bl_fields,
            "defect_fields": [],
            "field_comparisons": field_comparisons,
            "evidence": evidence
        }

    # Blank in BOTH documents = the fields could not be extracted at all;
    # reporting OK would be dishonest.
    both_blank = [f for f, c in field_comparisons.items() if c.get("blank") == "both"]
    if both_blank:
        return {
            "status": "NEEDS_REVIEW",
            "review_reason": "unreadable",
            "thoughts": f"Field extraction produced no value in either document for: {', '.join(both_blank)}.",
            "summary_reason": f"Escalated: could not extract {', '.join(both_blank)} from either document.",
            "si_fields": si_fields,
            "bl_fields": bl_fields,
            "defect_fields": [],
            "field_comparisons": field_comparisons,
            "evidence": evidence
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
        "field_comparisons": field_comparisons,
        "evidence": evidence
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
            err = d["meta"].get("error")
            if err:
                # The model call itself failed (auth, model retired, budget,
                # network) — say so instead of blaming the photo.
                return _early("NEEDS_REVIEW", "unreadable",
                              f"Vision analysis could not run: {err}",
                              f"{label} vision pass failed — check server AI configuration, then retry.")
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

    def _tiered(doc):
        if doc.get("fields"):
            return doc["fields"], {k: "vision" for k in doc["fields"]}
        return extract_fields_tiered(doc["text"])

    si_res, bl_res = await asyncio.gather(
        asyncio.to_thread(_tiered, docs["si"]),
        asyncio.to_thread(_tiered, docs["bl"]),
    )
    si_fields, si_prov = si_res
    bl_fields, bl_prov = bl_res

    resp = await asyncio.to_thread(_verdict_response, si_fields, bl_fields)
    resp.update({
        "si_provenance": si_prov,
        "bl_provenance": bl_prov,
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
      Exception: the attachment is the BL and the SI is inline in the body.
    - An SI written into the email body counts as received — never flagged.
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
    inline_si = extract_inline_si_fields(email_data.get("body", ""))
    if len(atts) == 1:
        # The single attachment may be the draft BL while the SI arrived
        # inline in the body — then nothing is actually missing.
        if inline_si:
            p1 = os.path.join(BUNDLE_DIR, atts[0])
            if not is_si_attachment(p1, extract_text(p1)):
                return None
        return "missing_attachment"
    if len(atts) == 0:
        if inline_si:
            # SI arrived written in the email body — it is not "missing";
            # only the carrier's draft BL is still owed (missing_bl queue).
            return None
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
    # SI submitted inline in the email body — no SI attachment at all,
    # draft BL still owed by the carrier.
    si_inline = len(atts) == 0 and bool(extract_inline_si_fields(body))
    det = classify_email_detailed(subj, body, len(atts) > 0)
    category = CLASSIFICATIONS_STORE.get(eid) or det["category"]
    chaser_status = CHASERS_STORE.get(eid, {}).get("status")
    corrupt_action = CORRUPTED_STORE.get(eid, {}).get("status")

    threads = EMAIL_THREADS_STORE.get(eid, [])
    inbound_replies = [m for m in threads if m.get("direction") == "INBOUND"]
    has_reply = len(inbound_replies) > 0
    latest_reply = inbound_replies[-1] if inbound_replies else None

    if resolved:
        queue_status = "RESOLVED"
    elif has_reply:
        queue_status = "REPLY_RECEIVED"
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
        "subcategory": det["subcategory"],
        "display_tag": det["display_tag"],
        "category_description": det["description"],
        "has_bl": has_bl,
        "si_inline": si_inline,
        "corrupted": corrupted,
        "corrupt_issue": corrupt_issue,
        "verdict_status": verdict_status,
        "review_reason": verdict.get("review_reason") if verdict else None,
        "defect_fields": (verdict.get("defect_fields") if verdict else []) or [],
        "missing_fields": [f for f, c in (verdict.get("field_comparisons") or {}).items()
                          if c.get("blank")] if verdict else [],
        "summary_reason": verdict.get("summary_reason") if verdict else None,
        "resolved": resolved,
        "chaser_status": chaser_status,
        "corrupt_action": corrupt_action,
        "queue_status": queue_status,
        "_missing_bl": missing_bl,
        "has_reply": has_reply,
        "reply_count": len(inbound_replies),
        "latest_reply": latest_reply,
        "thread_count": len(threads),
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
    if flt == "si_inline":
        return item["si_inline"]
    if flt == "corrupted":
        return item["corrupted"]
    if flt == "resolved":
        return item["resolved"]
    if flt == "chaser_sent":
        return item["chaser_status"] is not None
    if flt == "reply_received":
        return item.get("has_reply", False)
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
        "si_inline": sum(1 for i in items if _queue_match(i, "si_inline")),
        "corrupted": sum(1 for i in items if _queue_match(i, "corrupted")),
        "resolved": sum(1 for i in items if _queue_match(i, "resolved")),
        "chaser_sent": sum(1 for i in items if _queue_match(i, "chaser_sent")),
        "reply_received": sum(1 for i in items if _queue_match(i, "reply_received")),
    }

    filtered = [i for i in items if _queue_match(i, filter)]
    if search:
        s = search.lower()
        filtered = [i for i in filtered if s in i["email_id"].lower()
                    or s in i["subject"].lower() or s in i["from"].lower()
                    or s in (i.get("display_tag") or "").lower()
                    or s in (i.get("subcategory") or "").lower()
                    or s in (i.get("category_description") or "").lower()]

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
    subcategories = {}
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

        det = classify_email_detailed(
            d.get("subject", ""), d.get("body", ""), len(d.get("attachments", [])) > 0)
        category = CLASSIFICATIONS_STORE.get(eid) or det["category"]
        categories[category] = categories.get(category, 0) + 1
        disp_tag = det["display_tag"]
        subcategories[disp_tag] = subcategories.get(disp_tag, 0) + 1

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
        "subcategories": subcategories,
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
    return {"to": get_carrier_desk_email(carrier), "subject": subject,
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
            det = result.get("details") or {}
            VERDICTS_STORE[eid] = {
                "status": result.get("status"),
                "review_reason": result.get("review_reason"),
                "defect_fields": result.get("defect_fields", []),
                "si_fields": si_fields or det.get("si_fields") or {},
                "bl_fields": bl_fields or det.get("bl_fields") or {},
                "field_comparisons": det.get("field_comparisons") or {},
                "details": det,
                "evidence": det.get("evidence", []),
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
async def agent_chat(req: AgentChatRequest):
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
    from starlette.concurrency import run_in_threadpool
    out = await run_in_threadpool(run_agent, session, req.message.strip(), AGENT_EXECUTORS)
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

    for eid, edits in DOC_EDITS_STORE.items():
        for ed in edits:
            events.append({
                "email_id": eid,
                "action": ed.get("action", "DOCUMENT_EDITED"),
                "actor": "Shipping Operator",
                "notes": ed.get("notes"),
                "timestamp": ed.get("timestamp"),
                "details": ed
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
    "resume": True, "partial": False,
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
    # submission.json is scored on exactly the 5 organizer keys; pipeline
    # results carry a 'details' payload that lives in verification_details.json
    # and must not leak into the scored file.
    data = {k: (submission_row(v) if isinstance(v, dict) and "details" in v else v)
            for k, v in data.items()}
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
    # A fresh non-resume full run owns submission.json and may legitimately
    # truncate it; resume runs and subset (email_ids) runs must merge so
    # unticked/unprocessed entries survive.
    allow_shrink = (
        not PIPELINE_STATE.get("resume", True)
        and not PIPELINE_STATE.get("partial", False)
    )
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
                    det = result.get("details") or {}
                    VERDICTS_STORE[eid] = {
                        "status": result.get("status"),
                        "review_reason": result.get("review_reason"),
                        "defect_fields": result.get("defect_fields", []),
                        "si_fields": si_fields or det.get("si_fields") or {},
                        "bl_fields": bl_fields or det.get("bl_fields") or {},
                        "field_comparisons": det.get("field_comparisons") or {},
                        "details": det,
                        "evidence": det.get("evidence", []),
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
    email_ids: List[str] = []


@app.post("/api/pipeline/run")
def run_pipeline(req: PipelineRunRequest):
    if PIPELINE_STATE["running"]:
        return {"started": False, "message": "already running"}

    files = sorted([f for f in os.listdir(INBOX_DIR) if f.endswith('.json')])
    # Optional subset from the Submission Builder picker: only process the
    # ticked email ids. Ids are matched against real inbox files, so unknown
    # ones are simply ignored.
    selected = {e.strip() for e in (req.email_ids or []) if e and e.strip()}
    partial = bool(selected)
    if partial:
        files = [f for f in files if f.replace('.json', '') in selected]
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
        "partial": partial,
    })
    threading.Thread(target=_pipeline_worker, args=(files,), daemon=True).start()
    msg = f"started ({len(files)} to process"
    if partial:
        msg += f", {len(selected)} selected"
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
    sub_size = len(SUBMISSION_STORE)
    if sub_size == 0:
        existing = _load_submission_file()
        sub_size = len(existing) if existing else 0
    return dict(PIPELINE_STATE, submission_size=sub_size)


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


# ---------------------------------------------------------------------------
# Stress / edge-case test harness (tests/stress_dataset)
# ---------------------------------------------------------------------------
STRESS_DIR = os.path.join(BASE_DIR, "tests", "stress_dataset")
STRESS_INBOX_DIR = os.path.join(STRESS_DIR, "inbox")
STRESS_GT_PATH = os.path.join(STRESS_DIR, "stress_ground_truth.json")
STRESS_RESULTS_PATH = os.path.join(STRESS_DIR, "stress_results.json")

STRESS_STATE = {
    "running": False, "processed": 0, "total": 0, "current": None,
    "done": False, "error": None, "cancel_requested": False,
    "started_at": None, "finished_at": None, "elapsed_seconds": None,
}
STRESS_RESULTS = {}   # email_id -> predicted verdict
STRESS_METRICS = None


def _load_stress_gt():
    if not os.path.exists(STRESS_GT_PATH):
        return {}
    with open(STRESS_GT_PATH, 'r', encoding='utf-8') as f:
        return json.load(f)


def _stress_verdict_match(pred, gt):
    return (
        pred.get("category") == gt.get("category")
        and pred.get("status") == gt.get("status")
        and pred.get("review_reason") == gt.get("review_reason")
        and sorted(pred.get("defect_fields") or []) == sorted(gt.get("defect_fields") or [])
    )


def _stress_verdict_diffs(pred, gt):
    diffs = []
    for k in ("category", "status", "review_reason"):
        if pred.get(k) != gt.get(k):
            diffs.append(k)
    if sorted(pred.get("defect_fields") or []) != sorted(gt.get("defect_fields") or []):
        diffs.append("defect_fields")
    return diffs


def _stress_compute_metrics(results, gt):
    """Score stress-run predictions against the ground truth file."""
    from collections import Counter
    # Score only the cases this run actually processed (a limited run should
    # not count untouched ground-truth rows as misses).
    gt_eval = {eid: g for eid, g in gt.items() if eid in results}
    total = len(gt_eval)
    exact = category_ok = status_ok = 0
    rf_total = rf_correct = rf_false_alarms = 0      # expected-OK cases
    nrf_total = nrf_caught = 0                       # expected-flagged cases
    defect_total = defect_caught = 0
    review_total = review_caught = 0
    crashes = 0
    f_tp = f_fp = f_fn = 0
    per_type = {}
    failures = []

    for eid, g in gt_eval.items():
        pred = results.get(eid)
        if pred is None:
            pred = {"category": None, "status": "MISSING",
                    "review_reason": None, "defect_fields": []}
        tt = g.get("test_type", "unknown")
        bucket = per_type.setdefault(tt, {
            "total": 0, "exact": 0, "status_ok": 0, "missed": 0,
            "gt_status": g.get("status"),
        })
        bucket["total"] += 1

        if pred.get("status") == "ERROR":
            crashes += 1

        is_exact = _stress_verdict_match(pred, g)
        if is_exact:
            exact += 1
        if pred.get("category") == g.get("category"):
            category_ok += 1
        if pred.get("status") == g.get("status"):
            status_ok += 1
            bucket["status_ok"] += 1
        if is_exact:
            bucket["exact"] += 1
        else:
            bucket["missed"] += 1
            failures.append({
                "email_id": eid, "test_type": tt,
                "expected": {k: g.get(k) for k in ("category", "status", "review_reason", "defect_fields")},
                "predicted": {k: pred.get(k) for k in ("category", "status", "review_reason", "defect_fields")},
                "error": pred.get("error"),
                "diffs": _stress_verdict_diffs(pred, g),
            })

        if g.get("status") == "OK":
            rf_total += 1
            if is_exact:
                rf_correct += 1
            elif pred.get("status") in ("MISMATCH", "NEEDS_REVIEW"):
                rf_false_alarms += 1
        else:
            nrf_total += 1
            if pred.get("status") in ("MISMATCH", "NEEDS_REVIEW"):
                nrf_caught += 1
            if g.get("status") == "MISMATCH":
                defect_total += 1
                if pred.get("status") == "MISMATCH":
                    defect_caught += 1
                gtd = set(g.get("defect_fields") or [])
                prd = set(pred.get("defect_fields") or [])
                f_tp += len(gtd & prd)
                f_fp += len(prd - gtd)
                f_fn += len(gtd - prd)
            elif g.get("status") == "NEEDS_REVIEW":
                review_total += 1
                if pred.get("status") == "NEEDS_REVIEW":
                    review_caught += 1

    f_prec = f_tp / (f_tp + f_fp) if (f_tp + f_fp) else 1.0
    f_rec = f_tp / (f_tp + f_fn) if (f_tp + f_fn) else 1.0
    f_f1 = 2 * f_prec * f_rec / (f_prec + f_rec) if (f_prec + f_rec) else 0.0

    return {
        "total": total,
        "dataset_total": len(gt),
        "processed": len(results),
        "exact_verdict_accuracy": exact / total if total else 0,
        "category_accuracy": category_ok / total if total else 0,
        "status_accuracy": status_ok / total if total else 0,
        "crashes": crashes,
        "clean_accuracy": rf_correct / rf_total if rf_total else 0,
        "false_alarm_rate": rf_false_alarms / rf_total if rf_total else 0,
        "clean_total": rf_total,
        "flagged_total": nrf_total,
        "catch_rate": nrf_caught / nrf_total if nrf_total else 0,
        "defect_recall": defect_caught / defect_total if defect_total else 0,
        "review_recall": review_caught / review_total if review_total else 0,
        "field_precision": f_prec,
        "field_recall": f_rec,
        "field_f1": f_f1,
        "per_test_type": dict(sorted(per_type.items())),
        "status_counts": dict(Counter(p.get("status") for p in results.values())),
        "failures": sorted(failures, key=lambda r: (r["test_type"], r["email_id"])),
    }


def _stress_process_file(fname):
    path = os.path.join(STRESS_INBOX_DIR, fname)
    try:
        with open(path, 'r', encoding='utf-8') as fl:
            email_data = json.load(fl)
        eid = email_data.get("email_id") or fname.replace('.json', '')
        try:
            result, _, _ = process_email(email_data, bundle_dir=STRESS_DIR)
            return eid, result
        except Exception as e:
            return eid, {
                "category": "ERROR", "status": "ERROR",
                "review_reason": None, "defect_fields": [],
                "error": f"{type(e).__name__}: {e}",
            }
    except Exception as e:
        return fname.replace('.json', ''), {
            "category": "ERROR", "status": "ERROR",
            "review_reason": None, "defect_fields": [],
            "error": f"unreadable email json: {e}",
        }


def _stress_worker(files):
    from concurrent.futures import ThreadPoolExecutor
    import time as _time
    global STRESS_METRICS
    t0 = _time.time()
    try:
        with ThreadPoolExecutor(max_workers=STRESS_STATE.get("workers") or 8) as pool:
            for eid, result in pool.map(_stress_process_file, files):
                STRESS_RESULTS[eid] = result
                STRESS_STATE["processed"] += 1
                if STRESS_STATE["cancel_requested"]:
                    STRESS_STATE["error"] = "cancelled"
                    break
    except Exception as e:
        STRESS_STATE["error"] = str(e)
    finally:
        STRESS_STATE["elapsed_seconds"] = round(_time.time() - t0, 2)
        gt = _load_stress_gt()
        STRESS_METRICS = _stress_compute_metrics(STRESS_RESULTS, gt)
        STRESS_METRICS["elapsed_seconds"] = STRESS_STATE["elapsed_seconds"]
        done = STRESS_STATE["elapsed_seconds"] or 1
        STRESS_METRICS["throughput_eps"] = round(len(STRESS_RESULTS) / done, 1)
        try:
            with open(STRESS_RESULTS_PATH, 'w', encoding='utf-8') as f:
                json.dump({"results": STRESS_RESULTS, "metrics": STRESS_METRICS}, f)
        except Exception:
            pass
        STRESS_STATE["done"] = True
        STRESS_STATE["running"] = False
        STRESS_STATE["current"] = None
        STRESS_STATE["finished_at"] = _utcnow()


class StressRunRequest(BaseModel):
    limit: int = 0          # 0 = all emails in the dataset
    workers: int = 8        # parallel workers (deterministic pipeline, no LLM)


@app.get("/api/stress/dataset")
def stress_dataset_info():
    gt = _load_stress_gt()
    files = []
    if os.path.isdir(STRESS_INBOX_DIR):
        files = [f for f in os.listdir(STRESS_INBOX_DIR) if f.endswith('.json')]
    by_type, by_status = {}, {}
    for g in gt.values():
        by_type[g.get("test_type", "unknown")] = by_type.get(g.get("test_type", "unknown"), 0) + 1
        by_status[g.get("status", "?")] = by_status.get(g.get("status", "?"), 0) + 1
    return {
        "exists": os.path.isdir(STRESS_INBOX_DIR),
        "dataset_dir": STRESS_DIR,
        "email_count": len(files),
        "ground_truth_count": len(gt),
        "by_test_type": dict(sorted(by_type.items())),
        "by_status": dict(sorted(by_status.items())),
        "has_saved_results": os.path.exists(STRESS_RESULTS_PATH),
    }


@app.get("/api/stress/cases")
def stress_cases(search: str = "", test_type: str = "", limit: int = 200):
    gt = _load_stress_gt()
    rows = []
    for eid, g in sorted(gt.items()):
        if search and search.lower() not in eid.lower():
            continue
        if test_type and g.get("test_type") != test_type:
            continue
        rows.append({
            "email_id": eid,
            "test_type": g.get("test_type"),
            "category": g.get("category"),
            "status": g.get("status"),
            "review_reason": g.get("review_reason"),
            "defect_fields": g.get("defect_fields") or [],
        })
        if len(rows) >= limit:
            break
    return {"total": len(gt), "returned": len(rows), "cases": rows}


@app.post("/api/stress/run")
def stress_run(req: StressRunRequest):
    if STRESS_STATE["running"]:
        return {"started": False, "message": "stress test already running"}
    if not os.path.isdir(STRESS_INBOX_DIR):
        raise HTTPException(status_code=404,
                            detail="stress dataset not found — run tests/generate_stress_dataset.py first")

    files = sorted([f for f in os.listdir(STRESS_INBOX_DIR) if f.endswith('.json')])
    if req.limit and req.limit > 0:
        files = files[:req.limit]

    STRESS_RESULTS.clear()
    STRESS_STATE.update({
        "running": True, "processed": 0, "total": len(files),
        "current": None, "done": False, "error": None,
        "cancel_requested": False, "started_at": _utcnow(),
        "finished_at": None, "elapsed_seconds": None,
        "workers": max(1, min(req.workers, 32)),
    })
    threading.Thread(target=_stress_worker, args=(files,), daemon=True).start()
    return {"started": True, "total": len(files)}


@app.post("/api/stress/cancel")
def stress_cancel():
    if not STRESS_STATE["running"]:
        return {"cancelled": False, "message": "no stress run in progress"}
    STRESS_STATE["cancel_requested"] = True
    return {"cancelled": True}


@app.get("/api/stress/status")
def stress_status():
    return dict(STRESS_STATE, metrics=STRESS_METRICS, result_count=len(STRESS_RESULTS))


@app.get("/api/stress/results")
def stress_results(failures_only: bool = False, limit: int = 500):
    global STRESS_METRICS
    if STRESS_METRICS is None and os.path.exists(STRESS_RESULTS_PATH):
        try:
            with open(STRESS_RESULTS_PATH, 'r', encoding='utf-8') as f:
                saved = json.load(f)
            if not STRESS_RESULTS:
                STRESS_RESULTS.update(saved.get("results", {}))
            STRESS_METRICS = saved.get("metrics")
        except Exception:
            pass
    if STRESS_METRICS is None:
        return {"metrics": None, "failures": [], "rows": []}
    m = dict(STRESS_METRICS)
    failures = m.pop("failures", [])[:limit]
    rows = []
    if not failures_only:
        gt = _load_stress_gt()
        for eid in sorted(STRESS_RESULTS)[:limit]:
            rows.append({
                "email_id": eid,
                "test_type": (gt.get(eid) or {}).get("test_type"),
                "predicted": STRESS_RESULTS[eid],
                "expected": gt.get(eid),
                "match": _stress_verdict_match(STRESS_RESULTS[eid], gt.get(eid) or {}),
            })
    return {"metrics": m, "failures": failures, "rows": rows}


class StressRunOneRequest(BaseModel):
    email_id: str


@app.post("/api/stress/run-one")
def stress_run_one(req: StressRunOneRequest):
    eid = req.email_id.strip()
    path = os.path.join(STRESS_INBOX_DIR, f"{eid}.json")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail=f"{eid} not found in stress dataset")
    with open(path, 'r', encoding='utf-8') as f:
        email_data = json.load(f)
    try:
        pred, si_fields, bl_fields = process_email(email_data, bundle_dir=STRESS_DIR)
        error = None
    except Exception as e:
        pred, si_fields, bl_fields = {
            "category": "ERROR", "status": "ERROR",
            "review_reason": None, "defect_fields": [],
        }, {}, {}
        error = f"{type(e).__name__}: {e}"
    gt = _load_stress_gt().get(eid)
    return {
        "email_id": eid,
        "predicted": pred,
        "expected": gt,
        "si_fields": si_fields,
        "bl_fields": bl_fields,
        "error": error,
        "match": _stress_verdict_match(pred, gt) if gt else None,
        "diffs": _stress_verdict_diffs(pred, gt) if gt else [],
    }


if __name__ == "__main__":
    _port = int(os.getenv("PORT", "8000"))
    _host = os.getenv("HOST", "0.0.0.0")
    print(f"Starting FastAPI Backend on http://{_host}:{_port}")
    uvicorn.run(app, host=_host, port=_port)
