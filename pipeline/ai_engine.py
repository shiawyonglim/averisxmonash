import os
import json
import re
import time
import hashlib
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

MODEL = (os.getenv("AI_MODEL") or os.getenv("KIMI_MODEL")
         or os.getenv("MUSE_MODEL") or "meta/llama-3.2-11b-vision-instruct")

# When disabled (AI_FALLBACK=0), every LLM call is skipped and the pipeline
# degrades to "value not extracted" / "GENERAL" rather than silently matching.
AI_FALLBACK_ENABLED = os.getenv("AI_FALLBACK", "1").lower() not in ("0", "false", "no")

# Hard cap on live (uncached) LLM calls per process. Cache reads are free —
# only calls that would actually hit the network count. Once exhausted the
# engine behaves exactly as if AI_FALLBACK were disabled: extraction blanks
# stay 'missing', classification returns GENERAL/'fallback', intent returns
# OTHER/'fallback'.
# 0 or a negative value means unlimited (intended for the deployed server;
# the 30-call default protects local runs/tests from flooding the API).
AI_MAX_CALLS = int(os.getenv("AI_MAX_CALLS", "30"))
_LLM_CALL_COUNT = 0
_LLM_BUDGET_LOGGED = False

def _budget_gate():
    """
    Consume one unit of the live-LLM budget. Returns True while calls may hit
    the network; False once AI_MAX_CALLS is exhausted — the caller must then
    take its deterministic fallback path. Logs exactly once when the budget
    trips.
    """
    global _LLM_CALL_COUNT, _LLM_BUDGET_LOGGED
    if AI_MAX_CALLS <= 0:
        return True
    if _LLM_CALL_COUNT >= AI_MAX_CALLS:
        if not _LLM_BUDGET_LOGGED:
            _LLM_BUDGET_LOGGED = True
            print(f"AI call budget exhausted (AI_MAX_CALLS={AI_MAX_CALLS}); "
                  "remaining requests degrade to deterministic fallback.")
        return False
    _LLM_CALL_COUNT += 1
    return True

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LLM_CACHE_PATH = os.path.join(BASE_DIR, ".cache", "llm_cache.json")
_LLM_CACHE = None

def _llm_cache():
    global _LLM_CACHE
    if _LLM_CACHE is None:
        try:
            with open(_LLM_CACHE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            _LLM_CACHE = data if isinstance(data, dict) else {}
        except Exception:
            _LLM_CACHE = {}
    return _LLM_CACHE

def _cached_llm(task, text, call_fn):
    """
    Read-through / write-after on-disk cache for LLM responses.
    Key = sha256 of "<task>|<MODEL>|<text>". call_fn's return value must be
    JSON-serializable; None results are not cached (failures retry next run).
    """
    key = hashlib.sha256(f"{task}|{MODEL}|{text}".encode("utf-8")).hexdigest()
    cache = _llm_cache()
    if key in cache:
        return cache[key]
    if not _budget_gate():
        return None
    result = call_fn()
    if result is not None:
        cache[key] = result
        try:
            os.makedirs(os.path.dirname(_LLM_CACHE_PATH), exist_ok=True)
            tmp = _LLM_CACHE_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cache, f)
            os.replace(tmp, _LLM_CACHE_PATH)
        except Exception:
            pass
    return result

def get_nvidia_client():
    # Pair the API key with whichever model is active. Each model may have
    # its own scoped key in .env; fall back to the generic NVIDIA_API_KEY.
    if MODEL == os.getenv("KIMI_MODEL"):
        key = os.getenv("NVIDIA_KIMI_API_KEY") or os.getenv("NVIDIA_API_KEY")
    elif MODEL == os.getenv("MUSE_MODEL"):
        key = os.getenv("NVIDIA_MUSE_API_KEY") or os.getenv("NVIDIA_API_KEY")
    else:
        key = os.getenv("NVIDIA_API_KEY")
    return OpenAI(
        base_url=os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1"),
        api_key=key,
        timeout=240.0
    )

