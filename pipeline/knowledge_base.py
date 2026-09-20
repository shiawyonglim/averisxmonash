import os
import re
import json
import math
from collections import Counter

from pipeline.ai_engine import get_nvidia_client, _chat_with_retry, MODEL

STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with",
    "is", "are", "was", "were", "be", "been", "it", "its", "this", "that",
    "these", "those", "at", "by", "as", "from", "do", "does", "did", "not",
    "no", "all", "any", "can", "could", "would", "should", "i", "we", "you",
    "me", "my", "our", "your", "what", "which", "who", "whom", "how", "many",
    "much", "show", "tell", "give", "list", "get", "have", "has", "had",
    "there", "their", "they", "them", "about", "into", "over", "after",
    "before", "between", "if", "than", "then", "so", "such", "also", "just",
}

_EMAIL_ID_RX = re.compile(r"email_(\d{1,4})", re.IGNORECASE)
_BARE_NUM_RX = re.compile(r"\b(\d{1,4})\b")

_DOCS_CACHE = {}


def invalidate_cache():
    _DOCS_CACHE.clear()


def build_documents(inbox_dir, verdicts, classifications, resolutions):
    files = sorted([f for f in os.listdir(inbox_dir) if f.endswith('.json')])
    key = (len(files), len(verdicts), len(classifications), len(resolutions))
    if _DOCS_CACHE.get("key") == key and _DOCS_CACHE.get("docs") is not None:
        return _DOCS_CACHE["docs"]

    docs = []
    for f in files:
        eid = f.replace('.json', '')
        path = os.path.join(inbox_dir, f)
        try:
            with open(path, 'r', encoding='utf-8') as fl:
                d = json.load(fl)
        except Exception:
            continue

        v = verdicts.get(eid) or {}
        atts = d.get("attachments", []) or []
        docs.append({
            "email_id": eid,
            "subject": d.get("subject", ""),
            "sender": d.get("from", ""),
            "body": d.get("body", ""),
            "attachments": atts,
            "attachment_count": len(atts),
            "category": classifications.get(eid) or "UNCLASSIFIED",
            "status": v.get("status") or "UNVERIFIED",
            "review_reason": v.get("review_reason") or "",
            "defect_fields": v.get("defect_fields") or [],
            "si_fields": v.get("si_fields") or {},
            "bl_fields": v.get("bl_fields") or {},
            "resolved": eid in resolutions,
        })

    _DOCS_CACHE.clear()
    _DOCS_CACHE["key"] = key
    _DOCS_CACHE["docs"] = docs
    return docs


def compute_stats(docs):
    by_status = Counter()
    by_category = Counter()
    defect_field_counts = Counter()
    review_reason_counts = Counter()
    resolved_count = 0
    missing_attachment_count = 0
    verified_count = 0

    for d in docs:
        st = d["status"]
        by_status[st] += 1
        by_category[d["category"]] += 1
        for fld in d["defect_fields"]:
            defect_field_counts[fld] += 1
        if d["review_reason"]:
            review_reason_counts[d["review_reason"]] += 1
        if d["resolved"]:
            resolved_count += 1
        if d["attachment_count"] < 2:
            missing_attachment_count += 1
        if st != "UNVERIFIED":
            verified_count += 1

    return {
        "total_emails": len(docs),
        "by_status": dict(by_status),
        "by_category": dict(by_category),
        "defect_field_counts": dict(defect_field_counts),
        "review_reason_counts": dict(review_reason_counts),
        "resolved_count": resolved_count,
        "missing_attachment_count": missing_attachment_count,
        "verified_count": verified_count,
    }


