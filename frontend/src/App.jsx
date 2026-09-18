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

function App() {
  // ---- Shared / Verification Hub ----
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

  // ---- Inline Resolution ----
  const [resolutions, setResolutions] = useState({})
  const [resolutionNotes, setResolutionNotes] = useState('')
  const [resolveSuccess, setResolveSuccess] = useState(null)
  const [savingResolution, setSavingResolution] = useState(false)
  const [resolutionRecord, setResolutionRecord] = useState(null) // stored RESOLVED record for selected email

  // ---- Navigation: 'dashboard' | 'queue' | 'verify' | 'audit' | 'pipeline' ----
  const [view, setView] = useState('dashboard')

  // ---- Dashboard ----
  const [stats, setStats] = useState(null)

  // ---- Work Queue ----
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

  // ---- Chaser actions (Work Queue, missing_bl filter) ----
  const [batchChasing, setBatchChasing] = useState(false)

  // ---- Score vs Ground Truth (Pipeline Run page) ----
  const [compareData, setCompareData] = useState(null)
  const [compareLoading, setCompareLoading] = useState(false)
  const [compareError, setCompareError] = useState(null)

  // ---- Backend AI config (sidebar footer) ----
  const [aiConfig, setAiConfig] = useState(null)

  // ============================================================
  // DATA FETCHING
  // ============================================================

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

  // Email list (verification dropdown) — once on mount
  useEffect(() => {
    fetch(`${API}/api/emails`)
      .then(res => res.json())
      .then(data => {
        if (data.emails) setEmails(data.emails)
      })
      .catch(err => console.error('Failed to fetch emails:', err))

    fetch(`${API}/api/config`)
      .then(res => (res.ok ? res.json() : null))
      .then(data => setAiConfig(data && data.provider ? data : null))
      .catch(err => console.error('Failed to fetch AI config:', err))
  }, [])

  // Debounced (300ms) queue fetch — covers mount + filter/search/page changes
  useEffect(() => {
    const t = setTimeout(() => {
      fetchQueue(queueFilter, queueSearch, queuePage)
    }, 300)
    return () => clearTimeout(t)
  }, [queueFilter, queueSearch, queuePage, fetchQueue])

  // Dashboard stats — on entering the view
  useEffect(() => {
    if (view !== 'dashboard') return
    fetch(`${API}/api/stats`)
      .then(res => res.json())
      .then(data => setStats(data))
      .catch(err => console.error('Failed to fetch stats:', err))
  }, [view])

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
  }, [pipelineRunning])

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

  // ============================================================
  // VERIFICATION HUB HANDLERS
  // ============================================================

  const loadEmailData = async (eid) => {
    setSelectedEmail(eid)
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
    setSiText('Loading attachment...')
    setBlText('Loading attachment...')

    try {
      const response = await fetch(`${API}/api/email/${eid}`)
      if (!response.ok) throw new Error('Failed to load email content')
      const data = await response.json()
      setSiText(data.si_text || '')
      setBlText(data.bl_text || '')
      if (data.email) setEmailInfo(data.email)
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

  const handleSelectEmail = (e) => {
    const eid = e.target.value
    if (eid) loadEmailData(eid)
  }

  const openEmail = (eid) => {
    loadEmailData(eid)
    setView('verify')
  }

  const openQueueFilter = (filter) => {
    setQueueFilter(filter)
    setQueuePage(1)
    setQueueSearch('')
    setView('queue')
  }

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
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
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

  const handleSubmitResolution = async () => {
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

  async function handleDownloadSubmission() {
    try {
      const res = await fetch(`${API}/api/pipeline/submission`)
      if (!res.ok) throw new Error(`Server error: ${res.status}`)
      const data = await res.json()
      const blob = new Blob([JSON.stringify(data.submission ?? data, null, 2)], {
        type: 'application/json',
      })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = 'submission.json'
      document.body.appendChild(a)
      a.click()
      a.remove()
      URL.revokeObjectURL(url)
    } catch (err) {
      setPipelineMsg(`Download failed: ${err.message}`)
    }
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
            className={`sidebar-btn ${view === 'queue' ? 'active' : ''}`}
            onClick={() => setView('queue')}
          >
            <span>📥 Work Queue</span>
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
            <span>📄 Verification Hub</span>
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
            className={`sidebar-btn ${view === 'audit' ? 'active' : ''}`}
            onClick={() => setView('audit')}
          >
            <span>🧾 Audit Log</span>
          </button>

          <button
            className={`sidebar-btn ${view === 'pipeline' ? 'active' : ''}`}
            onClick={() => setView('pipeline')}
          >
            <span>� Submission</span>
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
                        title={targetFilter ? `Open ${k.label} in Work Queue` : undefined}
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
        {/* VIEW: WORK QUEUE                                           */}
        {/* ========================================================== */}
        {view === 'queue' && (
          <div style={{ maxWidth: '1100px' }}>
            <div className="page-header">
              <h1>Work Queue</h1>
              <p>Unified queue: mismatches, review flags, missing BLs, corrupted files, resolutions, and chasers.</p>
            </div>

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

        {/* VIEW: VERIFICATION HUB                                     */}
        {/* ========================================================== */}
        {view === 'verify' && (
          <>
            <div className="page-header">
              <h1>Document Verification Dashboard</h1>
              <p>Review incoming inbox emails, inspect attachments, and compare SI vs draft BL.</p>
            </div>

            <div className="action-bar">
              <select
                value={selectedEmail}
                onChange={handleSelectEmail}
                style={{ padding: '10px 14px', borderRadius: '8px', border: '1px solid var(--border-color)', fontSize: '1rem', minWidth: '280px' }}
              >
                <option value="">-- Select an Email / Bill --</option>
                {emails.map(e => <option key={e} value={e}>{e}</option>)}
              </select>

              <button onClick={handleVerify} disabled={loading || !selectedEmail}>
                {loading ? 'Analyzing via NVIDIA NIM...' : 'Verify Documents'}
              </button>

              <button
                onClick={() => setView('queue')}
                style={{ background: 'transparent', border: '1px solid var(--border-color)', color: 'var(--text-color)', marginLeft: 'auto', boxShadow: 'none' }}
              >
                Open Work Queue →
              </button>
            </div>

            {/* Banner: Corrupted File Detected Immediately on Selection */}
            {corruptWarning && (
              <div className="redirect-banner" style={{ background: '#f8d7da', borderColor: '#f5c6cb', color: '#721c24' }}>
                <div>
                  <strong>⚠️ Corrupted Attachment Detected:</strong>{' '}
                  {corruptWarning.details || corruptWarning.reason || 'Document is unreadable or truncated.'}
                </div>
                <button
                  onClick={() => openQueueFilter('corrupted')}
                  style={{ padding: '6px 14px', fontSize: '0.85rem', background: '#dc3545' }}
                >
                  Inspect in Work Queue →
                </button>
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
                <div className="result-panel">
                  {error && (
                    <div style={{ color: '#721c24', background: '#f8d7da', padding: '10px', borderRadius: '6px', marginBottom: '14px' }}>
                      <strong>Error:</strong> {error}
                    </div>
                  )}

                  {!result && !loading && !error && (
                    <div style={{ opacity: 0.6, textAlign: 'center', marginTop: '120px' }}>
                      Select a bill above or browse the work queue to run AI extraction and comparison.
                    </div>
                  )}

                  {loading && (
                    <div style={{ textAlign: 'center', marginTop: '120px' }}>
                      <div style={{ fontSize: '2.5rem' }}>🧠</div>
                      <p style={{ marginTop: '12px', fontWeight: 'bold' }}>Auditing with NVIDIA AI...</p>
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
                                    <span style={{
                                      fontSize: '0.75rem',
                                      padding: '2px 8px',
                                      borderRadius: '10px',
                                      fontWeight: 'bold',
                                      background: isMismatch ? '#f8d7da' : '#d4edda',
                                      color: isMismatch ? '#721c24' : '#155724',
                                    }}>
                                      {isMismatch ? 'MISMATCH' : 'MATCH'}
                                    </span>
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
                    onClick={handleSubmitResolution}
                    disabled={savingResolution}
                  >
                    {savingResolution ? 'Logging Resolution...' : 'Approve & Mark Resolved'}
                  </button>
                </div>
              </div>
            )}
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
          const scoreColor =
            score?.final_score == null ? '#888'
            : score.final_score >= 0.7 ? '#2e7d32'
            : score.final_score >= 0.4 ? '#e07a5f' : '#c62828'

          return (
            <div className="card-container">
              <div className="page-header">
                <h1>Submission Builder</h1>
                <p>Generate submission.json for the 520-email inbox, then score it against ground truth.</p>
              </div>

              {/* STEP 1 — GENERATE */}
              <div className="item-card">
                <div className="step-header">
                  <span className="step-num">1</span>
                  <div>
                    <h3>Generate submission.json</h3>
                    <p className="step-sub">
                      Classify + extract + compare, batched 10-at-a-time with 4 emails in parallel.
                    </p>
                  </div>
                </div>

                {/* Saved-state strip */}
                <div className="sub-state-strip">
                  {pipelineStatus?.submission_size > 0 ? (
                    <>
                      <span>📄 <strong>{pipelineStatus.submission_size}</strong> / 520 records already saved in submission.json</span>
                      {pipelineStatus.finished_at && <span>· last run {pipelineStatus.finished_at}</span>}
                    </>
                  ) : (
                    <span>📄 No submission generated yet — this will create one.</span>
                  )}
                </div>

                {/* Controls */}
                <div className="sub-controls">
                  <button onClick={handleStartPipeline} disabled={pipelineRunning} style={{ minWidth: '180px' }}>
                    {pipelineRunning ? 'Running…' : pipelineStatus?.submission_size > 0 ? '▶ Resume Generation' : '▶ Generate Submission'}
                  </button>

                  {pipelineRunning && (
                    <button onClick={handleCancelPipeline} className="sub-cancel-btn">
                      ⏹ Cancel
                    </button>
                  )}

                  <div className="sub-option">
                    <label>Limit</label>
                    <input
                      type="number"
                      min="0"
                      value={maxEmails}
                      onChange={(e) => setMaxEmails(e.target.value)}
                      disabled={pipelineRunning}
                    />
                    <span>emails (0 = all)</span>
                  </div>

                  <label className="sub-checkbox">
                    <input
                      type="checkbox"
                      checked={freshRun}
                      onChange={(e) => setFreshRun(e.target.checked)}
                      disabled={pipelineRunning}
                    />
                    Fresh run (ignore saved progress)
                  </label>
                </div>

                {pipelineMsg && (
                  <div className="sub-msg">{pipelineMsg}</div>
                )}

                {/* Progress */}
                {(pipelineRunning || (pipelineStatus && pipelineStatus.processed > 0 && !pipelineStatus.done)) && (
                  <div className="sub-progress">
                    <div className="sub-progress-meta">
                      <span>
                        {pipelineStatus?.current ? `Working on ${pipelineStatus.current}` : 'Starting…'}
                      </span>
                      <strong>{pipelinePct}%</strong>
                    </div>
                    <div className="progress-track">
                      <div className="progress-fill" style={{ width: `${pipelinePct}%` }} />
                    </div>
                    <p className="sub-progress-detail">
                      {pipelineStatus?.processed ?? 0} / {pipelineStatus?.total ?? 0} emails processed
                      {pipelineStatus?.skipped ? ` · ${pipelineStatus.skipped} resumed` : ''}
                    </p>
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
                    <strong>✅ {pipelineStatus.submission_size} records ready</strong>
                    {pipelineStatus.supabase && (
                      <span className="sub-done-note">
                        Supabase: {pipelineStatus.supabase}
                      </span>
                    )}
                    <button onClick={handleDownloadSubmission} style={{ padding: '8px 18px', fontSize: '0.85rem' }}>
                      ⬇ Download submission.json
                    </button>
                  </div>
                )}
              </div>

              {/* STEP 2 — SCORE */}
              <div className="item-card" style={{ marginTop: '20px' }}>
                <div className="step-header">
                  <span className="step-num">2</span>
                  <div>
                    <h3>Score vs Ground Truth</h3>
                    <p className="step-sub">
                      Grades the latest submission against data_v2/ground_truth.json
                      (Stage-1 30% · Stage-3 20% · End-to-end 50%).
                    </p>
                  </div>
                  <button
                    onClick={fetchCompare}
                    disabled={compareLoading}
                    style={{ marginLeft: 'auto', padding: '8px 18px', fontSize: '0.85rem' }}
                  >
                    {compareLoading ? 'Scoring…' : '🎯 Check Score'}
                  </button>
                </div>

                {compareError && (
                  <div className="sub-error" style={{ marginTop: '12px' }}>{compareError}</div>
                )}

                {compareData && (
                  <>
                    {/* Hero score */}
                    <div className="score-hero">
                      <div className="score-hero-value" style={{ color: scoreColor }}>
                        {score ? fmtPct(score.final_score) : '—'}
                      </div>
                      <div className="score-hero-meta">
                        <strong>Final Score</strong>
                        <span>
                          {compareData.submission_count}/{compareData.ground_truth_count} emails submitted
                          · {compareData.summary?.perfect} perfect
                          · {compareData.summary?.with_diffs} with diffs
                          {compareData.summary?.missing ? ` · ${compareData.summary.missing} missing` : ''}
                        </span>
                        <span style={{ fontSize: '0.78rem', opacity: 0.7 }}>source: {compareData.source}</span>
                      </div>
                    </div>

                    {score && (
                      <div className="kpi-grid" style={{ marginTop: '16px' }}>
                        <div className="kpi-card">
                          <div className="kpi-value">{fmtPct(score.stage1?.macro_f1)}</div>
                          <div className="kpi-label">Classification F1</div>
                          <div className="kpi-sub">{fmtPct(score.stage1?.accuracy)} accuracy</div>
                        </div>
                        <div className="kpi-card">
                          <div className="kpi-value">{fmtPct(score.stage3?.defect_f1)}</div>
                          <div className="kpi-label">Defect Detection F1</div>
                          <div className="kpi-sub">{fmtPct(score.stage3?.exact_match_rate)} exact field match</div>
                        </div>
                        <div className="kpi-card">
                          <div className="kpi-value">{fmtPct(score.reliability?.escalation_f1)}</div>
                          <div className="kpi-label">Escalation F1</div>
                          <div className="kpi-sub">{score.reliability?.pred_review} flagged for review</div>
                        </div>
                        <div className="kpi-card">
                          <div className="kpi-value" style={{ color: '#e07a5f' }}>
                            {score.end_to_end?.success}/{score.end_to_end?.total}
                          </div>
                          <div className="kpi-label">End-to-End Defects Caught</div>
                          <div className="kpi-sub">{fmtPct(score.end_to_end?.rate)} of planted defects</div>
                        </div>
                      </div>
                    )}

                    {/* Diff table */}
                    {diffRows.length > 0 && (
                      <div style={{ marginTop: '18px' }}>
                        <h4 style={{ marginBottom: '8px', color: 'var(--primary-color)' }}>
                          Differences ({diffRows.length})
                        </h4>
                        <div className="diff-table-wrap">
                          <table className="diff-table">
                            <thead>
                              <tr>
                                <th>Email</th>
                                <th>Category</th>
                                <th>Status</th>
                                <th>Differing fields</th>
                              </tr>
                            </thead>
                            <tbody>
                              {diffRows.slice(0, 25).map(r => (
                                <tr key={r.email_id}>
                                  <td style={{ fontFamily: 'monospace' }}>{r.email_id}</td>
                                  <td>{r.truth?.category || '—'} → {r.submission?.category || '—'}</td>
                                  <td>{r.truth?.status || '—'} → {r.submission?.status || '—'}</td>
                                  <td style={{ color: '#c62828' }}>{r.diffs.join(', ')}</td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
                          {diffRows.length > 25 && (
                            <p className="diff-more">…and {diffRows.length - 25} more</p>
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
            </div>
          )
        })()}


      </main>
    </div>
  )
}

export default App