def _chat_with_retry(client, **kwargs):
    """
    One retry on transient failures (429 rate-limit, timeouts) so batched
    concurrent calls don't silently degrade into fallback results.
    """
    try:
        return client.chat.completions.create(**kwargs)
    except Exception:
        time.sleep(3)
        return client.chat.completions.create(**kwargs)

def _rules_category_detailed(subject, body, has_attachments):
    """
    Deterministic rule-based classification returning:
    (base_category, subcategory_code, display_tag, description)
    """
    s = (subject or "").upper()
    b = (body or "").upper()
    text = s + " " + b

    # 1. SPAM check
    spam_tokens = [
        "WEIRD TRICK", "BITCOIN", "90% OFF", "INVESTMENT OPPORTUNITY",
        "UPDATE YOUR ACCOUNT", "LOTTERY", "WINNER", "CONGRATULATIONS",
        "PREMIUM LOGISTICS SOFTWARE THIS WEEK ONLY", "STORAGE IS FULL",
        "UNDELIVERED MESSAGES", "VERIFY ACCOUNT IMMEDIATELY",
        "BANK OFFICER", "BUSINESS PROPOSAL", "HELLO DEAR"
    ]
    if any(k in text for k in spam_tokens):
        if any(k in text for k in ["BANK OFFICER", "BUSINESS PROPOSAL", "HELLO DEAR"]):
            return (
                "SPAM",
                "SPAM_PHISHING_SCAM",
                "SPAM (Phishing Scam)",
                "Advance-fee fraud / Nigerian 419 financial solicitation scam"
            )
        elif any(k in text for k in ["STORAGE IS FULL", "UPDATE YOUR ACCOUNT", "VERIFY ACCOUNT", "UNDELIVERED MESSAGES"]):
            return (
                "SPAM",
                "SPAM_SECURITY_ALERT",
                "SPAM (Security Alert)",
                "Deceptive account suspension / mailbox quota alert"
            )
        elif any(k in text for k in ["BITCOIN", "INVESTMENT OPPORTUNITY"]):
            return (
                "SPAM",
                "SPAM_CRYPTO_SOLICITATION",
                "SPAM (Crypto Solicitation)",
                "Unsolicited cryptocurrency / high-yield investment scheme"
            )
        elif "WEIRD TRICK" in text:
            return (
                "SPAM",
                "SPAM_CLICKBAIT",
                "SPAM (Clickbait Promo)",
                "Sensationalist clickbait marketing solicitation"
            )
        elif any(k in text for k in ["90% OFF", "PREMIUM LOGISTICS SOFTWARE"]):
            return (
                "SPAM",
                "SPAM_SALES_PROMO",
                "SPAM (Sales Promotion)",
                "Cold software sales outreach and commercial marketing promo"
            )
        return (
            "SPAM",
            "SPAM_UNSOLICITED",
            "SPAM (Unsolicited Bulk)",
            "Unsolicited bulk email communication"
        )

    # 2. Emails with attachments -> BL_COMPARISON
    if has_attachments:
        if "AMEND" in s:
            return (
                "BL_COMPARISON",
                "BL_AMENDMENT_REVIEW",
                "BL AUDIT (Carrier Amendment)",
                "Carrier amended draft Bill of Lading comparison against approved SI"
            )
        return (
            "BL_COMPARISON",
            "BL_DOCUMENT_VERIFICATION",
            "BL AUDIT (2-Doc Cross-Check)",
            "Shipping Instructions vs Draft Bill of Lading 7-field cross-verification"
        )

    # 3. GENERAL patterns
    gen_tokens = [
        "RPA_", "_RPA_", "UPDATE SUMMARY", "REMINDER_PAPER - SUBMIT SI",
        "DELIVERY PLANNING", "TIME OFF REQUEST", "APPROVAL REQUIRED",
        "BERTHING REPORT", "MISS CONNECTION", "WELCOMING THE NEW YEAR",
        "PENDING BL RELEASE", "LIST OF OUTSTANDING BL"
    ]
    if any(k in s for k in gen_tokens):
        if "_RPA_" in s or "RPA_" in s:
            return (
                "GENERAL",
                "GENERAL_RPA_BOT",
                "GENERAL (RPA Automation)",
                "Automated RPA robotic system execution confirmation"
            )
        elif "UPDATE SUMMARY" in s or "BERTHING" in s or "MISS CONNECTION" in s:
            return (
                "GENERAL",
                "GENERAL_VESSEL_UPDATE",
                "GENERAL (Vessel Update)",
                "Vessel schedule, berthing report, or feeder connection advisory"
            )
        elif "DELIVERY PLANNING" in s:
            return (
                "GENERAL",
                "GENERAL_LOGISTICS_PLANNING",
                "GENERAL (Logistics Planning)",
                "Weekly warehouse delivery planning and container trucking schedule"
            )
        elif any(k in text for k in ["TIME OFF", "HOLIDAY", "NEW YEAR"]):
            return (
                "GENERAL",
                "GENERAL_ADMIN_NOTICE",
                "GENERAL (Admin Notice)",
                "Internal administrative announcement or operational holiday schedule"
            )
        return (
            "GENERAL",
            "GENERAL_OPERATIONAL",
            "GENERAL (Operational Advisory)",
            "General maritime operations notice or inquiry"
        )

    # 4. INVOICE_QUERY patterns
    inv_tokens = [
        "LOCAL CHARGES", "TOTAL FREIGHT", "D & D", "CANCEL INVOICE",
        "BILLING", "MISSING GR", "TELEX RELEASE", "INVOICE"
    ]
    if any(k in s for k in inv_tokens):
        if "D & D" in s or "DEMURRAGE" in text or "DETENTION" in text:
            return (
                "INVOICE_QUERY",
                "INVOICE_DEMURRAGE",
                "INVOICE (Demurrage & D&D)",
                "Container demurrage & detention charges calculation inquiry"
            )
        elif "LOCAL CHARGES" in s or "TELEX RELEASE" in s:
            return (
                "INVOICE_QUERY",
                "INVOICE_TELEX_CHARGES",
                "INVOICE (Telex & Local Fees)",
                "FOB local port charges and telex surrender release fee confirmation"
            )
        elif "TOTAL FREIGHT" in s or "FREIGHT" in text:
            return (
                "INVOICE_QUERY",
                "INVOICE_FREIGHT",
                "INVOICE (Ocean Freight Query)",
                "Ocean carrier base freight and bunker adjustment billing"
            )
        elif "CANCEL INVOICE" in s or "CREDIT NOTE" in text:
            return (
                "INVOICE_QUERY",
                "INVOICE_CANCELLATION",
                "INVOICE (Cancellation Request)",
                "Carrier invoice cancellation dispute or credit note issuance request"
            )
        return (
            "INVOICE_QUERY",
            "INVOICE_GENERAL",
            "INVOICE (Billing Inquiry)",
            "Financial accounting billing statement query"
        )

    # 5. SI_REQUEST patterns
    si_markers = ["REQUEST SI", "SI NEEDED", "CUST SI", "SI - "]
    is_si = any(k in s for k in si_markers) or (
        bool(re.search(r'\bSI\b', s)) and not any(k in s for k in ["BL", "DRAFT", "DOCS", "AFEMY", "AFPTME", "AFRT", "AIE"])
    )
    if is_si:
        if "REQUEST SI" in s or "SI NEEDED" in s:
            return (
                "SI_REQUEST",
                "SI_DEADLINE_URGENT",
                "SI REQUEST (Urgent Cutoff)",
                "Urgent carrier request for Shipping Instructions prior to port cutoff"
            )
        elif "DIRECT(" in s or "DIRECT (" in s:
            return (
                "SI_REQUEST",
                "SI_CARRIER_DIRECT",
                "SI REQUEST (Carrier Direct)",
                "Direct liner shipping instruction submission to ocean carrier"
            )
        elif "CUST SI" in s:
            return (
                "SI_REQUEST",
                "SI_CUSTOMER",
                "SI REQUEST (Customer Submission)",
                "Customer-drafted shipping instruction transmission"
            )
        return (
            "SI_REQUEST",
            "SI_GENERAL",
            "SI REQUEST (General)",
            "General shipping instruction filing communication"
        )

    # 6. BL_COMPARISON unattached drafts
    bl_markers = [
        "TO CONFIRM DOCS", "DRAFT BL", "REQUEST BL DRAFT", "AMEND BL",
        "AFEMY", "AFPTME", "AFRT", "AIE -"
    ]
    if any(k in s for k in bl_markers) or bool(re.search(r'\bBL\b|\bDRAFT\b|\bDOCS\b', s)):
        return (
            "BL_COMPARISON",
            "BL_AWAITING_ATTACHMENT",
            "BL AUDIT (Pending Draft BL)",
            "Ocean carrier draft BL confirmation email with pending attachment"
        )

    return (
        "GENERAL",
        "GENERAL_UNMATCHED",
        "GENERAL (Operational Advisory)",
        "General maritime operational communication"
    )

