import { useState, useEffect, useRef, useCallback } from 'react'
import './App.css'

const API = import.meta.env?.VITE_API_URL || 'http://localhost:8000'
const QUEUE_LIMIT = 50

const FIELDS = [
  'shipper', 'consignee', 'notify_party',
  'port_of_loading', 'port_of_discharge',
  'container_count', 'gross_weight_kg',
]

const QUEUE_FILTERS = [
  { key: 'all', label: 'All' },
  { key: 'mismatch', label: 'Mismatch' },
  { key: 'needs_review', label: 'Needs Review' },
  { key: 'missing_bl', label: 'Missing BL' },
  { key: 'corrupted', label: 'Corrupted' },
  { key: 'resolved', label: 'Resolved' },
  { key: 'chaser_sent', label: 'Chaser Sent' },
]

const AUDIT_DOT_COLORS = {
  AUTO_VERIFIED: '#28a745',
  RESOLVED: '#0d6efd',
  CARRIER_RE_REQUESTED: '#6c757d',
  MANUAL_OVERRIDE: '#fd7e14',
  CHASER_DISPATCHED: '#6f42c1',
}

const EMAIL_STATUS_META = {
  OK:           { label: 'OK',           color: '#2e7d32', bg: '#e8f5e9' },
  MISMATCH:     { label: 'Mismatch',     color: '#c62828', bg: '#ffebee' },
  NEEDS_REVIEW: { label: 'Needs review', color: '#b78103', bg: '#fff8e1' },
  RESOLVED:     { label: 'Resolved',     color: '#1a73e8', bg: '#e8f0fe' },
  CORRUPTED:    { label: 'Corrupted',    color: '#8e24aa', bg: '#f3e5f5' },
  MISSING_BL:   { label: 'Missing BL',   color: '#e07a5f', bg: '#fdeee7' },
  UNVERIFIED:   { label: 'Unverified',   color: '#777777', bg: '#f1f1f1' },
}

const emailStatusMeta = s => EMAIL_STATUS_META[s] || EMAIL_STATUS_META.UNVERIFIED


function BarRow({ label, value, max }) {
  const pct = max > 0 ? Math.round((value / max) * 100) : 0
  return (
    <div className="bar-row">
      <span className="bar-label">{label}</span>
      <div className="bar-track">
        <div className="bar-fill" style={{ width: `${pct}%` }} />
      </div>
      <span className="bar-count">{value}</span>
    </div>
  )
}

function FormattedChatContent({ content, onOpenEmail }) {
  if (!content) return null

  const renderInline = (text) => {
    const emailRegex = /\b(email_\d{3}|synth_[a-z]+_\d{3})\b/g
    const parts = text.split(emailRegex)
    return parts.map((part, idx) => {
      if (part.match(/^(email_\d{3}|synth_[a-z]+_\d{3})$/)) {
        return (
          <span
            key={idx}
            className="chat-email-pill"
            onClick={() => onOpenEmail(part)}
            title={`Click to open ${part} in Verification Hub`}
          >
            📧 {part}
          </span>
        )
      }
      const boldParts = part.split(/(\*\*[^*]+\*\*)/g)
      return boldParts.map((bp, bidx) => {
        if (bp.startsWith('**') && bp.endsWith('**')) {
          return <strong key={`${idx}-${bidx}`}>{bp.slice(2, -2)}</strong>
        }
        return bp
      })
    })
  }

  if (content.includes('```')) {
    const segments = content.split(/(```[\s\S]*?```)/g)
    return (
      <div className="formatted-chat-body">
        {segments.map((seg, sIdx) => {
          if (seg.startsWith('```') && seg.endsWith('```')) {
            const raw = seg.slice(3, -3)
            const firstLineBreak = raw.indexOf('\n')
            const lang = firstLineBreak > 0 ? raw.slice(0, firstLineBreak).trim() : ''
            const code = firstLineBreak > 0 ? raw.slice(firstLineBreak + 1) : raw
            return (
              <pre key={sIdx} className="chat-code-block">
                {lang && <div className="code-lang-tag">{lang}</div>}
                <code>{code}</code>
              </pre>
            )
          }
          return <div key={sIdx} style={{ margin: '4px 0' }}>{renderInline(seg)}</div>
        })}
      </div>
    )
  }

  const lines = content.split('\n')
  return (
    <div className="formatted-chat-body">
      {lines.map((line, lIdx) => {
        if (line.trim().startsWith('- ') || line.trim().startsWith('* ')) {
          return (
            <div key={lIdx} className="chat-bullet-line">
              <span className="bullet-dot">•</span>
              <span>{renderInline(line.replace(/^[-*]\s*/, ''))}</span>
            </div>
          )
        }
        return (
          <div key={lIdx} style={{ minHeight: line ? undefined : '8px', margin: '2px 0' }}>
            {renderInline(line)}
          </div>
        )
      })}
    </div>
  )
}

function VerdictPanel({ result, loading, error, verdictSource, idleHint, loadingHint }) {
  return (
    <div className="result-panel">
      {error && (
        <div style={{ color: '#721c24', background: '#f8d7da', padding: '10px', borderRadius: '6px', marginBottom: '14px' }}>
          <strong>Error:</strong> {error}
        </div>
      )}

      {!result && !loading && !error && (
        <div style={{ opacity: 0.6, textAlign: 'center', marginTop: '120px' }}>
          {idleHint || 'Select a bill above or browse the work queue to run AI extraction and comparison.'}
        </div>
      )}

      {loading && (
        <div style={{ textAlign: 'center', marginTop: '120px' }}>
          <div style={{ fontSize: '2.5rem' }}>🧠</div>
          <p style={{ marginTop: '12px', fontWeight: 'bold' }}>{loadingHint || 'Auditing with NVIDIA AI...'}</p>
          <p style={{ fontSize: '0.85rem', opacity: 0.7 }}>Normalizing abbreviations & checking 7 core fields</p>
        </div>
      )}

      {result && (
        <>
          <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '14px', flexWrap: 'wrap' }}>
            <div className={`status-badge status-${result.status.toLowerCase().replace(/_/g, '-')}`}>
              Status: {result.status}
            </div>
            {verdictSource && (
              <span style={{ fontSize: '0.78rem', padding: '2px 8px', borderRadius: '10px', background: verdictSource === 'stored' ? '#e8f0fe' : '#f0f4e8', color: verdictSource === 'stored' ? '#1a73e8' : 'var(--primary-color)', fontWeight: 'bold' }}>
                {verdictSource === 'stored' ? 'Stored verdict' : 'Fresh run'}
              </span>
            )}
            {result.review_reason && (
              <span style={{ fontSize: '0.85rem', color: '#856404', fontWeight: 'bold' }}>
                ({result.review_reason})
              </span>
            )}
          </div>

          {/* AI Thought Process Box */}
          {result.thoughts && (
            <div style={{
              background: '#fcf8f2',
              borderLeft: '4px solid var(--primary-color)',
              padding: '12px 16px',
              borderRadius: '6px',
              marginBottom: '16px',
              fontSize: '0.9rem',
              color: 'var(--text-color)',
            }}>
              <strong style={{ display: 'block', marginBottom: '4px', color: 'var(--primary-color)' }}>
                🧠 AI Thought Process:
              </strong>
              {result.thoughts}
            </div>
          )}

          {/* Summary Verdict */}
          {result.summary_reason && (
            <div style={{
              background: result.status === 'OK' ? 'rgba(89, 121, 40, 0.08)' : 'rgba(220, 53, 69, 0.08)',
              padding: '10px 14px',
              borderRadius: '6px',
              marginBottom: '16px',
              fontSize: '0.9rem',
              fontWeight: 500,
            }}>
              <strong>Verdict:</strong> {result.summary_reason}
            </div>
          )}

          {/* Field Breakdown */}
          {result.si_fields && Object.keys(result.si_fields).length > 0 && (
            <div>
              <h3 style={{ fontSize: '0.95rem', color: 'var(--primary-color)', marginBottom: '10px' }}>
                Field Audit ({result.defect_fields?.length || 0} defects)
              </h3>
              <div>
                {FIELDS.map(f => {
                  const isMismatch = result.defect_fields?.includes(f)
                  const fieldComp = result.field_comparisons?.[f]
                  return (
                    <div key={f} className={`field-comparison ${isMismatch ? 'mismatch' : ''}`}>
                      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                        <h4>{f.replace(/_/g, ' ')}</h4>
                        {isMismatch ? (
                          <span className="diff-chip diff-chip-defect">🔴 Material Defect</span>
                        ) : (fieldComp?.reason?.toLowerCase().includes('variation accepted') || fieldComp?.reason?.toLowerCase().includes('writing style')) ? (
                          <span className="diff-chip diff-chip-style">🟡 Style Match</span>
                        ) : (
                          <span className="diff-chip diff-chip-match">🟢 Exact Match</span>
                        )}
                      </div>

                      <div className="field-val"><span>SI:</span> {result.si_fields[f] || 'N/A'}</div>
                      <div className="field-val"><span>BL:</span> {result.bl_fields?.[f] || 'N/A'}</div>

                      {fieldComp?.reason && (
                        <div style={{
                          fontSize: '0.8rem',
                          marginTop: '4px',
                          color: isMismatch ? '#dc3545' : '#597928',
                          fontStyle: 'italic',
                        }}>
                          ℹ️ {fieldComp.reason}
                        </div>
                      )}
                    </div>
                  )
                })}
              </div>
            </div>
          )}
        </>
      )}
    </div>
  )
}

function ScanUploadCard({ title, file, preview, transcript, onSelect, onClear }) {
  return (
    <section className="doc-section glass-panel">
      <h2>{title}</h2>
      <div className="scan-upload">
        {preview ? (
          <img src={preview} alt={`${title} preview`} className="scan-preview" />
        ) : file ? (
          <div className="scan-filechip">📄 {file.name}</div>
        ) : (
          <div className="scan-empty">📷<br />No document yet — photograph or upload a paper {title.includes('BL') ? 'BL' : 'SI'}</div>
        )}
        <div className="scan-actions">
          <label className="scan-choose">
            📷 Choose Photo / File
            <input
              type="file"
              accept="image/*,.pdf,.docx,.xlsx,.txt"
              capture="environment"
              onChange={(e) => onSelect(e.target.files?.[0] || null)}
              hidden
            />
          </label>
          {file && (
            <button type="button" onClick={onClear} className="scan-clear">
              Clear
            </button>
          )}
        </div>
        {transcript && (
          <details className="scan-transcript">
            <summary>Transcription</summary>
            <pre>{transcript}</pre>
          </details>
        )}
      </div>
    </section>
  )
}

function detectCarrier(email) {
  if (!email) return 'Ocean Carrier Desk'
  const text = `${email.from || ''} ${email.subject || ''} ${email.body || ''}`.toLowerCase()
  if (text.includes('msc') || text.includes('mediterranean')) return 'Mediterranean Shipping Company (MSC)'
  if (text.includes('maersk') || text.includes('apm-terminals') || text.includes('sealand')) return 'Maersk Line (A.P. Moller)'
  if (text.includes('cma') || text.includes('cgm')) return 'CMA CGM Group'
  if (text.includes('hapag') || text.includes('hlcu')) return 'Hapag-Lloyd AG'
  if (text.includes('cosco') || text.includes('oocl')) return 'COSCO Shipping Lines'
  if (text.includes('ocean network') || text.includes('one(') || text.includes('one-line')) return 'Ocean Network Express (ONE)'
  if (text.includes('evergreen') || text.includes('ever(')) return 'Evergreen Marine'
  if (text.includes('yang ming') || text.includes('ym(')) return 'Yang Ming Marine Transport'
  if (text.includes('pil') || text.includes('pacific int')) return 'Pacific International Lines (PIL)'
  return 'Liner Operations Desk'
}

function CutoffProgressBar({ stats, onFilter }) {
  if (!stats) return null
  const total = stats.emails_total || stats.total_emails || 520
  const okCount = stats.status_counts?.OK || stats.match_count || 0
  const resolvedCount = stats.status_counts?.RESOLVED || stats.resolved_count || stats.resolutions || 0
  const cleared = okCount + resolvedCount
  const mismatchCount = stats.status_counts?.MISMATCH || stats.mismatch_count || 0
  const pendingMismatch = Math.max(0, mismatchCount - resolvedCount)
  const missingBL = stats.missing_bl_total || stats.missing_bl_count || 0
  const corrupted = stats.corrupted_total || stats.corrupted_count || 0
  const pct = Math.min(100, Math.round((cleared / (total || 1)) * 100))

  return (
    <div className="progress-card">
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: '8px' }}>
        <div>
          <span style={{ fontSize: '0.78rem', textTransform: 'uppercase', letterSpacing: '0.05em', color: '#888', fontWeight: 'bold' }}>
            DAILY SHIPPING CUTOFF MONITOR · TARGET: 17:00 SGT
          </span>
          <h3 style={{ margin: '2px 0 0', color: 'var(--primary-color)', fontSize: '1.1rem' }}>
            Port Documentation Clearance Velocity ({pct}% Cleared)
          </h3>
        </div>
        <div style={{ display: 'flex', gap: '10px', alignItems: 'center', flexWrap: 'wrap' }}>
          <span
            onClick={() => onFilter?.('resolved')}
            style={{ cursor: 'pointer', fontSize: '0.82rem', padding: '4px 10px', background: '#f0f4e8', color: 'var(--primary-color)', borderRadius: '6px', fontWeight: 600 }}
            title="View cleared shipments"
          >
            ✅ {cleared} Cleared
          </span>
          <span
            onClick={() => onFilter?.('mismatch')}
            style={{ cursor: 'pointer', fontSize: '0.82rem', padding: '4px 10px', background: '#fdf0ed', color: '#c0392b', borderRadius: '6px', fontWeight: 600 }}
            title="View pending discrepancy queue"
          >
            🔴 {pendingMismatch} Defect Queue
          </span>
          <span
            onClick={() => onFilter?.('missing_bl')}
            style={{ cursor: 'pointer', fontSize: '0.82rem', padding: '4px 10px', background: '#fcf4e6', color: '#d35400', borderRadius: '6px', fontWeight: 600 }}
            title="View missing bills awaiting carrier draft"
          >
            ⏳ {missingBL} Missing Draft BL
          </span>
          <span
            onClick={() => onFilter?.('corrupted')}
            style={{ cursor: 'pointer', fontSize: '0.82rem', padding: '4px 10px', background: '#fdeeed', color: '#c0392b', borderRadius: '6px', fontWeight: 600 }}
            title="View corrupted documents"
          >
            ⚠️ {corrupted} Corrupted
          </span>
        </div>
      </div>

      <div className="progress-bar-bg">
        <div className="progress-bar-fill" style={{ width: `${pct}%` }}></div>
      </div>
    </div>
  )
}

