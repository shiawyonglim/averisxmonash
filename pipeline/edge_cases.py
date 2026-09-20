import os
from pipeline.parsers import extract_text, is_image_file, validate_image

def is_si_attachment(path, text=""):
    """
    Decide whether an attachment is the Shipping Instruction (SI) by
    filename first ('..._SI.<ext>' / '..._BL.<ext>' naming convention),
    falling back to a 'SHIPPING INSTRUCTION' content marker. For image
    attachments the caller may pass the vision-reported document_type
    (e.g. 'SHIPPING_INSTRUCTION') in place of extracted text.
    """
    base = os.path.basename(path).upper()
    if "_SI" in base:
        return True
    if "_BL" in base:
        return False
    return "SHIPPING INSTRUCTION" in (text or "").upper().replace("_", " ")


def check_attachments(email_data, bundle_dir, intent=None):
    """
    Returns None if attachments are OK, or an edge case tuple:
    (status, review_reason) if it needs review.

    A comparison request carrying only the SI (1 attachment) is always
    missing the draft BL. With 0 attachments we escalate only when the
    sender's intent is a document comparison (COMPARE_NOW); "please send
    the draft BL" requests carry nothing to compare and are not defects.
    """
    atts = email_data.get("attachments", [])

    if len(atts) == 1:
        return ("NEEDS_REVIEW", "missing_attachment")
    if len(atts) == 0 and intent == "COMPARE_NOW":
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
    # Photos/scans of paper documents have no text layer — validate that
    # the image decodes; legibility is judged by the vision model later.
    if is_image_file(full_path):
        ok, reason = validate_image(full_path)
        if not ok:
            return True, reason
        return False, None
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

def _is_non_shipping_doc(text):
    if not text:
        return False
    t = text.strip().upper()
    first_lines = "\n".join(t.split("\n")[:5])
    invalid_headers = [
        "COMMERCIAL INVOICE", "PACKING LIST", "CERTIFICATE OF ORIGIN",
        "INSURANCE CERTIFICATE", "NOT A SHIPPING INSTRUCTION"
    ]
    for bad in invalid_headers:
        if bad in first_lines:
            return True
    if "COMMERCIAL INVOICE - NOT A SHIPPING INSTRUCTION" in t:
        return True
    return False

def check_wrong_doc_type(si_text, bl_text):
    """
    Check if either document strongly indicates it is NOT a Bill of Lading or SI.
    e.g. Commercial Invoice, Packing List, Certificate of Origin.
    """
    if _is_non_shipping_doc(si_text) or _is_non_shipping_doc(bl_text):
        return ("NEEDS_REVIEW", "wrong_doc_type")
    return None
