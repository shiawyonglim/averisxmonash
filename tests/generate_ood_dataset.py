"""Author hand-written out-of-distribution test cases.

Unlike tests/generate_synthetic_dataset.py (which emits the label shapes the
regex tier already knows), every attachment below uses LABELS OUR PIPELINE HAS
NEVER SEEN: 'Despatching Firm', 'Cnee', 'Loading Wharf', 'Qty of Cntrs',
'G.W.', 'Order Party', 'Notify To', bare UN/LOCODEs, CJK qualifiers, slash
qualifiers, multiline headers. The point is honest generalization, so
expectations were set by hand and failures are reported, not hidden.

Run:  python tests/generate_ood_dataset.py   (writes tests/ood_dataset/)
      AI_FALLBACK=0 python tests/eval_ood.py
"""

import json
import os

BASE = os.path.dirname(os.path.abspath(__file__))
OOD = os.path.join(BASE, "ood_dataset")
INBOX = os.path.join(OOD, "inbox")
ATTS = os.path.join(OOD, "attachments")

# Each case: email record, dict of {filename: text}, and hand-set expectations.
CASES = [
    # 1. Completely unfamiliar label set; identical values -> should be OK.
    {
        "email_id": "ood_001",
        "subject": "Draft BL check — booking TPEB-00912",
        "body": "Dear team,\n\nPlease compare our SI against the attached draft BL and confirm.\n\nRegards,\nExport Desk",
        "expect": {"category": "BL_COMPARISON", "status": "OK"},
        "files": {
            "ood_001_SI.txt": """SHIPPING INSTRUCTION
========================================
Despatching Firm: KORDIS PACKAGING GMBH
  INDUSTRIESTRASSE 12; 10115 BERLIN, GERMANY
Cnee: NATURA CARTONAGE SA
  14 RUE DE LA GARE; 75010 PARIS, FRANCE
Notify To: GALAXY FREIGHT SERVICES LTD
  8 CANNON STREET; LONDON, UK
Loading Wharf: ROTTERDAM, NETHERLANDS (NLRTM)
Unload Port: JEBEL ALI, UAE (AEJEA)
Qty of Cntrs: 4 x 40'HC
G.W.: 58,200 KG
Booking Ref: TPEB-00912
""",
            "ood_001_BL.txt": """DRAFT BILL OF LADING
========================================
Carrier: HAPAG-LLOYD
BL No.: HLCUTPE00912

Despatching Firm: KORDIS PACKAGING GMBH
  INDUSTRIESTRASSE 12; 10115 BERLIN, GERMANY
Cnee: NATURA CARTONAGE SA
  14 RUE DE LA GARE; 75010 PARIS, FRANCE
Notify To: GALAXY FREIGHT SERVICES LTD
  8 CANNON STREET; LONDON, UK
Loading Wharf: ROTTERDAM, NETHERLANDS (NLRTM)
Unload Port: JEBEL ALI, UAE (AEJEA)
Qty of Cntrs: 4 x 40'HC
G.W.: 58,200 KG
Booking Ref: TPEB-00912
""",
        },
    },
    # 2. Same unfamiliar labels, consignee differs -> MISMATCH on consignee.
    {
        "email_id": "ood_002",
        "subject": "Kindly verify draft BL vs SI — TPEB-00913",
        "body": "Please check the attached draft BL against our SI for booking TPEB-00913 and advise.",
        "expect": {"category": "BL_COMPARISON", "status": "MISMATCH", "defect_fields": ["consignee"]},
        "files": {
            "ood_002_SI.txt": """SHIPPING INSTRUCTION
========================================
Despatching Firm: KORDIS PACKAGING GMBH
Cnee: NATURA CARTONAGE SA
Notify To: GALAXY FREIGHT SERVICES LTD
Loading Wharf: ROTTERDAM, NETHERLANDS (NLRTM)
Unload Port: JEBEL ALI, UAE (AEJEA)
Qty of Cntrs: 4 x 40'HC
G.W.: 58,200 KG
Booking Ref: TPEB-00913
""",
            "ood_002_BL.txt": """DRAFT BILL OF LADING
========================================
Despatching Firm: KORDIS PACKAGING GMBH
Cnee: NATURA CARTONS LTD
Notify To: GALAXY FREIGHT SERVICES LTD
Loading Wharf: ROTTERDAM, NETHERLANDS (NLRTM)
Unload Port: JEBEL ALI, UAE (AEJEA)
Qty of Cntrs: 4 x 40'HC
G.W.: 58,200 KG
Booking Ref: TPEB-00913
""",
        },
    },
    # 3. Bare LOCODE on the BL side, port name on the SI side -> OK.
    {
        "email_id": "ood_003",
        "subject": "RE: SI/BL verification — TPEB-00915",
        "body": "Attached SI and draft BL for checking. Thanks.",
        "expect": {"category": "BL_COMPARISON", "status": "OK"},
        "files": {
            "ood_003_SI.txt": """SHIPPING INSTRUCTION
========================================
Despatching Firm: BALTIC BOARD OY
Cnee: LUMA PACKAGING LLC
Notify To: LUMA PACKAGING LLC
Loading Wharf: JEBEL ALI, UAE
Unload Port: HO CHI MINH, VIETNAM
Qty of Cntrs: 2 x 20'FCL
G.W.: 18,400 KG
""",
            "ood_003_BL.txt": """DRAFT BILL OF LADING
========================================
Despatching Firm: BALTIC BOARD OY
Cnee: LUMA PACKAGING LLC
Notify To: LUMA PACKAGING LLC
Loading Wharf: AEJEA
Unload Port: VNSGN
Qty of Cntrs: 2 x 20'FCL
G.W.: 18,400 KG
""",
        },
    },
    # 4. Port NAME differs, same LOCODE cited -> MISMATCH on the port.
    {
        "email_id": "ood_004",
        "subject": "Draft BL for cross-check — TPEB-00916",
        "body": "Please compare the SI and draft BL attached.",
        "expect": {"category": "BL_COMPARISON", "status": "MISMATCH", "defect_fields": ["port_of_discharge"]},
        "files": {
            "ood_004_SI.txt": """SHIPPING INSTRUCTION
========================================
Despatching Firm: BALTIC BOARD OY
Cnee: LUMA PACKAGING LLC
Notify To: LUMA PACKAGING LLC
Loading Wharf: JEBEL ALI, UAE (AEJEA)
Unload Port: HO CHI MINH, VIETNAM (VNSGN)
Qty of Cntrs: 2 x 20'FCL
G.W.: 18,400 KG
""",
            "ood_004_BL.txt": """DRAFT BILL OF LADING
========================================
Despatching Firm: BALTIC BOARD OY
Cnee: LUMA PACKAGING LLC
Notify To: LUMA PACKAGING LLC
Loading Wharf: JEBEL ALI, UAE (AEJEA)
Unload Port: BUSAN, SOUTH KOREA (VNSGN)
Qty of Cntrs: 2 x 20'FCL
G.W.: 18,400 KG
""",
        },
    },
    # 5. Weight expressed as MT on the BL, KG on the SI -> OK.
    {
        "email_id": "ood_005",
        "subject": "Verify draft BL against SI — TPEB-00917",
        "body": "Please check the two attached documents and confirm they agree.",
        "expect": {"category": "BL_COMPARISON", "status": "OK"},
        "files": {
            "ood_005_SI.txt": """SHIPPING INSTRUCTION
========================================
Despatching Firm: AURUM FIBRE AG
Cnee: POLESTAR PACKAGING PTY LTD
Notify To: POLESTAR PACKAGING PTY LTD
Loading Wharf: HAMBURG, GERMANY (DEHAM)
Unload Port: SINGAPORE (SGSIN)
Qty of Cntrs: 6 x 40'HC
G.W.: 122,540 KG
""",
            "ood_005_BL.txt": """DRAFT BILL OF LADING
========================================
Despatching Firm: AURUM FIBRE AG
Cnee: POLESTAR PACKAGING PTY LTD
Notify To: POLESTAR PACKAGING PTY LTD
Loading Wharf: HAMBURG, GERMANY (DEHAM)
Unload Port: SINGAPORE (SGSIN)
Qty of Cntrs: 6 x 40'HC
G.W.: 122.54 MT
""",
        },
    },
    # 6. Second attachment is actually a packing list -> wrong_doc_type.
    {
        "email_id": "ood_006",
        "subject": "SI + BL for checking — TPEB-00918",
        "body": "Attached SI and draft BL for verification.",
        "expect": {"category": "BL_COMPARISON", "status": "NEEDS_REVIEW", "review_reason": "wrong_doc_type"},
        "files": {
            "ood_006_SI.txt": """SHIPPING INSTRUCTION
========================================
Despatching Firm: AURUM FIBRE AG
Cnee: POLESTAR PACKAGING PTY LTD
Notify To: POLESTAR PACKAGING PTY LTD
Loading Wharf: HAMBURG, GERMANY (DEHAM)
Unload Port: SINGAPORE (SGSIN)
Qty of Cntrs: 6 x 40'HC
G.W.: 122,540 KG
""",
            "ood_006_BL.txt": """PACKING LIST
========================================
Shipment: TPEB-00918
Pallets: 12
Net Weight: 118,000 KG
Gross Weight: 122,540 KG
Marks: AURUM / POLESTAR
""",
        },
    },
    # 7. Misleading subject ("INVOICE") but the body is a comparison request.
    {
        "email_id": "ood_007",
        "subject": "INVOICE enclosed — please action urgently",
        "body": "Dear team,\n\nPlease compare the attached SI and draft BL for booking TPEB-00919 and confirm. (The subject line refers to a separate invoice thread.)\n\nRegards,\nDocs Desk",
        "expect": {"category": "BL_COMPARISON", "status": "OK"},
        "files": {
            "ood_007_SI.txt": """SHIPPING INSTRUCTION
========================================
Despatching Firm: NORDPULP GMBH
Cnee: HORIZON CARTON INC
Notify To: HORIZON CARTON INC
Loading Wharf: ANTWERP, BELGIUM (BEANR)
Unload Port: NEW YORK, US (USNYC)
Qty of Cntrs: 3 x 40'HC
G.W.: 61,300 KG
""",
            "ood_007_BL.txt": """DRAFT BILL OF LADING
========================================
Despatching Firm: NORDPULP GMBH
Cnee: HORIZON CARTON INC
Notify To: HORIZON CARTON INC
Loading Wharf: ANTWERP, BELGIUM (BEANR)
Unload Port: NEW YORK, US (USNYC)
Qty of Cntrs: 3 x 40'HC
G.W.: 61,300 KG
""",
        },
    },
    # 8. Comparison requested in the body, no attachments -> missing_attachment.
    {
        "email_id": "ood_008",
        "subject": "Please verify SI vs draft BL — TPEB-00920",
        "body": "Dear team,\n\nPlease compare the attached Shipping Instruction and draft Bill of Lading for TPEB-00920 and advise on any differences.\n\nRegards,\nOps",
        "expect": {"category": "BL_COMPARISON", "status": "NEEDS_REVIEW", "review_reason": "missing_attachment"},
        "files": {},
    },
    # 9. "Please send the draft BL" — a request, NOT a comparison -> classify-only OK.
    {
        "email_id": "ood_009",
        "subject": "Draft BL needed — TPEB-00921",
        "body": "Dear Hari,\n\nPlease assist to send the draft BL for booking TPEB-00921 for checking asap.\n\nBest,\nDocs",
        "expect": {"category": "BL_COMPARISON", "status": "OK"},
        "files": {},
    },
    # 10. Order-party consignee: "TO THE ORDER OF BANK" = negotiable bearer
    # style; same entity on both sides -> OK.
    {
        "email_id": "ood_010",
        "subject": "Compare SI vs draft BL — TPEB-00922",
        "body": "Please verify the two attached documents.",
        "expect": {"category": "BL_COMPARISON", "status": "OK"},
        "files": {
            "ood_010_SI.txt": """SHIPPING INSTRUCTION
========================================
Despatching Firm: VELUM PAPER MILLS LTD
Order Party: TO THE ORDER OF HSBC BANK PLC
Notify To: MERIDIAN LOGISTICS LTD
Loading Wharf: PORT KLANG, MALAYSIA (MYPKG)
Unload Port: CALLAO, PERU (PECLL)
Qty of Cntrs: 5 x 20'FCL
G.W.: 92,100 KG
""",
            "ood_010_BL.txt": """DRAFT BILL OF LADING
========================================
Despatching Firm: VELUM PAPER MILLS LTD
Order Party: TO THE ORDER OF HSBC BANK PLC
Notify To: MERIDIAN LOGISTICS LTD
Loading Wharf: PORT KLANG, MALAYSIA (MYPKG)
Unload Port: CALLAO, PERU (PECLL)
Qty of Cntrs: 5 x 20'FCL
G.W.: 92,100 KG
""",
        },
    },
    # 11. Genuinely blank consignee in the SI (customer left it empty) -> missing_value.
    {
        "email_id": "ood_011",
        "subject": "SI + draft BL attached — TPEB-00923",
        "body": "Please compare the attached SI and draft BL and confirm what we have.",
        "expect": {"category": "BL_COMPARISON", "status": "NEEDS_REVIEW", "review_reason": "missing_value"},
        "files": {
            "ood_011_SI.txt": """SHIPPING INSTRUCTION
========================================
Despatching Firm: VELUM PAPER MILLS LTD
Order Party: ______________________
Notify To: MERIDIAN LOGISTICS LTD
Loading Wharf: PORT KLANG, MALAYSIA (MYPKG)
Unload Port: CALLAO, PERU (PECLL)
Qty of Cntrs: 5 x 20'FCL
G.W.: 92,100 KG
""",
            "ood_011_BL.txt": """DRAFT BILL OF LADING
========================================
Despatching Firm: VELUM PAPER MILLS LTD
Order Party: MERIDIAN CARGO HOLDINGS SA
Notify To: MERIDIAN LOGISTICS LTD
Loading Wharf: PORT KLANG, MALAYSIA (MYPKG)
Unload Port: CALLAO, PERU (PECLL)
Qty of Cntrs: 5 x 20'FCL
G.W.: 92,100 KG
""",
        },
    },
    # 12. Non-standard SI-request phrasing -> SI_REQUEST.
    {
        "email_id": "ood_012",
        "subject": "Action required — shipment file for TPEB-00924",
        "body": "Hello,\n\nCould you please file the shipping instruction with the carrier for booking TPEB-00924? The vessel cutoff is Friday.\n\nThanks,\nExport Ops",
        "expect": {"category": "SI_REQUEST"},
        "files": {},
    },
]


def main():
    os.makedirs(INBOX, exist_ok=True)
    os.makedirs(ATTS, exist_ok=True)
    expectations = {}
    for case in CASES:
        eid = case["email_id"]
        attachments = []
        for fname, text in case["files"].items():
            with open(os.path.join(ATTS, fname), "w", encoding="utf-8") as f:
                f.write(text)
            attachments.append(f"attachments/{fname}")
        record = {
            "email_id": eid,
            "from": "export-ops@velum-logistics.example",
            "subject": case["subject"],
            "body": case["body"],
            "attachments": attachments,
        }
        with open(os.path.join(INBOX, f"{eid}.json"), "w", encoding="utf-8") as f:
            json.dump(record, f, indent=2, ensure_ascii=False)
        expectations[eid] = case["expect"]
    with open(os.path.join(OOD, "ood_expectations.json"), "w", encoding="utf-8") as f:
        json.dump(expectations, f, indent=2)
    print(f"Wrote {len(CASES)} OOD cases to {OOD}")


if __name__ == "__main__":
    main()
