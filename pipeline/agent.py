import json
import uuid

from pipeline.ai_engine import get_nvidia_client, _chat_with_retry, MODEL

# Tools that mutate state or trigger external side effects. They are never
# executed inside the reasoning loop — the loop parks them in
# session["pending"] and the user must approve via /api/agent/confirm.
WRITE_TOOLS = {
    "verify_emails",
    "run_pipeline",
    "send_chaser_email",
    "resolve_mismatch",
    "sync_to_supabase",
}

MAX_ITERATIONS = 8

SYSTEM_PROMPT = (
    "You are the operations assistant for the Averis shipping-document "
    "verification system. The pipeline cross-validates draft Bills of Lading "
    "(BL) against Shipping Instructions (SI) on 7 fields: shipper, consignee, "
    "notify_party, port_of_loading, port_of_discharge, container_count, "
    "gross_weight_kg. Each email has a status: OK (all fields match), "
    "MISMATCH (one or more fields differ, see defect_fields), or NEEDS_REVIEW "
    "(edge case: wrong_doc_type, missing_attachment, unreadable, or "
    "missing_value). Emails can also be RESOLVED by a human operator.\n\n"
    "Use tools rather than guessing. Never fabricate field values or counts: "
    "for any counting question call get_statistics; for specific emails call "
    "list_emails, search_knowledge_base or get_email_details rather than "
    "asking the user for ids you can look up yourself. Prefer list_emails + "
    "verify_emails over asking the user to supply email ids.\n\n"
    "Write actions (verifying emails, running the pipeline, sending chaser "
    "emails, resolving mismatches, syncing to Supabase) are gated on explicit "
    "user approval — when you call one, tell the user plainly that it needs "
    "their approval and what it will do. If the user declines an action, "
    "move on gracefully — do NOT retry the same call.\n\n"
    "Be concise; short markdown, tables or bullets where useful. Cite email "
    "ids inline like email_042."
)


def _fn(name, description, properties, required=None):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required or [],
            },
        },
    }


TOOL_SCHEMAS = [
    _fn("get_statistics",
        "Deterministic counts over the whole inbox: totals, by_status, "
        "by_category, defect_field_counts, review_reason_counts, resolved "
        "and missing-attachment counts. Use for ANY counting question.",
        {}),
    _fn("search_knowledge_base",
        "Keyword search over email records. Returns compact matching "
        "records (id, subject, status, category, defects).",
        {"query": {"type": "string", "description": "search query"},
         "k": {"type": "integer", "description": "max results, default 8"}},
        ["query"]),
    _fn("get_email_details",
        "Full record for one email: subject, sender, category, status, "
        "review_reason, defect_fields, SI/BL fields, field comparisons, "
        "attachment diagnostics, truncated body.",
        {"email_id": {"type": "string", "description": "e.g. email_042"}},
        ["email_id"]),
    _fn("list_emails",
        "Deterministically filter the inbox by status / category / "
        "review_reason / missing-BL / resolved. Returns total_matched plus "
        "a page of results.",
        {"status": {"type": "string", "description": "OK | MISMATCH | NEEDS_REVIEW | UNVERIFIED"},
         "category": {"type": "string", "description": "BL_COMPARISON | SI_REQUEST | INVOICE_QUERY | GENERAL | SPAM"},
         "review_reason": {"type": "string", "description": "wrong_doc_type | missing_attachment | unreadable | missing_value"},
         "missing_bl": {"type": "boolean", "description": "true = only emails with fewer than 2 attachments"},
         "resolved": {"type": "boolean"},
         "limit": {"type": "integer", "description": "page size, default 50"}}),
    _fn("list_missing_bls",
        "List emails that are BL-relevant but carry no Bill of Lading "
        "attachment (carriers that owe us a draft BL).",
        {"carrier": {"type": "string", "description": "optional carrier name filter"},
         "limit": {"type": "integer", "description": "page size, default 50"}}),
    _fn("get_pipeline_status",
        "Current batch-pipeline state: running, processed, total, done, "
        "error, skipped, submission size.",
        {}),
    _fn("get_score_report",
        "Score the current submission against ground truth. Returns the "
        "score summary and the top mismatching rows.",
        {}),
    _fn("draft_chaser",
        "Draft (but do NOT send) a chaser email for a missing draft BL. "
        "Returns proposed to/subject/body. Read-only.",
        {"email_id": {"type": "string"}}, ["email_id"]),
    _fn("verify_emails",
        "WRITE: run SI-vs-BL verification on a set of emails. Targets are "
        "explicit email_ids, or all emails matching status_filter. "
        "Batches of 25 by default, hard-capped at 50 per call. Requires "
        "user approval.",
        {"email_ids": {"type": "array", "items": {"type": "string"}},
         "status_filter": {"type": "string", "description": "OK | MISMATCH | NEEDS_REVIEW | UNVERIFIED"},
         "limit": {"type": "integer", "description": "max emails this batch, <=50"}}),
    _fn("run_pipeline",
        "WRITE: start the full batch pipeline run in the background "
        "(classify + extract + verify all unprocessed emails). Requires "
        "user approval.",
        {"max_emails": {"type": "integer", "description": "0 = all"},
         "resume": {"type": "boolean", "description": "skip already-processed emails"}}),
    _fn("send_chaser_email",
        "WRITE: send a REAL outbound email via SMTP (typically a missing-BL "
        "chaser to a carrier) and record it as dispatched. Requires user "
        "approval — never call without it.",
        {"email_id": {"type": "string"},
         "to": {"type": "string"},
         "subject": {"type": "string"},
         "body": {"type": "string"}},
        ["email_id", "to", "subject", "body"]),
    _fn("resolve_mismatch",
        "WRITE: record a human resolution for an email's defect fields "
        "(choose SI or BL value per field). Requires user approval.",
        {"email_id": {"type": "string"},
         "resolutions": {"type": "object", "description": "{field: {type: 'SI'|'BL', value: str}}"},
         "notes": {"type": "string"}},
        ["email_id", "resolutions"]),
    _fn("sync_to_supabase",
        "WRITE: push one email's current verdict to the shared Supabase "
        "verifications table. Requires user approval.",
        {"email_id": {"type": "string"}}, ["email_id"]),
]

