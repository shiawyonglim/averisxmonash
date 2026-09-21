# Tech Stack

## Frontend (Web Screen)
- **Tool:** React with Vite
- **Cloud Host:** Vercel
- **Job:** Gives the human worker a clean screen to see the two shipping files side by side to check for errors. Vercel puts it online instantly.

## Backend (Server Logic)
- **Tool:** Python with FastAPI
- **Cloud Host:** Render
- **Job:** Runs the AI pipeline, handles API requests, and manages database operations. Render keeps this code running on the internet all day.

## AI Engine (The Brain)
- **Current implementation:** NVIDIA NIM models — `z-ai/glm-5.3` (GLM 5.3) and `meta/muse-glimmer-30b` (Muse Glimmer 30B) — handle all three AI tasks: email classification, seven-field document extraction, and discrepancy reasoning.
- **Model is swappable:** select via the `AI_MODEL`, `KIMI_MODEL`, or `MUSE_MODEL` env vars, each optionally with its own scoped API key (`NVIDIA_KIMI_API_KEY`, `NVIDIA_MUSE_API_KEY`). Code fallback when unset: `meta/muse-glimmer-30b`.
- **Job:** Reads messy PDF scans, Excel tables, Word docs, and plain text; extracts the seven shipping fields; classifies emails into categories; and compares SI vs BL with strict discrepancy verification.

## Database (Data Storage)
- **Tool:** Supabase (managed PostgreSQL)
- **Cloud Host:** Supabase Cloud (free tier)
- **Job:** Stores email processing results, comparison outcomes, audit trail of all verifications, and user review decisions. (Not yet wired up — frontend and backend are being tested against the local bundle first.) Provides a REST API out of the box.

## Document Parsing (File Readers)
- **Tools:**
  - `pdfplumber` — reads PDF attachments
  - `openpyxl` — reads Excel (.xlsx) attachments
  - `python-docx` — reads Word (.docx) attachments
- **Job:** Extracts text and structured data from the four attachment formats (`.txt`, `.pdf`, `.xlsx`, `.docx`) so the AI engine can process them.

## Background Processing
- **Tool:** FastAPI async tasks (or Celery + Redis for scale)
- **Job:** Processes the 520 emails without blocking the UI. Allows the frontend to show real-time progress as emails are classified and compared.