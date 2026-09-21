# Averis x Monash Hackathon — ShipSure (Shipping Document Verification)

ShipSure is an AI-powered pipeline that processes a 520-email shipping inbox, classifies
each email, and — for `BL_COMPARISON` emails — verifies a draft **Bill of
Lading (BL)** against its **Shipping Instruction (SI)** by extracting and
comparing 7 core fields. A React front-end gives human reviewers a dashboard,
a work queue, a side-by-side document view, and an audit trail.

Built for the Averis x Monash Hackathon 2026 (see `docs/` for the info pack,
rules, and the Shipping Document Verification use case).

## How it works

The pipeline is **two-tier**: a deterministic tier does the bulk of the work
with zero LLM calls, and an LLM tier (`AI_FALLBACK`) resolves only what the
rules cannot. For every email in the inbox:

1. **Classify** — deterministic precedence rules first into
   `BL_COMPARISON`, `SI_REQUEST`, `INVOICE_QUERY`, `GENERAL`, or `SPAM`.
   Category keywords are matched on the subject only (bodies contain forwarded
   threads that mention BL/docs for unrelated reasons). The LLM is consulted
   **only** when the rules fall through to the unmatched `GENERAL` fallback.
2. **Intent + edge cases** — for comparison-shaped emails with fewer than 2
   attachments, sender intent is resolved (`COMPARE_NOW` vs `REQUEST_DOCS`,
   deterministic rules first, LLM only when inconclusive): a sender who
   believes the documents are attached gets flagged
   `NEEDS_REVIEW / missing_attachment`, while a routine "please send the draft
   BL" request is not a defect and routes to the chaser queue. Corrupt /
   unreadable files and wrong-document-type detection (e.g. a commercial
   invoice sent instead of a BL) also short-circuit to `NEEDS_REVIEW` with a
   `review_reason`.
3. **Extract (two-tier)** — `extract_fields_tiered`: a deterministic regex
   pass extracts the 7 fields from `.txt` / `.pdf` / `.xlsx` / `.docx`
   attachments; **only the fields left blank** escalate to one cached LLM call
   (NVIDIA NIM — `z-ai/glm-5.3` and `meta/muse-glimmer-30b`, selectable via
   env vars). Every field carries a **provenance** tag — `rule`, `model`, or
   `missing`. Camera photos / scans of paper documents (`.png` / `.jpg` /
   `.jpeg` / `.webp` / `.gif` / `.bmp` / `.tif`) go through the model's vision
   pass — handwriting, stamps, skew and poor lighting included — which also
   reports document type, legibility and a transcription.
4. **Compare** — SI vs BL with per-field normalization (company-name
   punctuation, UN/LOCODE stripping, weight/count numeric extraction).
   Writing-style variations are accepted; value differences are flagged.
   A field that cannot be extracted escalates to `NEEDS_REVIEW` rather than
   fabricating an `OK`. An SI written **inline in the email body** is
   materialized as a `generated_si` document — so a single-attachment email
   carrying an inline SI still runs a real comparison against the attached
   BL reply instead of being flagged `missing_attachment`.
5. **Adjudicate** — every `MISMATCH` and `NEEDS_REVIEW` verdict runs a
   Laya → Muse adjudication chain (`diagnose_mismatch` /
   `adjudicate_review`) that explains *why* the verdict landed and stores
   the diagnosis with provenance. Each stage of the pipeline records an
   **engine trail** (`rule` / `laya` / `model` / `vision`) in
   `verification_details.json`, surfaced per-decision in `/api/audit`.

**Compared fields:** `shipper`, `consignee`, `notify_party`,
`port_of_loading`, `port_of_discharge`, `container_count`, `gross_weight_kg`

**Statuses:** `OK` (all match) · `MISMATCH` (≥1 field differs →
`defect_fields`) · `NEEDS_REVIEW` (`wrong_doc_type` | `missing_attachment` |
`unreadable` | `missing_value`)

