import os
from pipeline.parsers import extract_text

def is_si_attachment(path, text=""):
    """
    Decide whether an attachment is the Shipping Instruction (SI) by
    filename first ('..._SI.<ext>' / '..._BL.<ext>' naming convention),
    falling back to a 'SHIPPING INSTRUCTION' content marker.
    """
    base = os.path.basename(path).upper()
    if "_SI" in base:
        return True
    if "_BL" in base:
        return False
    return "SHIPPING INSTRUCTION" in (text or "").upper()


def check_attachments(email_data, bundle_dir):
    """
    Returns None if attachments are OK, or an edge case tuple:
    (status, review_reason) if it needs review.
    """
    atts = email_data.get("attachments", [])
    
    # Needs exactly 2 attachments for BL_COMPARISON
    if len(atts) < 2:
        return ("NEEDS_REVIEW", "missing_attachment")
    
    return None

def diagnose_attachment(full_path):
    """
    Returns (is_corrupt: bool, reason: str) for a single attachment.
    """
    if not os.path.exists(full_path):
        return True, "File does not exist"
    size = os.path.getsize(full_path)
    if size == 0:
        return True, "Zero-byte empty file"
    if size < 1000 and full_path.endswith('.pdf'):
        return True, f"Truncated / Corrupted PDF ({size} bytes, invalid EOF/Root)"
    text = extract_text(full_path)
    if not text or len(text.strip()) < 10:
        return True, "Unreadable document (0 or < 10 characters extracted)"
    return False, None

def check_unreadable(email_data, bundle_dir):
    """
    Check if any attachment is too small (e.g. 1KB) or cannot be read.
    """
    atts = email_data.get("attachments", [])
    for att in atts:
        full_path = os.path.join(bundle_dir, att)
        is_bad, reason = diagnose_attachment(full_path)
        if is_bad:
            return ("NEEDS_REVIEW", "unreadable")
            
    return None

def check_wrong_doc_type(si_text, bl_text):
    """
    Check if one of the texts strongly indicates it is NOT a Bill of Lading or SI.
    e.g. Commercial Invoice, Packing List.
    """
    text = (si_text + " " + bl_text).upper()
    
    if "COMMERCIAL INVOICE - NOT A SHIPPING INSTRUCTION" in text:
        return ("NEEDS_REVIEW", "wrong_doc_type")
        
    if "COMMERCIAL INVOICE" in text and "BILL OF LADING" not in text and "SHIPPING INSTRUCTION" not in text:
         return ("NEEDS_REVIEW", "wrong_doc_type")
         
    if "PACKING LIST" in text and "BILL OF LADING" not in text and "SHIPPING INSTRUCTION" not in text:
         return ("NEEDS_REVIEW", "wrong_doc_type")
         
    return None