def _rules_category(subject, body, has_attachments):
    cat, _, _, _ = _rules_category_detailed(subject, body, has_attachments)
    return cat

def classify_email_detailed(subject, body, has_attachments):
    """
    Returns full dictionary with:
    category, subcategory, display_tag, description
    """
    cat, subcat, display_tag, desc = _rules_category_detailed(subject, body, has_attachments)
    return {
        "category": cat,
        "subcategory": subcat,
        "display_tag": display_tag,
        "description": desc
    }

def quick_classify(subject, body, has_attachments):
    """
    Fast rule-only classification (NO AI fallback). Returns 'GENERAL' when
    no deterministic rule matches. Used by queue/stats endpoints.
    """
    return _rules_category(subject, body, has_attachments) or "GENERAL"

VALID_CATEGORIES = ("BL_COMPARISON", "SI_REQUEST", "INVOICE_QUERY", "GENERAL", "SPAM")

def _llm_classify(subject, body):
    prompt = f"""Classify this shipping-operations email into exactly one category.

BL_COMPARISON - asks for a draft Bill of Lading to be checked or compared against a Shipping Instruction, or transmits those documents.
SI_REQUEST    - asks for a Shipping Instruction to be prepared, submitted or sent.
INVOICE_QUERY - about charges, freight, billing, invoices, demurrage or detention.
GENERAL       - operational updates, schedules, internal admin, automated reports.
SPAM          - unsolicited marketing, phishing or scams.

Subject: {subject}
Body:
{body}

Output only the category name."""
    client = get_nvidia_client()
    res = _chat_with_retry(
        client,
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        # Reasoning models (e.g. meta/muse-glimmer-30b) spend tokens on
        # chain-of-thought before emitting content — a tiny budget starves
        # the actual answer (content='' with finish_reason='length').
        max_tokens=512,
        temperature=0.0
    )
    msg = res.choices[0].message
    content = (getattr(msg, "content", None) or "").strip()
    if content:
        return content
    # Budget exhausted by reasoning: the chain-of-thought usually names the
    # final category near the end, so scan reasoning_content for the LAST
    # valid-category mention (the conclusion), not the first (the model
    # often restates the category list from the prompt up front).
    reasoning = getattr(msg, "reasoning_content", "") or ""
    best_cat, best_pos = "", -1
    for cat in VALID_CATEGORIES:
        for variant in (cat, cat.replace("_", " ")):
            pos = reasoning.upper().rfind(variant)
            if pos > best_pos:
                best_cat, best_pos = cat, pos
    return best_cat or reasoning.strip()

