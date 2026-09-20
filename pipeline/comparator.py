import os
import re

# The SI is the reference document: by default any weight difference is a
# real discrepancy. Set WEIGHT_TOLERANCE_PCT > 0 to allow tare/rounding
# variance (e.g. "1" for 1%).
WEIGHT_TOLERANCE_PCT = float(os.getenv("WEIGHT_TOLERANCE_PCT", "0"))

# UN/LOCODE -> canonical port name. A raw value containing a code token
# resolves to the canonical port name before comparison.
PORT_LOCODES = {
    "MYPKG": "port klang", "MYTPP": "tanjung pelepas", "MYPEN": "penang",
    "MYBTU": "bintulu", "MYLBU": "labuan", "SGSIN": "singapore",
    "IDBLW": "belawan", "IDJKT": "jakarta", "IDSRG": "semarang",
    "IDPNK": "pontianak", "VNSGN": "ho chi minh", "VNHPH": "hai phong",
    "THLCH": "laem chabang", "THBKK": "bangkok", "CNSHA": "shanghai",
    "CNNGB": "ningbo", "CNTAO": "qingdao", "HKHKG": "hong kong",
    "KRPUS": "busan", "KRPTK": "pyeongtaek", "JPNGO": "nagoya",
    "JPTYO": "tokyo", "JPYOK": "yokohama", "TWKHH": "kaohsiung",
    "INNSA": "nhava sheva", "INMAA": "chennai", "LKCMB": "colombo",
    "AEJEA": "jebel ali", "AEDXB": "dubai", "SAJED": "jeddah",
    "TRMER": "mersin", "EGALY": "alexandria", "JOAQJ": "aqaba",
    "GNCKY": "conakry", "ZADUR": "durban", "NLRTM": "rotterdam",
    "DEHAM": "hamburg", "BEANR": "antwerp", "GBFXT": "felixstowe",
    "ESBCN": "barcelona", "ESVLC": "valencia", "ITGOA": "genoa",
    "SIKOP": "koper", "USNYC": "new york", "USLAX": "los angeles",
    "USLGB": "long beach", "USSAV": "savannah", "USHOU": "houston",
    "CAVAN": "vancouver", "BRSSZ": "santos", "PECLL": "callao",
    "CLVAP": "valparaiso", "AUSYD": "sydney", "AUMEL": "melbourne",
    "NZAKL": "auckland", "CNNTG": "nantong",
}

FIELDS_TO_COMPARE = [
    "shipper",
    "consignee",
    "notify_party",
    "port_of_loading",
    "port_of_discharge",
    "container_count",
    "gross_weight_kg"
]

BLANK_PATTERNS = ["???", "_______", "tba", "tbc", "tbd", "n/a", "____mt", "none", "unknown", ""]

def is_blank_or_missing(val):
    if not val:
        return True
    s = str(val).strip().lower()
    if s in BLANK_PATTERNS or s.startswith("___") or "??" in s:
        return True
    if any(k in s for k in ["tbd", "pending", "not determined", "weight not determined", "to be advised", "unassigned"]):
        return True
    return False

def clean_company_name(text):
    text = str(text).lower()
    # Strip common prefixes like 'to the order of', 'consignee:', etc.
    text = re.sub(r'^(to the order of|consignee \(non-negotiable\):|consignee:|notify party:|shipper:|shipper/exporter:?)\s*', '', text)
    # Strip address after semicolon if present
    text = text.split(';')[0]
    # Normalize common corporate entity legal suffixes
    text = re.sub(r'\bcompany\b', 'co', text)
    text = re.sub(r'\blimited\b', 'ltd', text)
    text = re.sub(r'\bcorporation\b', 'corp', text)
    text = re.sub(r'\bincorporated\b', 'inc', text)
    # Strip common trailing address signposts
    text = re.sub(r'\b(p\.?o\.?\s*box|suite|level|floor|#\d+|road|street|avenue|bldg|building)\b.*$', '', text)
    # Remove punctuation
    text = re.sub(r'[^\w\s]', ' ', text)
    # Merge runs of single-letter tokens left by punctuation removal
    # ("S.L." -> "s l" -> "sl"), the same entity written with/without dots.
    text = re.sub(r'\b\w(?:\s+\w\b)+', lambda m: m.group(0).replace(' ', ''), text)
    return " ".join(text.split())

