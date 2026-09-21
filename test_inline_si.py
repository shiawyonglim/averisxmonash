"""Offline test for inline-SI materialization, conflict detection, and save."""
import json, os, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server

tmp = tempfile.mkdtemp(prefix="inline_si_test_")
inbox = os.path.join(tmp, "inbox")
attdir = os.path.join(tmp, "attachments")
os.makedirs(inbox)
os.makedirs(attdir)

server.BUNDLE_DIR = tmp
server.INBOX_DIR = inbox
server.BACKUP_ROOT = os.path.join(tmp, "_backups")
server.BACKUP_MANIFEST = os.path.join(server.BACKUP_ROOT, "manifest.json")
server._get_from_supabase_record = lambda eid: None
server._log_to_supabase_audit = lambda *a, **k: None
server._persist_audit_state = lambda: None
server.INBOX_CACHE.clear()

BODY_SI = """Hi Teo

Please find Shipping instruction for TEST-99999.

POL: NHAVA SHEVA, INDIA
POD: BUSAN, SOUTH KOREA

Shipper:
CONFLICTING SHIPPER CORP
TOWER 1, STREET 9

Consignee:
NAGAPPA EXPORTS

Notify Party:
NAGAPPA EXPORTS

3 x 40'GP containers
GROSS WT: 68,592 KG

Please revert with draft BL once available.
"""

ATT_SI = """SHIPPING INSTRUCTION

Shipper: ACME LINES LTD
Consignee: NAGAPPA EXPORTS
Notify Party: NAGAPPA EXPORTS
Port of Loading: NHAVA SHEVA, INDIA
Port of Discharge: SINGAPORE
Container Count: 3
Gross Weight (KG): 68,592 KG
"""

ATT_BL = """DRAFT BILL OF LADING

Shipper: CONFLICTING SHIPPER CORP
Consignee: NAGAPPA EXPORTS
Notify Party: NAGAPPA EXPORTS
Port of Loading: NHAVA SHEVA, INDIA
Port of Discharge: BUSAN, SOUTH KOREA
Container Count: 3
Gross Weight (KG): 68,592 KG
"""

def write_email(eid, attachments):
    with open(os.path.join(inbox, eid + ".json"), "w", encoding="utf-8") as f:
        json.dump({
            "email_id": eid,
            "from": "teo@aprilasia.com",
            "subject": "SI TEST DIRECT(OOCL) OOLU12345",
            "body": BODY_SI,
            "attachments": attachments,
        }, f)

def write_att(name, text):
    with open(os.path.join(attdir, name), "w", encoding="utf-8") as f:
        f.write(text)

# --- Case 1: attached SI conflicts with inline SI ------------------------------
write_email("test_conflict", ["attachments/ORD-9_SI.txt"])
write_att("ORD-9_SI.txt", ATT_SI)

r = server.get_email_content("test_conflict")
assert r["si_source"] == "attachment", r["si_source"]
assert r["si_conflict"], "expected si_conflict"
conf = r["si_conflict"]
print("CONFLICT fields:", conf["fields"])
print("  att:", conf["attachment_values"])
print("  body:", conf["body_values"])
assert "shipper" in conf["fields"] or "port_of_discharge" in conf["fields"]
assert "CONFLICTING" in (conf["body_values"].get("shipper") or "")
assert r["si_text"].startswith("SHIPPING INSTRUCTION")  # attachment stays authoritative
assert "ACME LINES" in r["si_text"]
assert r["missing_doc"] == "bl"

# --- Case 2: BL attached + SI inline -> full pair ------------------------------
write_email("test_blpair", ["attachments/ORD-8_BL.txt"])
write_att("ORD-8_BL.txt", ATT_BL)

r2 = server.get_email_content("test_blpair")
assert r2["si_source"] == "email_body", r2["si_source"]
assert r2["missing_doc"] is None, r2["missing_doc"]
assert "DRAFT BILL OF LADING" in r2["bl_text"]
assert r2["generated_si"]
gen_path = os.path.join(tmp, r2["generated_si"])
assert os.path.exists(gen_path), gen_path
print("BLPAIR: generated_si =", r2["generated_si"], "| missing_doc =", r2["missing_doc"])

# --- Case 3: save-documents writes to the generated SI file --------------------
req = server.SaveDocumentsRequest(si_text="EDITED SI TEXT\nShipper: EDITED SHIPPER", bl_text=r2["bl_text"])
r3 = server.save_email_documents("test_blpair", req)
assert r3["status"] == "SUCCESS", r3
assert r3["saved"].get("si"), r3
with open(gen_path, encoding="utf-8") as f:
    assert "EDITED SHIPPER" in f.read()
print("SAVE: wrote edits to", r3["saved"])

# --- Case 4: zero attachments -> materialize + missing_doc=bl ------------------
write_email("test_zero", [])
r4 = server.get_email_content("test_zero")
assert r4["si_source"] == "email_body"
assert r4["missing_doc"] == "bl"
assert r4["si_inline"] is True
assert os.path.exists(os.path.join(tmp, r4["generated_si"]))
# idempotent: second open reuses the file
r4b = server.get_email_content("test_zero")
assert r4b["generated_si"] == r4["generated_si"]
print("ZERO: materialized", r4["generated_si"], "| missing_doc = bl | idempotent OK")

# --- Case 5: carrier detection on DIRECT() subjects ----------------------------
c = server._detect_carrier("SI TEST DIRECT(OOCL) OOLU12345", "")
print("CARRIER DIRECT(OOCL):", c)
c2 = server._detect_carrier("Re: booking EGLV7547123", "")
print("CARRIER EGLV prefix:", c2)

print("\nALL TESTS PASSED")
