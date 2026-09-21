import os
import json
import re
import time
from tqdm import tqdm

from pipeline.parsers import extract_text, is_image_file, extract_shipping_fields_fast, extract_inline_si_fields
from pipeline.edge_cases import check_attachments, check_unreadable, check_wrong_doc_type, is_si_attachment
from pipeline.ai_engine import (
    classify_email_with_provenance,
    extract_fields_tiered,
    comparison_intent,
    analyze_document_image,
    diagnose_mismatch,
    adjudicate_review,
    CORE_FIELDS,
    MODEL,
)
from pipeline.comparator import compare_fields, is_blank_or_missing

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUNDLE_DIR = os.path.join(BASE_DIR, "sdoc-hackathon-bundle")
INBOX_DIR = os.path.join(BUNDLE_DIR, "inbox")
SUBMISSION_PATH = os.path.join(BASE_DIR, "submission.json")
DETAILS_PATH = os.path.join(BASE_DIR, "verification_details.json")

SUBMISSION_KEYS = ("category", "status", "review_reason", "has_defect", "defect_fields")


def submission_row(result):
    """Reduce a process_email result to exactly the 5 organizer-scored keys."""
    return {k: result.get(k) for k in SUBMISSION_KEYS}


def _verdict(category, status, review_reason, has_defect, defect_fields, details):
    details["engine_trail"] = _engine_trail(details)
    return {
        "category": category,
        "status": status,
        "review_reason": review_reason,
        "has_defect": has_defect,
        "defect_fields": defect_fields,
        "details": details,
    }


def _engine_trail(details):
    """
    Compact per-stage record of which engine produced each decision:
    'rule' (deterministic regex), 'laya' (neural tier), 'model' (the LLM,
    named in 'model'), 'vision', 'email_body', 'fallback'. Surfaces in
    verification_details.json and /api/audit for per-email engine tracking.
    """
    trail = []

    def add(stage, prov):
        if prov:
            entry = {"stage": stage, "engine": prov}
            if prov == "model":
                entry["model"] = MODEL
            trail.append(entry)

    add("classification", details.get("classification_provenance"))
    add("intent", details.get("intent_provenance"))
    for side in ("si", "bl"):
        provs = {p for p in (details.get(f"{side}_provenance") or {}).values() if p}
        if provs:
            trail.append({"stage": f"{side}_extraction", "engines": sorted(provs)})
    if details.get("field_comparisons"):
        trail.append({"stage": "comparison", "engine": "rule"})
    diag = details.get("mismatch_diagnosis") or details.get("review_diagnosis")
    if diag and diag.get("provenance"):
        entry = {"stage": "adjudication", "engine": diag["provenance"]}
        if diag["provenance"] == "model":
            entry["model"] = MODEL
        trail.append(entry)
    return trail


def _adjudicate(email_data, review_reason, details, bundle_dir,
                si_doc=None, bl_doc=None):
    """
    Run the Laya -> LLM adjudication chain on a NEEDS_REVIEW escalation and
    attach the result as details['review_diagnosis']. The scored verdict is
    unchanged — the diagnosis explains the escalation and names the engine
    that produced it.
    """
    context = _case_context(
        email_data, si_doc, bl_doc,
        details.get("si_fields") or {}, details.get("bl_fields") or {},
        [], details.get("field_comparisons") or {},
        bundle_dir, review_reason=review_reason)
    details["review_diagnosis"] = adjudicate_review(
        email_data, review_reason,
        details.get("si_fields") or {}, details.get("bl_fields") or {},
        context)


def _evidence_from_comparisons(field_comparisons, si_fields, bl_fields, si_prov, bl_prov):
    evidence = []
    for field, comp in (field_comparisons or {}).items():
        if comp.get("match"):
            continue
        entry = {
            "field": field,
            "si_value": si_fields.get(field),
            "bl_value": bl_fields.get(field),
            "provenance": {"si": si_prov.get(field), "bl": bl_prov.get(field)},
            "reason": comp.get("reason"),
        }
        if comp.get("confidence"):
            entry["confidence"] = comp["confidence"]
        evidence.append(entry)
    return evidence


_BOOKING_REF_RE = re.compile(r"\b5[A-Z]{3}-\d{4,6}\b")

def _booking_refs(subject):
    return set(_BOOKING_REF_RE.findall((subject or "").upper()))

