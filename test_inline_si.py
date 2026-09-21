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
server._async_save_to_supabase = lambda *a, **k: None
server._save_email_threads = lambda t: None  # keep real email_threads.json untouched
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

# --- Case 6: reply WITHOUT a BL doc -> still awaiting --------------------------
write_email("test_ack", [])
server.CHASERS_STORE["test_ack"] = {
    "status": "CHASER_DISPATCHED", "chaser_sent_at": server._utcnow()}
r5 = server.process_inbound_reply(
    email_id="test_ack",
    from_addr="doc.desk@oocl.com",
    subject="Re: SI TEST DIRECT(OOCL) OOLU12345 [REF: test_ack]",
    body="Booking confirmed, draft BL generation in progress.",
    attachments=[],
)
assert r5["bl_ingested"] is None and r5["auto_verdict"] is None
assert server.CHASERS_STORE["test_ack"]["status"] == "REPLY_RECEIVED"
assert "bl_received_at" not in server.CHASERS_STORE["test_ack"]
r6 = server.get_email_content("test_ack")
assert r6["missing_doc"] == "bl", r6["missing_doc"]
print("ACK-ONLY REPLY: status=REPLY_RECEIVED, still missing_doc=bl")

# --- Case 7: reply WITH a BL attachment -> ingest + auto-verify ----------------
r7 = server.process_inbound_reply(
    email_id="test_ack",
    from_addr="doc.desk@oocl.com",
    subject="Re: SI TEST DIRECT(OOCL) OOLU12345 [REF: test_ack]",
    body="Please find attached the draft Bill of Lading.",
    attachments=[{"filename": "Draft_BL_OOLU12345.pdf", "type": "application/pdf"}],
)
assert r7["bl_ingested"], r7
assert r7["auto_verdict"], "expected auto-verdict"
assert os.path.exists(os.path.join(tmp, r7["bl_ingested"]))
assert server.CHASERS_STORE["test_ack"]["status"] == "BL_RECEIVED"
d7 = json.load(open(os.path.join(inbox, "test_ack.json"), encoding="utf-8"))
assert r7["bl_ingested"] in d7["attachments"]
print("BL REPLY: ingested", r7["bl_ingested"], "| auto-verdict:", r7["auto_verdict"]["status"])

# reopen -> full pair, nothing missing, stored verdict surfaces
r8 = server.get_email_content("test_ack")
assert r8["missing_doc"] is None
assert "DRAFT BILL OF LADING" in r8["bl_text"]
assert r8["verdict"]["status"] == r7["auto_verdict"]["status"]
print("REOPEN: missing_doc=None, bl_text is the real doc, verdict surfaced")

# --- Case 8: aging — old unanswered chaser flags overdue ------------------------
write_email("test_overdue", [])
from datetime import datetime, timezone, timedelta
server.CHASERS_STORE["test_overdue"] = {
    "status": "CHASER_DISPATCHED",
    "chaser_sent_at": (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()}
d8 = json.load(open(os.path.join(inbox, "test_overdue.json"), encoding="utf-8"))
item = server._queue_item("test_overdue", d8)
assert item["overdue"] is True, item
assert item["chaser_age_days"] >= 5
assert item["queue_status"] == "OVERDUE", item["queue_status"]
assert server._queue_match(item, "overdue")
# fresh chaser is not overdue
server.CHASERS_STORE["test_overdue"]["chaser_sent_at"] = server._utcnow()
item2 = server._queue_item("test_overdue", d8)
assert item2["overdue"] is False
print("OVERDUE: 5d chaser -> OVERDUE queue status; fresh chaser not overdue")

# --- Case 9: si_missing_fields gate ---------------------------------------------
write_email("test_incomplete", [])
# strip fields from the body so extraction comes back partial
d9 = json.load(open(os.path.join(inbox, "test_incomplete.json"), encoding="utf-8"))
d9["body"] = """Hi

Please find Shipping instruction for TEST-77777.

Shipper:
ONLY SHIPPER LTD

Consignee:
NAGAPPA EXPORTS

Notify Party:
NAGAPPA EXPORTS
"""
json.dump(d9, open(os.path.join(inbox, "test_incomplete.json"), "w", encoding="utf-8"))
server.INBOX_CACHE.pop("test_incomplete", None)
r9 = server.get_email_content("test_incomplete")
assert r9["si_missing_fields"], "expected missing fields on partial inline SI"
assert "port_of_loading" in r9["si_missing_fields"]
print("SI GATE: missing fields detected ->", r9["si_missing_fields"])

print("\nALL TESTS PASSED")