_SOURCE_KEYS = ("results", "returned", "emails", "records", "missing_bills", "mismatches")


def _add_source(acc, r):
    if not isinstance(r, dict) or not r.get("email_id"):
        return
    if any(s["email_id"] == r["email_id"] for s in acc):
        return
    acc.append({
        "email_id": r["email_id"],
        "subject": r.get("subject", ""),
        "status": r.get("status", ""),
        "category": r.get("category", ""),
    })


def _extract_sources(result, acc):
    if not isinstance(result, dict):
        return
    for k in _SOURCE_KEYS:
        rows = result.get(k)
        if isinstance(rows, list):
            for r in rows:
                _add_source(acc, r)
    if isinstance(result.get("email_id"), str):
        _add_source(acc, result)


def _exec_tool(name, args, executors):
    """Run one executor; never raises — errors become result payloads."""
    fn = executors.get(name)
    if fn is None:
        return {"error": f"unknown tool: {name}"}, False, f"unknown tool {name}"
    try:
        result = fn(**(args or {}))
        if not isinstance(result, (dict, list)):
            result = {"result": result}
        summary = result.get("summary") if isinstance(result, dict) else None
        if not isinstance(summary, str) or not summary:
            summary = json.dumps(result, default=str)[:140]
        return result, True, summary
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}, False, f"{name} failed: {e}"


def _pending_summary(name, args):
    """One-line human description of a gated write action."""
    if name == "verify_emails":
        if args.get("email_ids"):
            target = f"{len(args['email_ids'])} specified emails"
        else:
            target = f"emails with status {args.get('status_filter') or 'UNVERIFIED'}"
        return f"Verify {target} (batch of up to {min(int(args.get('limit') or 25), 50)})"
    if name == "run_pipeline":
        return f"Run the batch pipeline (max_emails={args.get('max_emails') or 'all'}, resume={args.get('resume', True)})"
    if name == "send_chaser_email":
        return f"Send chaser email for {args.get('email_id')} to {args.get('to')}"
    if name == "resolve_mismatch":
        return f"Resolve mismatches for {args.get('email_id')} ({len(args.get('resolutions') or {})} fields)"
    if name == "sync_to_supabase":
        return f"Sync {args.get('email_id')} verdict to Supabase"
    return f"Run {name}({args})"


def _handle_calls(session, calls, executors, new_steps, sources):
    """Execute calls in order. Returns a pending_action dict if a write
    tool is hit (loop must stop), else None."""
    for i, c in enumerate(calls):
        name = c["name"]
        args = c["args"]
        if name in WRITE_TOOLS:
            pending = {
                "action_id": uuid.uuid4().hex,
                "tool": name,
                "args": args,
                "tool_call_id": c["id"],
                "summary": _pending_summary(name, args),
                "remaining_calls": calls[i + 1:],
            }
            session["pending"] = pending
            return pending
        result, ok, summary = _exec_tool(name, args, executors)
        session["messages"].append({
            "role": "tool",
            "tool_call_id": c["id"],
            "content": json.dumps(result, default=str)[:4000],
        })
        new_steps.append({"tool": name, "args": args, "ok": ok, "summary": summary})
        if ok:
            _extract_sources(result, sources)
    return None


