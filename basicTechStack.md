# Tech Stack

## Frontend (Web Screen)
- **Tool:** React with Vite
- **Cloud Host:** Vercel
- **Job:** Gives the human worker a clean screen to see the two shipping files side by side to check for errors. Vercel puts it online instantly.

## Backend (Server Logic)
- **Tool:** Python with FastAPI
- **Cloud Host:** Render
- **Job:** Runs the AI pipeline, handles API requests, and manages database operations. Render keeps this code running on the internet all day.

## AI Engine (The Multi-Model Brain)
- **Primary Vision & Text:** Google Gemini (Gemini 2.5 Flash / Flash-Lite)
- **NVIDIA NIM Multimodal:** Moonshot AI `kimi-k3` (Long-context visual reasoning for complex PDF scans & tables)
- **NVIDIA NIM Fast Inference:** Meta `muse-glimmer-30b` (High-speed document entity extraction & discrepancy reasoning)
- **Job:** Reads messy PDF scans, Excel tables, Word docs, and plain text; extracts the seven shipping fields; classifies emails into categories; and compares SI vs BL with strict discrepancy verification.

## Database (Data Storage)
- **Tool:** Supabase (managed PostgreSQL)
- **Cloud Host:** Supabase Cloud (free tier)
- **Job:** Stores email processing results, comparison outcomes, audit trail of all verifications, and user review decisions. Provides a REST API out of the box.

## Document Parsing (File Readers)
- **Tools:**
  - `pdfplumber` — reads PDF attachments
  - `openpyxl` — reads Excel (.xlsx) attachments
  - `python-docx` — reads Word (.docx) attachments
- **Job:** Extracts text and structured data from the four attachment formats (`.txt`, `.pdf`, `.xlsx`, `.docx`) so the AI engine can process them.

## Background Processing
- **Tool:** FastAPI async tasks (or Celery + Redis for scale)
- **Job:** Processes the 520 emails without blocking the UI. Allows the frontend to show real-time progress as emails are classified and compared.