**Outputs:** `submission.json` (per-email verdicts) plus a
`verification_details.json` sidecar with per-field provenance, extracted
values, and classification/intent provenance for auditing.

**Measured performance** (deterministic tier only, `AI_FALLBACK=0`, on the
organizers' 520-email set): **97.54%** weighted score, 100% category
accuracy, field-level F1 0.9859, 46/46 defects flagged with 0 false alarms,
25 `NEEDS_REVIEW` escalations vs 20 gold (the 5 extras are honest escalations
where a field could not be extracted). See `docs/conditions.md` §1 for the
full scorecard, including the hand-written OOD set (`tests/eval_ood.py`)
where rules-only scoring leaves a residual gap the LLM tier is designed to
catch.

## Project layout

```
server.py               FastAPI backend (~55 endpoints: verify, queue,
                        resolutions, chasers, audit, pipeline run, scoring,
                        agent/chat, email send/receive, Supabase sync,
                        dataset upload, admin reset)
pipeline/
  main.py               Batch pipeline -> writes submission.json
  ai_engine.py          LLM client (NVIDIA NIM), classify/extract/adjudicate
  comparator.py         7-field normalization + comparison logic
  edge_cases.py         Attachment/doc-type/unreadable checks
  parsers.py            Text extraction for txt/pdf/xlsx/docx
  agent.py              Tool-calling ops agent (confirm-gated actions)
  knowledge_base.py     RAG Q&A over inbox + verdicts + resolutions
frontend/               React 19 + Vite UI (dashboard, assistant, queue,
                        conversations, getter, verify, review, verified,
                        cloud, audit, pipeline, stress lab, reset) with a
                        driver.js guided judge tour (judgeTour.js)
modal_app.py            Modal serverless deploy: FastAPI + compiled
                        frontend + Laya weights in one container
email_threads.json      Runtime thread tracker (inbound replies, chaser
                        state, simulated carrier replies)
sdoc-hackathon-bundle/  Dataset: inbox/ (520 emails) + attachments/
sdoc-hackathon-docker/  Organizers' bundle: docker-compose.yml, server/,
                        data_v2/ dataset (its ground_truth.json and
                        generator sources are gitignored — kept local only)
tests/
  generate_synthetic_dataset.py   400-case generalization benchmark
  generate_stress_dataset.py      2,020-case edge/stress suite ->
                                  tests/stress_dataset/ (ground truth
                                  in stress_ground_truth.json)
  generate_ood_dataset.py         Hand-written out-of-distribution set
  eval_synthetic_benchmark.py     CLI scorer; accepts a dataset dir arg
  eval_ood.py                     OOD scorer (residual-gap analysis)
  score_against_ground_truth.py   Score submission.json vs ground truth
  edge_case_probe.py              Targeted edge-case probe
test_conversations.py   Tests for the conversations/thread index
test_inline_si.py       Tests for inline-body SI extraction + compare
supabase_schema.sql     Postgres schema (verifications + audit_logs)
submission.json         Pipeline output for all 520 emails
docs/                   Hackathon info pack, rules, use case PDF
```

## Setup

### Backend

```bash
pip install -r requirements.txt
```

Create a `.env` in the repo root:

```env
# Required — NVIDIA NIM
NVIDIA_API_KEY=nvapi-...
NVIDIA_BASE_URL=https://integrate.api.nvidia.com/v1

# Optional — model selection (we run z-ai/glm-5.3 and meta/muse-glimmer-30b;
# code fallback when unset is meta/muse-glimmer-30b)
AI_MODEL=...
# or scoped alternatives, each with its own key:
KIMI_MODEL=...        NVIDIA_KIMI_API_KEY=...
MUSE_MODEL=...        NVIDIA_MUSE_API_KEY=...

# Optional — enable the LLM tier (default 1). Set to 0 for a fully
# deterministic run with no API calls: extraction blanks stay 'missing'
# and unmatched classifications fall back to GENERAL
AI_FALLBACK=1

# Optional — hard cap on live (uncached) LLM calls per process; cache reads
# are free and exhaustion degrades to deterministic fallbacks (default 30)
AI_MAX_CALLS=30

# Optional — min calibrated confidence for the Laya System-1 neural tier to
# own a classification/intent decision (default 0.6; needs `pip install laya`,
# absent locally it degrades to rules -> LLM)
LAYA_MIN_CONFIDENCE=0.6

# Optional — gross-weight variance tolerance in percent (default 0 = exact)
WEIGHT_TOLERANCE_PCT=0

# Optional — Supabase (schema in supabase_schema.sql; not required to run)
SUPABASE_URL=...
SUPABASE_ANON_KEY=...
SUPABASE_SERVICE_ROLE_KEY=...

# Optional — outbound email via Gmail SMTP (falls back to simulated send)
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=...
SMTP_PASSWORD=...        # Google App Password
DEFAULT_SENDER=...

# Optional — inbound email polling (IMAP)
IMAP_HOST=...
IMAP_PORT=993
IMAP_USER=...
IMAP_PASSWORD=...

# Optional — real-Gmail ingestion endpoints (/api/getter/*). LOCAL DEMO ONLY:
# keep unset/0 on public deploys — it exposes mailbox content and sends mail
# via the stored SMTP credentials
REAL_GMAIL_ENABLED=0
```

Run the API (serves on `http://localhost:8000`):

```bash
python server.py
```

### Frontend

```bash
cd frontend
npm install
npm run dev
```

Point it at the backend with `VITE_API_URL` (defaults to
`http://localhost:8000`).

### Run the full pipeline (batch)

```bash
python -m pipeline.main
```

Processes all 520 emails and writes `submission.json` in the format expected
by the organizers' `score_cli.py`.

## API overview

| Endpoint | Purpose |
|---|---|
| `GET /api/inbox`, `/api/emails`, `/api/email/{id}` | Browse the inbox |
| `GET /api/email/{id}/thread` | Full thread view incl. tracked replies |
| `POST /api/email/{id}/save-documents`, `/restore-documents` | Edit/restore an email's SI+BL documents |
| `POST /api/verify`, `GET /api/compare` | Run/inspect verification (re-verify returns diagnosis + engine trail) |
| `POST /api/verify/scan` | Paper mode: multipart upload of SI + BL photos/scans (`si_file`, `bl_file`) — vision extraction + same 7-field audit |
| `GET /api/queue`, `/api/queue/adjacent`, `/api/stats` | Work queue, prev/next navigation, dashboard stats |
| `POST /api/resolve`, `GET /api/resolutions` | Human resolution workflow |
| `GET /api/corrupted`, `POST /api/corrupted/resolve` | Corrupted-attachment handling |
| `GET /api/missing-bills`, `POST /api/missing-bills/chase`, `/batch-chase` | Chase missing BLs |
| `GET /api/conversations`, `/api/conversations/{address}` | Per-person conversation index across sent + quoted messages |
| `POST /api/email/send-smtp`, `GET /api/smtp/config` | In-app compose → Gmail SMTP (simulated send when unconfigured) |
| `POST /api/email/inbound-webhook`, `/{id}/simulate-reply`, `/imap-poll` | Inbound reply capture (webhook, simulator, IMAP poll) |
| `GET /api/getter/*` (`status`, `emails`, `fetch`, `poll-gmail`, `ingest-custom`, `send-real-test`, `reset`) | Real-Gmail ingestion dashboard — gated by `REAL_GMAIL_ENABLED`, local demo only |
| `GET /api/supabase/status`, `/records`, `POST /sync-all`, `/pull/{id}` | Cloud persistence: inspect, push, pull |
| `POST /api/chat`, `GET /api/chat/stats` | RAG Q&A over inbox + verdicts + resolutions |
| `POST /api/agent/chat`, `/confirm`, `/reset` | Tool-calling ops agent (stats, verify, chasers, pipeline, Supabase sync) with confirm-gated side effects |
| `GET /api/audit` | Audit trail of all decisions, incl. per-stage engine badges |
| `POST /api/pipeline/run`, `/cancel`, `GET /status`, `/submission` | Batch pipeline control |
| `GET /api/stress/dataset`, `/cases` | Stress dataset info + case browser |
| `POST /api/stress/run`, `/cancel`, `GET /status`, `/results` | Batch stress test over `tests/stress_dataset` |
| `POST /api/stress/run-one` | Run a single stress case vs ground truth |
| `POST /api/stress/upload` | Upload a dataset (.zip bundle or loose files) into inbox + stress set; archives to Supabase Storage |
| `POST /api/admin/reset` | Demo reset: dry-run preview, typed `RESET` confirm, scoped wipes of local stores / Supabase / uploads (inbox + LLM cache preserved) |
| `GET /api/config` | Runtime config |

## Tech stack

- **Backend:** Python, FastAPI, Uvicorn, pydantic, pdfplumber, openpyxl,
  python-docx, pillow
- **Frontend:** React 19, Vite, oxlint, driver.js (guided judge tour)
- **Agent + chat:** tool-calling ops agent (`pipeline/agent.py`) with
  confirm-gated actions, and a RAG Q&A assistant
  (`pipeline/knowledge_base.py`) over inbox, verdicts and resolutions
- **Email integration:** Gmail SMTP compose (simulated send fallback),
  IMAP polling, inbound webhook + reply simulator, thread tracking in
  `email_threads.json`
- **Database:** Supabase (managed Postgres) — schema in `supabase_schema.sql`
- **AI — classification:** Laya System-1 neural decision model (Convai
  Innovations ModernBERT, `laya==0.3.4` + torch/transformers) — typed,
  calibrated-confidence decisions own email category and comparison-intent
  routing when confidence clears `LAYA_MIN_CONFIDENCE` (default 0.6);
  deterministic rules and the NIM LLM adjudicate what it can't settle. The
  same chain diagnoses every `MISMATCH`/`NEEDS_REVIEW` verdict
  (`diagnose_mismatch` / `adjudicate_review`) with provenance
- **Deterministic tier:** hand-tuned regex + precedence rules for 7-field
  extraction and SI↔BL normalization/comparison — does the bulk of the
  parsing work with zero model calls (97.54% weighted score rules-only)
- **AI — extraction:** NVIDIA NIM LLMs via the OpenAI SDK —
  `z-ai/glm-5.3` (GLM 5.3) and `meta/muse-glimmer-30b` (Muse Glimmer 30B),
  selectable via `AI_MODEL` / `KIMI_MODEL` / `MUSE_MODEL`
- **AI — inbox triage:** the same Laya pass enriches the live email
  dossier with urgency, needs-reply and spam scores
- **Deployment:** Modal serverless (`modal_app.py`) — single container serving
  FastAPI + compiled frontend, Laya weights pre-baked into the image;
  Render + Vercel blueprints also included for a lighter split deploy

## Deploy

The backend honors the `PORT` env var and a `render.yaml` blueprint is
included at the repo root.

**Backend → Render (or Railway):**

1. New Web Service → point at this repo (Render auto-detects `render.yaml`),
   or set manually:
   - Build: `pip install -r requirements.txt`
   - Start: `uvicorn server:app --host 0.0.0.0 --port $PORT`
   - Health check path: `/api/config`
2. Set env vars from `.env.example` (at minimum `NVIDIA_API_KEY`; add
   `AI_FALLBACK=1`, `AI_MAX_CALLS`, and `SUPABASE_*` if using persistence).
3. Note the deployed URL, e.g. `https://<service>.onrender.com`.

**Frontend → Vercel:**

1. Import the repo in Vercel (`vercel.json` already configures the Vite build).
2. Set `VITE_API_URL=https://<service>.onrender.com` in the project env.
3. Deploy — CORS on the backend is open (`allow_origins=["*"]`), so the
   frontend origin works without extra config.