def classify_email_with_provenance(subject, body, has_attachments):
    """
    Returns (category, provenance) where provenance is
    'rule' | 'model' | 'fallback'. Deterministic rules first; only the
    unmatched GENERAL fallback escalates to the LLM.
    """
    cat, subcat, _, _ = _rules_category_detailed(subject, body, has_attachments)
    if subcat != "GENERAL_UNMATCHED":
        return cat, "rule"
    if not AI_FALLBACK_ENABLED:
        return "GENERAL", "fallback"
    try:
        reply = _cached_llm("classify", f"{subject}\n{body}",
                            lambda: _llm_classify(subject, body))
        if reply:
            normalized = reply.strip().upper()
            # Reasoning content can echo the whole category list before the
            # conclusion — take the LAST word-bounded category mention.
            best_cat, best_idx = None, -1
            for valid in VALID_CATEGORIES:
                for m in re.finditer(rf"\b{valid}\b", normalized):
                    if m.start() > best_idx:
                        best_cat, best_idx = valid, m.start()
            if best_cat:
                return best_cat, "model"
    except Exception as e:
        print(f"Classification error: {e}")
    return "GENERAL", "fallback"

def classify_email(subject, body, has_attachments):
    """
    Categories: BL_COMPARISON, SI_REQUEST, INVOICE_QUERY, GENERAL, SPAM
    Deterministic rules first, then AI fallback for unclear cases.
    """
    cat, _ = classify_email_with_provenance(subject, body, has_attachments)
    return cat

