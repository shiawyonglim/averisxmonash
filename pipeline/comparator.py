import os
import re

# The SI is the reference document: by default any weight difference is a
# real discrepancy. Set WEIGHT_TOLERANCE_PCT > 0 to allow tare/rounding
# variance (e.g. "1" for 1%).
WEIGHT_TOLERANCE_PCT = float(os.getenv("WEIGHT_TOLERANCE_PCT", "0"))

# UN/LOCODE -> canonical port name. A raw value containing a code token
# resolves to the canonical port name before comparison.
# UN/LOCODE -> canonical port name. A raw value containing a code token
# resolves to the canonical port name before comparison.
PORT_LOCODES = {
    "MYPKG": "port klang", "PKG": "port klang", "MYTPP": "tanjung pelepas", "TPP": "tanjung pelepas",
    "MYPEN": "penang", "MYBTU": "bintulu", "MYLBU": "labuan", "SGSIN": "singapore", "SIN": "singapore",
    "IDBLW": "belawan", "IDJKT": "jakarta", "IDSRG": "semarang",
    "IDPNK": "pontianak", "VNSGN": "ho chi minh", "VNHPH": "hai phong",
    "THLCH": "laem chabang", "THBKK": "bangkok", "CNSHA": "shanghai", "SHA": "shanghai",
    "CNNGB": "ningbo", "CNTAO": "qingdao", "HKHKG": "hong kong", "HKG": "hong kong",
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

FIELD_STANDARDS = {
    "shipper": "DCSA eBL v3.0 / ICC UCP 600 Art. 20 (Legal Shipper Entity)",
    "consignee": "DCSA eBL v3.0 / ICC UCP 600 Art. 20 (Consignee Title & Negotiability)",
    "notify_party": "DCSA eBL v3.0 (Arrival Notice Party / Same as Consignee)",
    "port_of_loading": "UNECE Rec. 16 (UN/LOCODE Standard Port Nomenclature)",
    "port_of_discharge": "UNECE Rec. 16 (UN/LOCODE Standard Port Nomenclature)",
    "container_count": "ISO 6346 Container Equipment Quantity & Sizing Specification",
    "gross_weight_kg": "IMO SOLAS Chapter VI Reg. 2 (Verified Gross Mass / VGM Mandate)"
}

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
    for c in sorted(PORT_LOCODES, key=len, reverse=True):
        if re.search(rf'\b{c}\b', raw, re.IGNORECASE):
            code = c
            break
    # Strip 'pol', 'pod', 'port of loading', etc.
    text = re.sub(r'^(pol|pod|port of loading:|port of discharge:|load port:|discharge port:)\s*', '', raw)
    # Strip UN/LOCODE in parentheses like (mypkg), (usnyc), (vnsgn)
    text = re.sub(r'\([a-z0-9\s/]+\)', '', text)
    # Strip common country names when preceded by comma (excluding Singapore which is also the city/port)
    countries = [
        "malaysia", "netherlands", "indonesia", "vietnam", "china",
        "spain", "uae", "united arab emirates", "germany", "usa", "south korea",
        "korea", "japan", "brazil", "india", "peru", "taiwan", "thailand",
        "sri lanka", "egypt", "jordan", "guinea", "south africa", "belgium",
        "united kingdom", "uk", "italy", "slovenia", "canada", "chile",
        "australia", "new zealand", "myanmar"
    ]
    cty_rx = r',\s*(?:' + '|'.join(countries) + r')\b'
    text = re.sub(cty_rx, ' ', text, flags=re.IGNORECASE)

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
    s = str(text).lower().strip()
    if is_blank_or_missing(s):
        return "MISSING_VALUE"

    # Number words
    word_map = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
                "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10"}
    for w, num in word_map.items():
        if re.search(rf'\b{w}\b', s):
            return num

    # Pattern A: multiplier followed by size: '2 x 40hc', '6 x 20fcl', '12 x 20'gp'
    m_mult = re.search(r'\b(\d+)\s*(?:x|\*)\s*(?:20|40|45)', s)
    if m_mult:
        return str(int(m_mult.group(1)))

    # Pattern B: size followed by multiplier: '40hc x 2', '20ft x 4'
    m_inv = re.search(r'(?:20|40|45)\s*(?:[\'"`]?\s*(?:ft|foot|hc|hq|gp|dc|fcl|lcl|open\s*top|ot|fr|flat\s*rack))?\s*(?:x|\*)\s*(\d+)', s)
    if m_inv:
        return str(int(m_inv.group(1)))

    # Pattern C: count followed by container/unit keywords: '2 containers', '4 units', '5 pkgs'
    m_cnt = re.search(r'\b(\d+)\s*(?:containers?|cntrs?|units?|pkgs?|packages?|boxes?|ctns?)\b', s)
    if m_cnt:
        return str(int(m_cnt.group(1)))

    # Pattern D: Total containers: 4
    m_tot = re.search(r'(?:total\s*(?:no\.?\s*of\s*)?containers?|qty)[:\s]*(\d+)', s)
    if m_tot:
        return str(int(m_tot.group(1)))

    # Fallback: find all numbers; if there is a 20/40/45 size and another number, pick the other!
    nums = re.findall(r'\b(\d+)\b', s)
    if nums:
        non_size_nums = [n for n in nums if n not in ("20", "40", "45")]
        if non_size_nums:
            return str(int(non_size_nums[0]))
        return str(int(nums[0]))
    return s

