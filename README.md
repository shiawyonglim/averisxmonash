# Averis x Monash Hackathon — Shipping Document Verification

An AI-powered pipeline that processes a 520-email shipping inbox, classifies
each email, and — for `BL_COMPARISON` emails — verifies a draft **Bill of
Lading (BL)** against its **Shipping Instruction (SI)** by extracting and
comparing 7 core fields. A React front-end gives human reviewers a dashboard,
a work queue, a side-by-side document view, and an audit trail.

Built for the Averis x Monash Hackathon 2026 (see `docs/` for the info pack,
rules, and the Shipping Document Verification use case).

## How it works

For every email in `sdoc-hackathon-bundle/inbox/`:

1. **Classify** — rule-based + LLM classification into
   `BL_COMPARISON`, `SI_REQUEST`, `INVOICE_QUERY`, `GENERAL`, or `SPAM`.
   Category keywords are matched on the subject only (bodies contain forwarded
   threads that mention BL/docs for unrelated reasons).
2. **Edge cases** — missing/corrupt attachments, unreadable files, and
   wrong-document-type detection (e.g. a commercial invoice sent instead of a
   BL) short-circuit to `NEEDS_REVIEW` with a `review_reason`.
3. **Extract** — an LLM (NVIDIA NIM, `meta/llama-3.2-11b-vision-instruct` by
   default, swappable via env vars) reads `.txt` / `.pdf` / `.xlsx` / `.docx`
   attachments and extracts the 7 fields. Camera photos / scans of paper
   documents (`.png` / `.jpg` / `.jpeg` / `.webp` / `.gif` / `.bmp` / `.tif`)
   go through the same model's vision pass — handwriting, stamps, skew and
   poor lighting included — which also reports document type, legibility and
   a transcription.
4. **Compare** — SI vs BL with per-field normalization (company-name
   punctuation, UN/LOCODE stripping, weight/count numeric extraction).
   Writing-style variations are accepted; value differences are flagged.

**Compared fields:** `shipper`, `consignee`, `notify_party`,
`port_of_loading`, `port_of_discharge`, `container_count`, `gross_weight_kg`

**Statuses:** `OK` (all match) · `MISMATCH` (≥1 field differs →
`defect_fields`) · `NEEDS_REVIEW` (`wrong_doc_type` | `missing_attachment` |
`unreadable` | `missing_value`)

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
