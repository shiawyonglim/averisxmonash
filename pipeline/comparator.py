import re

FIELDS_TO_COMPARE = [
    "shipper",
    "consignee",
    "notify_party",
    "port_of_loading",
    "port_of_discharge",
    "container_count",
    "gross_weight_kg"
]

BLANK_PATTERNS = ["???", "_______", "tba", "tbc", "n/a", "____mt", "none", "unknown", ""]

def is_blank_or_missing(val):
    if not val:
        return True
    s = str(val).strip().lower()
    if s in BLANK_PATTERNS or s.startswith("___") or "??" in s:
        return True
    return False

def clean_company_name(text):
    text = str(text).lower()
    # Strip common prefixes like 'to the order of', 'consignee:', etc.
    text = re.sub(r'^(to the order of|consignee \(non-negotiable\):|consignee:|notify party:|shipper:|shipper/exporter:?)\s*', '', text)
    # Strip address after semicolon if present
    text = text.split(';')[0]
    # Strip common trailing address signposts
    text = re.sub(r'\b(p\.?o\.?\s*box|suite|level|floor|#\d+|road|street|avenue|bldg|building)\b.*$', '', text)
    # Remove punctuation
    text = re.sub(r'[^\w\s]', ' ', text)
    return " ".join(text.split())

def clean_port(text):
    text = str(text).lower()
    # Strip 'pol', 'pod', 'port of loading', etc.
    text = re.sub(r'^(pol|pod|port of loading:|port of discharge:|load port:|discharge port:)\s*', '', text)
    # Strip UN/LOCODE in parentheses like (mypkg), (usnyc), (vnsgn)
    text = re.sub(r'\([a-z0-9\s/]+\)', '', text)
    text = re.sub(r'[^\w\s]', ' ', text)
    return " ".join(text.split())

def clean_container_count(text):
    # Extract the main integer number of containers
    match = re.search(r'(\d+)', str(text))
    if match:
        return match.group(1)
    return str(text).strip()

def clean_gross_weight(text):
    # Order matters: replace 'kgs' before 'kg', 'mts' before 'mt'
    s = str(text).lower().replace('kgs', '').replace('kg', '').replace('mts', '').replace('mt', '')
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
            is_missing_value = True
            comparisons[field] = {
                "match": False,
                "reason": f"Required field is blank or missing (SI: '{si_raw}', BL: '{bl_raw}')"
            }
            continue

        is_match = (norm_si == norm_bl)
        # For company names, allow prefix matching if address was included in one
        if not is_match and field in ["shipper", "consignee", "notify_party"]:
            shorter = norm_si if len(norm_si) <= len(norm_bl) else norm_bl
            longer = norm_bl if len(norm_si) <= len(norm_bl) else norm_si
            
            # Check for different legal branch entity (e.g. MIDDLE EAST, FZE, BRANCH, SUBSIDIARY)
            has_branch_diff = any(b in longer and b not in shorter for b in ["middle east", "fze", "branch", "subsidiary"])
            
            if not has_branch_diff and len(shorter) >= 6 and longer.startswith(shorter):
                is_match = True
            
        if not is_match:
            defect_fields.append(field)
            comparisons[field] = {
                "match": False,
                "reason": f"Mismatch detected: SI specifies '{si_raw}' whereas BL specifies '{bl_raw}'"
            }
        else:
            if si_raw != bl_raw:
                reason = f"Match confirmed (Writing style variation accepted: '{si_raw}' vs '{bl_raw}')"
            else:
                reason = "Exact match"
            comparisons[field] = {
                "match": True,
                "reason": reason
            }
            
    return defect_fields, is_missing_value, comparisons