def extract_shipping_fields(text):
    """
    Extracts the 7 core fields from document text with awareness of shipping synonyms.
    """
    client = get_nvidia_client()
    prompt = f"""
    You are an expert shipping document parser.
    Extract the following 7 fields from the document text.
    
    CRITICAL EXTRACTION GUIDELINES:
    - If a field is missing, unreadable, or contains tokens like '???', '_______', 'TBA', 'TBC', 'N/A', return "MISSING_VALUE".
    - shipper: Core company name (e.g. 'APRIL FAR EAST (M) SDN BHD').
    - consignee: Core company name (e.g. 'VITAL SOLUTIONS PTE. LTD.'). Do NOT include prefixes like 'To the Order of' or 'Consignee (Non-Negotiable):'.
    - notify_party: Core company name.
    - port_of_loading: Port and country name without 'POL' or codes (e.g. 'PORT KLANG (WESTPORT), MALAYSIA').
    - port_of_discharge: Port and country name without 'POD' or codes (e.g. 'NEW YORK, US').
    - container_count: Total integer container quantity (e.g. '5' instead of '5 x 20\\'FCL').
    - gross_weight_kg: Total numeric gross weight in KG (e.g. '113,970 KG').
    
    Format the output strictly as a JSON object:
    {{
        "shipper": "...",
        "consignee": "...",
        "notify_party": "...",
        "port_of_loading": "...",
        "port_of_discharge": "...",
        "container_count": "...",
        "gross_weight_kg": "..."
    }}
    
    Document Text:
    {text[:4000]}
    """
    
    try:
        res = _chat_with_retry(
            client,
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=3000
        )
        content = (res.choices[0].message.content or "").strip()
        if content.startswith("```json"):
            content = content[7:]
        if content.startswith("```"):
            content = content[3:]
        if content.endswith("```"):
            content = content[:-3]
            
        return json.loads(content.strip())
    except Exception as e:
        print(f"Extraction error: {e}")
        return {
            "shipper": "ERROR", "consignee": "ERROR", "notify_party": "ERROR",
            "port_of_loading": "ERROR", "port_of_discharge": "ERROR", 
            "container_count": "ERROR", "gross_weight_kg": "ERROR"
        }

CORE_FIELDS = [
    "shipper", "consignee", "notify_party",
    "port_of_loading", "port_of_discharge",
    "container_count", "gross_weight_kg",
]

