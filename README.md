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
   (NVIDIA NIM, `meta/llama-3.2-11b-vision-instruct` by default, swappable via
   env vars). Every field carries a **provenance** tag — `rule`, `model`, or
   `missing`. Camera photos / scans of paper documents (`.png` / `.jpg` /
   `.jpeg` / `.webp` / `.gif` / `.bmp` / `.tif`) go through the model's vision
   pass — handwriting, stamps, skew and poor lighting included — which also
   reports document type, legibility and a transcription.
4. **Compare** — SI vs BL with per-field normalization (company-name
   punctuation, UN/LOCODE stripping, weight/count numeric extraction).
   Writing-style variations are accepted; value differences are flagged.
   A field that cannot be extracted escalates to `NEEDS_REVIEW` rather than
   fabricating an `OK`.

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
server.py               FastAPI backend (~20 endpoints: verify, queue,
                        resolutions, chasers, audit, pipeline run, scoring)
pipeline/
  main.py               Batch pipeline -> writes submission.json
  ai_engine.py          LLM client (NVIDIA NIM), classify/extract/reason
  comparator.py         7-field normalization + comparison logic
  edge_cases.py         Attachment/doc-type/unreadable checks
  parsers.py            Text extraction for txt/pdf/xlsx/docx
frontend/               React 19 + Vite UI (dashboard, queue, verify,
                        paper scan, audit, stress lab)
sdoc-hackathon-bundle/  Dataset: inbox/ (520 emails) + attachments/
sdoc-hackathon-docker/  Organizers' bundle: docker-compose.yml, server/,
                        data_v2/ dataset (its ground_truth.json and
                        generator sources are gitignored — kept local only)
tests/
  generate_synthetic_dataset.py   400-case generalization benchmark
  generate_stress_dataset.py      2,020-case edge/stress suite ->
                                  tests/stress_dataset/ (ground truth
                                  in stress_ground_truth.json)
  eval_synthetic_benchmark.py     CLI scorer; accepts a dataset dir arg
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

# Optional — model selection (defaults to meta/llama-3.2-11b-vision-instruct)
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

# Optional — Supabase (schema in supabase_schema.sql; not required to run)
SUPABASE_URL=...
SUPABASE_ANON_KEY=...
SUPABASE_SERVICE_ROLE_KEY=...
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
| `POST /api/verify`, `GET /api/compare` | Run/inspect verification |
| `POST /api/verify/scan` | Paper mode: multipart upload of SI + BL photos/scans (`si_file`, `bl_file`) — vision extraction + same 7-field audit |
| `GET /api/queue`, `/api/stats` | Work queue + dashboard stats |
| `POST /api/resolve`, `GET /api/resolutions` | Human resolution workflow |
| `GET /api/corrupted`, `POST /api/corrupted/resolve` | Corrupted-attachment handling |
| `GET /api/missing-bills`, `POST /api/missing-bills/chase`, `/batch-chase` | Chase missing BLs |
| `GET /api/audit` | Audit trail of all decisions |
| `POST /api/pipeline/run`, `/cancel`, `GET /status`, `/submission` | Batch pipeline control |
| `GET /api/stress/dataset`, `/cases` | Stress dataset info + case browser |
| `POST /api/stress/run`, `/cancel`, `GET /status`, `/results` | Batch stress test over `tests/stress_dataset` |
| `POST /api/stress/run-one` | Run a single stress case vs ground truth |
| `GET /api/config` | Runtime config |

## Tech stack

- **Backend:** Python, FastAPI, Uvicorn, OpenAI SDK (NVIDIA NIM), pdfplumber,
  openpyxl, python-docx, pydantic
- **Frontend:** React 19, Vite, oxlint
- **Database:** Supabase (managed Postgres) — schema in `supabase_schema.sql`
- **AI:** NVIDIA NIM vision LLM (swappable via `AI_MODEL` / `KIMI_MODEL` /
  `MUSE_MODEL`)

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