def _loop(session, executors, new_steps, sources):
    for _ in range(MAX_ITERATIONS):
        resp = _chat_with_retry(
            get_nvidia_client(),
            model=MODEL,
            messages=session["messages"],
            tools=TOOL_SCHEMAS,
            tool_choice="auto",
            temperature=0.2,
            max_tokens=900,
        )
        msg = resp.choices[0].message
        tool_calls = getattr(msg, "tool_calls", None) or []
        if not tool_calls:
            content = msg.content or ""
            session["messages"].append({"role": "assistant", "content": content})
            return {"answer": content, "pending_action": None}

        session["messages"].append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in tool_calls
            ],
        })

        calls = []
        for tc in tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except Exception:
                args = {}
            calls.append({"id": tc.id, "name": tc.function.name, "args": args})

        pending = _handle_calls(session, calls, executors, new_steps, sources)
        if pending:
            answer = msg.content or f"Action requested: {pending['summary']} — awaiting your approval."
            return {"answer": answer, "pending_action": pending}

    return {
        "answer": (session["messages"][-1].get("content") or "") +
                  "\n\n(Note: step budget reached — ask me to continue if needed.)",
        "pending_action": None,
    }


def _supersede_pending(session, steps):
    """A new user message kills any approval card still parked in the
    session: close out its tool_call_ids so the transcript stays coherent,
    record a step so the UI can mark the card dead, and drop the pending
    record so /api/agent/confirm on its action_id 409s."""
    pend = session["pending"]
    session["pending"] = None
    note = json.dumps({
        "superseded": True,
        "executed": False,
        "note": "This action was superseded because the user sent a new "
                "instruction. It was NOT executed and must not be retried "
                "unless the user asks again.",
    })
    session["messages"].append({
        "role": "tool",
        "tool_call_id": pend["tool_call_id"],
        "content": note,
    })
    for c in pend.get("remaining_calls") or []:
        session["messages"].append({
            "role": "tool",
            "tool_call_id": c["id"],
            "content": note,
        })
    steps.append({"tool": pend["tool"], "args": pend.get("args") or {},
                  "ok": True, "summary": "superseded — not executed"})


def run_agent(session, user_message, executors):
    session.setdefault("messages", [])
    session.setdefault("steps", [])
    session.setdefault("pending", None)
    new_steps, sources = [], []
    if session.get("pending"):
        _supersede_pending(session, new_steps)
    session["messages"].append({"role": "user", "content": user_message})
    try:
        out = _loop(session, executors, new_steps, sources)
    except Exception as e:
        print(f"Agent degraded (LLM unavailable): {e}")
        return {
            "answer": "The AI agent model is currently unavailable, so I can't "
                      "plan or execute actions right now. The basic assistant "
                      "(/api/chat) still answers stats/retrieval questions.",
            "steps": new_steps,
            "pending_action": None,
            "sources": [],
            "degraded": True,
        }
    session["steps"].extend(new_steps)
    out["steps"] = new_steps
    out["sources"] = sources[:8]
    out["degraded"] = False
    return out


def resume_agent(session, action_id, approved, executors):
    pend = session.get("pending")
    if not pend or pend.get("action_id") != action_id:
        raise ValueError("no matching pending action")
    session["pending"] = None
    new_steps, sources = [], []

    if approved:
        result, ok, summary = _exec_tool(pend["tool"], pend["args"], executors)
        session["messages"].append({
            "role": "tool",
            "tool_call_id": pend["tool_call_id"],
            "content": json.dumps(result, default=str)[:4000],
        })
        new_steps.append({"tool": pend["tool"], "args": pend["args"], "ok": ok, "summary": summary})
        if ok:
            _extract_sources(result, sources)
    else:
        session["messages"].append({
            "role": "tool",
            "tool_call_id": pend["tool_call_id"],
            "content": json.dumps({
                "declined": True,
                "note": "The user declined this action. Proceed without it; do NOT retry the same call.",
            }),
        })
        new_steps.append({"tool": pend["tool"], "args": pend["args"], "ok": True,
                          "summary": "declined by user"})

    # Handle any sibling calls that were queued behind the gated one.
    pending = _handle_calls(session, pend.get("remaining_calls") or [], executors, new_steps, sources)
    if pending:
        session["steps"].extend(new_steps)
        return {"answer": f"Action requested: {pending['summary']} — awaiting your approval.",
                "steps": new_steps, "pending_action": pending,
                "sources": sources[:8], "degraded": False}

    try:
        out = _loop(session, executors, new_steps, sources)
    except Exception as e:
        print(f"Agent degraded (LLM unavailable): {e}")
        out = {
            "answer": "The AI agent model is currently unavailable — the action "
                      "was recorded but I can't continue the conversation.",
            "pending_action": None,
        }
        out["degraded"] = True
    session["steps"].extend(new_steps)
    out["steps"] = new_steps
    out["sources"] = sources[:8]
    out.setdefault("degraded", False)
    return out
