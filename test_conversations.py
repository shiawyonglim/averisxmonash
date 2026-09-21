"""Offline test for the Conversations view: quoted-message parser + endpoints."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server

# --- parser on a known email -------------------------------------------------
d059 = server._get_email_data("email_059")
top, quoted = server._parse_quoted_messages(d059["body"])
assert len(quoted) == 1, quoted
q = quoted[0]
assert q["from_addr"] == "sales@roxcel.at", q["from_addr"]
assert q["timestamp"] == "2026-01-01T21:54:00", q["timestamp"]
assert q["from_name"] == "Sales Desk"
assert "follow the previous instruction" in q["body"]
assert not top.rstrip().endswith("_"), "separator underscores left in top_text"
print("email_059: 1 quoted block from sales@roxcel.at @", q["timestamp"])

# --- parser across the whole bundle (ground truth: 258 blocks / 195 emails) --
tot_blocks = 0
emails_with = 0
for eid, data in server.INBOX_CACHE.items():
    if not data:
        continue
    _, qs = server._parse_quoted_messages(data.get("body") or "")
    tot_blocks += len(qs)
    if qs:
        emails_with += 1
assert tot_blocks == 258, tot_blocks
assert emails_with == 195, emails_with
print(f"bundle scan: {tot_blocks} quoted blocks in {emails_with} emails")

# --- /api/conversations ------------------------------------------------------
people = server.get_conversations()["people"]
elisa = next(p for p in people if p["address"] == "elisa_tukiman@april.com.my")
assert elisa["sent_count"] == 24, elisa
assert elisa["quoted_count"] == 12, elisa
assert elisa["total"] == 36
assert elisa["audience"] == "internal"
print("people:", len(people), "| elisa:", {k: elisa[k] for k in ('sent_count','quoted_count','total','display_name','latest_at')})
for p in people:
    assert p["total"] == p["sent_count"] + p["quoted_count"]
assert people == sorted(people, key=lambda r: (-r["total"], r["address"]))

# --- /api/conversations/{address} --------------------------------------------
conv = server.get_conversation("elisa_tukiman@april.com.my")
msgs = conv["messages"]
assert msgs, "no messages"
print("elisa messages:", len(msgs), "| stats:", conv["stats"])

# focus-authored sent_count must match the list endpoint (no email is deduped away)
assert conv["stats"]["sent_count"] == elisa["sent_count"] == 24, (conv["stats"], elisa)
# every qualifying email appears exactly once as a top-level bubble
email_bubbles = [m["email_id"] for m in msgs if m["kind"] == "email"]
assert len(email_bubbles) == len(set(email_bubbles)), "duplicate email bubbles"
# coherent denominators
assert conv["stats"]["message_count"] == len(msgs)
assert conv["stats"]["dated"] + conv["stats"]["undated"] == conv["stats"]["message_count"]

# ordering: timestamps non-decreasing, nulls last
seen_null = False
prev_ts = None
for m in msgs:
    if m["timestamp"] is None:
        seen_null = True
        assert m["timestamp_source"] is None
    else:
        assert not seen_null, f"timestamped message after undated tail: {m['email_id']}"
        assert m["timestamp_source"] in ("quoted_header", "inferred"), m["timestamp_source"]
        if prev_ts:
            assert m["timestamp"] >= prev_ts, (prev_ts, m["timestamp"])
        prev_ts = m["timestamp"]
# no fabricated source
assert all(m["timestamp_source"] in (None, "quoted_header", "inferred") for m in msgs)
# focus flags make sense
assert any(m["is_focus"] for m in msgs)
# undated tail ordered by email_id
undated_ids = [m["email_id"] for m in msgs if m["timestamp"] is None]
assert undated_ids == sorted(undated_ids), undated_ids
print("ordering OK | dated:", conv["stats"]["dated"], "undated:", conv["stats"]["undated"])

# dedup: also_in populated for repeated quoted messages
with_also = [m for m in msgs if m["also_in"]]
print("deduped bubbles with also_in:", len(with_also))

# a customer address too
conv2 = server.get_conversation("sales@roxcel.at")
print("sales@roxcel.at:", len(conv2["messages"]), "msgs | stats:", conv2["stats"],
      "| audience:", conv2["audience"], "| name:", conv2["display_name"])
assert conv2["audience"] == "customer"

# 404
try:
    server.get_conversation("nobody@nowhere.example")
    raise SystemExit("expected 404")
except server.HTTPException as e:
    assert e.status_code == 404
print("404 for unknown address OK")

print("\nALL CONVERSATION TESTS PASSED")