def clean_gross_weight(text):
    s = str(text).lower().strip()
    if is_blank_or_missing(s):
        return "MISSING_VALUE"

    # Reject physically invalid negative values
    if re.search(r'-\s*\d', s):
        return "INVALID_NEGATIVE_WEIGHT"

    # Check for Imperial Pounds (LBS)
    if re.search(r'\b(?:lbs?|pounds?)\b', s):
        m = re.search(r'(\d[\d,.\s]*\d|\d+)', s)
        if m:
            clean_num = m.group(1).replace(' ', '').replace(',', '')
            try:
                kg_val = float(clean_num) * 0.45359237
                return str(int(round(kg_val)))
            except ValueError:
                pass

    # Check for Metric Tonnes (MT / MTS)
    if re.search(r'\b(?:mt|mts|metric\s*tonn?es?)\b', s):
        m = re.search(r'(\d+(?:[.,]\d+)?)', s)
        if m:
            val_str = m.group(1).replace(',', '.')
            try:
                kg_val = float(val_str) * 1000.0
                return str(int(round(kg_val)))
            except ValueError:
                pass

    # Standard KG / Numeric parsing with safe decimal handling
    # Strip unit words first
    for tok in ("kgs", "mts", "kg", "mt", "kilos", "kilograms"):
        s = re.sub(rf'\b{tok}\b', '', s)

    # Extract number sequence
    m = re.search(r'(\d[\d,.\s]*\d|\d+)', s)
    if not m:
        return s.strip()

    raw_num = m.group(1).replace(' ', '')
    # Handle thousands and decimals
    if ',' in raw_num and '.' in raw_num:
        if raw_num.rfind('.') > raw_num.rfind(','):
            raw_num = raw_num.replace(',', '')
        else:
            raw_num = raw_num.replace('.', '').replace(',', '.')
    elif ',' in raw_num:
        parts = raw_num.split(',')
        if len(parts) == 2 and len(parts[1]) != 3:
            raw_num = raw_num.replace(',', '.')
        else:
            raw_num = raw_num.replace(',', '')
    elif '.' in raw_num:
        parts = raw_num.split('.')
        if len(parts) > 2:
            raw_num = raw_num.replace('.', '')

    try:
        val = float(raw_num)
        return str(int(round(val)))
    except ValueError:
        cleaned = re.sub(r'[^\d]', '', raw_num)
        return cleaned or s.strip()

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
    Compares the 7 core fields between SI and BL with smart normalization,
    transparent decision logging, and maritime standard citations.
    Returns:
    - defect_fields: list of field names that mismatched
    - is_missing_value: bool, True if any field was extracted as 'MISSING_VALUE'
    - comparisons: dict with detailed per-field match status, normalized values,
                   transformation steps, and governance standard citations.
    """
    defect_fields = []
    is_missing_value = False
    comparisons = {}
    
    for field in FIELDS_TO_COMPARE:
        si_raw = str(si_data.get(field, "")).strip()
        bl_raw = str(bl_data.get(field, "")).strip()
        
        norm_si = normalize_field(field, si_raw)
        norm_bl = normalize_field(field, bl_raw)
        std_citation = FIELD_STANDARDS.get(field, "Maritime Transport Standard")
        
        # Build transparency steps
        steps = [
            f"Raw SI: '{si_raw}' -> Normalized: '{norm_si}'",
            f"Raw BL: '{bl_raw}' -> Normalized: '{norm_bl}'",
            f"Governance: {std_citation}"
        ]
        
        if norm_si == "INVALID_NEGATIVE_WEIGHT" or norm_bl == "INVALID_NEGATIVE_WEIGHT":
            defect_fields.append(field)
            comparisons[field] = {
                "match": False,
                "confidence": "HIGH",
                "reason": "Physically invalid negative cargo weight detected",
                "raw_si": si_raw, "raw_bl": bl_raw,
                "norm_si": norm_si, "norm_bl": norm_bl,
                "standard_citation": std_citation,
                "transformation_steps": steps
            }
            continue

        if norm_si == "MISSING_VALUE" or norm_bl == "MISSING_VALUE":
            if norm_si == "MISSING_VALUE" and norm_bl == "MISSING_VALUE":
                comparisons[field] = {
                    "match": False,
                    "blank": "both",
                    "reason": f"Field could not be extracted from either document (SI: '{si_raw}', BL: '{bl_raw}')",
                    "raw_si": si_raw, "raw_bl": bl_raw,
                    "norm_si": norm_si, "norm_bl": norm_bl,
                    "standard_citation": std_citation,
                    "transformation_steps": steps
                }
            else:
                is_missing_value = True
                side = "si_only" if norm_si == "MISSING_VALUE" else "bl_only"
                comparisons[field] = {
                    "match": False,
                    "blank": side,
                    "reason": f"Required field is blank or missing on the {'SI' if side == 'si_only' else 'BL'} side (SI: '{si_raw}', BL: '{bl_raw}')",
                    "raw_si": si_raw, "raw_bl": bl_raw,
                    "norm_si": norm_si, "norm_bl": norm_bl,
                    "standard_citation": std_citation,
                    "transformation_steps": steps
                }
            continue

        is_match = (norm_si == norm_bl)
        prefix_match = False
        confidence = "HIGH"
        reason = None

        # Optional weight tolerance: the SI is the reference document
        if not is_match and field == "gross_weight_kg" and WEIGHT_TOLERANCE_PCT > 0:
            try:
                w1 = float(norm_si)
                w2 = float(norm_bl)
                if max(w1, w2) > 0 and abs(w1 - w2) / max(w1, w2) <= WEIGHT_TOLERANCE_PCT / 100.0:
                    is_match = True
                    steps.append(f"Accepted under {WEIGHT_TOLERANCE_PCT}% SOLAS VGM tolerance window")
            except (ValueError, TypeError):
                pass

        # Ports are compared by NAME only (equal, token-subsequence, or
        # equal after removing whitespace e.g. "HOCHIMINH CITY" vs
        # "HO CHI MINH CITY"). The UN/LOCODE is evidence, not a match key.
        if field in ["port_of_loading", "port_of_discharge"]:
            si_name, si_code = split_port(si_raw)
            bl_name, bl_code = split_port(bl_raw)
            si_cmp = si_name or (PORT_LOCODES.get(si_code, "") if si_code else "")
            bl_cmp = bl_name or (PORT_LOCODES.get(bl_code, "") if bl_code else "")
            if not is_match:
                si_toks, bl_toks = si_cmp.split(), bl_cmp.split()
                is_match = (
                    _is_subsequence(si_toks, bl_toks)
                    or _is_subsequence(bl_toks, si_toks)
                    or (si_cmp and si_cmp.replace(" ", "") == bl_cmp.replace(" ", ""))
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

            has_branch_diff = any(b in longer and b not in shorter for b in ["middle east", "fze", "branch", "subsidiary"])

            if not has_branch_diff and len(shorter) >= 6 and longer.startswith(shorter):
                is_match = True
                prefix_match = True

        comp_dict = {
            "match": is_match,
            "raw_si": si_raw,
            "raw_bl": bl_raw,
            "norm_si": norm_si,
            "norm_bl": norm_bl,
            "standard_citation": std_citation,
            "transformation_steps": steps
        }

        if not is_match:
            defect_fields.append(field)
            comp_dict["confidence"] = "HIGH"
            comp_dict["reason"] = reason or f"Mismatch detected: SI specifies '{si_raw}' whereas BL specifies '{bl_raw}'"
        else:
            if prefix_match:
                comp_dict["confidence"] = "LOW"
                comp_dict["reason"] = (f"Prefix match: '{shorter}' is a prefix of '{longer}' "
                                       f"('{si_raw}' vs '{bl_raw}') — human confirmation recommended.")
            elif reason:
                comp_dict["confidence"] = confidence
                comp_dict["reason"] = reason
            elif si_raw != bl_raw:
                comp_dict["confidence"] = "HIGH"
                comp_dict["reason"] = f"Match confirmed (Trade writing style variation accepted: '{si_raw}' vs '{bl_raw}')"
            else:
                comp_dict["confidence"] = "HIGH"
                comp_dict["reason"] = "Exact match"

        comparisons[field] = comp_dict
            
    return defect_fields, is_missing_value, comparisons
            
    return defect_fields, is_missing_value, comparisons