def extract_fields_tiered(text):
    """
    Two-tier extraction. Returns (fields, provenance) where provenance maps
    each of the 7 fields to 'rule' | 'model' | 'missing'.

    Tier 1 is the deterministic regex extractor; values it produces are never
    overridden by the model. Only blank fields escalate to one cached LLM
    call (when AI_FALLBACK is enabled). Never raises — on any failure the
    remaining blanks stay 'missing'.
    """
    from pipeline.parsers import extract_shipping_fields_fast
    from pipeline.comparator import is_blank_or_missing

    fields = extract_shipping_fields_fast(text or "") or {}
    fields = {k: fields.get(k, "") for k in CORE_FIELDS}
    provenance = {}
    blanks = []
    for k in CORE_FIELDS:
        if is_blank_or_missing(fields.get(k)):
            blanks.append(k)
        else:
            provenance[k] = "rule"

    if blanks and AI_FALLBACK_ENABLED and len((text or "").strip()) >= 40:
        try:
            llm_fields = _cached_llm("extract", text,
                                     lambda: extract_shipping_fields(text))
        except Exception as e:
            print(f"Tiered extraction fallback error: {e}")
            llm_fields = None
        if isinstance(llm_fields, dict):
            for k in blanks:
                v = str(llm_fields.get(k, "")).strip()
                if v and v.upper() not in ("MISSING_VALUE", "ERROR"):
                    fields[k] = v
                    provenance[k] = "model"

    for k in CORE_FIELDS:
        provenance.setdefault(k, "missing")
    return fields, provenance


def _llm_comparison_intent(subject, body):
    prompt = f"""You are triaging one email in a shipping operations inbox.
Decide what the sender wants RIGHT NOW, based only on the text.

Answer with exactly one label:
REQUEST_DOCS - the sender is asking someone else to send or issue a document (e.g. "please assist to send the draft BL for checking"). No document comparison can be performed from this email alone.
COMPARE_NOW  - the sender is asking us to compare, check or verify a Shipping Instruction against a draft Bill of Lading now, and writes as though the documents accompany the message.
OTHER        - neither of the above.

Subject: {subject}
Body:
{body}

Output only the label."""
    client = get_nvidia_client()
    res = _chat_with_retry(
        client,
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=512,
        temperature=0.0
    )
    msg = res.choices[0].message
    # Reasoning models (e.g. muse-glimmer) emit chain-of-thought in
    # reasoning_content and may leave content empty when truncated —
    # scan it too so the answer isn't lost.
    return ((msg.content or "") or (getattr(msg, "reasoning_content", "") or "")).strip()

def comparison_intent(subject, body):
    """
    Returns (intent, provenance) where intent is
    'COMPARE_NOW' | 'REQUEST_DOCS' | 'OTHER' and provenance is
    'rule' | 'model' | 'fallback'.
    """
    text = f"{subject or ''}\n{body or ''}".lower()

    if re.search(r"(assist to send|please send|kindly send|pls send|revert with|share the draft|send (?:us |me )?the (?:draft )?bl)", text):
        return "REQUEST_DOCS", "rule"

    if (re.search(r"(please|kindly|pls)\s+\w*\s*(compare|check|verify|confirm)", text)
            and re.search(r"(\bsi\b|shipping instruction)", text)) \
       or (re.search(r"(attached|enclosed|find attached|herewith)", text)
           and re.search(r"\bbl\b|bill of lading", text)):
        return "COMPARE_NOW", "rule"

    if AI_FALLBACK_ENABLED:
        try:
            reply = _cached_llm("intent", f"{subject}\n{body}",
                                lambda: _llm_comparison_intent(subject, body))
            if reply:
                normalized = reply.strip().upper().replace(" ", "_")
                # Reasoning text echoes the label list before concluding —
                # take the LAST word-bounded occurrence (not the first,
                # and not inside words like "ANOTHER").
                best, best_idx = None, -1
                for label in ("REQUEST_DOCS", "COMPARE_NOW", "OTHER"):
                    for m in re.finditer(rf"\b{label}\b", normalized):
                        if m.start() > best_idx:
                            best, best_idx = label, m.start()
                if best:
                    return best, "model"
        except Exception as e:
            print(f"Intent classification error: {e}")
    return "OTHER", "fallback"