function AutoDraftEmailModal({ open, onClose, draft, onChange, onSend, sending, result, smtpConfig, onSmtpChange, showSmtp, onToggleSmtp }) {
  if (!open) return null
  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-container" onClick={e => e.stopPropagation()}>
        <div className="modal-header">
          <h3>✉️ Auto-Draft & Google SMTP Studio · {draft.email_id}</h3>
          <button className="modal-close-btn" onClick={onClose}>&times;</button>
        </div>
        <div className="modal-body">
          {result && (
            <div style={{
              padding: '12px 16px',
              borderRadius: '8px',
              background: result.status === 'LIVE_SENT' || result.status === 'SIMULATED_SENT' ? '#d4edda' : '#f8d7da',
              color: result.status === 'LIVE_SENT' || result.status === 'SIMULATED_SENT' ? '#155724' : '#721c24',
              border: `1px solid ${result.status === 'LIVE_SENT' || result.status === 'SIMULATED_SENT' ? '#c3e6cb' : '#f5c6cb'}`,
              fontSize: '0.88rem'
            }}>
              <strong>{result.status === 'LIVE_SENT' ? '🚀 Delivered Live:' : (result.status === 'SIMULATED_SENT' ? '⚡ Dispatched (Simulated):' : '⚠️ Alert:')}</strong>{' '}
              {result.message}
            </div>
          )}

          <div className="email-form-group">
            <label>Recipient (To):</label>
            <input
              type="text"
              className="email-input"
              value={draft.to}
              onChange={e => onChange({ ...draft, to: e.target.value })}
              placeholder="carrier-desk@shippingline.com"
            />
          </div>

          <div className="email-form-group">
            <label>Subject Line:</label>
            <input
              type="text"
              className="email-input"
              value={draft.subject}
              onChange={e => onChange({ ...draft, subject: e.target.value })}
            />
          </div>

          <div className="email-form-group">
            <label>Auto-Generated Operational Body (Editable):</label>
            <textarea
              className="email-textarea"
              value={draft.body}
              onChange={e => onChange({ ...draft, body: e.target.value })}
            />
          </div>

          <div className="smtp-accordion">
            <div className="smtp-accordion-header" onClick={onToggleSmtp}>
              <span>⚙️ Google SMTP Relay Settings {showSmtp ? '▲' : '▼'}</span>
              <span style={{ fontSize: '0.78rem', color: '#888' }}>
                {smtpConfig.user ? `Relay: ${smtpConfig.user}` : 'Default: Google SMTP Pipeline Ready'}
              </span>
            </div>
            {showSmtp && (
              <div className="smtp-accordion-content">
                <div className="email-form-group">
                  <label>SMTP Host</label>
                  <input
                    type="text"
                    className="email-input"
                    value={smtpConfig.host}
                    onChange={e => onSmtpChange({ ...smtpConfig, host: e.target.value })}
                    placeholder="smtp.gmail.com"
                  />
                </div>
                <div className="email-form-group">
                  <label>SMTP Port</label>
                  <input
                    type="number"
                    className="email-input"
                    value={smtpConfig.port}
                    onChange={e => onSmtpChange({ ...smtpConfig, port: e.target.value })}
                    placeholder="587"
                  />
                </div>
                <div className="email-form-group">
                  <label>Google Account (Gmail)</label>
                  <input
                    type="email"
                    className="email-input"
                    value={smtpConfig.user}
                    onChange={e => onSmtpChange({ ...smtpConfig, user: e.target.value })}
                    placeholder="your-email@gmail.com"
                  />
                </div>
                <div className="email-form-group">
                  <label>Google App Password (16-char)</label>
                  <input
                    type="password"
                    className="email-input"
                    value={smtpConfig.password}
                    onChange={e => onSmtpChange({ ...smtpConfig, password: e.target.value })}
                    placeholder="xxxx xxxx xxxx xxxx"
                  />
                </div>
              </div>
            )}
          </div>
        </div>
        <div className="modal-footer">
          <button
            onClick={onClose}
            style={{ background: 'transparent', border: '1px solid var(--border-color)', color: 'var(--text-color)', boxShadow: 'none', padding: '10px 18px', fontSize: '0.88rem' }}
          >
            Close
          </button>
          <button
            className="btn-primary-next"
            onClick={onSend}
            disabled={sending || !draft.to || !draft.subject}
          >
            {sending ? 'Dispatching via Google SMTP...' : '🚀 Send via Google SMTP'}
          </button>
        </div>
      </div>
    </div>
  )
}