def _related_correspondence(email_data, bundle_dir, max_emails=12):
    """
    Collect the rest of the correspondence for a MISMATCH case: every other
    inbox email from the same sender OR sharing a booking reference
    (5XXX-NNNNN) in the subject, oldest first. Each entry carries the text
    of its SI/BL attachments so the adjudicating model sees all previous
    documents, not just the current pair.
    """
    inbox_dir = os.path.join(bundle_dir, "inbox")
    if not os.path.isdir(inbox_dir):
        return []
    cur_id = email_data.get("email_id", "")
    cur_from = (email_data.get("from") or "").strip().lower()
    cur_refs = _booking_refs(email_data.get("subject"))
    related = []
    for fname in sorted(os.listdir(inbox_dir)):
        if not fname.endswith(".json"):
            continue
        try:
            with open(os.path.join(inbox_dir, fname), "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        eid = data.get("email_id", fname[:-5])
        if eid == cur_id:
            continue
        same_sender = bool(cur_from) and (data.get("from") or "").strip().lower() == cur_from
        if not (same_sender or _booking_refs(data.get("subject")) & cur_refs):
            continue
        docs = []
        for att in data.get("attachments") or []:
            path = os.path.join(bundle_dir, att)
            if not os.path.exists(path) or is_image_file(path):
                continue
            try:
                text = extract_text(path)
            except Exception:
                continue
            docs.append({
                "name": att,
                "kind": "SI" if is_si_attachment(path, text) else "BL",
                "text": text,
            })
        related.append({
            "email_id": eid,
            "from": data.get("from", ""),
            "subject": data.get("subject", ""),
            "body": data.get("body", ""),
            "docs": docs,
        })
    related.sort(key=lambda e: e["email_id"])
    return related[-max_emails:]

def _truncate(text, limit):
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + " …[truncated]"

def _thread_replies(email_id):
    """
    Inbound/outbound messages captured by the live server's thread tracker
    (email_threads.json) for this email_id — e.g. the carrier replying with
    an amended draft BL. Empty when the store does not exist (batch runs).
    """
    path = os.path.join(BASE_DIR, "email_threads.json")
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            store = json.load(f)
    except Exception:
        return []
    msgs = store.get(email_id)
    return msgs if isinstance(msgs, list) else []

def _case_context(email_data, si_doc, bl_doc, si_fields, bl_fields,
                  defect_fields, field_comparisons, bundle_dir,
                  review_reason=None):
    """
    Assemble the full case file handed to the adjudicating model: current
    email + current documents + extracted fields + the flagged issue
    (mismatched fields or a review escalation), then all prior
    correspondence with its SI/BL documents. Prior messages are added
    newest-first and the oldest are dropped once the prompt-size budget
    is hit — recent context is the most likely to explain the issue.
    """
    si_doc, bl_doc = si_doc or {}, bl_doc or {}
    parts = [
        "## CURRENT EMAIL",
        f"id: {email_data.get('email_id')}\n"
        f"from: {email_data.get('from')}\n"
        f"subject: {email_data.get('subject')}\n"
        f"body:\n{_truncate(email_data.get('body'), 3000)}",

        f"\n## CURRENT SHIPPING INSTRUCTION ({os.path.basename(si_doc.get('path', '') or 'not attached')})",
        _truncate(si_doc.get("text"), 4000) or "[no document]",

        f"\n## CURRENT DRAFT BILL OF LADING ({os.path.basename(bl_doc.get('path', '') or 'not attached')})",
        _truncate(bl_doc.get("text"), 4000) or "[no document]",

        "\n## EXTRACTED FIELDS",
        "SI: " + json.dumps(si_fields or {}, ensure_ascii=False),
        "BL: " + json.dumps(bl_fields or {}, ensure_ascii=False),
    ]
    if defect_fields:
        parts.append("\n## FLAGGED MISMATCHES")
        for f in defect_fields:
            c = (field_comparisons or {}).get(f) or {}
            parts.append(
                f"- {f}: SI='{c.get('raw_si', (si_fields or {}).get(f))}' "
                f"vs BL='{c.get('raw_bl', (bl_fields or {}).get(f))}' — {c.get('reason')}")
    if review_reason:
        parts.append(f"\n## FLAGGED REVIEW REASON\n- {review_reason}")

    head = "\n".join(parts)

    reply_blocks = []
    for m in _thread_replies(email_data.get("email_id", "")):
        att_names = ", ".join(
            (a.get("filename") if isinstance(a, dict) else str(a))
            for a in (m.get("attachments") or []))
        reply_blocks.append(
            f"\n### [{m.get('direction', 'MSG')}] {m.get('from_addr', '')} "
            f"({m.get('received_at') or m.get('sent_at') or ''})\n"
            f"subject: {m.get('subject', '')}\n"
            f"{_truncate(m.get('body'), 1500)}"
            + (f"\nattachments: {att_names}" if att_names else ""))
    if reply_blocks:
        head += "\n\n## THREAD REPLIES" + "".join(reply_blocks)

    MAX = 24000
    prior = _related_correspondence(email_data, bundle_dir)
    tail = []
    used = len(head)
    for e in reversed(prior):
        block_parts = [
            f"\n### [{e['email_id']}] from: {e['from']} | subject: {e['subject']}",
            _truncate(e["body"], 1500),
        ]
        for d in e["docs"]:
            block_parts.append(f"-- attachment {d['name']} ({d['kind']}):")
            block_parts.append(_truncate(d["text"], 2500))
        block = "\n".join(block_parts)
        if used + len(block) > MAX:
            continue
        tail.insert(0, block)
        used += len(block)
    if tail:
        head += "\n\n## PRIOR CORRESPONDENCE (oldest first)" + "".join(tail)
    return head


KNOWN_PAPER_TYPES = (
    "SHIPPING_INSTRUCTION", "BILL_OF_LADING", "DRAFT_BILL_OF_LADING",
    "SI", "BL", "OTHER", "UNKNOWN"
)


def _read_doc(path):
    """Build a doc dict for one attachment — vision fields for photos/scans
    of paper documents, extracted text for office files."""
    if is_image_file(path):
        fields, meta = analyze_document_image(path)
        return {"path": path, "fields": fields, "meta": meta,
                "text": meta.get("transcription", "")}
    return {"path": path, "fields": None, "meta": {}, "text": extract_text(path)}


def _compare_docs(category, email_data, si_doc, bl_doc, details, bundle_dir):
    """
    Shared extraction + comparison + adjudication tail for an SI/BL pair.
    si_doc may be synthesised from the email body (fields pre-populated,
    provenance 'email_body').
    """
    # 4. Extract Fields — tiered regex->LLM for text docs; vision fields for photos
    def _extract(doc):
        if doc.get("fields"):
            fields = {k: str(doc["fields"].get(k) or "") for k in CORE_FIELDS}
            prov = dict(doc.get("provenance") or {k: "vision" for k in CORE_FIELDS})
            # Body-sourced docs (inline SI): blanks still go through the
            # regex->LLM tier so a missed field doesn't force NEEDS_REVIEW.
            blanks = [k for k in CORE_FIELDS if is_blank_or_missing(fields[k])]
            if blanks and doc.get("text"):
                tiered, tprov = extract_fields_tiered(doc["text"])
                for k in blanks:
                    if not is_blank_or_missing(tiered.get(k)):
                        fields[k] = tiered[k]
                        prov[k] = tprov.get(k, "model")
            return fields, prov
        return extract_fields_tiered(doc["text"])

    si_fields, si_prov = _extract(si_doc)
    bl_fields, bl_prov = _extract(bl_doc)
    details["si_provenance"] = si_prov
    details["bl_provenance"] = bl_prov
    details["si_fields"] = si_fields
    details["bl_fields"] = bl_fields

    # Extraction failures return "ERROR" placeholders - never treat as a match
    extracted = list(si_fields.values()) + list(bl_fields.values())
    if any(str(v).strip().upper() == "ERROR" for v in extracted):
        _adjudicate(email_data, "unreadable", details, bundle_dir,
                    si_doc=si_doc, bl_doc=bl_doc)
        return _verdict(category, "NEEDS_REVIEW", "unreadable", False, [], details), si_fields, bl_fields

    # 5. Compare Fields
    defect_fields, is_missing_val, field_comparisons = compare_fields(si_fields, bl_fields)
    details["field_comparisons"] = field_comparisons
    details["evidence"] = _evidence_from_comparisons(
        field_comparisons, si_fields, bl_fields, si_prov, bl_prov)

    # Precedence: a genuine value-vs-value defect is never swallowed by an
    # escalation; blanks (one-sided or both) go to details/evidence.
    if defect_fields:
        # Adjudicate the mismatch — Laya triages first; when it cannot
        # answer, the LLM reviews the whole correspondence (all prior
        # emails and their SI/BL documents plus this pair) to explain
        # what could be the issue. The scored verdict stays MISMATCH.
        context = _case_context(email_data, si_doc, bl_doc, si_fields,
                                bl_fields, defect_fields, field_comparisons,
                                bundle_dir)
        details["mismatch_diagnosis"] = diagnose_mismatch(
            email_data, defect_fields, field_comparisons,
            si_fields, bl_fields, context)
        return _verdict(category, "MISMATCH", None, True, defect_fields, details), si_fields, bl_fields

    if is_missing_val:
        _adjudicate(email_data, "missing_value", details, bundle_dir,
                    si_doc=si_doc, bl_doc=bl_doc)
        return _verdict(category, "NEEDS_REVIEW", "missing_value", False, [], details), si_fields, bl_fields

    # Fields blank in BOTH documents mean we could not read them — we are
    # not entitled to report OK.
    both_blank = [f for f, c in field_comparisons.items() if c.get("blank") == "both"]
    if both_blank:
        _adjudicate(email_data, "unreadable", details, bundle_dir,
                    si_doc=si_doc, bl_doc=bl_doc)
        return _verdict(category, "NEEDS_REVIEW", "unreadable", False, [], details), si_fields, bl_fields

    return _verdict(category, "OK", None, False, [], details), si_fields, bl_fields


def process_email(email_data, bundle_dir=BUNDLE_DIR):
    eid = email_data["email_id"]
    subj = email_data.get("subject", "")
    body = email_data.get("body", "")
    atts = email_data.get("attachments", [])
    has_atts = len(atts) > 0

    details = {
        "classification_provenance": None,
        "intent": None,
        "intent_provenance": None,
        "si_provenance": {},
        "bl_provenance": {},
        "si_fields": {},
        "bl_fields": {},
        "field_comparisons": {},
        "attachment_paths": list(atts),
        "evidence": [],
    }

    # 1. Classify Email
    category, cls_prov = classify_email_with_provenance(
        subj, body, has_atts, sender=email_data.get("from", ""),
        n_attachments=len(atts))
    details["classification_provenance"] = cls_prov

    if category != "BL_COMPARISON":
        return _verdict(category, "OK", None, False, [], details), {}, {}

    # 2. Fewer than 2 attachments: decide by sender intent whether a
    #    comparison was actually requested (nothing to compare otherwise).
    if len(atts) < 2:
        # An SI written into the email body is still real SI content —
        # record it in the audit trail even though no comparison can run.
        inline_si = extract_inline_si_fields(body)
        if inline_si:
            details["si_fields"] = inline_si
            details["si_provenance"] = {k: "email_body" for k in CORE_FIELDS}
            details["si_source"] = "email_body"
        intent, intent_prov = comparison_intent(subj, body, sender=email_data.get("from", ""))
        details["intent"] = intent
        details["intent_provenance"] = intent_prov

        # A single attachment is only "missing the pair" when the body does
        # not already carry the SI. SI inline + one attachment = the
        # carrier's draft-BL reply — the comparison can still run.
        if len(atts) == 1 and inline_si:
            read_err = check_unreadable(email_data, bundle_dir)
            if read_err:
                _adjudicate(email_data, read_err[1], details, bundle_dir,
                            si_doc={"path": "<email body>", "text": body})
                return _verdict(category, read_err[0], read_err[1], False, [], details), {}, {}
            att_path = os.path.join(bundle_dir, atts[0])
            bl_doc = _read_doc(att_path)
            if bl_doc["meta"].get("legibility") == "ILLEGIBLE":
                _adjudicate(email_data, "unreadable", details, bundle_dir,
                            si_doc={"path": "<email body>", "text": body})
                return _verdict(category, "NEEDS_REVIEW", "unreadable", False, [], details), {}, {}
            doc_type = (bl_doc["meta"].get("document_type") or "").upper().replace(" ", "_")
            if doc_type and doc_type not in KNOWN_PAPER_TYPES:
                _adjudicate(email_data, "wrong_doc_type", details, bundle_dir,
                            si_doc={"path": "<email body>", "text": body},
                            bl_doc=bl_doc)
                return _verdict(category, "NEEDS_REVIEW", "wrong_doc_type", False, [], details), {}, {}
            wrong_doc_err = check_wrong_doc_type(body, bl_doc["text"])
            if wrong_doc_err:
                _adjudicate(email_data, wrong_doc_err[1], details, bundle_dir,
                            si_doc={"path": "<email body>", "text": body},
                            bl_doc=bl_doc)
                return _verdict(category, wrong_doc_err[0], wrong_doc_err[1], False, [], details), {}, {}
            si_key = bl_doc["meta"].get("document_type") or bl_doc["text"]
            # If the lone attachment is itself an SI there is still no BL to
            # compare against — fall through to the missing_attachment check.
            if not is_si_attachment(att_path, si_key):
                si_doc = {"path": "<email body>", "fields": inline_si, "meta": {},
                          "text": body,
                          "provenance": {k: "email_body" for k in CORE_FIELDS}}
                return _compare_docs(category, email_data, si_doc, bl_doc,
                                     details, bundle_dir)

        att_err = check_attachments(email_data, bundle_dir, intent)
        if att_err:
            _adjudicate(email_data, att_err[1], details, bundle_dir,
                        si_doc={"path": "<email body>", "text": body} if inline_si else None)
            return _verdict(category, att_err[0], att_err[1], False, [], details), {}, {}
        return _verdict(category, "OK", None, False, [], details), {}, {}

    read_err = check_unreadable(email_data, bundle_dir)
    if read_err:
        _adjudicate(email_data, read_err[1], details, bundle_dir)
        return _verdict(category, read_err[0], read_err[1], False, [], details), {}, {}

    # Read documents — text parsers for office files, a single vision pass
    # for camera photos/scans of paper documents (incl. handwritten bills).
    path1 = os.path.join(bundle_dir, atts[0])
    path2 = os.path.join(bundle_dir, atts[1])
    docs = [_read_doc(path1), _read_doc(path2)]

    # An illegible photo escalates the same way as an unreadable file
    if any(d["meta"].get("legibility") == "ILLEGIBLE" for d in docs):
        _adjudicate(email_data, "unreadable", details, bundle_dir,
                    si_doc=docs[0], bl_doc=docs[1])
        return _verdict(category, "NEEDS_REVIEW", "unreadable", False, [], details), {}, {}

    # Vision-reported doc types catch paper wrong-docs (e.g. a photographed
    # commercial invoice) that the text keyword check cannot see.
    if any((d["meta"].get("document_type") or "").upper().replace(" ", "_") not in KNOWN_PAPER_TYPES
           and d["meta"].get("document_type") for d in docs):
        _adjudicate(email_data, "wrong_doc_type", details, bundle_dir,
                    si_doc=docs[0], bl_doc=docs[1])
        return _verdict(category, "NEEDS_REVIEW", "wrong_doc_type", False, [], details), {}, {}

    # Check Wrong Doc Type
    wrong_doc_err = check_wrong_doc_type(docs[0]["text"], docs[1]["text"])
    if wrong_doc_err:
        _adjudicate(email_data, wrong_doc_err[1], details, bundle_dir,
                    si_doc=docs[0], bl_doc=docs[1])
        return _verdict(category, wrong_doc_err[0], wrong_doc_err[1], False, [], details), {}, {}

    # 3. Determine which is SI and which is BL (filename-first detection;
    #    for photos the vision-reported document_type acts as the content marker)
    si_key = docs[0]["meta"].get("document_type") or docs[0]["text"]
    if is_si_attachment(path1, si_key):
        si_doc, bl_doc = docs[0], docs[1]
    else:
        si_doc, bl_doc = docs[1], docs[0]

    return _compare_docs(category, email_data, si_doc, bl_doc, details, bundle_dir)


def main():
    email_files = sorted([f for f in os.listdir(INBOX_DIR) if f.endswith('.json')])
    submission = {}
    details_out = {}

    print(f"Processing {len(email_files)} emails...")

    for f in tqdm(email_files):
        path = os.path.join(INBOX_DIR, f)
        with open(path, 'r', encoding='utf-8') as file:
            email_data = json.load(file)

        eid = email_data["email_id"]
        result, si_fields, bl_fields = process_email(email_data)
        details_out[eid] = result.pop("details", {})
        submission[eid] = submission_row(result)
        print(f"\n{eid} Result: {result['status']}")

    with open(SUBMISSION_PATH, "w") as f:
        json.dump(submission, f, indent=2)

    with open(DETAILS_PATH, "w") as f:
        json.dump(details_out, f, indent=2)

    print("Saved submission.json and verification_details.json")

if __name__ == "__main__":
    main()