def split_port(value):
    """
    Split a raw port value into (name, code):
      name -> cleaned port-name part ('' when only a code was given)
      code -> the first known UN/LOCODE found, or None
    The printed name is authoritative: a code never overrides a present name —
    when the two disagree, that disagreement IS the defect.
    """
    raw = str(value).lower()
    code = None
    for c in PORT_LOCODES:
        if re.search(rf'\b{c}\b', raw, re.IGNORECASE):
            code = c
            break
    # Strip 'pol', 'pod', 'port of loading', etc.
    text = re.sub(r'^(pol|pod|port of loading:|port of discharge:|load port:|discharge port:)\s*', '', raw)
    # Strip UN/LOCODE in parentheses like (mypkg), (usnyc), (vnsgn)
    text = re.sub(r'\([a-z0-9\s/]+\)', '', text)
    # Drop bare code tokens from the name part
    if code:
        text = re.sub(rf'\b{code}\b', ' ', text, flags=re.IGNORECASE)
    text = re.sub(r'[^\w\s]', ' ', text)
    return " ".join(text.split()), code

def clean_port(text):
    # Only a bare code (no name present) resolves to its canonical name.
    name, code = split_port(text)
    if name:
        return name
    if code:
        return PORT_LOCODES[code]
    return name

def clean_container_count(text):
    # Extract the main integer number of containers
    match = re.search(r'(\d+)', str(text))
    if match:
        return match.group(1)
    return str(text).strip()

def clean_gross_weight(text):
    s = str(text).lower().strip()
    # Check for Metric Tonnes (MT / MTS)
    if re.search(r'\b(?:mt|mts|metric\s*tonn?es?)\b', s):
        num_m = re.search(r'(\d+(?:[.,]\d+)?)', s)
        if num_m:
            try:
                val_str = num_m.group(1).replace(',', '.')
                kg_val = float(val_str) * 1000.0
                return str(int(round(kg_val)))
            except ValueError:
                pass

    # Standard KG — strip unit tokens longest-first so removing 'kg' or 'mt'
    # can't leave a stray 's' behind from 'kgs'/'mts'.
    for tok in ("kgs", "mts", "kg", "mt"):
        s = s.replace(tok, '')
    cleaned = re.sub(r'[,.\s]', '', s)
    match = re.search(r'(\d+)', cleaned)
    if match:
        return match.group(1)
    return cleaned.strip()

def normalize_field(field, val):
    if is_blank_or_missing(val):
        return "MISSING_VALUE"
        
    if field in ["shipper", "consignee", "notify_party"]:
        return clean_company_name(val)
    elif field in ["port_of_loading", "port_of_discharge"]:
        return clean_port(val)
    elif field == "container_count":
        return clean_container_count(val)
    elif field == "gross_weight_kg":
        return clean_gross_weight(val)
    else:
        text = str(val).lower()
        text = re.sub(r'[^\w\s]', ' ', text)
        return " ".join(text.split())

def _is_subsequence(shorter_toks, longer_toks):
    """True if shorter_toks appears in longer_toks as an ordered subsequence."""
    if not shorter_toks or len(shorter_toks) > len(longer_toks):
        return False
    it = iter(longer_toks)
    return all(t in it for t in shorter_toks)