def _fmt_counts(counts):
    if not counts:
        return "none"
    return ", ".join(f"{k}={v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1]))


def format_stats_block(stats):
    return "\n".join([
        "STATISTICS (authoritative, precomputed):",
        f"Total emails: {stats['total_emails']}",
        f"Verified: {stats['verified_count']} | Resolved: {stats['resolved_count']} | Missing/insufficient attachments: {stats['missing_attachment_count']}",
        f"By status: {_fmt_counts(stats['by_status'])}",
        f"By category: {_fmt_counts(stats['by_category'])}",
        f"Defect field counts: {_fmt_counts(stats['defect_field_counts'])}",
        f"Review reason counts: {_fmt_counts(stats['review_reason_counts'])}",
    ])


def _tokenize(text):
    toks = re.findall(r"[a-z0-9_]+", (text or "").lower())
    return [t for t in toks if t not in STOPWORDS and len(t) > 1]


def _meta_text(d):
    parts = [
        d["category"], d["status"], d["review_reason"], d["sender"],
        " ".join(d["defect_fields"]),
        " ".join(str(v) for v in d["si_fields"].values()),
        " ".join(str(v) for v in d["bl_fields"].values()),
    ]
    if d["resolved"]:
        parts.append("resolved")
    return " ".join(str(p) for p in parts if p)


_STATUS_CATEGORY_BOOSTS = [
    (["mismatch"], lambda d: d["status"] == "MISMATCH"),
    (["needs review", "needs_review", "review"], lambda d: d["status"] == "NEEDS_REVIEW"),
    (["ok"], lambda d: d["status"] == "OK"),
    (["spam"], lambda d: d["category"] == "SPAM"),
    (["invoice"], lambda d: d["category"] == "INVOICE_QUERY"),
    (["resolved", "resolution"], lambda d: d["resolved"]),
    (["corrupt", "corrupted", "unreadable"], lambda d: "unreadable" in (d["review_reason"] or "").lower() or "corrupt" in (d["review_reason"] or "").lower()),
    (["missing"], lambda d: "missing" in (d["review_reason"] or "").lower() or d["attachment_count"] < 2),
]


def retrieve(query, docs, k=8):
    q_tokens = _tokenize(query)
    q_lower = (query or "").lower()

    forced_ids = set()
    for m in _EMAIL_ID_RX.finditer(q_lower):
        forced_ids.add(f"email_{int(m.group(1)):03d}")
    for m in _BARE_NUM_RX.finditer(q_lower):
        forced_ids.add(f"email_{int(m.group(1)):03d}")

    boost_predicates = [pred for kws, pred in _STATUS_CATEGORY_BOOSTS if any(kw in q_lower for kw in kws)]

    N = len(docs)
    if N == 0:
        return []

    doc_tokens = []
    df = Counter()
    for d in docs:
        subj = _tokenize(d["subject"])
        meta = _tokenize(_meta_text(d))
        body = _tokenize(d["body"])
        doc_tokens.append((subj, meta, body))
        for t in set(subj + meta + body):
            df[t] += 1

    idf = {t: math.log(1 + N / (1 + cnt)) for t, cnt in df.items()}

    scored = []
    for i, d in enumerate(docs):
        subj, meta, body = doc_tokens[i]
        score = 0.0
        for t in q_tokens:
            w = idf.get(t, 0.0)
            if w == 0.0:
                continue
            score += w * (3 * subj.count(t) + 2 * meta.count(t) + 1 * body.count(t))
        if boost_predicates and any(pred(d) for pred in boost_predicates):
            score += 5.0
        scored.append((score, i, d))

    forced = [d for _, _, d in scored if d["email_id"] in forced_ids]
    rest = sorted(
        [(s, i, d) for s, i, d in scored if d["email_id"] not in forced_ids and s > 0],
        key=lambda x: -x[0],
    )
    out = forced + [d for _, _, d in rest]
    return out[:k]


def _fmt_fields(fields):
    return "{" + ", ".join(f"{k}={v}" for k, v in fields.items() if v not in (None, "")) + "}"


def format_context(docs_subset, body_chars=600):
    blocks = []
    for d in docs_subset:
        defects = ",".join(d["defect_fields"]) if d["defect_fields"] else "none"
        head = f"### {d['email_id']} | status={d['status']} | category={d['category']} | defects={defects}"
        lines = [head, f"From: {d['sender']}", f"Subject: {d['subject']}",
                 f"Attachments: {d['attachment_count']}"]
        if d["review_reason"]:
            lines.append(f"Review reason: {d['review_reason']}")
        if d["resolved"]:
            lines.append("Resolved: yes")
        si = {k: v for k, v in d["si_fields"].items() if v not in (None, "")}
        bl = {k: v for k, v in d["bl_fields"].items() if v not in (None, "")}
        if si:
            lines.append(f"SI: {_fmt_fields(si)}")
        if bl:
            lines.append(f"BL: {_fmt_fields(bl)}")
        body = re.sub(r"\s+", " ", d["body"] or "").strip()[:body_chars]
        if body:
            lines.append(f"Body: {body}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


SYSTEM_PROMPT = (
    "You are the analyst assistant for a shipping-document verification system. "
    "The pipeline verifies draft Bills of Lading (BL) against Shipping Instructions (SI) "
    "by comparing 7 fields: shipper, consignee, notify_party, port_of_loading, "
    "port_of_discharge, container_count, gross_weight_kg. Statuses are OK (all match), "
    "MISMATCH (one or more fields differ, see defect_fields), and NEEDS_REVIEW "
    "(edge cases: wrong_doc_type, missing_attachment, unreadable, missing_value).\n\n"
    "Answer ONLY from the STATISTICS block and RECORDS provided. The statistics are "
    "authoritative and already computed — never recompute or estimate counts. Cite "
    "email ids inline like email_042. If the context doesn't contain the answer, say "
    "so plainly and suggest a better query. Be concise; use short markdown, tables or "
    "bullets where useful; never invent field values."
)


def _degraded_answer(question, stats_block, retrieved):
    lines = [
        "Note: the AI model is currently unavailable — this is a deterministic "
        "summary built from the local knowledge base.",
        "",
        stats_block,
        "",
        "Relevant records:",
    ]
    if not retrieved:
        lines.append("  (no matching records found — try mentioning an email id like email_042 or a status/category keyword)")
    else:
        lines.append(format_context(retrieved, body_chars=200))
    return "\n".join(lines)


def answer_question(question, history, docs):
    stats = compute_stats(docs)
    stats_block = format_stats_block(stats)
    retrieved = retrieve(question, docs, k=8)
    sources = [
        {
            "email_id": d["email_id"],
            "subject": d["subject"],
            "status": d["status"],
            "category": d["category"],
        }
        for d in retrieved
    ]

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.append({
        "role": "user",
        "content": stats_block + "\n\nRECORDS:\n" + (format_context(retrieved) or "(no records matched)"),
    })
    for h in (history or [])[-6:]:
        role = h.get("role") if isinstance(h, dict) else None
        content = h.get("content") if isinstance(h, dict) else None
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": question})

    try:
        client = get_nvidia_client()
        resp = _chat_with_retry(
            client,
            model=MODEL,
            messages=messages,
            temperature=0.2,
            max_tokens=900,
        )
        answer = resp.choices[0].message.content or ""
        return {"answer": answer, "sources": sources, "degraded": False}
    except Exception as e:
        print(f"Chat answer degraded (LLM unavailable): {e}")
        return {
            "answer": _degraded_answer(question, stats_block, retrieved),
            "sources": sources,
            "degraded": True,
        }
