-- ============================================================
-- Averis x Monash Hackathon 2026 - Supabase Database Schema
-- Shipping Document Verification & Audit System
-- ============================================================

-- 1. Verifications Table (Stores all 520 classified and verified records)
CREATE TABLE IF NOT EXISTS public.verifications (
    id TEXT PRIMARY KEY,                       -- e.g. 'email_001'
    subject TEXT,                              -- Email subject line
    sender TEXT,                               -- Sender email address
    category TEXT NOT NULL,                    -- BL_COMPARISON, SI_REQUEST, INVOICE_QUERY, GENERAL, SPAM
    status TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('OK','MISMATCH','NEEDS_REVIEW','RESOLVED','PENDING')),  -- OK | MISMATCH | NEEDS_REVIEW | RESOLVED | PENDING (RESOLVED = human-resolved; PENDING = not yet verified)
    review_reason TEXT,                        -- wrong_doc_type, missing_attachment, unreadable, missing_value
    defect_fields TEXT[] DEFAULT '{}',         -- Array of mismatched fields, e.g. {'gross_weight_kg', 'consignee'}
    has_discrepancy BOOLEAN DEFAULT FALSE,
    si_data JSONB DEFAULT '{}'::jsonb,         -- Extracted SI fields (shipper, consignee, ports, weights...)
    bl_data JSONB DEFAULT '{}'::jsonb,         -- Extracted BL fields
    discrepancies JSONB DEFAULT '{}'::jsonb,   -- Field-level comparison diffs
    human_verdict TEXT,                        -- APPROVED, REJECTED, RESOLVED
    human_notes TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- 2. Audit Logs Table (Tracks human reviewer decisions for compliance)
CREATE TABLE IF NOT EXISTS public.audit_logs (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    email_id TEXT REFERENCES public.verifications(id) ON DELETE CASCADE,
    action TEXT NOT NULL,                      -- e.g. 'REVIEW_APPROVED', 'OVERRIDE_DEFECT', 'STATUS_CHANGE'
    details JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 3. Row Level Security (Enable public read/write for hackathon demo)
ALTER TABLE public.verifications ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.audit_logs ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Allow public read access on verifications" 
ON public.verifications FOR SELECT USING (true);

CREATE POLICY "Allow public insert on verifications" 
ON public.verifications FOR INSERT WITH CHECK (true);

CREATE POLICY "Allow public update on verifications" 
ON public.verifications FOR UPDATE USING (true);

-- NOTE: No public DELETE policy is granted - DELETE requires the service role.

CREATE POLICY "Allow public read access on audit_logs" 
ON public.audit_logs FOR SELECT USING (true);

CREATE POLICY "Allow public insert on audit_logs" 
ON public.audit_logs FOR INSERT WITH CHECK (true);

-- Enable real-time updates for frontend live dashboard
ALTER PUBLICATION supabase_realtime ADD TABLE public.verifications;