function App() {
  // ---- Shared / Verify Documents ----
  const [emails, setEmails] = useState([])
  const [selectedEmail, setSelectedEmail] = useState('')
  const [emailInfo, setEmailInfo] = useState(null)
  const [siText, setSiText] = useState('')
  const [blText, setBlText] = useState('')
  const [loading, setLoading] = useState(false)
  const [result, setResult] = useState(null)
  const [verdictSource, setVerdictSource] = useState(null) // 'stored' | 'fresh'
  const [error, setError] = useState(null)
  const [corruptWarning, setCorruptWarning] = useState(null)

  // ---- Email picker (status-aware dropdown) ----
  const [emailStatusMap, setEmailStatusMap] = useState({})
  const [pickerOpen, setPickerOpen] = useState(false)
  const [pickerSearch, setPickerSearch] = useState('')
  const pickerRef = useRef(null)

  // ---- Inline Resolution ----
  const [resolutions, setResolutions] = useState({})
  const [resolutionNotes, setResolutionNotes] = useState('')
  const [resolveSuccess, setResolveSuccess] = useState(null)
  const [savingResolution, setSavingResolution] = useState(false)
  const [resolutionRecord, setResolutionRecord] = useState(null) // stored RESOLVED record for selected email

  // ---- Auto-Draft Email & Google SMTP Studio ----
  const [emailModalOpen, setEmailModalOpen] = useState(false)
  const [emailDraft, setEmailDraft] = useState({ email_id: '', to: '', subject: '', body: '', status: '' })
  const [sendingEmail, setSendingEmail] = useState(false)
  const [emailSendResult, setEmailSendResult] = useState(null)
  const [smtpConfig, setSmtpConfig] = useState({ host: 'smtp.gmail.com', port: 587, user: '', password: '' })
  const [showSmtpSettings, setShowSmtpSettings] = useState(false)

  // ---- Supabase Shared Records ----
  const [cloudRecords, setCloudRecords] = useState([])
  const [cloudStats, setCloudStats] = useState({ connected: false, total_in_supabase: 0, synced_count: 0, pending_count: 0 })
  const [cloudFilter, setCloudFilter] = useState('all')
  const [cloudSearch, setCloudSearch] = useState('')
  const [cloudLoading, setCloudLoading] = useState(false)
  const [cloudSyncingAll, setCloudSyncingAll] = useState(false)
  const [cloudSyncMsg, setCloudSyncMsg] = useState(null)
  const [cloudPage, setCloudPage] = useState(1)
  const [cloudSyncInfo, setCloudSyncInfo] = useState(null)

  // ---- Navigation: 'dashboard' | 'chat' | 'queue' | 'verify' | 'cloud' | 'scan' | 'audit' | 'pipeline' ----
  const [view, setView] = useState('dashboard')

  // ---- Chat Assistant ----
  const [chatMessages, setChatMessages] = useState([])
  const [chatInput, setChatInput] = useState('')
  const [chatLoading, setChatLoading] = useState(false)
  const [chatSessionId, setChatSessionId] = useState('')
  const chatEndRef = useRef(null)

  // ---- Dashboard ----
  const [stats, setStats] = useState(null)

  // ---- Inbox ----
  const [queueData, setQueueData] = useState(null)
  const [queueFilter, setQueueFilter] = useState('all')
  const [queueSearch, setQueueSearch] = useState('')
  const [queuePage, setQueuePage] = useState(1)
  const [loadingQueue, setLoadingQueue] = useState(false)
  const [queueMsg, setQueueMsg] = useState(null)
  const [focusedCorruptRow, setFocusedCorruptRow] = useState(null)
  const [corruptActionNotes, setCorruptActionNotes] = useState('')
  const queueReqId = useRef(0)
  const autoSavedRef = useRef(false)

  // ---- Audit Log ----
  const [auditEvents, setAuditEvents] = useState([])

  // ---- Pipeline Run ----
  const [maxEmails, setMaxEmails] = useState(0)
  const [freshRun, setFreshRun] = useState(false)
  const [pipelineStatus, setPipelineStatus] = useState(null)
  const [pipelineRunning, setPipelineRunning] = useState(false)
  const [pipelineMsg, setPipelineMsg] = useState(null)

  // ---- Chaser actions (Inbox, missing_bl filter) ----
  const [batchChasing, setBatchChasing] = useState(false)

  // ---- Score vs Ground Truth (Pipeline Run page) ----
  const [compareData, setCompareData] = useState(null)
  const [compareLoading, setCompareLoading] = useState(false)
  const [compareError, setCompareError] = useState(null)
  const [diffFilter, setDiffFilter] = useState('')
  const [showAllDiffs, setShowAllDiffs] = useState(false)

  // ---- Review & Download (submission export picker) ----
  const [submissionData, setSubmissionData] = useState(null)
  const [selectedEmails, setSelectedEmails] = useState(() => new Set())
  const [subLoading, setSubLoading] = useState(false)
  const [subSearch, setSubSearch] = useState('')
  const [subFilter, setSubFilter] = useState('all') // 'all' | 'issues' | 'ok'
  const [showAllSub, setShowAllSub] = useState(false)
  const [exportedOnce, setExportedOnce] = useState(false)

  // ---- Backend AI config (sidebar footer) ----
  const [aiConfig, setAiConfig] = useState(null)

  // ---- Paper Scan (camera photos / handwritten docs) ----
  const [scanSi, setScanSi] = useState(null) // { file, preview }
  const [scanBl, setScanBl] = useState(null)
  const [scanResult, setScanResult] = useState(null)
  const [scanLoading, setScanLoading] = useState(false)
  const [scanError, setScanError] = useState(null)

  // ---- Queue Adjacent Navigation ----
  const [adjacentInfo, setAdjacentInfo] = useState(null)

  const fetchAdjacent = useCallback(async (eid, filter = 'all') => {
    if (!eid) return
    try {
      const res = await fetch(`${API}/api/queue/adjacent?email_id=${eid}&filter=${filter}`)
      if (res.ok) {
        const data = await res.json()
        setAdjacentInfo(data)
      }
    } catch (err) {
      console.error('Failed to fetch adjacent info:', err)
    }
  }, [])

  const fetchQueue = useCallback(async (filter, search, page) => {
    const reqId = ++queueReqId.current
    setLoadingQueue(true)
    try {
      const res = await fetch(
        `${API}/api/queue?filter=${filter}&search=${encodeURIComponent(search)}&page=${page}&limit=${QUEUE_LIMIT}`
      )
      const data = await res.json()
      if (reqId !== queueReqId.current) return // stale request, discard
      setQueueData(data)
    } catch (err) {
      if (reqId === queueReqId.current) console.error('Failed to fetch queue:', err)
    } finally {
      if (reqId === queueReqId.current) setLoadingQueue(false)
    }
  }, [])

  const refreshQueue = useCallback(() => {
    fetchQueue(queueFilter, queueSearch, queuePage)
  }, [fetchQueue, queueFilter, queueSearch, queuePage])

  // Status map for the email picker — one bulk fetch of queue_status for all emails
  const refreshEmailStatuses = useCallback(async () => {
    try {
      const res = await fetch(`${API}/api/queue?filter=all&page=1&limit=1000`)
      const data = await res.json()
      const map = {}
      ;(data.emails || []).forEach(i => { map[i.email_id] = i.queue_status })
      setEmailStatusMap(map)
    } catch (err) {
      console.error('Failed to fetch email statuses:', err)
    }
  }, [])

  // Email list (verification dropdown) — once on mount
  useEffect(() => {
    fetch(`${API}/api/emails`)
      .then(res => res.json())
      .then(data => {
        if (data.emails) setEmails(data.emails)
      })
      .catch(err => console.error('Failed to fetch emails:', err))

    refreshEmailStatuses()

    fetch(`${API}/api/config`)
      .then(res => (res.ok ? res.json() : null))
      .then(data => setAiConfig(data && data.provider ? data : null))
      .catch(err => console.error('Failed to fetch AI config:', err))
  }, [refreshEmailStatuses])

  // Debounced (300ms) queue fetch — covers mount + filter/search/page changes
  useEffect(() => {
    const t = setTimeout(() => {
      fetchQueue(queueFilter, queueSearch, queuePage)
    }, 300)
    return () => clearTimeout(t)
  }, [queueFilter, queueSearch, queuePage, fetchQueue])

  // Keyboard navigation shortcuts in Verification Hub ([J] Next, [K] Prev, [E] Auto-Draft)
  useEffect(() => {
    if (view !== 'verify') return
    const handleKeyDown = (e) => {
      if (['INPUT', 'TEXTAREA', 'SELECT'].includes(e.target.tagName)) return
      if (e.key === 'ArrowRight' || e.key === 'j' || e.key === 'J') {
        if (adjacentInfo?.next) {
          e.preventDefault()
          openEmail(adjacentInfo.next)
        }
      } else if (e.key === 'ArrowLeft' || e.key === 'k' || e.key === 'K') {
        if (adjacentInfo?.prev) {
          e.preventDefault()
          openEmail(adjacentInfo.prev)
        }
      } else if (e.key === 'e' || e.key === 'E') {
        if (selectedEmail && !emailModalOpen) {
          e.preventDefault()
          openDraftEmail(selectedEmail)
        }
      }
    }
    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [view, adjacentInfo, selectedEmail, emailModalOpen])

  // Dashboard stats — on entering the view
  useEffect(() => {
    if (view !== 'dashboard') return
    fetch(`${API}/api/stats`)
      .then(res => res.json())
      .then(data => setStats(data))
      .catch(err => console.error('Failed to fetch stats:', err))
  }, [view])

  // Verify Documents — refresh per-email statuses when entering the view
  useEffect(() => {
    if (view !== 'verify') return
    refreshEmailStatuses()
  }, [view, refreshEmailStatuses])

  // Email picker — close on outside click / Escape
  useEffect(() => {
    if (!pickerOpen) return undefined
    const onDown = (e) => {
      if (pickerRef.current && !pickerRef.current.contains(e.target)) setPickerOpen(false)
    }
    const onKey = (e) => { if (e.key === 'Escape') setPickerOpen(false) }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [pickerOpen])

  // Audit log — on entering the view
  useEffect(() => {
    if (view !== 'audit') return
    fetch(`${API}/api/audit`)
      .then(res => res.json())
      .then(data => setAuditEvents(data.events || []))
      .catch(err => console.error('Failed to fetch audit log:', err))
  }, [view])

  // Pipeline status snapshot — on entering the view (may already be running)
  useEffect(() => {
    if (view !== 'pipeline') return
    fetch(`${API}/api/pipeline/status`)
      .then(res => res.json())
      .then(data => {
        setPipelineStatus(data)
        setPipelineRunning(Boolean(data.running) && !data.done)
      })
      .catch(err => console.error('Failed to fetch pipeline status:', err))
  }, [view])

  // Submission records for the export table — cached, selectable per-email
  const loadSubmissionRecords = useCallback(async () => {
    setSubLoading(true)
    try {
      const res = await fetch(`${API}/api/pipeline/submission`)
      if (!res.ok) throw new Error(`Server error: ${res.status}`)
      const data = await res.json()
      const sub = data.submission ?? data
      setSubmissionData(sub)
      setSelectedEmails(new Set(Object.keys(sub)))
    } catch (err) {
      setPipelineMsg(`Could not load submission records: ${err.message}`)
    } finally {
      setSubLoading(false)
    }
  }, [])

  // Pipeline polling — every 2s while running, cleaned up on stop/unmount
  useEffect(() => {
    if (!pipelineRunning) return undefined
    const iv = setInterval(() => {
      fetch(`${API}/api/pipeline/status`)
        .then(res => res.json())
        .then(data => {
          setPipelineStatus(data)
          if (data.done || !data.running) {
            setPipelineRunning(false)
            if (data.done && !autoSavedRef.current) {
              autoSavedRef.current = true
              handleDownloadSubmission()
              loadSubmissionRecords()
              setPipelineMsg(
                `Run complete — submission.json saved to your downloads.` +
                (data.supabase ? ` Supabase: ${data.supabase}.` : '')
              )
            }
          }
        })
        .catch(err => console.error('Pipeline status poll failed:', err))
    }, 2000)
    return () => clearInterval(iv)
  }, [pipelineRunning, loadSubmissionRecords])

  // Score-vs-ground-truth fetcher — called on demand from the Pipeline Run page
  const fetchCompare = useCallback(async () => {
    setCompareLoading(true)
    setCompareError(null)
    try {
      const res = await fetch(`${API}/api/compare`)
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail || `Server error: ${res.status}`)
      setCompareData(data)
    } catch (err) {
      setCompareError(err.message)
    } finally {
      setCompareLoading(false)
    }
  }, [])

  // Auto-load the export table once a submission exists on disk
  useEffect(() => {
    if (view !== 'pipeline') return
    if ((pipelineStatus?.submission_size ?? 0) > 0 && !submissionData && !subLoading) {
      loadSubmissionRecords()
    }
  }, [view, pipelineStatus?.submission_size, submissionData, subLoading, loadSubmissionRecords])

  // ============================================================
  // AUTO-DRAFT EMAIL & GOOGLE SMTP HANDLERS
  // ============================================================

  const openDraftEmail = (emailId, info = null, verdict = null) => {
    const curEmail = emailId || selectedEmail
    const curInfo = info || emailInfo
    const curVerdict = verdict || result
    const carrier = detectCarrier(curInfo)
    const to = curInfo?.from || 'carrier-desk@shippingline.com'
    const subjectPrefix = curVerdict?.status === 'MISMATCH'
      ? 'URGENT: Discrepancy Notice & Draft BL Amendment'
      : (curInfo?.attachments?.length === 0 ? 'URGENT CHASER: Missing Draft Bill of Lading' : 'Documentation Clearance Notice')
    const subject = `${subjectPrefix} — ${curInfo?.subject || curEmail} [Ref: ${curEmail}]`

    let body = ''
    if (curVerdict?.status === 'MISMATCH' && curVerdict?.defect_fields?.length > 0) {
      const defectsList = curVerdict.defect_fields.map((f, i) => {
        const siVal = curVerdict.si_fields?.[f] || 'N/A'
        const blVal = curVerdict.bl_fields?.[f] || 'N/A'
        return `  ${i + 1}. ${f.replace(/_/g, ' ').toUpperCase()}:\n     - Shipper Instruction (SI): "${siVal}"\n     - Draft Bill of Lading (BL): "${blVal}"`
      }).join('\n\n')

      body = `Dear ${carrier} Operations Desk,\n\nDuring automated documentation cross-validation for shipment ref [${curEmail}], our verification engine detected discrepancies between our Shipping Instructions (SI) and your draft Bill of Lading (BL):\n\n${defectsList}\n\nPlease issue an amended draft Bill of Lading reflecting the validated Shipping Instruction values before the port cutoff (17:00 SGT) to avoid terminal loading delays.\n\nShipment Reference: ${curEmail}\nOriginal Subject: ${curInfo?.subject || ''}\n\nKind regards,\nShipping Documentation Operations Desk\nAveris Automated Logistics Pipeline`
    } else if (curInfo?.attachments?.length === 0) {
      body = `Dear ${carrier} Documentation Desk,\n\nWe are following up on our Shipping Instruction submitted for shipment ref [${curEmail}].\n\nThe operational port cutoff (17:00 SGT) is approaching and our system has not yet received the draft Bill of Lading.\n\nPlease urgently furnish the draft BL so our clearance team can complete cross-validation against the shipper instructions.\n\nShipment Reference: ${curEmail}\nBooking Subject: ${curInfo?.subject || ''}\n\nKind regards,\nShipping Documentation Operations Desk\nAveris Automated Logistics Pipeline`
    } else {
      body = `Dear Shipper / Carrier Team,\n\nRegarding shipment ref [${curEmail}], all documentation cross-checks have completed. All 7 critical shipping attributes (shipper, consignee, notify party, ports, container count, and gross weight) have been verified.\n\nShipment Reference: ${curEmail}\nStatus: APPROVED / CLEARED FOR ISSUANCE\n\nKind regards,\nShipping Documentation Operations Desk\nAveris Automated Logistics Pipeline`
    }

    setEmailDraft({
      email_id: curEmail,
      to: to,
      subject: subject,
      body: body,
      status: curVerdict?.status || 'GENERAL'
    })
    setEmailSendResult(null)
    setEmailModalOpen(true)
  }

  const handleSendSmtpEmail = async () => {
    setSendingEmail(true)
    setEmailSendResult(null)
    try {
      const payload = {
        email_id: emailDraft.email_id,
        to_email: emailDraft.to,
        subject: emailDraft.subject,
        body: emailDraft.body,
        smtp_host: smtpConfig.host,
        smtp_port: parseInt(smtpConfig.port) || 587,
        smtp_user: smtpConfig.user,
        smtp_pass: smtpConfig.password
      }
      const res = await fetch(`${API}/api/email/send-smtp`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      })
      if (!res.ok) throw new Error(`HTTP error: ${res.status}`)
      const data = await res.json()
      setEmailSendResult(data)
      refreshQueue()
      fetch(`${API}/api/audit`).then(r => r.json()).then(d => setAuditEvents(d.events || [])).catch(() => {})
    } catch (err) {
      setEmailSendResult({
        status: 'ERROR',
        message: `Failed to dispatch email: ${err.message}`
      })
    } finally {
      setSendingEmail(false)
    }
  }

  // ============================================================
  // SUPABASE CLOUD REPOSITORY & CACHE HANDLERS
  // ============================================================

  const fetchCloudRecords = useCallback(async () => {
    setCloudLoading(true)
    try {
      const [resRec, resStat] = await Promise.all([
        fetch(`${API}/api/supabase/records?filter_status=${cloudFilter}&search=${encodeURIComponent(cloudSearch)}`),
        fetch(`${API}/api/supabase/status`)
      ])
      if (resRec.ok) {
        const dataRec = await resRec.json()
        setCloudRecords(dataRec.records || [])
      }
      if (resStat.ok) {
        const dataStat = await resStat.json()
        setCloudStats(dataStat)
      }
    } catch (err) {
      console.error('Error fetching Supabase cloud records:', err)
    } finally {
      setCloudLoading(false)
    }
  }, [cloudFilter, cloudSearch])

  useEffect(() => {
    if (view === 'cloud') {
      fetchCloudRecords()
    }
  }, [view, cloudFilter, fetchCloudRecords])

  const handleBulkSyncAll = async () => {
    setCloudSyncingAll(true)
    setCloudSyncMsg(null)
    try {
      const res = await fetch(`${API}/api/supabase/sync-all`, { method: 'POST' })
      if (!res.ok) throw new Error('Sync failed')
      const data = await res.json()
      setCloudSyncMsg(`✅ ${data.message}`)
      fetchCloudRecords()
      fetch(`${API}/api/supabase/status`).then(r => r.json()).then(s => setCloudStats(s)).catch(() => {})
    } catch (err) {
      setCloudSyncMsg(`❌ Error syncing to Supabase: ${err.message}`)
    } finally {
      setCloudSyncingAll(false)
    }
  }

  const handlePullCloudRecord = async (eid) => {
    try {
      const res = await fetch(`${API}/api/supabase/pull/${eid}`, { method: 'POST' })
      if (!res.ok) throw new Error('Pull failed')
      const data = await res.json()
      openEmail(eid)
      setView('verify')
    } catch (err) {
      setError(`Failed to pull from Supabase: ${err.message}`)
    }
  }

  // ============================================================
  // VERIFY DOCUMENTS HANDLERS
  // ============================================================

  const loadEmailData = async (eid, filter = queueFilter) => {
    setSelectedEmail(eid)
    fetchAdjacent(eid, filter)
    setLoading(true)
    setError(null)
    setResult(null)
    setVerdictSource(null)
    setResolutions({})
    setResolutionNotes('')
    setResolveSuccess(null)
    setResolutionRecord(null)
    setQueueMsg(null)
    setCorruptWarning(null)
    setEmailInfo(null)
    setCloudSyncInfo(null)
    setSiText('Loading attachment...')
    setBlText('Loading attachment...')

    try {
      const response = await fetch(`${API}/api/email/${eid}`)
      if (!response.ok) throw new Error('Failed to load email content')
      const data = await response.json()
      setSiText(data.si_text || '')
      setBlText(data.bl_text || '')
      if (data.email) setEmailInfo(data.email)
      if (data.cloud_synced) setCloudSyncInfo(data.supabase_record)
      if (data.is_corrupted) {
        setCorruptWarning({
          reason: data.corrupt_reason,
          details: data.issue_details,
        })
      }
      // Stored verdict from a previous run
      if (data.verdict) {
        setResult(data.verdict)
        setVerdictSource('stored')
        if (data.verdict.status === 'MISMATCH' && data.verdict.defect_fields) {
          const initialRes = {}
          data.verdict.defect_fields.forEach(f => {
            initialRes[f] = { type: 'SI', value: data.verdict.si_fields?.[f] || '' }
          })
          setResolutions(initialRes)
        }
      }
      // Stored resolution record (email already resolved by an operator)
      if (data.resolution) {
        setResolutionRecord(data.resolution)
      }
    } catch (err) {
      setError(err.message)
      setSiText('')
      setBlText('')
    } finally {
      setLoading(false)
    }
  }

  const handlePickEmail = (eid) => {
    setPickerOpen(false)
    setPickerSearch('')
    if (eid) loadEmailData(eid)
  }

  const openEmail = (eid, customFilter) => {
    const f = customFilter || queueFilter || 'all'
    loadEmailData(eid, f)
    fetchAdjacent(eid, f)
    setView('verify')
  }

  const openQueueFilter = (filter) => {
    setQueueFilter(filter)
    setQueuePage(1)
    setQueueSearch('')
    setView('queue')
  }

  // ============================================================
  // CHAT ASSISTANT HANDLERS
  // ============================================================

  const sendChat = async (text) => {
    const message = (text !== undefined ? text : chatInput).trim()
    if (!message || chatLoading) return
    setChatMessages(prev => [...prev, { role: 'user', content: message }])
    setChatInput('')
    setChatLoading(true)
    try {
      const res = await fetch(`${API}/api/agent/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: chatSessionId, message }),
      })
      const data = await res.json().catch(() => null)
      if (!res.ok) throw new Error(data?.detail || `Server error: ${res.status}`)
      if (data.session_id) setChatSessionId(data.session_id)
      setChatMessages(prev => {
        const next = prev.map(m =>
          m.pendingAction && !m.pendingAction.decided
            ? { ...m, pendingAction: { ...m.pendingAction, decided: 'superseded' } }
            : m
        )
        next.push({
          role: 'assistant',
          content: data.answer,
          sources: data.sources || [],
          steps: data.steps || [],
          pendingAction: data.pending_action || null,
          degraded: data.degraded,
        })
        return next
      })
    } catch (err) {
      setChatMessages(prev => [...prev, {
        role: 'assistant',
        content: `⚠️ Failed to reach the assistant: ${err.message}`,
        sources: [],
        degraded: true,
      }])
    } finally {
      setChatLoading(false)
    }
  }

  const confirmAgentAction = async (msgIndex, approved) => {
    const action = chatMessages[msgIndex]?.pendingAction
    if (!action || chatLoading) return
    setChatLoading(true)
    try {
      const res = await fetch(`${API}/api/agent/confirm`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          session_id: chatSessionId,
          action_id: action.action_id,
          approved,
        }),
      })
      const data = await res.json().catch(() => null)
      if (!res.ok) {
        if (res.status === 409) {
          setChatMessages(prev => {
            const next = [...prev]
            next[msgIndex] = { ...next[msgIndex], pendingAction: { ...action, decided: 'superseded' } }
            next.push({
              role: 'assistant',
              content: 'That action expired — it was superseded by a newer instruction and is no longer live. Ask me again if you still want it done.',
              sources: [],
              steps: [],
            })
            return next
          })
          return
        }
        throw new Error(data?.detail || `Server error: ${res.status}`)
      }
      if (data.session_id) setChatSessionId(data.session_id)
      setChatMessages(prev => {
        const next = [...prev]
        next[msgIndex] = {
          ...next[msgIndex],
          pendingAction: { ...action, decided: approved ? 'approved' : 'rejected' },
        }
        next.push({
          role: 'assistant',
          content: data.answer,
          sources: data.sources || [],
          steps: data.steps || [],
          pendingAction: data.pending_action || null,
          degraded: data.degraded,
        })
        return next
      })
    } catch (err) {
      setChatMessages(prev => {
        const next = [...prev]
        next[msgIndex] = { ...next[msgIndex], pendingAction: { ...action, decided: 'error' } }
        next.push({
          role: 'assistant',
          content: `⚠️ Confirmation failed: ${err.message}`,
          sources: [],
          degraded: true,
        })
        return next
      })
    } finally {
      setChatLoading(false)
    }
  }

  const resetChat = async () => {
    if (chatSessionId) {
      fetch(`${API}/api/agent/reset`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: chatSessionId }),
      }).catch(() => {})
    }
    setChatSessionId('')
    setChatMessages([])
    setChatInput('')
  }

  useEffect(() => {
    if (view === 'chat' && chatEndRef.current) {
      chatEndRef.current.scrollIntoView({ behavior: 'smooth' })
    }
  }, [chatMessages, chatLoading, view])

  const handleVerify = async () => {
    if (!selectedEmail) {
      setError('Please select an email first.')
      return
    }
    if (!siText.trim() || !blText.trim()) {
      setError('Please ensure both Shipping Instruction and Bill of Lading texts are loaded.')
      return
    }

    setLoading(true)
    setError(null)
    setResult(null)
    setVerdictSource(null)
    setResolveSuccess(null)

    try {
      const response = await fetch(`${API}/api/verify`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          email_id: selectedEmail,
          si_text: siText,
          bl_text: blText,
        }),
      })

      if (!response.ok) throw new Error(`Server error: ${response.status}`)

      const data = await response.json()
      setResult(data)
      setVerdictSource('fresh')

      if (data.status === 'MISMATCH' && data.defect_fields) {
        const initialRes = {}
        data.defect_fields.forEach(f => {
          initialRes[f] = { type: 'SI', value: data.si_fields?.[f] || '' }
        })
        setResolutions(initialRes)
      }

      refreshQueue() // verdict may change queue counts
      refreshEmailStatuses() // keep picker status dots in sync
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  // ============================================================
  // PAPER SCAN HANDLERS (camera photos / handwritten documents)
  // ============================================================

  const handleScanFile = (side, file) => {
    const prev = side === 'si' ? scanSi : scanBl
    const setter = side === 'si' ? setScanSi : setScanBl
    if (prev?.preview) URL.revokeObjectURL(prev.preview)
    if (!file) {
      setter(null)
      return
    }
    setter({
      file,
      preview: file.type?.startsWith('image/') ? URL.createObjectURL(file) : null,
    })
    setScanResult(null)
    setScanError(null)
  }

  const handleScanVerify = async () => {
    if (!scanSi?.file || !scanBl?.file) {
      setScanError('Attach both an SI and a BL document first.')
      return
    }
    setScanLoading(true)
    setScanError(null)
    setScanResult(null)
    try {
      const fd = new FormData()
      fd.append('si_file', scanSi.file)
      fd.append('bl_file', scanBl.file)
      const res = await fetch(`${API}/api/verify/scan`, { method: 'POST', body: fd })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail || `Server error: ${res.status}`)
      setScanResult(data)
    } catch (err) {
      setScanError(err.message)
    } finally {
      setScanLoading(false)
    }
  }

  // ============================================================
  // RESOLUTION HANDLERS
  // ============================================================

  const handleResolutionChoice = (field, type, value) => {
    setResolutions(prev => ({
      ...prev,
      [field]: { type, value },
    }))
  }

  const handleSubmitResolution = async (andNext = false) => {
    setSavingResolution(true)
    try {
      const payload = {
        email_id: selectedEmail,
        resolutions: resolutions,
        notes: resolutionNotes,
        resolved_by: 'Documentation Specialist',
      }

      const res = await fetch(`${API}/api/resolve`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })

      if (!res.ok) throw new Error('Failed to save resolution')
      const data = await res.json()
      setResolveSuccess(data)
      if (result) setResult(prev => ({ ...prev, status: 'RESOLVED' }))
      refreshQueue()
      refreshEmailStatuses()
      fetch(`${API}/api/stats`).then(r => r.json()).then(s => setStats(s)).catch(() => {})

      if (andNext && adjacentInfo?.next) {
        openEmail(adjacentInfo.next)
      }
    } catch (err) {
      setError(err.message)
    } finally {
      setSavingResolution(false)
    }
  }

  // ============================================================
  // QUEUE ROW ACTIONS
  // ============================================================

  const handleChase = async (eid) => {
    try {
      const res = await fetch(`${API}/api/missing-bills/chase`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          email_id: eid,
          notes: 'Automated chaser sent to liner operations desk.',
        }),
      })
      if (!res.ok) throw new Error('Failed to send chaser')
      const data = await res.json()
      setQueueMsg(data.message || `Chaser reminder dispatched for ${eid}.`)
      refreshQueue()
    } catch (err) {
      setError(err.message)
    }
  }

  const handleCorruptAction = async (eid, action) => {
    try {
      const res = await fetch(`${API}/api/corrupted/resolve`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          email_id: eid,
          action: action,
          notes: corruptActionNotes,
        }),
      })
      if (!res.ok) throw new Error('Failed to update corrupted status')
      const data = await res.json()
      setQueueMsg(data.message || `Successfully logged: ${eid} -> ${action}`)
      setCorruptActionNotes('')
      setFocusedCorruptRow(null)
      refreshQueue()
    } catch (err) {
      setError(err.message)
    }
  }

  // Batch-chase every unchased missing-BL item on the current queue page
  const handleBatchChase = async () => {
    const unchased = (queueData?.emails || []).filter(b => !b.chaser_status)
    if (!unchased.length) {
      setQueueMsg('All displayed shipments already have chasers dispatched.')
      return
    }
    setBatchChasing(true)
    try {
      const res = await fetch(`${API}/api/missing-bills/batch-chase`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          email_ids: unchased.map(b => b.email_id),
          notes: `Batch urgent chaser dispatched to ocean carrier for ${unchased.length} shipments pending draft BL.`,
        }),
      })
      const data = await res.json()
      setQueueMsg(data.message || `Dispatched chasers to ${unchased.length} shipments!`)
      refreshQueue()
      fetch(`${API}/api/stats`).then(r => r.json()).then(setStats).catch(() => {})
    } catch (err) {
      setError(err.message)
    } finally {
      setBatchChasing(false)
    }
  }

  // ============================================================
  // PIPELINE HANDLERS
  // ============================================================

  const handleStartPipeline = async () => {
    setPipelineMsg(null)
    autoSavedRef.current = false
    try {
      const res = await fetch(`${API}/api/pipeline/run`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ max_emails: Number(maxEmails) || 0, resume: !freshRun }),
      })
      if (!res.ok) throw new Error(`Server error: ${res.status}`)
      const data = await res.json()
      if (data.started) {
        setPipelineMsg(data.message || 'Submission run started.')
        setPipelineRunning(true)
        setPipelineStatus({ running: true, processed: 0, total: 0, current: '', done: false })
        setCompareData(null)
        setCompareError(null)
        setDiffFilter('')
        setShowAllDiffs(false)
        setSubmissionData(null)
        setSelectedEmails(new Set())
        setExportedOnce(false)
      } else {
        setPipelineMsg(data.message || 'Submission run could not be started (already running?).')
      }
    } catch (err) {
      setPipelineMsg(`Error: ${err.message}`)
    }
  }

  const handleCancelPipeline = async () => {
    try {
      await fetch(`${API}/api/pipeline/cancel`, { method: 'POST' })
      setPipelineMsg('Cancel requested — finishing the current batch first...')
    } catch (err) {
      setPipelineMsg(`Cancel failed: ${err.message}`)
    }
  }

  const downloadJson = (obj, filename) => {
    const blob = new Blob([JSON.stringify(obj, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    document.body.appendChild(a)
    a.click()
    a.remove()
    URL.revokeObjectURL(url)
  }

  async function handleDownloadSubmission() {
    try {
      const res = await fetch(`${API}/api/pipeline/submission`)
      if (!res.ok) throw new Error(`Server error: ${res.status}`)
      const data = await res.json()
      downloadJson(data.submission ?? data, 'submission.json')
      setExportedOnce(true)
    } catch (err) {
      setPipelineMsg(`Download failed: ${err.message}`)
    }
  }

  const handleDownloadSelected = () => {
    if (!submissionData || selectedEmails.size === 0) return
    const picked = {}
    selectedEmails.forEach(e => {
      if (submissionData[e] !== undefined) picked[e] = submissionData[e]
    })
    downloadJson(picked, `submission_${selectedEmails.size}_emails.json`)
    setExportedOnce(true)
  }

  // ============================================================
  // DERIVED DASHBOARD VALUES
  // ============================================================

  const kpis = stats
    ? [
        { label: 'Total Emails', value: stats.emails_total },
        { label: 'Verified', value: stats.verified_count },
        { label: 'Mismatch', value: stats.status_counts?.MISMATCH },
        { label: 'Needs Review', value: stats.status_counts?.NEEDS_REVIEW },
        { label: 'Resolved', value: stats.status_counts?.RESOLVED },
        { label: 'Corrupted', value: stats.corrupted_total },
        { label: 'Missing BL', value: stats.missing_bl_total },
        { label: 'Chasers Sent', value: stats.chasers_sent },
      ]
    : []

  const defectMax = stats
    ? Math.max(1, ...FIELDS.map(f => stats.defect_fields?.[f] || 0))
    : 1
  const categoryEntries = stats ? Object.entries(stats.categories || {}) : []
  const categoryMax = Math.max(1, ...categoryEntries.map(([, n]) => n))
  const carrierEntries = stats ? Object.entries(stats.missing_bl_by_carrier || {}) : []

  const totalPages = queueData
    ? Math.max(1, Math.ceil(queueData.total / queueData.limit))
    : 1

  const pipelinePct =
    pipelineStatus && pipelineStatus.total > 0
      ? Math.round((pipelineStatus.processed / pipelineStatus.total) * 100)
      : 0

  // Email picker derived values
  const pickerEmails = pickerSearch
    ? emails.filter(e => e.toLowerCase().includes(pickerSearch.trim().toLowerCase()))
    : emails
  const pickerSummary = emails.reduce((acc, e) => {
    const s = emailStatusMap[e]
    if (s === 'OK' || s === 'RESOLVED') acc.ok += 1
    else if (s && s !== 'UNVERIFIED') acc.issues += 1
    else acc.unverified += 1
    return acc
  }, { ok: 0, issues: 0, unverified: 0 })

  // ============================================================
  // RENDER
  // ============================================================

  return (
    <div className="app-layout">
      {/* SIDEBAR NAVIGATION */}
      <aside className="sidebar">
        <div className="sidebar-brand">
          <h2>Averis x Monash</h2>
          <p>AI Document Auditor</p>
        </div>

        <nav className="sidebar-nav">
          <button
            className={`sidebar-btn ${view === 'dashboard' ? 'active' : ''}`}
            onClick={() => setView('dashboard')}
          >
            <span>📊 Dashboard</span>
          </button>

          <button
            className={`sidebar-btn ${view === 'chat' ? 'active' : ''}`}
            onClick={() => setView('chat')}
          >
            <span>💬 Assistant</span>
          </button>

          <button
            className={`sidebar-btn ${view === 'queue' ? 'active' : ''}`}
            onClick={() => setView('queue')}
          >
            <span>📥 Inbox</span>
            {queueData?.counts?.all > 0 && (
              <span
                className="sidebar-badge"
                style={{ background: 'rgba(89, 121, 40, 0.15)', color: 'var(--primary-color)' }}
              >
                {queueData.counts.all}
              </span>
            )}
          </button>

          <button
            className={`sidebar-btn ${view === 'verify' ? 'active' : ''}`}
            onClick={() => setView('verify')}
          >
            <span>🔍 Verify Documents</span>
            {selectedEmail && (
              <span
                className="sidebar-badge"
                style={{ background: 'rgba(89, 121, 40, 0.15)', color: 'var(--primary-color)' }}
              >
                {selectedEmail}
              </span>
            )}
          </button>

          <button
            className={`sidebar-btn ${view === 'cloud' ? 'active' : ''}`}
            onClick={() => { setView('cloud'); fetchCloudRecords(); }}
          >
            <span>☁️ Shared Records</span>
            {cloudStats?.total_in_supabase > 0 && (
              <span
                className="sidebar-badge"
                style={{ background: 'rgba(52, 168, 83, 0.15)', color: '#2e7d32' }}
              >
                {cloudStats.total_in_supabase}
              </span>
            )}
          </button>

          <button
            className={`sidebar-btn ${view === 'scan' ? 'active' : ''}`}
            onClick={() => setView('scan')}
          >
            <span>📷 Paper Scan</span>
          </button>

          <button
            className={`sidebar-btn ${view === 'audit' ? 'active' : ''}`}
            onClick={() => setView('audit')}
          >
            <span>🧾 Audit Log</span>
          </button>

          <button
            className={`sidebar-btn ${view === 'pipeline' ? 'active' : ''}`}
            onClick={() => setView('pipeline')}
          >
            <span>📦 Submission Builder</span>
          </button>
        </nav>

        <div className="sidebar-footer">
          <p><span className="status-dot"></span> {aiConfig ? `${aiConfig.provider} Online` : 'Connecting...'}</p>
          <p style={{ opacity: 0.7, marginTop: '4px' }}>{aiConfig ? aiConfig.model : ''}</p>
        </div>
      </aside>

      {/* MAIN CONTENT WORKSPACE */}
      <main className="content-area">

        {/* ========================================================== */}
        {/* VIEW: DASHBOARD                                            */}
        {/* ========================================================== */}
        {view === 'dashboard' && (
          <div style={{ maxWidth: '1200px' }}>
            <div className="page-header">
              <h1>Operations Dashboard</h1>
              <p>Portfolio-level insight across verifications, defects, missing bills, and corrupted files.</p>
            </div>

            {!stats ? (
              <div style={{ textAlign: 'center', padding: '60px' }}>Loading statistics...</div>
            ) : (
              <>
                <CutoffProgressBar stats={stats} onFilter={openQueueFilter} />

                <div className="kpi-grid">
                  {kpis.map(k => {
                    const targetFilter =
                      k.label === 'Missing BL' ? 'missing_bl'
                        : k.label === 'Corrupted' ? 'corrupted'
                          : k.label === 'Mismatch' ? 'mismatch'
                            : k.label === 'Needs Review' ? 'needs_review'
                              : k.label === 'Resolved' ? 'resolved'
                                : null
                    return (
                      <div
                        key={k.label}
                        className="kpi-card"
                        onClick={targetFilter ? () => openQueueFilter(targetFilter) : undefined}
                        style={{ cursor: targetFilter ? 'pointer' : 'default', transition: 'all 0.2s ease' }}
                        title={targetFilter ? `Open ${k.label} in Inbox` : undefined}
                      >
                        <div className="kpi-value">{k.value ?? 0}</div>
                        <div className="kpi-label">
                          {k.label} {targetFilter ? '→' : ''}
                        </div>
                      </div>
                    )
                  })}
                </div>

                {stats.verified_count === 0 && (
                  <div
                    className="item-card"
                    style={{ textAlign: 'center', padding: '28px', color: '#856404', background: '#fff8e1' }}
                  >
                    💡 Run verifications or the batch pipeline to populate insights.
                  </div>
                )}

                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '20px' }}>
                  <div className="item-card" style={{ marginBottom: 0 }}>
                    <h3 style={{ color: 'var(--primary-color)', marginBottom: '14px' }}>
                      Defects by Field
                    </h3>
                    {FIELDS.map(f => (
                      <BarRow
                        key={f}
                        label={f.replace(/_/g, ' ')}
                        value={stats.defect_fields?.[f] || 0}
                        max={defectMax}
                      />
                    ))}
                  </div>

                  <div className="item-card" style={{ marginBottom: 0 }}>
                    <h3 style={{ color: 'var(--primary-color)', marginBottom: '14px' }}>
                      Category Mix
                    </h3>
                    {categoryEntries.length === 0 ? (
                      <p style={{ opacity: 0.7, fontSize: '0.9rem' }}>No categories recorded yet.</p>
                    ) : (
                      categoryEntries.map(([cat, n]) => (
                        <BarRow key={cat} label={cat.replace(/_/g, ' ')} value={n} max={categoryMax} />
                      ))
                    )}
                  </div>
                </div>

                <div className="item-card" style={{ marginTop: '20px' }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '14px' }}>
                    <h3 style={{ color: 'var(--primary-color)', margin: 0 }}>
                      Missing Draft BL by Carrier
                    </h3>
                    <button
                      onClick={() => openQueueFilter('missing_bl')}
                      style={{ padding: '6px 14px', fontSize: '0.82rem' }}
                    >
                      View All Missing Bills →
                    </button>
                  </div>
                  {carrierEntries.length === 0 ? (
                    <p style={{ opacity: 0.7, fontSize: '0.9rem' }}>No missing bills by carrier.</p>
                  ) : (
                    <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.9rem' }}>
                      <thead>
                        <tr>
                          <th style={{ textAlign: 'left', padding: '8px', borderBottom: '2px solid var(--border-color)' }}>
                            Carrier
                          </th>
                          <th style={{ textAlign: 'right', padding: '8px', borderBottom: '2px solid var(--border-color)' }}>
                            Missing BLs
                          </th>
                          <th style={{ textAlign: 'right', padding: '8px', borderBottom: '2px solid var(--border-color)' }}>
                            Action
                          </th>
                        </tr>
                      </thead>
                      <tbody>
                        {carrierEntries.map(([carrier, n]) => (
                          <tr
                            key={carrier}
                            style={{ cursor: 'pointer', transition: 'background 0.2s ease' }}
                            onClick={() => openQueueFilter('missing_bl')}
                          >
                            <td style={{ padding: '8px', borderBottom: '1px solid var(--border-color)', fontWeight: 'bold' }}>
                              🚢 {carrier}
                            </td>
                            <td style={{ padding: '8px', borderBottom: '1px solid var(--border-color)', textAlign: 'right', fontWeight: 'bold', color: 'var(--primary-color)' }}>
                              {n}
                            </td>
                            <td style={{ padding: '8px', borderBottom: '1px solid var(--border-color)', textAlign: 'right', color: 'var(--primary-color)' }}>
                              Filter Carrier →
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  )}
                </div>
              </>
            )}
          </div>
        )}

        {/* ========================================================== */}
        {/* VIEW: CHAT ASSISTANT                                       */}
        {/* ========================================================== */}
        {view === 'chat' && (
          <div className="chat-shell">
            <div className="page-header chat-header-row">
              <div>
                <h1>Assistant</h1>
                <p>An operations agent for the inbox — it can look things up, verify emails, draft chasers and run the pipeline. Write actions ask for your approval first.</p>
              </div>
              {chatMessages.length > 0 && (
                <button className="chat-reset-btn" onClick={resetChat}>
                  New conversation
                </button>
              )}
            </div>

            <div className="chat-messages">
              {chatMessages.length === 0 && (
                <div className="chat-empty">
                  <h3>What would you like me to do?</h3>
                  <p>Ask questions, or let me take actions — I'll always ask before writing or sending anything.</p>
                  <div className="chat-suggestions">
                    {[
                      'Verify the next 25 unverified emails',
                      'Which carriers owe us the most draft BLs?',
                      'Run the pipeline and report my score',
                      'Summarise today\'s mismatches',
                    ].map(q => (
                      <button key={q} className="chat-chip" onClick={() => sendChat(q)}>
                        {q}
                      </button>
                    ))}
                  </div>
                </div>
              )}

              {chatMessages.map((m, i) => (
                <div key={i} className={`chat-row ${m.role}`}>
                  <div className={`chat-bubble ${m.role}`}>
                    {m.degraded && <div className="chat-degraded-tag">degraded mode</div>}
                    {m.steps && m.steps.length > 0 && (
                      <details className="chat-steps">
                        <summary>{m.steps.length} tool{m.steps.length !== 1 ? 's' : ''} used</summary>
                        {m.steps.map((s, j) => (
                          <div key={j} className={`chat-step ${s.ok ? '' : 'step-failed'}`}>
                            🔧 {s.tool} → {s.summary}
                          </div>
                        ))}
                      </details>
                    )}
                    <div className="chat-text">
                      <FormattedChatContent content={m.content} onOpenEmail={eid => openEmail(eid)} />
                    </div>
                    {m.pendingAction && (
                      <div className={`chat-approval ${m.pendingAction.decided ? 'decided' : ''}`}>
                        <div className="chat-approval-title">
                          ⚠️ Approval required — <strong>{m.pendingAction.tool}</strong>
                        </div>
                        <div className="chat-approval-summary">{m.pendingAction.summary}</div>
                        {m.pendingAction.args && Object.keys(m.pendingAction.args).length > 0 && (
                          <div className="chat-approval-args">
                            {Object.entries(m.pendingAction.args).map(([k, v]) => (
                              <div key={k} className="chat-approval-arg">
                                <span className="arg-key">{k}</span>
                                <span className="arg-val">
                                  {typeof v === 'object' ? JSON.stringify(v) : String(v)}
                                </span>
                              </div>
                            ))}
                          </div>
                        )}
                        {m.pendingAction.decided ? (
                          <div className={`chat-approval-decision ${m.pendingAction.decided}`}>
                            {m.pendingAction.decided === 'approved' ? '✓ Approved' :
                             m.pendingAction.decided === 'rejected' ? '✗ Rejected' :
                             m.pendingAction.decided === 'superseded' ? '⊘ Superseded — not executed' :
                             '⚠ Confirmation failed'}
                          </div>
                        ) : (
                          <div className="chat-approval-btns">
                            <button
                              className="chat-approve-btn"
                              disabled={chatLoading}
                              onClick={() => confirmAgentAction(i, true)}
                            >
                              Approve
                            </button>
                            <button
                              className="chat-reject-btn"
                              disabled={chatLoading}
                              onClick={() => confirmAgentAction(i, false)}
                            >
                              Reject
                            </button>
                          </div>
                        )}
                      </div>
                    )}
                    {m.role === 'assistant' && m.sources && m.sources.length > 0 && (
                      <div className="chat-sources">
                        {m.sources.map(s => (
                          <button
                            key={s.email_id}
                            className="chat-source-chip"
                            title={`${s.subject || ''} — ${s.status || ''}`}
                            onClick={() => openEmail(s.email_id)}
                          >
                            {s.email_id} · {s.status}
                          </button>
                        ))}
                      </div>
                    )}
                  </div>
                </div>
              ))}

              {chatLoading && (
                <div className="chat-row assistant">
                  <div className="chat-bubble assistant">
                    <div className="chat-typing">
                      <span></span><span></span><span></span>
                    </div>
                  </div>
                </div>
              )}
              <div ref={chatEndRef} />
            </div>

            <div className="chat-composer">
              <textarea
                value={chatInput}
                onChange={e => setChatInput(e.target.value)}
                onKeyDown={e => {
                  if (e.key === 'Enter' && !e.shiftKey) {
                    e.preventDefault()
                    sendChat()
                  }
                }}
                placeholder="Ask about the inbox… (Enter to send, Shift+Enter for a new line)"
                disabled={chatLoading}
                rows={2}
              />
              <button
                className="chat-send"
                onClick={() => sendChat()}
                disabled={chatLoading || !chatInput.trim()}
              >
                Send
              </button>
            </div>
          </div>
        )}

        {/* ========================================================== */}
        {/* VIEW: WORK QUEUE                                           */}
        {/* ========================================================== */}
        {view === 'queue' && (
          <div style={{ maxWidth: '1100px' }}>
            <div className="page-header">
              <h1>Inbox</h1>
              <p>Every incoming email in one place — filter by issue type: mismatches, review flags, missing BLs, corrupted files, resolutions, and chasers.</p>
            </div>

            <CutoffProgressBar stats={stats} onFilter={setQueueFilter} />

            {/* Filter chips */}
            <div className="action-bar" style={{ flexWrap: 'wrap', gap: '10px' }}>
              {QUEUE_FILTERS.map(f => (
                <button
                  key={f.key}
                  className={`chip ${queueFilter === f.key ? 'active' : ''}`}
                  onClick={() => {
                    setQueueFilter(f.key)
                    setQueuePage(1)
                  }}
                >
                  {f.label}
                  {queueData?.counts?.[f.key] != null ? ` · ${queueData.counts[f.key]}` : ''}
                </button>
              ))}
              {queueFilter === 'missing_bl' && (
                <button
                  onClick={handleBatchChase}
                  disabled={batchChasing || !queueData?.emails?.length}
                  style={{ marginLeft: 'auto', padding: '8px 18px', fontSize: '0.85rem', background: '#e07a5f' }}
                >
                  ⚡ {batchChasing ? 'Dispatching...' : 'Batch Chase Page'}
                </button>
              )}
            </div>

            {/* Debounced search */}
            <div className="action-bar">
              <input
                type="text"
                placeholder="Search by Email ID, Subject, or Sender..."
                value={queueSearch}
                onChange={(e) => {
                  setQueueSearch(e.target.value)
                  setQueuePage(1)
                }}
                style={{
                  flex: 1,
                  padding: '10px 14px',
                  borderRadius: '8px',
                  border: '1px solid var(--border-color)',
                  fontSize: '0.95rem',
                }}
              />
            </div>

            {queueMsg && (
              <div style={{ background: '#d4edda', color: '#155724', padding: '12px 16px', borderRadius: '8px', marginBottom: '16px' }}>
                ✅ {queueMsg}
              </div>
            )}

            {loadingQueue ? (
              <div style={{ textAlign: 'center', padding: '60px' }}>Loading queue...</div>
            ) : !queueData?.emails?.length ? (
              <div className="item-card" style={{ textAlign: 'center', padding: '40px' }}>
                <p>No items match this filter.</p>
              </div>
            ) : (
              <>
                {queueData.emails.map(item => {
                  const qs = (item.queue_status || 'unverified').toLowerCase()
                  const cardMod =
                    qs === 'corrupted' ? 'danger' : qs === 'missing_bl' ? 'warning' : ''
                  return (
                    <div
                      key={item.email_id}
                      className={`item-card ${cardMod}`}
                      style={{ padding: '16px 20px' }}
                    >
                      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
                        <div style={{ flex: 1, marginRight: '16px' }}>
                          <div style={{ display: 'flex', alignItems: 'center', gap: '10px', flexWrap: 'wrap' }}>
                            <strong style={{ color: 'var(--primary-color)', fontSize: '0.95rem' }}>
                              {item.email_id}
                            </strong>
                            <span className={`status-badge status-${qs.replace(/_/g, '-')}`}>
                              {item.queue_status || 'UNVERIFIED'}
                            </span>
                            {item.category && (
                              <span style={{ fontSize: '0.75rem', padding: '2px 8px', borderRadius: '10px', background: '#f0f4e8', color: 'var(--primary-color)', fontWeight: 'bold' }}>
                                {item.category}
                              </span>
                            )}
                            <span style={{ fontSize: '0.75rem', padding: '2px 8px', borderRadius: '10px', background: '#f0f4e8', color: 'var(--primary-color)', fontWeight: 'bold' }}>
                              📎 {item.attachments_count} doc(s)
                            </span>
                            {item.has_bl === false && (
                              <span style={{ fontSize: '0.75rem', color: '#856404', fontWeight: 'bold' }}>
                                NO BL
                              </span>
                            )}
                          </div>
                          <p style={{ fontSize: '0.9rem', marginTop: '4px', color: 'var(--text-color)' }}>
                            {item.subject}
                          </p>
                          <p style={{ fontSize: '0.8rem', color: '#666' }}>From: {item.from}</p>
                          {item.corrupt_issue && (
                            <p style={{ fontSize: '0.8rem', color: '#c62828', marginTop: '4px' }}>
                              ⚠️ {item.corrupt_issue}
                            </p>
                          )}
                          {item.chaser_status && (
                            <p style={{ fontSize: '0.78rem', color: '#155724', fontStyle: 'italic', marginTop: '4px' }}>
                              📧 Chaser: {item.chaser_status}
                            </p>
                          )}
                          {item.corrupt_action && (
                            <p style={{ fontSize: '0.78rem', color: '#555', fontStyle: 'italic', marginTop: '2px' }}>
                              Corruption action: {item.corrupt_action}
                            </p>
                          )}
                        </div>

                        <div style={{ display: 'flex', flexDirection: 'column', gap: '8px', alignItems: 'flex-end' }}>
                          <button
                            onClick={(e) => {
                              e.stopPropagation()
                              openEmail(item.email_id)
                            }}
                            style={{ padding: '8px 16px', fontSize: '0.82rem' }}
                          >
                            Open & Verify →
                          </button>
                          {(qs === 'missing_bl' || queueFilter === 'missing_bl') && (
                            <button
                              onClick={(e) => {
                                e.stopPropagation()
                                handleChase(item.email_id)
                              }}
                              style={{ padding: '8px 16px', fontSize: '0.82rem', background: '#e07a5f' }}
                            >
                              📧 Send Chaser
                            </button>
                          )}
                        </div>
                      </div>

                      {qs === 'corrupted' && (
                        <div
                          style={{ display: 'flex', gap: '10px', alignItems: 'center', marginTop: '12px', borderTop: '1px solid #f0e9df', paddingTop: '12px' }}
                          onClick={(e) => e.stopPropagation()}
                        >
                          <input
                            type="text"
                            placeholder="Add operator remediation notes..."
                            value={focusedCorruptRow === item.email_id ? corruptActionNotes : ''}
                            onFocus={() => setFocusedCorruptRow(item.email_id)}
                            onChange={(e) => setCorruptActionNotes(e.target.value)}
                            style={{ flex: 1, padding: '8px 12px', borderRadius: '6px', border: '1px solid var(--border-color)', fontSize: '0.85rem' }}
                          />
                          <button
                            onClick={() => handleCorruptAction(item.email_id, 'CARRIER_RE_REQUESTED')}
                            style={{ padding: '8px 16px', fontSize: '0.82rem', background: '#6c757d' }}
                          >
                            Request New Copy
                          </button>
                          <button
                            onClick={() => handleCorruptAction(item.email_id, 'MANUAL_OVERRIDE')}
                            style={{ padding: '8px 16px', fontSize: '0.82rem' }}
                          >
                            Mark Handled
                          </button>
                        </div>
                      )}
                    </div>
                  )
                })}

                {/* Pagination */}
                <div className="pager">
                  <button
                    disabled={queuePage <= 1}
                    onClick={() => setQueuePage(p => p - 1)}
                    style={{ padding: '8px 18px', fontSize: '0.85rem' }}
                  >
                    ← Prev
                  </button>
                  <span style={{ fontSize: '0.9rem' }}>
                    Page {queueData.page} of {totalPages} ({queueData.total})
                  </span>
                  <button
                    disabled={queuePage >= totalPages}
                    onClick={() => setQueuePage(p => p + 1)}
                    style={{ padding: '8px 18px', fontSize: '0.85rem' }}
                  >
                    Next →
                  </button>
                </div>
              </>
            )}
          </div>
        )}

        {/* VIEW: VERIFY DOCUMENTS                                     */}
        {/* ========================================================== */}
        {view === 'verify' && (
          <>
            <div className="page-header">
              <h1>Verify Documents</h1>
              <p>Review incoming inbox emails, inspect attachments, and compare SI vs draft BL.</p>
            </div>

            <div className="queue-nav-bar">
              <div style={{ display: 'flex', alignItems: 'center', gap: '10px', flexWrap: 'wrap' }}>
                <div className="email-picker" ref={pickerRef}>
                  <button
                    type="button"
                    className={`email-picker-btn ${pickerOpen ? 'open' : ''}`}
                    onClick={() => setPickerOpen(o => !o)}
                  >
                    {selectedEmail ? (
                      <>
                        <span className="ep-dot" style={{ background: emailStatusMeta(emailStatusMap[selectedEmail]).color }} />
                        <span className="ep-current">{selectedEmail}</span>
                        <span
                          className="ep-status"
                          style={{
                            color: emailStatusMeta(emailStatusMap[selectedEmail]).color,
                            background: emailStatusMeta(emailStatusMap[selectedEmail]).bg,
                          }}
                        >
                          {emailStatusMeta(emailStatusMap[selectedEmail]).label}
                        </span>
                      </>
                    ) : (
                      <span className="ep-placeholder">Select an Email / Bill…</span>
                    )}
                    <span className="ep-caret">▾</span>
                  </button>

                  {pickerOpen && (
                    <div className="email-picker-panel">
                      <input
                        autoFocus
                        className="email-picker-search"
                        placeholder="Search email id…"
                        value={pickerSearch}
                        onChange={(e) => setPickerSearch(e.target.value)}
                      />
                      <div className="email-picker-summary">
                        <span className="s-ok">● {pickerSummary.ok} OK</span>
                        <span className="s-issues">● {pickerSummary.issues} with issues</span>
                        <span className="s-unverified">● {pickerSummary.unverified} unverified</span>
                      </div>
                      <div className="email-picker-list">
                        {pickerEmails.map(e => {
                          const m = emailStatusMeta(emailStatusMap[e])
                          return (
                            <button
                              key={e}
                              type="button"
                              className={`email-picker-item ${e === selectedEmail ? 'selected' : ''}`}
                              onClick={() => handlePickEmail(e)}
                            >
                              <span className="ep-dot" style={{ background: m.color }} />
                              <span className="ep-id">{e}</span>
                              <span className="ep-status" style={{ color: m.color, background: m.bg }}>{m.label}</span>
                            </button>
                          )
                        })}
                        {pickerEmails.length === 0 && (
                          <div className="email-picker-empty">No emails match “{pickerSearch}”.</div>
                        )}
                      </div>
                    </div>
                  )}
                </div>

                <button onClick={handleVerify} disabled={loading || !selectedEmail} style={{ padding: '8px 16px', fontSize: '0.9rem' }}>
                  {loading ? 'Analyzing via NVIDIA NIM...' : 'Re-verify'}
                </button>

                {adjacentInfo && (
                  <span style={{ fontSize: '0.85rem', color: '#555', fontWeight: 600, padding: '4px 10px', background: '#f5efe6', borderRadius: '6px' }}>
                    Item {adjacentInfo.index} of {adjacentInfo.total} {queueFilter ? `(${queueFilter})` : ''}
                  </span>
                )}
              </div>

              <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginLeft: 'auto' }}>
                <button
                  className="queue-nav-btn"
                  disabled={!adjacentInfo?.has_prev}
                  onClick={() => adjacentInfo?.prev && openEmail(adjacentInfo.prev)}
                  title={adjacentInfo?.prev ? `Go to previous: ${adjacentInfo.prev}` : 'No previous item'}
                >
                  ◀ Prev
                </button>
                <button
                  className="queue-nav-btn"
                  disabled={!adjacentInfo?.has_next}
                  onClick={() => adjacentInfo?.next && openEmail(adjacentInfo.next)}
                  title={adjacentInfo?.next ? `Go to next: ${adjacentInfo.next}` : 'No next item'}
                >
                  Next ▶
                </button>
                <button
                  onClick={() => openDraftEmail(selectedEmail)}
                  disabled={!selectedEmail}
                  style={{ background: '#6e3511', color: '#fff', border: 'none', borderRadius: '8px', padding: '7px 14px', fontSize: '0.85rem', fontWeight: 600, display: 'inline-flex', alignItems: 'center', gap: '6px', cursor: 'pointer' }}
                  title="Auto-draft and send email via Google SMTP"
                >
                  ✉️ Auto-Draft Email
                </button>
                <div className="keyboard-shortcuts-hint">
                  <span title="Keyboard shortcuts: Press [K] for Previous, [J] for Next, [E] to Auto-Draft">
                    ⚡ <kbd>K</kbd> Prev · <kbd>J</kbd> Next · <kbd>E</kbd> Draft
                  </span>
                </div>
                <button
                  onClick={() => setView('queue')}
                  style={{ background: 'transparent', border: '1px solid var(--border-color)', color: 'var(--text-color)', boxShadow: 'none', padding: '7px 14px', fontSize: '0.85rem' }}
                >
                  Inbox 📥
                </button>
              </div>
            </div>

            {/* Banner: Pulled from Supabase Cloud Cache */}
            {cloudSyncInfo && (
              <div className="cloud-cache-banner">
                <div>
                  <strong>☁️ Supabase Cloud Synchronized:</strong> Previously verified & cached in shared Supabase database (Status: {cloudSyncInfo.status || 'OK'}{cloudSyncInfo.updated_at ? ` · Last Synced: ${new Date(cloudSyncInfo.updated_at).toLocaleTimeString()}` : ''}).
                </div>
                <div style={{ display: 'flex', gap: '8px' }}>
                  <button
                    onClick={() => openDraftEmail(selectedEmail)}
                    style={{ padding: '6px 14px', fontSize: '0.82rem', background: '#6e3511', color: '#fff' }}
                  >
                    ✉️ Draft Email
                  </button>
                  <button
                    onClick={handleVerify}
                    style={{ padding: '6px 14px', fontSize: '0.82rem', background: '#597928', color: '#fff' }}
                  >
                    🔄 Force Local Re-Scan
                  </button>
                </div>
              </div>
            )}

            {/* Contextual Action Card: Missing Draft BL Carrier Chaser */}
            {emailInfo && emailInfo.attachments?.length === 0 && (
              <div className="action-card chaser">
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', flexWrap: 'wrap', gap: '10px' }}>
                  <div>
                    <span style={{ fontSize: '0.75rem', fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.05em', color: '#e07a5f' }}>
                      MISSING BILL OF LADING · IMMEDIATE REMEDIATION
                    </span>
                    <h3 style={{ margin: '4px 0', color: 'var(--primary-color)' }}>
                      Automated Carrier Chaser Dispatch
                    </h3>
                    <p style={{ fontSize: '0.88rem', color: '#555', margin: 0 }}>
                      Shipper instructions were received, but no draft BL attachment is present. Dispatch an automated chaser to <strong>{detectCarrier(emailInfo)}</strong> before port cutoff.
                    </p>
                  </div>
                  <button
                    onClick={() => handleChase(selectedEmail)}
                    style={{ background: '#e07a5f', padding: '10px 20px', fontSize: '0.9rem', fontWeight: 600, display: 'inline-flex', alignItems: 'center', gap: '8px' }}
                  >
                    ⚡ Dispatch Carrier Chaser Email
                  </button>
                </div>

                <div className="email-preview-box">
                  <div style={{ fontWeight: 600, marginBottom: '6px', color: '#6e3511' }}>
                    📧 Outgoing Carrier Chaser:
                  </div>
                  <div><strong>To:</strong> {emailInfo.from || 'carrier-desk@shippingline.com'} ({detectCarrier(emailInfo)})</div>
                  <div><strong>Subject:</strong> URGENT: Missing Draft Bill of Lading — {emailInfo.subject}</div>
                  <div style={{ marginTop: '8px', fontStyle: 'italic', color: '#444' }}>
                    "Dear Carrier Operations Team, we are following up on our Shipping Instruction for shipment ref {selectedEmail}. Port cutoff is approaching and our automated verification pipeline has not received the draft BL. Please urgently furnish the draft BL to avoid shipping delays."
                  </div>
                </div>
              </div>
            )}

            {/* Contextual Action Card: Corrupted Document Remediation */}
            {(corruptWarning || emailInfo?.attachments?.some(a => a.is_corrupt)) && (
              <div className="action-card corrupt">
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', flexWrap: 'wrap', gap: '10px' }}>
                  <div>
                    <span style={{ fontSize: '0.75rem', fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.05em', color: '#c62828' }}>
                      UNREADABLE ATTACHMENT · REMEDIATION PROTOCOL
                    </span>
                    <h3 style={{ margin: '4px 0', color: '#c62828' }}>
                      Document Corruption / Truncation Detected
                    </h3>
                    <p style={{ fontSize: '0.88rem', color: '#555', margin: 0 }}>
                      {corruptWarning?.details || corruptWarning?.reason || 'Document binary stream is truncated or unreadable.'}
                    </p>
                  </div>
                  <div style={{ display: 'flex', gap: '10px', flexWrap: 'wrap' }}>
                    <button
                      onClick={() => handleCorruptAction(selectedEmail, 'REUPLOAD_REQUESTED')}
                      style={{ background: '#597928', padding: '8px 16px', fontSize: '0.85rem' }}
                    >
                      🔄 Request Re-upload from Shipper
                    </button>
                    <button
                      onClick={() => setView('scan')}
                      style={{ background: '#6e3511', padding: '8px 16px', fontSize: '0.85rem' }}
                    >
                      📷 Route to Paper Scan / OCR
                    </button>
                    <button
                      onClick={() => handleCorruptAction(selectedEmail, 'REJECTED')}
                      style={{ background: '#dc3545', padding: '8px 16px', fontSize: '0.85rem' }}
                    >
                      ❌ Reject Document
                    </button>
                  </div>
                </div>
              </div>
            )}

            {/* Banner: Discrepancy resolved by an operator (stored record) */}
            {resolutionRecord && (
              <div className="redirect-banner" style={{ background: '#d4edda', borderColor: '#c3e6cb', color: '#155724' }}>
                <div>
                  <strong>✅ Resolved by {resolutionRecord.resolved_by || 'operator'}:</strong>{' '}
                  {resolutionRecord.timestamp ? new Date(resolutionRecord.timestamp).toLocaleString() : ''}
                  {resolutionRecord.notes ? ` — ${resolutionRecord.notes}` : ''}
                </div>
              </div>
            )}

            {/* Banner: Mismatch Detected — resolution panel is rendered below */}
            {result?.status === 'MISMATCH' && !resolutionRecord && (
              <div className="redirect-banner">
                <div>
                  <strong>⚠️ Discrepancy Found:</strong> {result.defect_fields?.length} field mismatch(es) detected — resolve below.
                </div>
              </div>
            )}

            {/* INCOMING EMAIL CONTEXT CARD */}
            {emailInfo && (
              <div className="glass-panel" style={{ marginBottom: '22px', background: '#ffffff' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', borderBottom: '1px solid var(--border-color)', paddingBottom: '10px', marginBottom: '12px' }}>
                  <div>
                    <span style={{ fontSize: '0.75rem', textTransform: 'uppercase', color: '#888', fontWeight: 'bold' }}>
                      INBOX MESSAGE &bull; {emailInfo.email_id}
                    </span>
                    <h3 style={{ color: 'var(--primary-color)', margin: '4px 0 2px' }}>{emailInfo.subject}</h3>
                    <p style={{ fontSize: '0.85rem', color: '#555' }}><strong>From:</strong> {emailInfo.from}</p>
                  </div>
                  <span className="status-badge" style={{ background: 'rgba(89, 121, 40, 0.15)', color: 'var(--primary-color)' }}>
                    Category: {emailInfo.category || 'CLASSIFYING'}
                  </span>
                </div>

                {/* Email Body preview */}
                <div style={{ background: '#faf8f5', padding: '10px 14px', borderRadius: '6px', fontSize: '0.88rem', whiteSpace: 'pre-wrap', maxHeight: '110px', overflowY: 'auto', border: '1px solid #efe8df', marginBottom: '12px' }}>
                  {emailInfo.body}
                </div>

                {/* Attachments Chips */}
                <div style={{ display: 'flex', gap: '10px', alignItems: 'center', flexWrap: 'wrap' }}>
                  <span style={{ fontSize: '0.8rem', fontWeight: 'bold', color: '#666' }}>
                    Attachments ({emailInfo.attachments?.length || 0}):
                  </span>
                  {emailInfo.attachments?.length === 0 && (
                    <span style={{ fontSize: '0.8rem', color: '#856404', fontStyle: 'italic' }}>
                      No attachments (Draft BL Pending from shipping line)
                    </span>
                  )}
                  {emailInfo.attachments?.map((a, idx) => (
                    <span key={idx} style={{
                      display: 'inline-flex',
                      alignItems: 'center',
                      gap: '6px',
                      padding: '4px 10px',
                      borderRadius: '6px',
                      fontSize: '0.8rem',
                      fontFamily: 'monospace',
                      background: a.is_corrupt ? '#ffebee' : '#f0f4e8',
                      color: a.is_corrupt ? '#c62828' : '#2e5b15',
                      border: `1px solid ${a.is_corrupt ? '#ffcdd2' : '#dcedc8'}`,
                    }}>
                      📎 {a.path.split('/').pop()}
                      <span style={{ opacity: 0.7 }}>
                        ({a.size > 0 ? `${(a.size / 1024).toFixed(1)} KB` : '0 B'})
                      </span>
                      {a.is_corrupt && <strong style={{ color: '#d32f2f' }}>[CORRUPT]</strong>}
                    </span>
                  ))}
                </div>
              </div>
            )}

            <div className="main-container">
              {/* Column 1: SI Text */}
              <section className="doc-section glass-panel">
                <h2>Shipping Instruction (SI)</h2>
                <textarea
                  placeholder="Paste or load SI text here..."
                  value={siText}
                  onChange={(e) => setSiText(e.target.value)}
                />
              </section>

              {/* Column 2: BL Text */}
              <section className="doc-section glass-panel">
                <h2>Bill of Lading (BL)</h2>
                <textarea
                  placeholder="Paste or load BL text here..."
                  value={blText}
                  onChange={(e) => setBlText(e.target.value)}
                />
              </section>

              {/* Column 3: AI Output */}
              <section className="doc-section glass-panel">
                <h2>Audit Verdict & AI Reasoning</h2>
                <VerdictPanel
                  result={result}
                  loading={loading}
                  error={error}
                  verdictSource={verdictSource}
                />
              </section>
            </div>

            {/* Resolution success box */}
            {resolveSuccess && (
              <div className="item-card" style={{ marginTop: '22px', background: '#d4edda', color: '#155724', border: '1px solid #c3e6cb' }}>
                <strong>✅ Resolution Approved & Logged!</strong>
                <p style={{ marginTop: '4px', fontSize: '0.9rem' }}>{resolveSuccess.message}</p>
                {resolveSuccess.record?.timestamp && (
                  <p style={{ fontSize: '0.8rem', color: '#666', marginTop: '6px' }}>
                    Logged at {resolveSuccess.record.timestamp}
                  </p>
                )}
              </div>
            )}

            {/* INLINE RESOLUTION PANEL (hidden when already resolved) */}
            {result?.status === 'MISMATCH' && result.defect_fields?.length > 0 && !resolutionRecord && (
              <div className="item-card" style={{ marginTop: '22px' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', borderBottom: '1px solid var(--border-color)', paddingBottom: '12px', marginBottom: '16px' }}>
                  <div>
                    <span style={{ fontSize: '0.85rem', color: '#666' }}>Active Bill Record</span>
                    <h3 style={{ color: 'var(--primary-color)' }}>{selectedEmail || 'No Bill Selected'}</h3>
                  </div>
                  <div className={`status-badge status-${(result?.status || 'UNKNOWN').toLowerCase().replace(/_/g, '-')}`}>
                    {result?.status || 'NOT VERIFIED'}
                  </div>
                </div>

                <p style={{ marginBottom: '20px', fontSize: '0.95rem' }}>
                  The AI verified this document and identified{' '}
                  <strong>{result.defect_fields.length} discrepancy/discrepancies</strong> requiring operator resolution:
                </p>

                {result.defect_fields.map(f => {
                  const currentChoice = resolutions[f] || { type: 'SI', value: result.si_fields?.[f] }
                  return (
                    <div key={f} style={{ border: '1px solid #e0d8cf', borderRadius: '8px', padding: '16px', marginBottom: '18px', background: '#fff' }}>
                      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                        <h4 style={{ textTransform: 'uppercase', color: 'var(--primary-color)' }}>
                          Field: {f.replace(/_/g, ' ')}
                        </h4>
                        <span style={{ fontSize: '0.8rem', color: '#dc3545', fontWeight: 'bold' }}>Needs Resolution</span>
                      </div>

                      {result.field_comparisons?.[f]?.reason && (
                        <p style={{ fontSize: '0.85rem', color: '#856404', margin: '8px 0 12px', fontStyle: 'italic' }}>
                          ⚠️ AI Finding: {result.field_comparisons[f].reason}
                        </p>
                      )}

                      <div className="option-group">
                        {/* SI Option */}
                        <label className={`choice-box ${currentChoice.type === 'SI' ? 'selected' : ''}`}>
                          <input
                            type="radio"
                            name={`choice-${f}`}
                            checked={currentChoice.type === 'SI'}
                            onChange={() => handleResolutionChoice(f, 'SI', result.si_fields?.[f])}
                          />
                          <div>
                            <strong>Accept Shipping Instruction (SI) Value</strong>
                            <div style={{ fontFamily: 'monospace', fontSize: '0.9rem', color: 'var(--primary-color)', marginTop: '4px' }}>
                              {result.si_fields?.[f] || '(blank)'}
                            </div>
                          </div>
                        </label>

                        {/* BL Option */}
                        <label className={`choice-box ${currentChoice.type === 'BL' ? 'selected' : ''}`}>
                          <input
                            type="radio"
                            name={`choice-${f}`}
                            checked={currentChoice.type === 'BL'}
                            onChange={() => handleResolutionChoice(f, 'BL', result.bl_fields?.[f])}
                          />
                          <div>
                            <strong>Accept Bill of Lading (BL) Draft Value</strong>
                            <div style={{ fontFamily: 'monospace', fontSize: '0.9rem', color: 'var(--primary-color)', marginTop: '4px' }}>
                              {result.bl_fields?.[f] || '(blank)'}
                            </div>
                          </div>
                        </label>

                        {/* Custom Option */}
                        <label className={`choice-box ${currentChoice.type === 'CUSTOM' ? 'selected' : ''}`}>
                          <input
                            type="radio"
                            name={`choice-${f}`}
                            checked={currentChoice.type === 'CUSTOM'}
                            onChange={() => handleResolutionChoice(f, 'CUSTOM', currentChoice.value || '')}
                          />
                          <div style={{ width: '100%' }}>
                            <strong>Manual Override / Corrected Value</strong>
                            <input
                              type="text"
                              placeholder="Enter approved value..."
                              value={currentChoice.type === 'CUSTOM' ? currentChoice.value : ''}
                              onFocus={() => handleResolutionChoice(f, 'CUSTOM', currentChoice.value || '')}
                              onChange={(e) => handleResolutionChoice(f, 'CUSTOM', e.target.value)}
                              style={{ width: '100%', marginTop: '6px', padding: '8px 12px', borderRadius: '6px', border: '1px solid var(--border-color)', fontSize: '0.9rem' }}
                            />
                          </div>
                        </label>
                      </div>
                    </div>
                  )
                })}

                {/* Operator Notes */}
                <div style={{ marginTop: '20px' }}>
                  <label style={{ display: 'block', fontWeight: 'bold', marginBottom: '6px' }}>
                    Operator Audit Justification & Notes:
                  </label>
                  <textarea
                    placeholder="E.g., Confirmed with shipper via phone call; updated to approved legal entity."
                    value={resolutionNotes}
                    onChange={(e) => setResolutionNotes(e.target.value)}
                    style={{ height: '90px', width: '100%' }}
                  />
                </div>

                <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '15px', marginTop: '20px' }}>
                  <button
                    onClick={() => handleSubmitResolution(false)}
                    disabled={savingResolution}
                    style={{ background: 'transparent', border: '1px solid var(--border-color)', color: 'var(--text-color)', boxShadow: 'none', padding: '10px 18px', fontSize: '0.9rem' }}
                  >
                    {savingResolution ? 'Logging...' : 'Approve Only'}
                  </button>
                  <button
                    className="btn-primary-next"
                    onClick={() => handleSubmitResolution(true)}
                    disabled={savingResolution}
                  >
                    {savingResolution ? 'Logging Resolution...' : 'Approve & Next Discrepancy →'}
                  </button>
                </div>
              </div>
            )}
          </>
        )}

        {/* ========================================================== */}
        {/* VIEW: CLOUD REPOSITORY (Supabase Shared Cache & Registry)  */}
        {/* ========================================================== */}
        {view === 'cloud' && (
          <div style={{ maxWidth: '1200px' }}>
            <div className="cloud-header-banner">
              <div>
                <span style={{ fontSize: '0.78rem', textTransform: 'uppercase', letterSpacing: '0.05em', opacity: 0.9, fontWeight: 700 }}>
                  MULTI-USER PERSISTENCE & SHARED CLOUD REGISTRY
                </span>
                <h1 style={{ margin: '4px 0 2px', fontSize: '1.6rem', color: '#ffffff' }}>
                  Shared Records
                </h1>
                <p style={{ margin: 0, opacity: 0.85, fontSize: '0.9rem' }}>
                  Verified results synced to Supabase so the whole team shares one source of truth. Previously scanned emails load instantly with zero recompute.
                </p>
              </div>

              <div style={{ display: 'flex', gap: '12px', alignItems: 'center' }}>
                <span style={{ background: 'rgba(255,255,255,0.2)', padding: '6px 14px', borderRadius: '20px', fontSize: '0.85rem', fontWeight: 600 }}>
                  {cloudStats.connected ? '🟢 Connected to Supabase' : '⚪ Connecting to Supabase...'}
                </span>
                <button
                  onClick={handleBulkSyncAll}
                  disabled={cloudSyncingAll}
                  style={{ background: '#ffffff', color: '#2b580c', fontWeight: 700, padding: '10px 18px', fontSize: '0.9rem', boxShadow: '0 4px 12px rgba(0,0,0,0.1)' }}
                >
                  {cloudSyncingAll ? '☁️ Syncing 520 Records...' : '☁️ Bulk Sync All 520 to Supabase'}
                </button>
              </div>
            </div>

            {cloudSyncMsg && (
              <div style={{ background: '#d4edda', color: '#155724', padding: '12px 18px', borderRadius: '8px', marginBottom: '16px' }}>
                {cloudSyncMsg}
              </div>
            )}

            {/* Metrics cards */}
            <div className="cloud-metrics-row">
              <div className="kpi-card">
                <div className="kpi-value">{cloudStats.total_local || 520}</div>
                <div className="kpi-label">Total Operational Records</div>
              </div>
              <div className="kpi-card" onClick={() => setCloudFilter('synced')} style={{ cursor: 'pointer' }}>
                <div className="kpi-value" style={{ color: '#2e7d32' }}>{cloudStats.total_in_supabase || 0}</div>
                <div className="kpi-label">🟢 Synced in Supabase Cloud →</div>
              </div>
              <div className="kpi-card" onClick={() => setCloudFilter('pending')} style={{ cursor: 'pointer' }}>
                <div className="kpi-value" style={{ color: '#e65100' }}>{Math.max(0, (cloudStats.total_local || 520) - (cloudStats.total_in_supabase || 0))}</div>
                <div className="kpi-label">⚪ Pending Local Scan →</div>
              </div>
              <div className="kpi-card" onClick={() => setCloudFilter('mismatch')} style={{ cursor: 'pointer' }}>
                <div className="kpi-value" style={{ color: '#c62828' }}>{cloudRecords.filter(r => r.cloud_status === 'MISMATCH').length}</div>
                <div className="kpi-label">🔴 Cloud Mismatches →</div>
              </div>
            </div>

            {/* Filter chips & Search */}
            <div className="action-bar" style={{ flexWrap: 'wrap', gap: '10px', marginBottom: '16px' }}>
              {[
                { key: 'all', label: 'All Records' },
                { key: 'synced', label: '🟢 Synced to Supabase' },
                { key: 'pending', label: '⚪ Pending Local Scan' },
                { key: 'mismatch', label: '🔴 Mismatches' },
                { key: 'needs_review', label: '⚠️ Needs Review' },
                { key: 'resolved', label: '✅ Resolved' }
              ].map(f => (
                <button
                  key={f.key}
                  className={`chip ${cloudFilter === f.key ? 'active' : ''}`}
                  onClick={() => setCloudFilter(f.key)}
                >
                  {f.label}
                </button>
              ))}
            </div>

            <div className="action-bar" style={{ marginBottom: '20px' }}>
              <input
                type="text"
                placeholder="Search Cloud Registry by Email ID, Subject, or Carrier..."
                value={cloudSearch}
                onChange={e => setCloudSearch(e.target.value)}
                style={{ flex: 1, padding: '10px 14px', borderRadius: '8px', border: '1px solid var(--border-color)', fontSize: '0.95rem' }}
              />
              <button onClick={fetchCloudRecords} style={{ padding: '10px 20px', fontSize: '0.9rem' }}>
                🔄 Refresh Cloud
              </button>
            </div>

            {cloudLoading ? (
              <div style={{ textAlign: 'center', padding: '60px' }}>Loading Supabase Cloud Registry...</div>
            ) : cloudRecords.length === 0 ? (
              <div className="item-card" style={{ textAlign: 'center', padding: '40px' }}>
                <p>No records found matching this cloud filter.</p>
              </div>
            ) : (
              <div style={{ overflowX: 'auto', background: '#ffffff', borderRadius: '12px', border: '1px solid var(--border-color)', boxShadow: '0 2px 8px rgba(0,0,0,0.03)' }}>
                <table style={{ width: '100%', borderCollapse: 'collapse', textAlign: 'left', fontSize: '0.9rem' }}>
                  <thead>
                    <tr style={{ background: '#fbf9f6', borderBottom: '2px solid var(--border-color)', color: '#666' }}>
                      <th style={{ padding: '14px 16px' }}>Email ID</th>
                      <th style={{ padding: '14px 16px' }}>Subject & Carrier</th>
                      <th style={{ padding: '14px 16px' }}>Cloud Differentiation</th>
                      <th style={{ padding: '14px 16px' }}>Defect Summary</th>
                      <th style={{ padding: '14px 16px' }}>Last Scanned / Synced</th>
                      <th style={{ padding: '14px 16px', textAlign: 'right' }}>Actions</th>
                    </tr>
                  </thead>
                  <tbody>
                    {cloudRecords.slice((cloudPage - 1) * 25, cloudPage * 25).map(r => (
                      <tr key={r.email_id} style={{ borderBottom: '1px solid var(--border-color)', transition: 'background 0.15s ease' }}>
                        <td style={{ padding: '12px 16px', fontWeight: 700, color: 'var(--primary-color)' }}>
                          {r.email_id}
                        </td>
                        <td style={{ padding: '12px 16px', maxWidth: '320px' }}>
                          <div style={{ fontWeight: 600, color: 'var(--text-color)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                            {r.subject || 'No Subject'}
                          </div>
                          <span style={{ fontSize: '0.78rem', color: '#888' }}>
                            Carrier: {r.carrier || 'Unknown'} · 📎 {r.attachments_count} doc(s)
                          </span>
                        </td>
                        <td style={{ padding: '12px 16px' }}>
                          {r.cloud_synced ? (
                            <span className="cloud-badge-synced">
                              ☁️ Synced ({r.cloud_status})
                            </span>
                          ) : (
                            <span className="cloud-badge-pending">
                              ⚪ Local Only
                            </span>
                          )}
                          {r.human_verdict && (
                            <span className="cloud-badge-resolved" style={{ marginLeft: '6px' }}>
                              ✅ Resolved
                            </span>
                          )}
                        </td>
                        <td style={{ padding: '12px 16px' }}>
                          {r.defect_fields?.length > 0 ? (
                            <span style={{ color: '#c62828', fontWeight: 600, fontSize: '0.82rem' }}>
                              🔴 {r.defect_fields.length} defect(s): {r.defect_fields.join(', ')}
                            </span>
                          ) : r.review_reason ? (
                            <span style={{ color: '#e65100', fontWeight: 600, fontSize: '0.82rem' }}>
                              ⚠️ {r.review_reason}
                            </span>
                          ) : r.cloud_status === 'OK' ? (
                            <span style={{ color: '#2e7d32', fontWeight: 600, fontSize: '0.82rem' }}>
                              🟢 7 Fields Matched
                            </span>
                          ) : (
                            <span style={{ color: '#888', fontStyle: 'italic', fontSize: '0.82rem' }}>
                              Pending verification
                            </span>
                          )}
                        </td>
                        <td style={{ padding: '12px 16px', fontSize: '0.82rem', color: '#666' }}>
                          {r.updated_at ? new Date(r.updated_at).toLocaleString() : 'Not synced'}
                        </td>
                        <td style={{ padding: '12px 16px', textAlign: 'right' }}>
                          <div style={{ display: 'inline-flex', gap: '8px', alignItems: 'center' }}>
                            {r.cloud_synced && (
                              <button
                                onClick={() => handlePullCloudRecord(r.email_id)}
                                style={{ padding: '6px 12px', fontSize: '0.8rem', background: '#597928', color: '#fff' }}
                                title="Pull from Supabase Cloud cache without re-running AI models"
                              >
                                ⚡ Pull Cloud
                              </button>
                            )}
                            <button
                              onClick={() => openDraftEmail(r.email_id, { subject: r.subject, from: r.sender, attachments: Array(r.attachments_count) }, { status: r.cloud_status, defect_fields: r.defect_fields })}
                              style={{ padding: '6px 12px', fontSize: '0.8rem', background: '#6e3511', color: '#fff' }}
                              title="Auto-draft email for this shipment"
                            >
                              ✉️ Draft
                            </button>
                            <button
                              onClick={() => openEmail(r.email_id)}
                              style={{ padding: '6px 12px', fontSize: '0.8rem', background: 'transparent', border: '1px solid var(--border-color)', color: 'var(--text-color)', boxShadow: 'none' }}
                            >
                              Inspect →
                            </button>
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>

                {/* Pagination */}
                <div style={{ padding: '12px 16px', display: 'flex', justifyContent: 'space-between', alignItems: 'center', borderTop: '1px solid var(--border-color)', background: '#faf8f5' }}>
                  <span style={{ fontSize: '0.85rem', color: '#666' }}>
                    Showing {Math.min(cloudRecords.length, (cloudPage - 1) * 25 + 1)}–{Math.min(cloudRecords.length, cloudPage * 25)} of {cloudRecords.length} records
                  </span>
                  <div style={{ display: 'flex', gap: '8px' }}>
                    <button
                      disabled={cloudPage <= 1}
                      onClick={() => setCloudPage(p => p - 1)}
                      style={{ padding: '6px 14px', fontSize: '0.82rem' }}
                    >
                      ← Prev
                    </button>
                    <span style={{ fontSize: '0.85rem', alignSelf: 'center', fontWeight: 600 }}>
                      Page {cloudPage} of {Math.max(1, Math.ceil(cloudRecords.length / 25))}
                    </span>
                    <button
                      disabled={cloudPage >= Math.ceil(cloudRecords.length / 25)}
                      onClick={() => setCloudPage(p => p + 1)}
                      style={{ padding: '6px 14px', fontSize: '0.82rem' }}
                    >
                      Next →
                    </button>
                  </div>
                </div>
              </div>
            )}
          </div>
        )}

        {/* ========================================================== */}
        {/* VIEW: PAPER SCAN (camera photos / handwritten documents)   */}
        {/* ========================================================== */}
        {view === 'scan' && (
          <>
            <div className="page-header">
              <h1>Paper Document Scan</h1>
              <p>Snap or upload a photo of a paper / handwritten SI and BL — the vision model reads handwriting, stamps, and skewed photos.</p>
            </div>

            <div className="action-bar">
              <button onClick={handleScanVerify} disabled={scanLoading || !scanSi?.file || !scanBl?.file}>
                {scanLoading ? 'Reading documents via vision AI...' : 'Verify Scanned Documents'}
              </button>
              <span style={{ fontSize: '0.82rem', color: '#777', alignSelf: 'center' }}>
                Accepts photos (JPG/PNG), scans, PDF, DOCX, XLSX, TXT · max 15 MB each
              </span>
            </div>

            {scanResult?.status === 'MISMATCH' && (
              <div className="redirect-banner">
                <div>
                  <strong>⚠️ Discrepancy Found:</strong> {scanResult.defect_fields?.length} field mismatch(es) detected between the scanned documents.
                </div>
              </div>
            )}

            <div className="main-container">
              <ScanUploadCard
                title="Shipping Instruction (SI)"
                file={scanSi?.file}
                preview={scanSi?.preview}
                transcript={scanResult?.si_text}
                onSelect={(f) => handleScanFile('si', f)}
                onClear={() => handleScanFile('si', null)}
              />
              <ScanUploadCard
                title="Bill of Lading (BL)"
                file={scanBl?.file}
                preview={scanBl?.preview}
                transcript={scanResult?.bl_text}
                onSelect={(f) => handleScanFile('bl', f)}
                onClear={() => handleScanFile('bl', null)}
              />
              <section className="doc-section glass-panel">
                <h2>Audit Verdict & AI Reasoning</h2>
                <VerdictPanel
                  result={scanResult}
                  loading={scanLoading}
                  error={scanError}
                  idleHint="Photograph or upload a paper SI and BL, then run the vision audit."
                  loadingHint="Reading paper documents via vision AI..."
                />
              </section>
            </div>
          </>
        )}

        {/* ========================================================== */}
        {/* VIEW: AUDIT LOG                                            */}
        {/* ========================================================== */}
        {view === 'audit' && (
          <div className="card-container">
            <div className="page-header">
              <h1>Audit Log</h1>
              <p>Immutable trail of every automated verdict, chaser, and operator action. <em>(In-memory until the database is wired up — resets on backend restart.)</em></p>
            </div>

            <div className="item-card">
              {auditEvents.length === 0 ? (
                <p style={{ textAlign: 'center', padding: '20px', opacity: 0.7 }}>
                  No audit events recorded yet.
                </p>
              ) : (
                auditEvents.map((ev, idx) => (
                  <div key={idx} className="timeline-item">
                    <span
                      className="timeline-dot"
                      style={{ background: AUDIT_DOT_COLORS[ev.action] || '#999' }}
                    />
                    <div style={{ flex: 1 }}>
                      <div style={{ display: 'flex', gap: '10px', alignItems: 'center', flexWrap: 'wrap' }}>
                        {String(ev.email_id).startsWith('scan-') ? (
                          <span style={{ fontWeight: 'bold', fontSize: '0.9rem', color: 'var(--primary-color)' }}>
                            📷 {ev.email_id}
                          </span>
                        ) : (
                          <button
                            onClick={() => openEmail(ev.email_id)}
                            style={{
                              background: 'transparent',
                              color: 'var(--primary-color)',
                              padding: 0,
                              boxShadow: 'none',
                              fontWeight: 'bold',
                              fontSize: '0.9rem',
                              textDecoration: 'underline',
                            }}
                          >
                            {ev.email_id}
                          </button>
                        )}
                        <span style={{ fontWeight: 'bold', fontSize: '0.85rem' }}>{ev.action}</span>
                        <span style={{ fontSize: '0.78rem', color: '#777' }}>
                          by {ev.actor || 'system'}
                        </span>
                        <span style={{ fontSize: '0.78rem', color: '#999', marginLeft: 'auto' }}>
                          {ev.timestamp ? new Date(ev.timestamp).toLocaleString() : ''}
                        </span>
                      </div>
                      {ev.notes && (
                        <p style={{ fontSize: '0.85rem', marginTop: '4px', color: '#555' }}>{ev.notes}</p>
                      )}
                    </div>
                  </div>
                ))
              )}
            </div>
          </div>
        )}

        {/* ========================================================== */}
        {/* VIEW: SUBMISSION                                           */}
        {/* ========================================================== */}
        {view === 'pipeline' && (() => {
          const score = compareData?.score
          const fmtPct = v => (v == null ? '—' : `${(v * 100).toFixed(1)}%`)
          const diffRows = (compareData?.rows || []).filter(r => r.diffs?.length)
          const visibleDiffRows = diffFilter
            ? diffRows.filter(r => r.email_id.toLowerCase().includes(diffFilter.toLowerCase()))
            : diffRows
          const shownRows = showAllDiffs ? visibleDiffRows : visibleDiffRows.slice(0, 15)
          const scoreColor =
            score?.final_score == null ? '#888'
            : score.final_score >= 0.7 ? '#2e7d32'
            : score.final_score >= 0.4 ? '#e07a5f' : '#c62828'
          const scoreGrade =
            score?.final_score == null ? null
            : score.final_score >= 0.9 ? 'Excellent'
            : score.final_score >= 0.7 ? 'Good'
            : score.final_score >= 0.4 ? 'Needs work' : 'Poor'
          const hasSubmission = (pipelineStatus?.submission_size ?? 0) > 0
          const step1Done = hasSubmission && !pipelineRunning
          const interrupted =
            !pipelineRunning && pipelineStatus &&
            pipelineStatus.processed > 0 && !pipelineStatus.done && !pipelineStatus.error
          const scrollToScore = () => {
            fetchCompare()
            document.getElementById('score-card')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
          }
          const kpiMeter = v => (
            <div className="kpi-meter"><div style={{ width: `${Math.min(100, Math.max(0, (v ?? 0) * 100))}%` }} /></div>
          )

          // Export table derived values — issues surface first
          const statusRank = s => (s === 'MISMATCH' ? 0 : s === 'NEEDS_REVIEW' ? 1 : s === 'OK' ? 2 : 3)
          const subEntries = submissionData ? Object.entries(submissionData) : []
          const filteredSubEntries = subEntries
            .filter(([eid, r]) => {
              if (subSearch && !eid.toLowerCase().includes(subSearch.trim().toLowerCase())) return false
              if (subFilter === 'issues') return r?.status !== 'OK'
              if (subFilter === 'ok') return r?.status === 'OK'
              return true
            })
            .sort((a, b) => statusRank(a[1]?.status) - statusRank(b[1]?.status) || a[0].localeCompare(b[0]))
          const shownSubEntries = showAllSub ? filteredSubEntries : filteredSubEntries.slice(0, 50)
          const visibleSubIds = filteredSubEntries.map(([eid]) => eid)
          const allVisibleSelected = visibleSubIds.length > 0 && visibleSubIds.every(id => selectedEmails.has(id))
          const someVisibleSelected = visibleSubIds.some(id => selectedEmails.has(id))
          const toggleSubEmail = (eid) => setSelectedEmails(prev => {
            const next = new Set(prev)
            if (next.has(eid)) next.delete(eid); else next.add(eid)
            return next
          })
          const toggleAllVisible = () => setSelectedEmails(prev => {
            const next = new Set(prev)
            if (allVisibleSelected) visibleSubIds.forEach(id => next.delete(id))
            else visibleSubIds.forEach(id => next.add(id))
            return next
          })

          return (
            <div className="card-container">
              <div className="page-header">
                <h1>Submission Builder</h1>
                <p>Generate submission.json from the 520-email inbox, then grade it against ground truth.</p>
              </div>

              {/* Workflow stepper */}
              <div className="sub-stepper">
                <div className={`sub-step ${step1Done ? 'is-done' : 'is-active'}`}>
                  <span className="sub-step-dot">{step1Done ? '✓' : '1'}</span>
                  <div className="sub-step-text">
                    <strong>Generate</strong>
                    <span>{hasSubmission ? `${pipelineStatus.submission_size}/520 records saved` : 'Build submission.json'}</span>
                  </div>
                </div>
                <div className={`sub-step-line ${step1Done ? 'is-filled' : ''}`} />
                <div
                  className={`sub-step ${compareData ? 'is-done' : step1Done ? 'is-active' : ''} ${step1Done ? 'is-clickable' : ''}`}
                  onClick={() => step1Done && document.getElementById('score-card')?.scrollIntoView({ behavior: 'smooth', block: 'start' })}
                >
                  <span className="sub-step-dot">{compareData ? '✓' : '2'}</span>
                  <div className="sub-step-text">
                    <strong>Score</strong>
                    <span>{compareData ? 'Graded vs ground truth' : 'Grade against ground truth'}</span>
                  </div>
                </div>
                <div className={`sub-step-line ${hasSubmission ? 'is-filled' : ''}`} />
                <div
                  className={`sub-step ${exportedOnce ? 'is-done' : hasSubmission ? 'is-active' : ''} ${hasSubmission ? 'is-clickable' : ''}`}
                  onClick={() => hasSubmission && document.getElementById('export-card')?.scrollIntoView({ behavior: 'smooth', block: 'start' })}
                >
                  <span className="sub-step-dot">{exportedOnce ? '✓' : '3'}</span>
                  <div className="sub-step-text">
                    <strong>Export</strong>
                    <span>{exportedOnce ? 'Downloaded' : 'Pick records & download'}</span>
                  </div>
                </div>
              </div>

              {/* STEP 1 — GENERATE */}
              <div className="item-card">
                <div className="sub-card-head">
                  <div>
                    <h3>Generate submission.json</h3>
                    <p className="step-sub">
                      Classifies, extracts and cross-checks every inbox email — batched 10 at a time,
                      4 in parallel. Progress is saved continuously, so an interrupted run can be resumed.
                    </p>
                  </div>
                  <span className={`sub-file-pill ${hasSubmission ? 'has-file' : ''}`}>
                    {hasSubmission ? `📄 ${pipelineStatus.submission_size}/520 saved` : '📄 No submission yet'}
                  </span>
                </div>

                {!pipelineRunning && (
                  <>
                    <div className="sub-cta-row">
                      <button className="sub-primary-btn" onClick={handleStartPipeline}>
                        {hasSubmission ? '▶ Resume Generation' : '▶ Generate Submission'}
                      </button>
                      {hasSubmission && pipelineStatus?.finished_at && (
                        <span className="sub-last-run">Last completed run: {pipelineStatus.finished_at}</span>
                      )}
                    </div>

                    <details className="sub-advanced">
                      <summary>Advanced options</summary>
                      <div className="sub-advanced-body">
                        <div className="sub-option">
                          <label htmlFor="sub-limit">Limit</label>
                          <input
                            id="sub-limit"
                            type="number"
                            min="0"
                            value={maxEmails}
                            onChange={(e) => setMaxEmails(e.target.value)}
                          />
                          <span>emails (0 = all 520)</span>
                        </div>
                        <label className="sub-checkbox">
                          <input
                            type="checkbox"
                            checked={freshRun}
                            onChange={(e) => setFreshRun(e.target.checked)}
                          />
                          Fresh run — discard saved progress and start over
                        </label>
                      </div>
                    </details>
                  </>
                )}

                {pipelineMsg && !pipelineRunning && (
                  <div className="sub-msg">{pipelineMsg}</div>
                )}

                {/* Live progress */}
                {pipelineRunning && (
                  <div className="sub-progress-panel">
                    <div className="sub-progress-meta">
                      <span className="sub-progress-current">
                        <span className="sub-pulse" />
                        {pipelineStatus?.current ? `Processing ${pipelineStatus.current}` : 'Starting…'}
                      </span>
                      <strong>{pipelinePct}%</strong>
                    </div>
                    <div className="progress-track">
                      <div className="progress-fill animated" style={{ width: `${pipelinePct}%` }} />
                    </div>
                    <div className="sub-progress-foot">
                      <span>
                        {pipelineStatus?.processed ?? 0} / {pipelineStatus?.total ?? 0} emails processed
                        {pipelineStatus?.skipped ? ` · ${pipelineStatus.skipped} resumed` : ''}
                      </span>
                      <button onClick={handleCancelPipeline} className="sub-cancel-btn">
                        ⏹ Cancel run
                      </button>
                    </div>
                    {pipelineMsg && <div className="sub-msg">{pipelineMsg}</div>}
                  </div>
                )}

                {/* Interrupted run hint */}
                {interrupted && (
                  <div className="sub-warn">
                    ⏸ Previous run stopped at {pipelineStatus.processed}/{pipelineStatus.total} emails —
                    press Resume Generation to pick up where it left off.
                  </div>
                )}

                {pipelineStatus?.error && !pipelineRunning && (
                  <div className="sub-error">
                    {pipelineStatus.error === 'cancelled' ? '⏹ Run cancelled — partial results were saved.' : `Error: ${pipelineStatus.error}`}
                  </div>
                )}

                {/* Done state */}
                {pipelineStatus?.done && !pipelineStatus.error && (
                  <div className="sub-done">
                    <div className="sub-done-text">
                      <strong>✅ {pipelineStatus.submission_size} records ready</strong>
                      {pipelineStatus.supabase && (
                        <span className="sub-done-note">Synced to Supabase: {pipelineStatus.supabase}</span>
                      )}
                    </div>
                    <div className="sub-done-actions">
                      <button onClick={handleDownloadSubmission} className="sub-ghost-btn">
                        ⬇ Download JSON
                      </button>
                      <button onClick={scrollToScore} className="sub-primary-btn">
                        Score it →
                      </button>
                    </div>
                  </div>
                )}
              </div>

              {/* STEP 2 — SCORE */}
              <div className="item-card" id="score-card">
                <div className="sub-card-head">
                  <div>
                    <h3>Score vs Ground Truth</h3>
                    <p className="step-sub">
                      Grades the latest submission.json against data_v2/ground_truth.json —
                      weighted: Stage-1 classification 30% · Stage-3 defects 20% · End-to-end 50%.
                    </p>
                  </div>
                  {compareData && (
                    <button onClick={fetchCompare} disabled={compareLoading} className="sub-ghost-btn">
                      {compareLoading ? 'Scoring…' : '↻ Re-check'}
                    </button>
                  )}
                </div>

                {compareError && (
                  <div className="sub-error" style={{ marginTop: 0 }}>{compareError}</div>
                )}

                {/* Empty state */}
                {!compareData && !compareError && (
                  <div className="sub-empty">
                    <span className="sub-empty-icon">🎯</span>
                    <p>
                      {hasSubmission
                        ? 'Your submission is on disk — run the scorer to see how it grades against ground truth.'
                        : 'No score yet. Generate a submission above, then grade it here.'}
                    </p>
                    <button onClick={fetchCompare} disabled={compareLoading} className="sub-primary-btn">
                      {compareLoading ? 'Scoring…' : '🎯 Check Score'}
                    </button>
                  </div>
                )}

                {compareData && (
                  <>
                    {/* Hero score */}
                    <div className="score-hero">
                      <div className="score-hero-left">
                        <div className="score-hero-value" style={{ color: scoreColor }}>
                          {score ? fmtPct(score.final_score) : '—'}
                        </div>
                        {scoreGrade && (
                          <span className="score-grade" style={{ background: scoreColor }}>{scoreGrade}</span>
                        )}
                      </div>
                      <div className="score-hero-meta">
                        <strong>Final Score</strong>
                        <span>
                          {compareData.submission_count}/{compareData.ground_truth_count} emails submitted
                          · {compareData.summary?.perfect} perfect
                          · {compareData.summary?.with_diffs} with diffs
                          {compareData.summary?.missing ? ` · ${compareData.summary.missing} missing` : ''}
                        </span>
                        <span className="score-hero-src">source: {compareData.source}</span>
                      </div>
                    </div>

                    {compareData.summary?.missing > 0 && (
                      <div className="sub-warn">
                        ⚠ {compareData.summary.missing} emails are missing from the submission and score zero —
                        re-run generation to fill the gaps.
                      </div>
                    )}

                    {score && (
                      <div className="kpi-grid" style={{ marginTop: '16px' }}>
                        <div className="kpi-card">
                          <div className="kpi-value">{fmtPct(score.stage1?.macro_f1)}</div>
                          <div className="kpi-label">Classification F1</div>
                          {kpiMeter(score.stage1?.macro_f1)}
                          <div className="kpi-sub">{fmtPct(score.stage1?.accuracy)} accuracy · 30% of final</div>
                        </div>
                        <div className="kpi-card">
                          <div className="kpi-value">{fmtPct(score.stage3?.defect_f1)}</div>
                          <div className="kpi-label">Defect Detection F1</div>
                          {kpiMeter(score.stage3?.defect_f1)}
                          <div className="kpi-sub">{fmtPct(score.stage3?.exact_match_rate)} exact field match · 20% of final</div>
                        </div>
                        <div className="kpi-card">
                          <div className="kpi-value">{fmtPct(score.reliability?.escalation_f1)}</div>
                          <div className="kpi-label">Escalation F1</div>
                          {kpiMeter(score.reliability?.escalation_f1)}
                          <div className="kpi-sub">{score.reliability?.pred_review} flagged for review</div>
                        </div>
                        <div className="kpi-card">
                          <div className="kpi-value" style={{ color: '#e07a5f' }}>
                            {score.end_to_end?.success}/{score.end_to_end?.total}
                          </div>
                          <div className="kpi-label">End-to-End Defects Caught</div>
                          {kpiMeter(score.end_to_end?.rate)}
                          <div className="kpi-sub">{fmtPct(score.end_to_end?.rate)} of planted defects · 50% of final</div>
                        </div>
                      </div>
                    )}

                    {/* Diff table */}
                    {diffRows.length > 0 && (
                      <div className="sub-diffs">
                        <div className="sub-diffs-head">
                          <h4>
                            Differences
                            <span className="sub-diffs-count">
                              {diffFilter ? `${visibleDiffRows.length} of ${diffRows.length}` : diffRows.length}
                            </span>
                          </h4>
                          <input
                            className="sub-diff-filter"
                            placeholder="Filter by email id…"
                            value={diffFilter}
                            onChange={(e) => setDiffFilter(e.target.value)}
                          />
                        </div>
                        <div className="diff-table-wrap">
                          <table className="diff-table">
                            <thead>
                              <tr>
                                <th>Email</th>
                                <th>Category (truth → yours)</th>
                                <th>Status (truth → yours)</th>
                                <th>Differing fields</th>
                              </tr>
                            </thead>
                            <tbody>
                              {shownRows.map(r => (
                                <tr key={r.email_id}>
                                  <td className="mono">{r.email_id}</td>
                                  <td>
                                    <span className="truth-val">{r.truth?.category || '—'}</span>
                                    <span className="arrow">→</span>
                                    <span className={`sub-val ${r.truth?.category !== r.submission?.category ? 'bad' : ''}`}>
                                      {r.submission?.category || '—'}
                                    </span>
                                  </td>
                                  <td>
                                    <span className="truth-val">{r.truth?.status || '—'}</span>
                                    <span className="arrow">→</span>
                                    <span className={`sub-val ${r.truth?.status !== r.submission?.status ? 'bad' : ''}`}>
                                      {r.submission?.status || '—'}
                                    </span>
                                  </td>
                                  <td>
                                    <div className="diff-chips">
                                      {r.diffs.map(d => <span key={d} className="diff-field-chip">{d}</span>)}
                                    </div>
                                  </td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
                          {visibleDiffRows.length === 0 && (
                            <p className="diff-more">No rows match “{diffFilter}”.</p>
                          )}
                          {visibleDiffRows.length > 15 && (
                            <button className="diff-toggle" onClick={() => setShowAllDiffs(s => !s)}>
                              {showAllDiffs ? '▲ Show less' : `▼ Show all ${visibleDiffRows.length} differences`}
                            </button>
                          )}
                        </div>
                      </div>
                    )}

                    {score && (
                      <details style={{ marginTop: '14px', fontSize: '0.82rem' }}>
                        <summary style={{ cursor: 'pointer', opacity: 0.7 }}>Raw score JSON</summary>
                        <pre style={{ marginTop: '8px', background: '#faf8f5', padding: '12px', borderRadius: '8px', overflowX: 'auto' }}>
                          {JSON.stringify(score, null, 2)}
                        </pre>
                      </details>
                    )}
                  </>
                )}
              </div>

              {/* STEP 3 — REVIEW & DOWNLOAD */}
              <div className="item-card" id="export-card">
                <div className="sub-card-head">
                  <div>
                    <h3>Review & Download</h3>
                    <p className="step-sub">
                      Tick the records to include in the downloaded submission.json — issues are listed
                      first so you can spot-check them. Unchecked rows are left out of the file.
                    </p>
                  </div>
                  {submissionData && (
                    <button onClick={loadSubmissionRecords} disabled={subLoading} className="sub-ghost-btn">
                      {subLoading ? 'Loading…' : '↻ Refresh'}
                    </button>
                  )}
                </div>

                {!hasSubmission ? (
                  <div className="sub-empty">
                    <span className="sub-empty-icon">📦</span>
                    <p>Nothing to export yet — generate a submission in step 1 first.</p>
                  </div>
                ) : !submissionData ? (
                  <div className="sub-empty">
                    <span className="sub-empty-icon">⏳</span>
                    <p>Loading submission records…</p>
                  </div>
                ) : (
                  <>
                    {/* Toolbar: search + status filter chips */}
                    <div className="sub-export-tools">
                      <input
                        className="sub-diff-filter"
                        placeholder="Filter by email id…"
                        value={subSearch}
                        onChange={(e) => setSubSearch(e.target.value)}
                      />
                      <div className="sub-export-chips">
                        {[
                          ['all', `All (${subEntries.length})`],
                          ['issues', `With issues (${subEntries.filter(([, r]) => r?.status !== 'OK').length})`],
                          ['ok', `OK only (${subEntries.filter(([, r]) => r?.status === 'OK').length})`],
                        ].map(([k, label]) => (
                          <button
                            key={k}
                            className={`chip ${subFilter === k ? 'active' : ''}`}
                            onClick={() => setSubFilter(k)}
                          >
                            {label}
                          </button>
                        ))}
                      </div>
                    </div>

                    <div className="diff-table-wrap sub-export-wrap">
                      <table className="diff-table sub-export-table">
                        <thead>
                          <tr>
                            <th className="check-col">
                              <input
                                type="checkbox"
                                checked={allVisibleSelected}
                                ref={el => { if (el) el.indeterminate = !allVisibleSelected && someVisibleSelected }}
                                onChange={toggleAllVisible}
                                title="Select all shown"
                              />
                            </th>
                            <th>Email</th>
                            <th>Category</th>
                            <th>Status</th>
                            <th>Defects / notes</th>
                          </tr>
                        </thead>
                        <tbody>
                          {shownSubEntries.map(([eid, r]) => {
                            const m = emailStatusMeta(r?.status)
                            return (
                              <tr
                                key={eid}
                                className={selectedEmails.has(eid) ? '' : 'row-excluded'}
                                onClick={() => toggleSubEmail(eid)}
                              >
                                <td className="check-col" onClick={e => e.stopPropagation()}>
                                  <input
                                    type="checkbox"
                                    checked={selectedEmails.has(eid)}
                                    onChange={() => toggleSubEmail(eid)}
                                  />
                                </td>
                                <td className="mono">{eid}</td>
                                <td>{r?.category || '—'}</td>
                                <td>
                                  <span className="ep-status" style={{ color: m.color, background: m.bg }}>
                                    {m.label}
                                  </span>
                                </td>
                                <td>
                                  {r?.defect_fields?.length ? (
                                    <div className="diff-chips">
                                      {r.defect_fields.map(d => <span key={d} className="diff-field-chip">{d}</span>)}
                                    </div>
                                  ) : r?.review_reason ? (
                                    <span className="truth-val">{r.review_reason}</span>
                                  ) : '—'}
                                </td>
                              </tr>
                            )
                          })}
                        </tbody>
                      </table>
                      {filteredSubEntries.length === 0 && (
                        <p className="diff-more">No records match the current filters.</p>
                      )}
                      {filteredSubEntries.length > 50 && (
                        <button className="diff-toggle" onClick={() => setShowAllSub(s => !s)}>
                          {showAllSub ? '▲ Show less' : `▼ Show all ${filteredSubEntries.length} records`}
                        </button>
                      )}
                    </div>

                    {/* Selection footer */}
                    <div className="sub-export-foot">
                      <span className="sub-export-count">
                        <strong>{selectedEmails.size}</strong> of {subEntries.length} selected
                        {filteredSubEntries.length !== subEntries.length && ` · ${filteredSubEntries.length} shown`}
                      </span>
                      <div className="sub-export-actions">
                        <button
                          className="sub-ghost-btn"
                          onClick={() => setSelectedEmails(new Set(subEntries.map(([eid]) => eid)))}
                        >
                          Select all
                        </button>
                        <button className="sub-ghost-btn" onClick={() => setSelectedEmails(new Set())}>
                          Clear
                        </button>
                        <button
                          className="sub-primary-btn"
                          onClick={handleDownloadSelected}
                          disabled={selectedEmails.size === 0}
                        >
                          ⬇ Download selected ({selectedEmails.size})
                        </button>
                      </div>
                    </div>
                  </>
                )}
              </div>
            </div>
          )
        })()}


      </main>

      <AutoDraftEmailModal
        open={emailModalOpen}
        onClose={() => setEmailModalOpen(false)}
        draft={emailDraft}
        onChange={setEmailDraft}
        onSend={handleSendSmtpEmail}
        sending={sendingEmail}
        result={emailSendResult}
        smtpConfig={smtpConfig}
        onSmtpChange={setSmtpConfig}
        showSmtp={showSmtpSettings}
        onToggleSmtp={() => setShowSmtpSettings(s => !s)}
      />
    </div>
  )
}

export default App