def compare_fields(si_data, bl_data):
    """
    Compares the 7 core fields between SI and BL with smart normalization.
    Returns:
    - defect_fields: list of field names that mismatched
    - is_missing_value: bool, True if any field was extracted as 'MISSING_VALUE'
    - comparisons: dict with detailed per-field match status and values
    """
    defect_fields = []
    is_missing_value = False
    comparisons = {}
    
    for field in FIELDS_TO_COMPARE:
        si_raw = str(si_data.get(field, "")).strip()
        bl_raw = str(bl_data.get(field, "")).strip()
        
        norm_si = normalize_field(field, si_raw)
        norm_bl = normalize_field(field, bl_raw)
        
        if norm_si == "MISSING_VALUE" or norm_bl == "MISSING_VALUE":
            if norm_si == "MISSING_VALUE" and norm_bl == "MISSING_VALUE":
                # Blank on BOTH sides = our extraction could not read the
                # field in either document — not a customer blank, and it
                # must not mask a real defect nor produce a match.
                comparisons[field] = {
                    "match": False,
                    "blank": "both",
                    "reason": f"Field could not be extracted from either document (SI: '{si_raw}', BL: '{bl_raw}')"
                }
            else:
                # Blank on exactly one side = the customer left it blank.
                is_missing_value = True
                side = "si_only" if norm_si == "MISSING_VALUE" else "bl_only"
                comparisons[field] = {
                    "match": False,
                    "blank": side,
                    "reason": f"Required field is blank or missing on the {'SI' if side == 'si_only' else 'BL'} side (SI: '{si_raw}', BL: '{bl_raw}')"
                }
            continue

        is_match = (norm_si == norm_bl)
        prefix_match = False
        confidence = "HIGH"
        reason = None

        # Optional weight tolerance (disabled by default): the SI is the
        # reference document, so a difference is a real discrepancy unless
        # WEIGHT_TOLERANCE_PCT is explicitly configured.
        if not is_match and field == "gross_weight_kg" and WEIGHT_TOLERANCE_PCT > 0:
            try:
                w1 = float(norm_si)
                w2 = float(norm_bl)
                if max(w1, w2) > 0 and abs(w1 - w2) / max(w1, w2) <= WEIGHT_TOLERANCE_PCT / 100.0:
                    is_match = True
            except (ValueError, TypeError):
                pass

        # Ports are compared by NAME only (equal, token-subsequence, or
        # equal after removing whitespace e.g. "HOCHIMINH CITY" vs
        # "HO CHI MINH CITY"). The UN/LOCODE is evidence, not a match key.
        if field in ["port_of_loading", "port_of_discharge"]:
            si_name, si_code = split_port(si_raw)
            bl_name, bl_code = split_port(bl_raw)
            if not is_match:
                si_toks, bl_toks = si_name.split(), bl_name.split()
                is_match = (
                    _is_subsequence(si_toks, bl_toks)
                    or _is_subsequence(bl_toks, si_toks)
                    or (si_name and si_name.replace(" ", "") == bl_name.replace(" ", ""))
                )
            if not is_match and si_code and si_code == bl_code:
                reason = (f"Port name mismatch (SI '{si_raw}' vs BL '{bl_raw}') while both "
                          f"documents cite UN/LOCODE {si_code} — the code and the name disagree.")
            elif is_match and si_code and bl_code and si_code != bl_code:
                confidence = "LOW"
                reason = (f"Port names match but the cited UN/LOCODEs differ "
                          f"(SI {si_code} vs BL {bl_code}) — a human should confirm.")

        # For company names, allow prefix matching if address was included in one
        if not is_match and field in ["shipper", "consignee", "notify_party"]:
            shorter = norm_si if len(norm_si) <= len(norm_bl) else norm_bl
            longer = norm_bl if len(norm_si) <= len(norm_bl) else norm_si

            # Check for different legal branch entity (e.g. MIDDLE EAST, FZE, BRANCH, SUBSIDIARY)
            has_branch_diff = any(b in longer and b not in shorter for b in ["middle east", "fze", "branch", "subsidiary"])

            if not has_branch_diff and len(shorter) >= 6 and longer.startswith(shorter):
                is_match = True
                prefix_match = True

        if not is_match:
            defect_fields.append(field)
            comparisons[field] = {
                "match": False,
                "reason": reason or f"Mismatch detected: SI specifies '{si_raw}' whereas BL specifies '{bl_raw}'"
            }
        else:
            if prefix_match:
                comparisons[field] = {
                    "match": True,
                    "confidence": "LOW",
                    "reason": (f"Prefix match: '{shorter}' is a prefix of '{longer}' "
                               f"('{si_raw}' vs '{bl_raw}') — a human should confirm "
                               f"these are the same legal entity.")
                }
            elif reason:
                comparisons[field] = {
                    "match": True,
                    "confidence": confidence,
                    "reason": reason
                }
            elif si_raw != bl_raw:
                comparisons[field] = {
                    "match": True,
                    "confidence": "HIGH",
                    "reason": f"Match confirmed (Writing style variation accepted: '{si_raw}' vs '{bl_raw}')"
                }
            else:
                comparisons[field] = {
                    "match": True,
                    "confidence": "HIGH",
                    "reason": "Exact match"
                }
            
    return defect_fields, is_missing_value, comparisons