def _image_to_data_uri(image_path, max_dim=1600):
    """
    Load a photo/scan, downscale it (vision tokens + NIM payload limits),
    and return a base64 JPEG data URI for the OpenAI-style image_url field.
    """
    import base64
    import io
    from PIL import Image
    with Image.open(image_path) as im:
        im = im.convert("RGB")
        im.thumbnail((max_dim, max_dim))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=85)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{b64}"


def analyze_document_image(image_path):
    """
    Vision pass over a camera photo or scan of a paper shipping document —
    including handwritten bills, stamps, skewed angles and low contrast.

    Returns (fields, meta):
      fields -> the same 7-field dict produced by extract_shipping_fields
      meta   -> {"document_type", "legibility", "transcription"}
    """
    # Vision pass is a live LLM call — skipped when the fallback flag is off
    # or the AI_MAX_CALLS budget is exhausted (degrades to 'unreadable').
    if not AI_FALLBACK_ENABLED or not _budget_gate():
        return {
            "shipper": "ERROR", "consignee": "ERROR", "notify_party": "ERROR",
            "port_of_loading": "ERROR", "port_of_discharge": "ERROR",
            "container_count": "ERROR", "gross_weight_kg": "ERROR"
        }, {"document_type": "OTHER", "legibility": "ILLEGIBLE", "transcription": "",
            "error": "AI vision disabled (AI_FALLBACK=0 or AI_MAX_CALLS budget exhausted)"}
    client = get_nvidia_client()
    prompt = """
    You are an expert shipping document digitizer. The image shows a paper
    shipping document captured by a camera or scanner — it may be HANDWRITTEN,
    stamped, creased, skewed, or photographed in poor lighting. Read it
    carefully and do not invent values you cannot see.

    STEP 1 - Identify document_type, exactly one of:
    SHIPPING_INSTRUCTION, BILL_OF_LADING, COMMERCIAL_INVOICE, PACKING_LIST, OTHER

    STEP 2 - Rate legibility, exactly one of: CLEAR, PARTIAL, ILLEGIBLE
    (ILLEGIBLE only when most of the document cannot be read at all.)

    STEP 3 - Transcribe the visible text, best effort, roughly preserving layout.
    Use '???' for individual words you cannot make out.

    STEP 4 - Extract these 7 fields:
    - shipper: Core company name (e.g. 'APRIL FAR EAST (M) SDN BHD').
    - consignee: Core company name. Do NOT include prefixes like 'To the Order of'.
    - notify_party: Core company name.
    - port_of_loading: Port and country name without 'POL' or codes.
    - port_of_discharge: Port and country name without 'POD' or codes.
    - container_count: Total integer container quantity (e.g. '5' instead of "5 x 20'FCL").
    - gross_weight_kg: Total numeric gross weight in KG (e.g. '113,970 KG').
    - If a field is absent, unreadable, or a placeholder ('???', '____', 'TBA',
      'TBC', 'N/A'), return "MISSING_VALUE" for it.

    Output strictly as JSON:
    {
        "document_type": "...",
        "legibility": "...",
        "transcription": "...",
        "fields": {
            "shipper": "...",
            "consignee": "...",
            "notify_party": "...",
            "port_of_loading": "...",
            "port_of_discharge": "...",
            "container_count": "...",
            "gross_weight_kg": "..."
        }
    }
    """
    try:
        res = _chat_with_retry(
            client,
            model=MODEL,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": _image_to_data_uri(image_path)}},
                ],
            }],
            temperature=0.0,
            max_tokens=3000
        )
        content = (res.choices[0].message.content or "").strip()
        if content.startswith("```json"):
            content = content[7:]
        if content.startswith("```"):
            content = content[3:]
        if content.endswith("```"):
            content = content[:-3]

        data = json.loads(content.strip())
        fields = data.get("fields") or {}
        meta = {
            "document_type": str(data.get("document_type", "OTHER")).upper(),
            "legibility": str(data.get("legibility", "PARTIAL")).upper(),
            "transcription": str(data.get("transcription", "")),
        }
        return fields, meta
    except Exception as e:
        print(f"Vision extraction error: {e}")
        return {
            "shipper": "ERROR", "consignee": "ERROR", "notify_party": "ERROR",
            "port_of_loading": "ERROR", "port_of_discharge": "ERROR",
            "container_count": "ERROR", "gross_weight_kg": "ERROR"
        }, {"document_type": "OTHER", "legibility": "ILLEGIBLE", "transcription": "",
            "error": str(e)[:300]}


