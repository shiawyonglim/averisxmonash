import os
import json
import time
from tqdm import tqdm

from pipeline.parsers import extract_text
from pipeline.edge_cases import check_attachments, check_unreadable, check_wrong_doc_type, is_si_attachment
from pipeline.ai_engine import classify_email, extract_shipping_fields
from pipeline.comparator import compare_fields

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUNDLE_DIR = os.path.join(BASE_DIR, "sdoc-hackathon-bundle")
INBOX_DIR = os.path.join(BUNDLE_DIR, "inbox")
SUBMISSION_PATH = os.path.join(BASE_DIR, "submission.json")

def process_email(email_data):
    eid = email_data["email_id"]
    subj = email_data.get("subject", "")
    body = email_data.get("body", "")
    atts = email_data.get("attachments", [])
    has_atts = len(atts) > 0
    
    # 1. Classify Email
    category = classify_email(subj, body, has_atts)
    
    # Default non-BL_COMPARISON response
    if category != "BL_COMPARISON":
        return {
            "category": category,
            "status": "OK",
            "review_reason": None,
            "has_defect": False,
            "defect_fields": []
        }, {}, {}
        
    # 2. Check Edge Cases (Attachments)
    att_err = check_attachments(email_data, BUNDLE_DIR)
    if att_err:
        return {
            "category": category,
            "status": att_err[0],
            "review_reason": att_err[1],
            "has_defect": False,
            "defect_fields": []
        }, {}, {}
        
    read_err = check_unreadable(email_data, BUNDLE_DIR)
    if read_err:
        return {
            "category": category,
            "status": read_err[0],
            "review_reason": read_err[1],
            "has_defect": False,
            "defect_fields": []
        }, {}, {}
        
    # Read text
    # Assuming first is SI and second is BL (from standard ordering), 
    # but we should just pass both to checking logic
    path1 = os.path.join(BUNDLE_DIR, atts[0])
    path2 = os.path.join(BUNDLE_DIR, atts[1])
    
    text1 = extract_text(path1)
    text2 = extract_text(path2)
    
    # Check Wrong Doc Type
    wrong_doc_err = check_wrong_doc_type(text1, text2)
    if wrong_doc_err:
        return {
            "category": category,
            "status": wrong_doc_err[0],
            "review_reason": wrong_doc_err[1],
            "has_defect": False,
            "defect_fields": []
        }, {}, {}
        
    # 3. Determine which is SI and which is BL (filename-first detection)
    if is_si_attachment(path1, text1):
        si_text, bl_text = text1, text2
    else:
        si_text, bl_text = text2, text1
        
    # 4. Extract Fields via AI
    si_fields = extract_shipping_fields(si_text)
    bl_fields = extract_shipping_fields(bl_text)
    
    # Extraction failures return "ERROR" placeholders - never treat as a match
    extracted = list(si_fields.values()) + list(bl_fields.values())
    if any(str(v).strip().upper() == "ERROR" for v in extracted):
        return {
            "category": category,
            "status": "NEEDS_REVIEW",
            "review_reason": "unreadable",
            "has_defect": False,
            "defect_fields": []
        }, si_fields, bl_fields
    
    # 5. Compare Fields
    defect_fields, is_missing_val, field_comparisons = compare_fields(si_fields, bl_fields)
    
    if is_missing_val:
        return {
            "category": category,
            "status": "NEEDS_REVIEW",
            "review_reason": "missing_value",
            "has_defect": False,
            "defect_fields": []
        }, si_fields, bl_fields
        
    has_defect = len(defect_fields) > 0
    status = "MISMATCH" if has_defect else "OK"
    
    return {
        "category": category,
        "status": status,
        "review_reason": None,
        "has_defect": has_defect,
        "defect_fields": defect_fields
    }, si_fields, bl_fields


def main():
    email_files = sorted([f for f in os.listdir(INBOX_DIR) if f.endswith('.json')])
    submission = {}
    
    print(f"Processing {len(email_files)} emails...")
    
    for f in tqdm(email_files):
        path = os.path.join(INBOX_DIR, f)
        with open(path, 'r', encoding='utf-8') as file:
            email_data = json.load(file)
            
        eid = email_data["email_id"]
        result, si_fields, bl_fields = process_email(email_data)
        submission[eid] = result
        print(f"\n{eid} Result: {result['status']}")
        
    with open(SUBMISSION_PATH, "w") as f:
        json.dump(submission, f, indent=2)
        
    print("Saved submission.json")

if __name__ == "__main__":
    main()
