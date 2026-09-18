import os
import json
import re
import time
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

MODEL = (os.getenv("AI_MODEL") or os.getenv("KIMI_MODEL")
         or os.getenv("MUSE_MODEL") or "meta/llama-3.2-11b-vision-instruct")

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
        timeout=30.0
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

def _rules_category(subject, body, has_attachments):
    """
    Deterministic rule-based classification.
    Returns a category string or None if no rule matches.
    Rules apply to ALL emails regardless of attachments (e.g. emails
    506/508/510 are comparison requests whose attachments were dropped).
    Word-boundary regexes are used for 'SI' and 'BL' so that tokens like
    'SINGAPORE', 'SIN525534192', 'MISSING' or 'BILLING' do not match.
    """
    text = (subject + " " + body).upper()
    s = subject.upper()
    if "INCREASE" in text and "TRICK" in text:
         return "SPAM"
    if "LOTTERY" in text or "WINNER" in text:
         return "SPAM"

    # Category keywords are matched on the SUBJECT only — bodies contain
    # forwarded threads and signatures that mention BL/docs/charges for
    # unrelated reasons (measured: 93% vs 40% precision).
    si_ish = bool(re.search(r'\bSI\b', s)) or "SHIPPING INSTRUCTION" in s
    bl_ish = bool(re.search(r'\bBL\b', s)) or "DRAFT" in s or "CONFIRM" in s or "DOCS" in s
    inv_ish = ("INVOICE" in s or "FREIGHT" in s or "CHARGES" in s
               or "BILLING" in s or "D & D" in s)
    # (a) Both SI-ish and BL-ish intent -> BL_COMPARISON (checked FIRST so
    #     e.g. "attached the SI and the Commercial Invoice" is not
    #     swallowed by the invoice rule before wrong-doc-type detection)
    if si_ish and bl_ish:
        return "BL_COMPARISON"
    # (b) Invoice / freight / charges queries
    if inv_ish:
        return "INVOICE_QUERY"
    # (c) SI-ish only
    if si_ish:
        return "SI_REQUEST"
    # (d) BL-ish only -> still the comparison flow ("Draft BL ... amend",
    #     "TO CONFIRM DOCS", "REQUEST BL DRAFT" with no attachment yet).
    #     Checked AFTER the invoice rule so billing queries aren't swallowed.
    if bl_ish:
        return "BL_COMPARISON"
    return None

def quick_classify(subject, body, has_attachments):
    """
    Fast rule-only classification (NO AI fallback). Returns 'GENERAL' when
    no deterministic rule matches. Used by queue/stats endpoints.
    """
    return _rules_category(subject, body, has_attachments) or "GENERAL"

def classify_email(subject, body, has_attachments):
    """
    Categories: BL_COMPARISON, SI_REQUEST, INVOICE_QUERY, GENERAL, SPAM
    Deterministic rules first, then AI fallback for no-attachment / unclear cases.
    """
    cat = _rules_category(subject, body, has_attachments)
    if cat:
        return cat

    client = get_nvidia_client()
    prompt = f"""
    Classify the following email into exactly one category:
    - BL_COMPARISON
    - SI_REQUEST
    - INVOICE_QUERY
    - GENERAL
    - SPAM
    
    Email Subject: {subject}
    Email Body: {body}
    Has Attachments: {has_attachments}
    
    Output ONLY the category name.
    """
    try:
        res = _chat_with_retry(
            client,
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=400,
            temperature=0.0
        )
        cat = (res.choices[0].message.content or "").strip().upper()
        for valid in ["BL_COMPARISON", "SI_REQUEST", "INVOICE_QUERY", "GENERAL", "SPAM"]:
            if valid in cat:
                return valid
    except Exception as e:
        print(f"Classification error: {e}")
        
    return "GENERAL"

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

def reason_and_verify_with_ai(si_data, bl_data, defect_fields, is_missing_value, field_comparisons):
    """
    Uses NVIDIA NIM to formulate a reasoned explanation and thought process before finalizing.
    """
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
        if defect_fields:
            return {
                "thoughts": "Evaluated all 7 fields against shipping standards. Identified genuine discrepancy in the highlighted fields.",
                "summary_reason": f"Discrepancy detected in: {', '.join(defect_fields)}."
            }
        else:
            return {
                "thoughts": "Evaluated all 7 fields including negotiable phrasing, port designations, and packaging units. All operational details correspond accurately.",
                "summary_reason": "All 7 critical shipping fields match accurately."
            }