def _reasoning_fallback(defect_fields):
    """Deterministic reasoning text used when the LLM is unavailable."""
    if defect_fields:
        return {
            "thoughts": "Evaluated all 7 fields against shipping standards. Identified genuine discrepancy in the highlighted fields.",
            "summary_reason": f"Discrepancy detected in: {', '.join(defect_fields)}."
        }
    return {
        "thoughts": "Evaluated all 7 fields including negotiable phrasing, port designations, and packaging units. All operational details correspond accurately.",
        "summary_reason": "All 7 critical shipping fields match accurately."
    }

def reason_and_verify_with_ai(si_data, bl_data, defect_fields, is_missing_value, field_comparisons):
    """
    Uses NVIDIA NIM to formulate a reasoned explanation and thought process before finalizing.
    """
    # Live LLM call — skipped when AI_FALLBACK is off or the AI_MAX_CALLS
    # budget is exhausted; the deterministic wording is used instead.
    if not AI_FALLBACK_ENABLED or not _budget_gate():
        return _reasoning_fallback(defect_fields)
    client = get_nvidia_client()
    prompt = f"""
    You are a senior maritime shipping auditor reviewing an automated comparison between a Shipping Instruction (SI) and a Bill of Lading (BL).
    
    Extracted SI Fields:
    {json.dumps(si_data, indent=2)}
    
    Extracted BL Fields:
    {json.dumps(bl_data, indent=2)}
    
    Deterministic Rule Check:
    - Mismatched fields: {defect_fields}
    - Is missing/blank required value: {is_missing_value}
    - Field notes: {json.dumps(field_comparisons, indent=2)}
    
    YOUR TASK:
    Think through whether variations are simply writing styles or genuine operational defects:
    1. 'To the Order of Company X' and 'Company X' represent the same consignee (legal negotiable bill style).
    2. 'POL Port Klang' and 'Port Klang' represent the same port of loading.
    3. '5' and '5 x 20\\'FCL' represent the same container count (5 units).
    4. '113,970 KG' and '113,970' represent the same gross weight.
    
    Generate:
    1. "thoughts": 2-3 sentences explaining your chain-of-thought analysis, specifically stating how writing styles or abbreviations were evaluated.
    2. "summary_reason": A concise 1-sentence final verdict explaining whether all fields match or explaining the exact cause of any defect.
    
    Output strictly as JSON:
    {{
      "thoughts": "...",
      "summary_reason": "..."
    }}
    """
    try:
        res = _chat_with_retry(
            client,
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=3000
        )
        content = (res.choices[0].message.content or "").strip()
        if content.startswith("```json"):
            content = content[7:]
        if content.startswith("```"):
            content = content[3:]
        if content.endswith("```"):
            content = content[:-3]
            
        import re
        try:
            return json.loads(content.strip(), strict=False)
        except Exception:
            cleaned = re.sub(r'\\(?![/"\\bfnrtu])', r'\\\\', content.strip())
            return json.loads(cleaned, strict=False)
    except Exception as e:
        print(f"Reasoning error: {e}")
        return _reasoning_fallback(defect_fields)
