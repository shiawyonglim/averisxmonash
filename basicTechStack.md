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
- **Tool:** Google Gemini (Vision LLM)
- **Job:** Reads messy PDF scans and structured documents, extracts the seven shipping fields, classifies emails into categories, and compares SI vs BL for mismatches.

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