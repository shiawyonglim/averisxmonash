import { useState, useEffect, useRef, useCallback } from 'react'
import NavIcon from './NavIcon'
import './App.css'

const API = import.meta.env?.VITE_API_URL || (typeof window !== 'undefined' && window.location.port === '5173' ? 'http://localhost:8000' : (typeof window !== 'undefined' ? window.location.origin : 'http://localhost:8000'))
const QUEUE_LIMIT = 50

const fmtCount = (n) => (n > 999 ? '999+' : String(n))

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
  { key: 'reply_received', label: 'Replies' },
]

const REVIEW_REASON_META = {
  missing_value: { label: 'Missing value', color: '#b3560e', bg: '#fdeee7' },
  missing_attachment: { label: 'Missing attachment', color: '#b3560e', bg: '#fdeee7' },
  unreadable: { label: 'Unreadable / corrupt', color: '#8e24aa', bg: '#f3e5f5' },
  wrong_doc_type: { label: 'Wrong doc type', color: '#c62828', bg: '#ffebee' },
}

const REVIEW_FILTERS = [
  { key: 'all', label: 'All Pending' },
  { key: 'mismatch', label: 'Mismatches' },
  { key: 'needs_review', label: 'Needs Review' },
  { key: 'corrupted', label: 'Corrupted' },
  { key: 'reply', label: 'Replies' },
]

const FIELD_STANDARDS = {
  shipper: 'DCSA eBL v3.0 / ICC UCP 600 Art. 20 (Legal Shipper Entity)',
  consignee: 'DCSA eBL v3.0 / ICC UCP 600 Art. 20 (Consignee Title & Negotiability)',
  notify_party: 'DCSA eBL v3.0 (Arrival Notice Party / Same as Consignee)',
  port_of_loading: 'UNECE Rec. 16 (UN/LOCODE Standard Port Nomenclature)',
  port_of_discharge: 'UNECE Rec. 16 (UN/LOCODE Standard Port Nomenclature)',
  container_count: 'ISO 6346 Container Equipment Quantity & Sizing Specification',
  gross_weight_kg: 'IMO SOLAS Chapter VI Reg. 2 (Verified Gross Mass / VGM Mandate)',
}

const CARRIER_DESK_EMAILS = {
  EVERGREEN: 'doc.desk@evergreen-marine.com',
  MSC: 'bl.documentation@msc.com',
  MAERSK: 'liner.documentation@maersk.com',
  CMA: 'doc.desk@cma-cgm.com',
  HAPAG: 'doc.service@hlag.com',
  ONE: 'ocean.docs@one-line.com',
  PIL: 'bl.desk@pilship.com',
  OOCL: 'liner.docs@oocl.com',
  'YANG MING': 'doc.desk@yangming.com',
  MONTER: 'documentation@monter-lines.com',
}

function getCarrierDeskEmail(carrier) {
  if (!carrier) return 'carrier-desk@shippingline.com'
  const upper = String(carrier).toUpperCase()
  for (const [k, v] of Object.entries(CARRIER_DESK_EMAILS)) {
    if (upper.includes(k)) return v
  }
  const clean = String(carrier).toLowerCase().replace(/[^a-z0-9]/g, '')
  return `doc.desk@${clean || 'carrier'}-lines.com`
}

const AUDIT_DOT_COLORS = {
  AUTO_VERIFIED: '#28a745',
  RESOLVED: '#0d6efd',
  CARRIER_RE_REQUESTED: '#6c757d',
  MANUAL_OVERRIDE: '#fd7e14',
  CHASER_DISPATCHED: '#6f42c1',
  EMAIL_DISPATCHED: '#6f42c1',
  INBOUND_REPLY_RECEIVED: '#0d6efd',
  DOCUMENT_EDITED: '#e07a5f',
  DOCUMENT_RESTORED: '#6f42c1',
}

// Texts the UI injects when a document can't be shown — never save these back.
const DOC_PLACEHOLDER_RX = /^\s*\[(MISSING ATTACHMENT|CORRUPTED FILE|NON-COMPARISON|INBOUND CHASER|AWAITING CARRIER|IMAGE DOCUMENT)/i

const EMAIL_STATUS_META = {
  OK:           { label: 'OK',           color: '#2e7d32', bg: '#e8f5e9' },
  MISMATCH:     { label: 'Mismatch',     color: '#c62828', bg: '#ffebee' },
  NEEDS_REVIEW: { label: 'Needs review', color: '#b78103', bg: '#fff8e1' },
  RESOLVED:     { label: 'Resolved',     color: '#1a73e8', bg: '#e8f0fe' },
  CORRUPTED:    { label: 'Corrupted',    color: '#8e24aa', bg: '#f3e5f5' },
  MISSING_BL:   { label: 'Missing BL',   color: '#e07a5f', bg: '#fdeee7' },
  REPLY_RECEIVED: { label: 'Reply received', color: '#004085', bg: '#d6e4ff' },
  UNVERIFIED:   { label: 'Unverified',   color: '#777777', bg: '#f1f1f1' },
}

const emailStatusMeta = s => EMAIL_STATUS_META[s] || EMAIL_STATUS_META.UNVERIFIED

// Fields that failed comparison without being value-vs-value defects
// (blank on one or both sides) — they escalate to review, not "match".
const countMissingFields = (result) =>
  FIELDS.filter(f => {
    const c = result?.field_comparisons?.[f]
    return c && c.match === false && !(result?.defect_fields || []).includes(f)
  }).length

const CHAT_SUGGESTIONS = [
  { icon: '🔍', text: 'Verify the next 25 unverified emails' },
  { icon: '🚢', text: 'Which carriers owe us the most draft BLs?' },
  { icon: '📦', text: 'Run the pipeline and report my score' },
  { icon: '📋', text: "Summarise today's mismatches" },
]

const formatChatTime = ts =>
  new Date(ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })


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
    const parts = text.split(
      /(\bemail_\d{3}\b|\bsynth_[a-z]+_\d{3}\b|\*\*[^*\n]+\*\*|`[^`\n]+`|\*[^*\n]+\*)/g
    )
    return parts.map((part, idx) => {
      if (/^(email_\d{3}|synth_[a-z]+_\d{3})$/.test(part)) {
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
      if (part.length > 4 && part.startsWith('**') && part.endsWith('**')) {
        return <strong key={idx}>{part.slice(2, -2)}</strong>
      }
      if (part.length > 2 && part.startsWith('`') && part.endsWith('`')) {
        return <code key={idx} className="chat-inline-code">{part.slice(1, -1)}</code>
      }
      if (part.length > 2 && part.startsWith('*') && part.endsWith('*')) {
        return <em key={idx}>{part.slice(1, -1)}</em>
      }
      return part
    })
  }

  const renderTable = (rows, key) => {
    const parsed = rows.map(r =>
      r.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(c => c.trim())
    )
    const isSep = cells => cells.every(c => /^:?-{2,}:?$/.test(c))
    const header = parsed.length && !isSep(parsed[0]) ? parsed[0] : null
    const body = parsed.slice(header ? 1 : 0).filter(c => !isSep(c))
    return (
      <div key={key} className="chat-table-wrap">
        <table className="chat-table">
          {header && (
            <thead>
              <tr>{header.map((c, i) => <th key={i}>{renderInline(c)}</th>)}</tr>
            </thead>
          )}
          <tbody>
            {body.map((r, i) => (
              <tr key={i}>{r.map((c, j) => <td key={j}>{renderInline(c)}</td>)}</tr>
            ))}
          </tbody>
        </table>
      </div>
    )
  }

  const renderTextSegment = (seg, keyPrefix) => {
    const lines = seg.split('\n')
    const out = []
    let i = 0
    let k = 0
    while (i < lines.length) {
      const t = lines[i].trim()

      if (t.startsWith('|')) {
        const rows = []
        while (i < lines.length && lines[i].trim().startsWith('|')) {
          rows.push(lines[i])
          i += 1
        }
        out.push(renderTable(rows, `${keyPrefix}-t${k++}`))
        continue
      }
      i += 1

      if (!t) {
        out.push(<div key={`${keyPrefix}-${k++}`} style={{ height: '8px' }} />)
        continue
      }
      const h = t.match(/^(#{1,4})\s+(.*)$/)
      if (h) {
        out.push(
          <div key={`${keyPrefix}-${k++}`} className={`chat-h chat-h${h[1].length}`}>
            {renderInline(h[2])}
          </div>
        )
        continue
      }
      if (/^(-{3,}|\*{3,}|_{3,})$/.test(t)) {
        out.push(<div key={`${keyPrefix}-${k++}`} className="chat-hr" />)
        continue
      }
      if (/^[-*•]\s+/.test(t)) {
        out.push(
          <div key={`${keyPrefix}-${k++}`} className="chat-bullet-line">
            <span className="bullet-dot">•</span>
            <span>{renderInline(t.replace(/^[-*•]\s+/, ''))}</span>
          </div>
        )
        continue
      }
      const num = t.match(/^(\d+)[.)]\s+(.*)$/)
      if (num) {
        out.push(
          <div key={`${keyPrefix}-${k++}`} className="chat-num-line">
            <span className="chat-num">{num[1]}.</span>
            <span>{renderInline(num[2])}</span>
          </div>
        )
        continue
      }
      out.push(
        <div key={`${keyPrefix}-${k++}`} style={{ margin: '2px 0' }}>
          {renderInline(t)}
        </div>
      )
    }
    return out
  }

  return (
    <div className="formatted-chat-body">
      {content.split(/(```[\s\S]*?```)/g).map((seg, sIdx) => {
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
        return <div key={sIdx}>{renderTextSegment(seg, `s${sIdx}`)}</div>
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
            {/* Engine Provenance Badge */}
            {(() => {
              const provValues = [
                ...Object.values(result.si_provenance || {}),
                ...Object.values(result.bl_provenance || {}),
                result.classification_provenance,
                result.intent_provenance
              ].filter(Boolean)
              const hasVision = provValues.some(p => String(p).toLowerCase().includes('vision'))
              const hasLLM = provValues.some(p => String(p).toLowerCase().includes('llm') || String(p).toLowerCase().includes('nvidia'))
              if (hasVision) {
                return (
                  <span className="provenance-badge provenance-vision" title="Audited via Multimodal Vision OCR & Layout Engine">
                    👁️ Multimodal Vision Engine
                  </span>
                )
              } else if (hasLLM) {
                return (
                  <span className="provenance-badge provenance-llm" title="Audited via NVIDIA DeepSeek / Qwen LLM Fallback">
                    🧠 Tier-2 LLM Reasoning
                  </span>
                )
              } else {
                return (
                  <span className="provenance-badge provenance-regex" title="Audited via Deterministic Fast-Path Engine (<15ms latency)">
                    ⚡ Deterministic Fast-Path (Regex)
                  </span>
                )
              }
            })()}
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
                Field Audit ({result.defect_fields?.length || 0} defects
                {countMissingFields(result) > 0 ? ` · ${countMissingFields(result)} missing` : ''})
              </h3>
              <div>
                {FIELDS.map(f => {
                  const isMismatch = result.defect_fields?.includes(f)
                  const fieldComp = result.field_comparisons?.[f]
                  const isMissing = !isMismatch && (fieldComp?.match === false || Boolean(fieldComp?.blank))
                  const missingLabel = fieldComp?.blank === 'si_only' ? 'Missing on SI'
                    : fieldComp?.blank === 'bl_only' ? 'Missing on BL'
                    : fieldComp?.blank === 'both' ? 'Missing — Both Docs'
                    : 'Missing Value'
                  return (
                    <div key={f} className={`field-comparison ${isMismatch ? 'mismatch' : isMissing ? 'missing' : ''}`}>
                      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '6px' }}>
                        <div>
                          <h4 style={{ margin: 0, display: 'inline-block' }}>{f.replace(/_/g, ' ')}</h4>
                          <span style={{
                            marginLeft: '8px',
                            fontSize: '0.72rem',
                            color: '#666',
                            background: '#f0ede6',
                            padding: '2px 6px',
                            borderRadius: '4px',
                            fontWeight: 500
                          }}>
                            {fieldComp?.standard_citation || FIELD_STANDARDS[f] || 'Maritime Standard'}
                          </span>
                        </div>
                        {isMismatch ? (
                          <span className="diff-chip diff-chip-defect">🔴 Material Defect</span>
                        ) : isMissing ? (
                          <span className="diff-chip diff-chip-missing">🟠 {missingLabel}</span>
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
                          color: isMismatch ? '#dc3545' : isMissing ? '#b3560e' : '#597928',
                          fontStyle: 'italic',
                        }}>
                          ℹ️ {fieldComp.reason}
                        </div>
                      )}

                      {/* Normalization & Decision Trail */}
                      <details className="norm-decision-trail" style={{
                        marginTop: '8px',
                        background: '#faf8f5',
                        border: '1px solid #e8e3d9',
                        borderRadius: '6px',
                        padding: '6px 10px',
                        fontSize: '0.8rem'
                      }}>
                        <summary style={{ cursor: 'pointer', fontWeight: 600, color: 'var(--primary-color)' }}>
                          🔍 View Normalization & Decision Trail
                        </summary>
                        <div style={{ marginTop: '8px', display: 'flex', flexDirection: 'column', gap: '6px' }}>
                          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '8px', background: '#fff', padding: '8px', borderRadius: '4px', border: '1px solid #eee' }}>
                            <div>
                              <div style={{ fontSize: '0.72rem', fontWeight: 700, color: '#888', textTransform: 'uppercase' }}>Raw SI Input</div>
                              <div style={{ fontFamily: 'monospace', fontSize: '0.78rem', wordBreak: 'break-word', color: '#333' }}>
                                {fieldComp?.raw_si || result.si_fields[f] || '—'}
                              </div>
                              <div style={{ fontSize: '0.72rem', fontWeight: 700, color: '#888', textTransform: 'uppercase', marginTop: '4px' }}>Normalized SI</div>
                              <div style={{ fontFamily: 'monospace', fontSize: '0.78rem', wordBreak: 'break-word', color: '#597928', fontWeight: 600 }}>
                                {fieldComp?.norm_si !== undefined ? String(fieldComp.norm_si) : '—'}
                              </div>
                            </div>
                            <div>
                              <div style={{ fontSize: '0.72rem', fontWeight: 700, color: '#888', textTransform: 'uppercase' }}>Raw BL Input</div>
                              <div style={{ fontFamily: 'monospace', fontSize: '0.78rem', wordBreak: 'break-word', color: '#333' }}>
                                {fieldComp?.raw_bl || result.bl_fields?.[f] || '—'}
                              </div>
                              <div style={{ fontSize: '0.72rem', fontWeight: 700, color: '#888', textTransform: 'uppercase', marginTop: '4px' }}>Normalized BL</div>
                              <div style={{ fontFamily: 'monospace', fontSize: '0.78rem', wordBreak: 'break-word', color: isMismatch ? '#dc3545' : '#597928', fontWeight: 600 }}>
                                {fieldComp?.norm_bl !== undefined ? String(fieldComp.norm_bl) : '—'}
                              </div>
                            </div>
                          </div>

                          {fieldComp?.transformation_steps && fieldComp.transformation_steps.length > 0 && (
                            <div style={{ background: '#fff', padding: '6px 8px', borderRadius: '4px', border: '1px solid #eee' }}>
                              <div style={{ fontSize: '0.72rem', fontWeight: 700, color: '#666', marginBottom: '3px' }}>⚙️ Transformation Steps Applied:</div>
                              <ol style={{ margin: 0, paddingLeft: '18px', fontSize: '0.75rem', color: '#444' }}>
                                {fieldComp.transformation_steps.map((step, sIdx) => (
                                  <li key={sIdx}>{step}</li>
                                ))}
                              </ol>
                            </div>
                          )}

                          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', fontSize: '0.74rem', color: '#777', paddingTop: '2px' }}>
                            <span><strong>Regulatory Reference:</strong> {fieldComp?.standard_citation || FIELD_STANDARDS[f] || 'Standard Shipping Practice'}</span>
                            <span><strong>Confidence:</strong> {fieldComp?.confidence || 'HIGH'}</span>
                          </div>
                        </div>
                      </details>
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

  // ---- Navigation: 'dashboard' | 'chat' | 'queue' | 'verify' | 'cloud' | 'scan' | 'audit' | 'pipeline' | 'stress' ----
  const [view, setView] = useState('dashboard')

  // ---- Chat Assistant ----
  const [chatMessages, setChatMessages] = useState([])
  const [chatInput, setChatInput] = useState('')
  const [chatLoading, setChatLoading] = useState(false)
  const [chatSessionId, setChatSessionId] = useState('')
  const chatEndRef = useRef(null)
  const chatInputRef = useRef(null)

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

  // ---- Generate picker (which inbox emails to process) ----
  const [genPicker, setGenPicker] = useState(null)
  const [genSelected, setGenSelected] = useState(() => new Set())
  const [genSearch, setGenSearch] = useState('')
  const [genShowAll, setGenShowAll] = useState(false)

  // ---- Chaser actions (Inbox, missing_bl filter) ----
  const [batchChasing, setBatchChasing] = useState(false)

  // ---- Inbound Reply Ingestion / Email Conversation Threads ----
  const [emailThreads, setEmailThreads] = useState([])
  const [simulatingReply, setSimulatingReply] = useState(false)
  const [pollingInbox, setPollingInbox] = useState(false)
  const [replyNotice, setReplyNotice] = useState(null)

  // ---- Stress Lab (edge-case dataset + batch runner) ----
  const [stressDataset, setStressDataset] = useState(null)
  const [stressStatus, setStressStatus] = useState(null)
  const [stressRunning, setStressRunning] = useState(false)
  const [stressMetrics, setStressMetrics] = useState(null)
  const [stressFailures, setStressFailures] = useState([])
  const [stressLimit, setStressLimit] = useState(0)
  const [stressWorkers, setStressWorkers] = useState(8)
  const [stressMsg, setStressMsg] = useState(null)
  const [stressCaseSearch, setStressCaseSearch] = useState('')
  const [stressCaseType, setStressCaseType] = useState('')
  const [stressCases, setStressCases] = useState([])
  const [stressCaseId, setStressCaseId] = useState('')
  const [stressCaseResult, setStressCaseResult] = useState(null)
  const [stressCaseLoading, setStressCaseLoading] = useState(false)
  const [stressShowAllFailures, setStressShowAllFailures] = useState(false)
  const [stressFailFilter, setStressFailFilter] = useState('')

  // ---- Score vs Ground Truth (Pipeline Run page) ----
  const [compareData, setCompareData] = useState(null)
  const [compareLoading, setCompareLoading] = useState(false)
  const [compareError, setCompareError] = useState(null)
  const [diffFilter, setDiffFilter] = useState('')
  // ---- Auto Email Getter & Laya Decision Classifier ----
  const [getterSource, setGetterSource] = useState('real') // 'real' (shiawyonglim@gmail.com) | 'dataset' (520 emails)
  const [getterStatus, setGetterStatus] = useState(null)
  const [getterEmails, setGetterEmails] = useState([])
  const [getterTotal, setGetterTotal] = useState(0)
  const [getterPage, setGetterPage] = useState(1)
  const [getterLimit] = useState(25)
  const [getterLoading, setGetterLoading] = useState(false)
  const [getterCategory, setGetterCategory] = useState('all')
  const [getterQueueFilter, setGetterQueueFilter] = useState('all')
  const [getterSearch, setGetterSearch] = useState('')
  const [selectedGetterEmail, setSelectedGetterEmail] = useState(null)
  const [isLiveStreaming, setIsLiveStreaming] = useState(false)
  const [customModalOpen, setCustomModalOpen] = useState(false)
  const [pollingRealGmail, setPollingRealGmail] = useState(false)
  const [sendingRealTest, setSendingRealTest] = useState(false)
  const [customForm, setCustomForm] = useState({
    from_addr: 'liner.desk@evergreen-marine.com',
    to_addr: 'shiawyonglim@gmail.com',
    subject: 'DRAFT BL READY _ 5AKR-61849 _ PORT KLANG _ SIN832764835',
    body: 'Dear Shiaw Yong Lim,\n\nPlease find attached draft Bill of Lading for verification before vessel cutoff.\n\nBest regards,\nEvergreen Marine Operations Desk',
    attachments: ['attachments/email_custom_SI.txt', 'attachments/email_custom_BL.txt']
  })
  const [customIngesting, setCustomIngesting] = useState(false)
  const [getterMsg, setGetterMsg] = useState(null)
  const streamIntervalRef = useRef(null)

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

  // ---- Document editing + backup (Verify page) ----
  const [savingDocs, setSavingDocs] = useState(false)
  const [saveDocMsg, setSaveDocMsg] = useState(null)
  const [docBackups, setDocBackups] = useState(null)
  const [restoringDocs, setRestoringDocs] = useState(false)

  // ---- Missing-attachment detection + auto draft-chaser prompt ----
  const [missingDoc, setMissingDoc] = useState(null) // 'si' | 'bl' | 'both' | null
  const [missingPrompt, setMissingPrompt] = useState(null) // { emailId, doc }
  const missingPromptDismissed = useRef(new Set())
  const [threadOpen, setThreadOpen] = useState(false)

  // ---- Paper Scan (camera photos / handwritten docs) — merged into Verify Documents ----
  const [verifyMode, setVerifyMode] = useState('email') // 'email' | 'scan'
  const [scanSi, setScanSi] = useState(null) // { file, preview }
  const [scanBl, setScanBl] = useState(null)
  const [scanResult, setScanResult] = useState(null)
  const [scanLoading, setScanLoading] = useState(false)
  const [scanError, setScanError] = useState(null)

  // ---- Queue Adjacent Navigation ----
  const [adjacentInfo, setAdjacentInfo] = useState(null)

  // ---- Human-in-the-Loop Review ----
  const [reviewItems, setReviewItems] = useState(null)
  const [reviewLoading, setReviewLoading] = useState(false)
  const [reviewFilter, setReviewFilter] = useState('all')
  const [reviewMsg, setReviewMsg] = useState(null)

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

  // Human-in-the-Loop queue — one unpaginated pull, bucketed client-side
  const fetchReviewQueue = useCallback(async () => {
    setReviewLoading(true)
    try {
      const res = await fetch(`${API}/api/queue?filter=all&page=1&limit=1000`)
      const data = await res.json()
      setReviewItems(data.emails || [])
    } catch (err) {
      console.error('Failed to fetch review queue:', err)
      setReviewItems([])
    } finally {
      setReviewLoading(false)
    }
  }, [])

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

  // HITL review queue — refresh on entering the view
  useEffect(() => {
    if (view === 'review') fetchReviewQueue()
  }, [view, fetchReviewQueue])

  // Keyboard navigation shortcuts in Verification Hub ([Q] Prev, [W] Next, [E] Auto-Draft)
  useEffect(() => {
    if (view !== 'verify' || verifyMode !== 'email') return
    const handleKeyDown = (e) => {
      if (['INPUT', 'TEXTAREA', 'SELECT'].includes(e.target.tagName)) return
      if (e.key === 'ArrowLeft' || e.key === 'q' || e.key === 'Q' || e.key === 'k' || e.key === 'K') {
        if (adjacentInfo?.prev) {
          e.preventDefault()
          openEmail(adjacentInfo.prev)
        }
      } else if (e.key === 'ArrowRight' || e.key === 'w' || e.key === 'W' || e.key === 'j' || e.key === 'J') {
        if (adjacentInfo?.next) {
          e.preventDefault()
          openEmail(adjacentInfo.next)
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
  }, [view, verifyMode, adjacentInfo, selectedEmail, emailModalOpen])

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
    // Inbox rows for the generate picker — all ticked by default
    fetch(`${API}/api/inbox?limit=1000`)
      .then(res => res.json())
      .then(data => {
        const items = data.emails || []
        setGenPicker(items)
        setGenSelected(new Set(items.map(i => i.email_id)))
      })
      .catch(err => console.error('Failed to fetch inbox for picker:', err))
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

  // ---- Stress Lab ----
  const fetchStressResults = useCallback(async () => {
    try {
      const res = await fetch(`${API}/api/stress/results?limit=1000`)
      if (!res.ok) return
      const data = await res.json()
      setStressMetrics(data.metrics)
      setStressFailures(data.failures || [])
    } catch (err) {
      console.error('Failed to fetch stress results:', err)
    }
  }, [])

  // Stress Lab — dataset info + any in-flight run on entering the view
  useEffect(() => {
    if (view !== 'stress') return
    fetch(`${API}/api/stress/dataset`)
      .then(res => res.json())
      .then(data => setStressDataset(data))
      .catch(err => console.error('Failed to fetch stress dataset info:', err))
    fetch(`${API}/api/stress/status`)
      .then(res => res.json())
      .then(data => {
        setStressStatus(data)
        setStressRunning(Boolean(data.running) && !data.done)
        if (data.metrics) setStressMetrics(data.metrics)
      })
      .catch(err => console.error('Failed to fetch stress status:', err))
    fetchStressResults()
  }, [view, fetchStressResults])

  // Stress Lab — case picker list (debounced search)
  useEffect(() => {
    if (view !== 'stress') return
    const t = setTimeout(() => {
      const params = new URLSearchParams({ limit: '300' })
      if (stressCaseSearch) params.set('search', stressCaseSearch)
      if (stressCaseType) params.set('test_type', stressCaseType)
      fetch(`${API}/api/stress/cases?${params}`)
        .then(res => res.json())
        .then(data => setStressCases(data.cases || []))
        .catch(err => console.error('Failed to fetch stress cases:', err))
    }, 250)
    return () => clearTimeout(t)
  }, [view, stressCaseSearch, stressCaseType])

  // Stress polling — every 1s while running
  useEffect(() => {
    if (!stressRunning) return undefined
    const iv = setInterval(() => {
      fetch(`${API}/api/stress/status`)
        .then(res => res.json())
        .then(data => {
          setStressStatus(data)
          if (data.done || !data.running) {
            setStressRunning(false)
            fetchStressResults()
            if (data.error === 'cancelled') setStressMsg('Run cancelled — partial results shown.')
            else if (data.error) setStressMsg(`Error: ${data.error}`)
            else setStressMsg(null)
          }
        })
        .catch(err => console.error('Stress status poll failed:', err))
    }, 1000)
    return () => clearInterval(iv)
  }, [stressRunning, fetchStressResults])

  const handleStartStress = useCallback(async () => {
    setStressMsg(null)
    try {
      const res = await fetch(`${API}/api/stress/run`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ limit: Number(stressLimit) || 0, workers: Number(stressWorkers) || 8 }),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail || `Server error: ${res.status}`)
      if (data.started) {
        setStressRunning(true)
        setStressMetrics(null)
        setStressFailures([])
        setStressStatus({ running: true, processed: 0, total: data.total, done: false })
      } else {
        setStressMsg(data.message || 'Could not start stress run')
      }
    } catch (err) {
      setStressMsg(err.message)
    }
  }, [stressLimit, stressWorkers])

  const handleCancelStress = useCallback(async () => {
    try {
      await fetch(`${API}/api/stress/cancel`, { method: 'POST' })
    } catch (err) {
      console.error('Failed to cancel stress run:', err)
    }
  }, [])

  const handleRunStressCase = useCallback(async (eid) => {
    if (!eid) return
    setStressCaseLoading(true)
    setStressCaseResult(null)
    try {
      const res = await fetch(`${API}/api/stress/run-one`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email_id: eid }),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail || `Server error: ${res.status}`)
      setStressCaseResult(data)
    } catch (err) {
      setStressCaseResult({ email_id: eid, error: err.message, match: null })
    } finally {
      setStressCaseLoading(false)
    }
  }, [])

  // ============================================================
  // AUTO EMAIL GETTER & CLASSIFIER HANDLERS
  // ============================================================
  const fetchGetterStatus = useCallback(async () => {
    try {
      const res = await fetch(`${API}/api/getter/status`)
      if (res.ok) {
        const data = await res.json()
        setGetterStatus(data)
      }
    } catch (err) {
      console.error('Failed to fetch getter status:', err)
    }
  }, [])

  const fetchGetterEmails = useCallback(async (page = 1, cat = getterCategory, q = getterQueueFilter, search = getterSearch, src = getterSource) => {
    setGetterLoading(true)
    try {
      const params = new URLSearchParams({
        source: src,
        page: String(page),
        limit: String(getterLimit),
        category: cat,
        queue_filter: q,
        search: search || ''
      })
      const res = await fetch(`${API}/api/getter/emails?${params.toString()}`)
      if (res.ok) {
        const data = await res.json()
        setGetterEmails(data.emails || [])
        setGetterTotal(data.total || 0)
        setGetterPage(data.page || 1)
        if (data.emails?.length > 0) {
          setSelectedGetterEmail(prev => {
            if (prev) {
              const stillPresent = data.emails.find(e => e.email_id === prev.email_id)
              if (stillPresent) return stillPresent
            }
            return data.emails[0]
          })
        }
      }
    } catch (err) {
      console.error('Failed to fetch getter emails:', err)
    } finally {
      setGetterLoading(false)
    }
  }, [getterCategory, getterQueueFilter, getterSearch, getterLimit, getterSource])

  const handlePollRealGmail = async () => {
    setPollingRealGmail(true)
    try {
      const res = await fetch(`${API}/api/getter/poll-gmail`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ limit: 15, only_unread: false })
      })
      const data = await res.json()
      if (res.ok) {
        setGetterMsg(`Polled real Gmail inbox: ${data.fetched_count} emails fetched (${data.newly_added} new). Neural enrichment running in background.`)
        fetchGetterStatus()
        fetchGetterEmails(1, getterCategory, getterQueueFilter, getterSearch, 'real')
      } else {
        setGetterMsg(`Gmail poll failed: ${data.detail || 'Connection error'}`)
      }
    } catch (err) {
      setGetterMsg(`Gmail error: ${err.message}`)
    } finally {
      setPollingRealGmail(false)
    }
  }

  const handleSendRealTestEmail = async () => {
    setSendingRealTest(true)
    try {
      const res = await fetch(`${API}/api/getter/send-real-test`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          subject: "URGENT DRAFT BL READY _ 5AKR-61849 _ PORT KLANG _ SIN832764835",
          body: "Dear Shiaw Yong Lim,\n\nPlease find attached draft Bill of Lading for verification before vessel cutoff at Port Klang.\n\nBest regards,\nEvergreen Marine Operations Desk"
        })
      })
      const data = await res.json()
      if (res.ok) {
        setGetterMsg(`Real email sent to ${data.sent_to} via Google SMTP and received back via IMAP. Neural enrichment running.`)
        fetchGetterStatus()
        fetchGetterEmails(1, 'all', 'all', '', 'real')
        if (data.latest_email) {
          setSelectedGetterEmail(data.latest_email)
        }
      } else {
        setGetterMsg(`Send test failed: ${data.detail || 'SMTP error'}`)
      }
    } catch (err) {
      setGetterMsg(`SMTP error: ${err.message}`)
    } finally {
      setSendingRealTest(false)
    }
  }

  const handleFetchBatch = async (count = 10) => {
    try {
      const res = await fetch(`${API}/api/getter/fetch`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ count })
      })
      if (res.ok) {
        const data = await res.json()
        setGetterMsg(`Successfully ingested +${data.added_count} emails into live stream.`)
        fetchGetterStatus()
        fetchGetterEmails(getterPage)
      }
    } catch (err) {
      setGetterMsg(`Ingestion failed: ${err.message}`)
    }
  }

  const handleResetGetter = async () => {
    try {
      const res = await fetch(`${API}/api/getter/reset?initial_count=25`, { method: 'POST' })
      if (res.ok) {
        setGetterMsg('Stream reset. Refreshed real personal Gmail inbox.')
        fetchGetterStatus()
        fetchGetterEmails(1)
      }
    } catch (err) {
      setGetterMsg(`Reset failed: ${err.message}`)
    }
  }

  const handleCustomIngestSubmit = async (e) => {
    if (e) e.preventDefault()
    setCustomIngesting(true)
    try {
      const res = await fetch(`${API}/api/getter/ingest-custom`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(customForm)
      })
      const data = await res.json()
      if (res.ok && data.status === 'INGESTED_SUCCESSFULLY') {
        setCustomModalOpen(false)
        setSelectedGetterEmail(data.dossier)
        setGetterMsg(`Email ${data.email_id} ingested → ${data.dossier.classification.category} (Laya neural pass queued)`)
        fetchGetterStatus()
        fetchGetterEmails(1)
      } else {
        alert(data.message || 'Ingestion failed')
      }
    } catch (err) {
      alert(`Error: ${err.message}`)
    } finally {
      setCustomIngesting(false)
    }
  }

  // Auto stream polling interval
  useEffect(() => {
    if (isLiveStreaming && view === 'getter') {
      streamIntervalRef.current = setInterval(async () => {
        try {
          const res = await fetch(`${API}/api/getter/fetch`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ count: 1 })
          })
          if (res.ok) {
            fetchGetterStatus()
            fetchGetterEmails(getterPage)
          }
        } catch (e) {
          console.error('Stream poll failed:', e)
        }
      }, 1800)
    } else {
      if (streamIntervalRef.current) clearInterval(streamIntervalRef.current)
    }
    return () => {
      if (streamIntervalRef.current) clearInterval(streamIntervalRef.current)
    }
  }, [isLiveStreaming, view, getterPage, fetchGetterStatus, fetchGetterEmails])

  // Refresh on entering getter view
  useEffect(() => {
    if (view === 'getter') {
      fetchGetterStatus()
      fetchGetterEmails(1)
    }
  }, [view, fetchGetterStatus, fetchGetterEmails])

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

  const openDraftEmail = (emailId, info = null, verdict = null, missingOverride = null) => {
    const curEmail = emailId || selectedEmail
    const curInfo = info || emailInfo
    const curVerdict = verdict || result
    const carrier = detectCarrier(curInfo)
    const category = curInfo?.category || 'BL_COMPARISON'

    let to = ''
    let subject = ''
    let body = ''

    if (category === 'INVOICE_QUERY') {
      to = curInfo?.from || 'billing-desk@client.com'
      const subj = curInfo?.subject || curEmail
      subject = subj.toUpperCase().startsWith('RE:') ? subj : `RE: ${subj} [Ref: ${curEmail}]`
      const senderName = curInfo?.from?.split('@')[0]?.replace(/[._]/g, ' ') || 'Customer / Operations Partner'
      body = `Dear ${senderName},\n\nThank you for reaching out regarding invoice charges for shipment ref [${curEmail}].\n\nIn response to your query regarding the Terminal Handling Charges (THC) and local port fee breakdown:\n- Terminal Handling Charges (THC): FOB origin/destination charges have been verified against the agreed tariff schedule.\n- Telex Release & Documentation Fees: Applied in accordance with the standard liner documentation schedule.\n- Local Charges Breakdown: All applicable origin/destination port charges have been reviewed by our billing team.\n\nPlease find the itemized charge breakdown attached for your reference. Please let us know if you require any additional supporting documentation or revised debit notes.\n\nShipment Reference: ${curEmail}\nOriginal Subject: ${curInfo?.subject || ''}\n\nKind regards,\nShipping Documentation & Accounts Billing Desk\nAPRIL Logistics / Averis Global Shared Services`
    } else if (category === 'SI_REQUEST') {
      to = curInfo?.from || 'operations@shippingline.com'
      const subj = curInfo?.subject || curEmail
      subject = subj.toUpperCase().startsWith('RE:') ? subj : `RE: ${subj} [Ref: ${curEmail}]`
      const senderName = curInfo?.from?.split('@')[0]?.replace(/[._]/g, ' ') || 'Customer / Operations Partner'
      body = `Dear ${senderName},\n\nThank you for reaching out regarding Shipping Instructions for shipment ref [${curEmail}].\n\nPlease find the validated Shipping Instruction (SI) details attached for your review and booking confirmation.\n\nKindly confirm receipt and verify that all vessel booking particulars, container specifications, and consignee details align with your requirements.\n\nShipment Reference: ${curEmail}\nOriginal Subject: ${curInfo?.subject || ''}\n\nKind regards,\nShipping Documentation Operations Desk\nAPRIL Logistics / Averis Global Shared Services`
    } else if (category === 'GENERAL') {
      to = curInfo?.from || 'operations@shippingline.com'
      const subj = curInfo?.subject || curEmail
      subject = subj.toUpperCase().startsWith('RE:') ? subj : `RE: ${subj} [Ref: ${curEmail}]`
      const senderName = curInfo?.from?.split('@')[0]?.replace(/[._]/g, ' ') || 'Customer / Operations Partner'
      body = `Dear ${senderName},\n\nThank you for contacting our documentation desk regarding shipment ref [${curEmail}].\n\nOur operations team has reviewed your inquiry. Shipment documentation and cargo dispatch are proceeding on schedule according to standard operational timelines.\n\nPlease let us know if you require any specific vessel tracking updates or supplemental documentation.\n\nShipment Reference: ${curEmail}\nOriginal Subject: ${curInfo?.subject || ''}\n\nKind regards,\nShipping Documentation Operations Desk\nAPRIL Logistics / Averis Global Shared Services`
    } else {
      // BL_COMPARISON category
      const missing = missingOverride
        || (curEmail === selectedEmail ? missingDoc : null)
        || (DOC_PLACEHOLDER_RX.test(siText) && !DOC_PLACEHOLDER_RX.test(blText) ? 'si'
            : DOC_PLACEHOLDER_RX.test(blText) && !DOC_PLACEHOLDER_RX.test(siText) ? 'bl'
            : (curInfo?.attachments?.length === 0 && curInfo?.category === 'BL_COMPARISON' ? 'both' : null))

      to = missing === 'si'
        ? (curInfo?.from || '')
        : (curVerdict?.status === 'MISMATCH' || missing === 'bl' || missing === 'both'
            ? getCarrierDeskEmail(carrier)
            : (curInfo?.from || 'carrier-desk@shippingline.com'))

      const subjectPrefix = curVerdict?.status === 'MISMATCH'
        ? 'URGENT: Discrepancy Notice & Draft BL Amendment'
        : missing === 'si'
          ? 'MISSING DOCUMENT: Shipping Instruction Required'
          : (missing === 'bl' || missing === 'both'
              ? 'URGENT CHASER: Missing Draft Bill of Lading'
              : 'Documentation Clearance Notice')
      subject = `${subjectPrefix} — ${curInfo?.subject || curEmail} [Ref: ${curEmail}]`

      if (curVerdict?.status === 'MISMATCH' && curVerdict?.defect_fields?.length > 0) {
        const defectsList = curVerdict.defect_fields.map((f, i) => {
          const siVal = curVerdict.si_fields?.[f] || 'N/A'
          const blVal = curVerdict.bl_fields?.[f] || 'N/A'
          return `  ${i + 1}. ${f.replace(/_/g, ' ').toUpperCase()}:\n     - Shipper Instruction (SI): "${siVal}"\n     - Draft Bill of Lading (BL): "${blVal}"`
        }).join('\n\n')

        body = `Dear ${carrier} Operations Desk,\n\nDuring automated documentation cross-validation for shipment ref [${curEmail}], our verification engine detected discrepancies between our Shipping Instructions (SI) and your draft Bill of Lading (BL):\n\n${defectsList}\n\nPlease issue an amended draft Bill of Lading reflecting the validated Shipping Instruction values before the port cutoff (17:00 SGT) to avoid terminal loading delays.\n\nShipment Reference: ${curEmail}\nOriginal Subject: ${curInfo?.subject || ''}\n\nKind regards,\nShipping Documentation Operations Desk\nAveris Automated Logistics Pipeline`
      } else if (missing === 'si') {
        body = `Dear ${curInfo?.from || 'Operations Team'},\n\nRegarding shipment ref [${curEmail}] — your recent correspondence requested a draft Bill of Lading comparison, but the required Shipping Instruction (SI) document was not attached to the email.\n\nPlease re-send the Shipping Instruction at your earliest convenience so our automated verification pipeline can complete the 7-field cross-audit before port cutoff.\n\nShipment Reference: ${curEmail}\nOriginal Subject: ${curInfo?.subject || ''}\n\nKind regards,\nShipping Documentation Operations Desk\nAveris Automated Logistics Pipeline`
      } else if (missing === 'bl' || missing === 'both') {
        body = `Dear ${carrier} Documentation Desk,\n\nWe are following up on our Shipping Instruction submitted for shipment ref [${curEmail}].\n\nThe operational port cutoff (17:00 SGT) is approaching and our system has not yet received the draft Bill of Lading.\n\nPlease urgently furnish the draft BL so our clearance team can complete cross-validation against the shipper instructions.\n\nShipment Reference: ${curEmail}\nBooking Subject: ${curInfo?.subject || ''}\n\nKind regards,\nShipping Documentation Operations Desk\nAveris Automated Logistics Pipeline`
      } else {
        body = `Dear Shipper / Carrier Team,\n\nRegarding shipment ref [${curEmail}], all documentation cross-checks have completed. All 7 critical shipping attributes (shipper, consignee, notify party, ports, container count, and gross weight) have been verified.\n\nShipment Reference: ${curEmail}\nStatus: APPROVED / CLEARED FOR ISSUANCE\n\nKind regards,\nShipping Documentation Operations Desk\nAveris Automated Logistics Pipeline`
      }
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
      refreshThread(emailDraft.email_id)
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
    setEmailThreads([])
    setReplyNotice(null)
    setSaveDocMsg(null)
    setDocBackups(null)
    setMissingDoc(null)
    setMissingPrompt(null)
    setThreadOpen(false)
    setSiText('Loading attachment...')
    setBlText('Loading attachment...')

    try {
      const response = await fetch(`${API}/api/email/${eid}`)
      if (!response.ok) throw new Error('Failed to load email content')
      const data = await response.json()
      setSiText(data.si_text || '')
      setBlText(data.bl_text || '')
      if (data.email) setEmailInfo(data.email)
      setEmailThreads(data.threads || data.email?.threads || [])
      setThreadOpen((data.threads || data.email?.threads || []).length > 0)
      if (data.cloud_synced) setCloudSyncInfo(data.supabase_record)
      if (data.backups) setDocBackups(data.backups)
      if (data.missing_doc) setMissingDoc(data.missing_doc)
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
      // Auto-draft prompt: a required document is missing and no chaser/
      // outbound reply has been dispatched yet — offer to draft it now.
      const threads = data.threads || data.email?.threads || []
      const alreadySent = threads.some(m => m.direction === 'OUTBOUND')
      if (data.missing_doc && data.email?.category === 'BL_COMPARISON'
          && !alreadySent && !data.resolution
          && !missingPromptDismissed.current.has(eid)) {
        setMissingPrompt({ emailId: eid, doc: data.missing_doc })
      }
      // Auto-verify: two real documents and no stored verdict — run the
      // audit immediately so the operator never has to press Re-verify.
      const canAutoVerify = !data.verdict && !data.is_corrupted
        && (data.email?.attachments?.length || 0) >= 2
        && data.si_text && data.bl_text
        && !DOC_PLACEHOLDER_RX.test(data.si_text)
        && !DOC_PLACEHOLDER_RX.test(data.bl_text)
      if (canAutoVerify) await handleVerify(data.si_text, data.bl_text, eid)
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

  const autogrowChatInput = () => {
    const el = chatInputRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`
  }

  const sendChat = async (text) => {
    const message = (text !== undefined ? text : chatInput).trim()
    if (!message || chatLoading) return
    setChatMessages(prev => [...prev, { role: 'user', content: message, ts: Date.now() }])
    setChatInput('')
    if (chatInputRef.current) chatInputRef.current.style.height = 'auto'
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
          ts: Date.now(),
        })
        return next
      })
    } catch (err) {
      setChatMessages(prev => [...prev, {
        role: 'assistant',
        content: `⚠️ Failed to reach the assistant: ${err.message}`,
        sources: [],
        degraded: true,
        ts: Date.now(),
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
              ts: Date.now(),
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
          ts: Date.now(),
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
          ts: Date.now(),
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
    if (chatInputRef.current) chatInputRef.current.style.height = 'auto'
  }

  useEffect(() => {
    if (view === 'chat' && chatEndRef.current) {
      chatEndRef.current.scrollIntoView({ behavior: 'smooth' })
    }
  }, [chatMessages, chatLoading, view])

  const handleVerify = async (siOverride, blOverride, emailOverride) => {
    // onClick handlers pass the event object — only honour string overrides
    const siBody = typeof siOverride === 'string' ? siOverride : siText
    const blBody = typeof blOverride === 'string' ? blOverride : blText
    const verifyEmail = typeof emailOverride === 'string' ? emailOverride : selectedEmail
    if (!verifyEmail) {
      setError('Please select an email first.')
      return
    }
    if (!siBody.trim() || !blBody.trim()) {
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
          email_id: verifyEmail,
          si_text: siBody,
          bl_text: blBody,
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
  // DOCUMENT EDIT + BACKUP HANDLERS
  // ============================================================

  const handleSaveDocuments = async () => {
    if (!selectedEmail) return
    setSavingDocs(true)
    setSaveDocMsg(null)
    try {
      const res = await fetch(`${API}/api/email/${selectedEmail}/save-documents`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ si_text: siText, bl_text: blText }),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail || `Server error: ${res.status}`)
      setDocBackups(data.backups || null)
      const skipped = data.skipped?.length ? ` (${data.skipped.join('; ')})` : ''
      const remapped = data.remapped?.length
        ? ` Binary attachment repointed to: ${data.remapped.map(r => r.to).join(', ')}.`
        : ''
      setSaveDocMsg(`💾 ${data.message}${skipped}${remapped}`)
      // Re-run the audit so the verdict reflects the saved text immediately
      await handleVerify()
    } catch (err) {
      setSaveDocMsg(`❌ Save failed: ${err.message}`)
    } finally {
      setSavingDocs(false)
    }
  }

  const handleRestoreDocuments = async () => {
    if (!selectedEmail) return
    setRestoringDocs(true)
    setSaveDocMsg(null)
    try {
      const res = await fetch(`${API}/api/email/${selectedEmail}/restore-documents`, {
        method: 'POST',
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail || `Server error: ${res.status}`)
      setSiText(data.si_text || '')
      setBlText(data.bl_text || '')
      setDocBackups(data.backups || null)
      setSaveDocMsg(`↺ ${data.message}`)
      // Re-run the audit against the restored originals
      if (data.si_text && data.bl_text) await handleVerify(data.si_text, data.bl_text)
    } catch (err) {
      setSaveDocMsg(`❌ Restore failed: ${err.message}`)
    } finally {
      setRestoringDocs(false)
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
      fetchReviewQueue()
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
      setReviewMsg(data.message || `Chaser reminder dispatched for ${eid}.`)
      refreshQueue()
      fetchReviewQueue()
    } catch (err) {
      setError(err.message)
    }
  }

  // ============================================================
  // INBOUND REPLY INGESTION — thread refresh, simulate, IMAP poll
  // ============================================================

  const refreshThread = async (eid) => {
    if (!eid) return
    try {
      const res = await fetch(`${API}/api/email/${eid}/thread`)
      if (res.ok) {
        const data = await res.json()
        setEmailThreads(data.messages || [])
      }
    } catch (err) {
      console.error('Failed to fetch email thread:', err)
    }
  }

  const handleSimulateReply = async (eid) => {
    if (!eid) return
    setSimulatingReply(true)
    setReplyNotice(null)
    try {
      const res = await fetch(`${API}/api/email/${eid}/simulate-reply`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ has_attachment: true }),
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data = await res.json()
      setReplyNotice(`📨 ${data.message}`)
      await refreshThread(eid)
      refreshQueue()
      refreshEmailStatuses()
      fetch(`${API}/api/audit`).then(r => r.json()).then(d => setAuditEvents(d.events || [])).catch(() => {})
    } catch (err) {
      setReplyNotice(`❌ Reply simulation failed: ${err.message}`)
    } finally {
      setSimulatingReply(false)
    }
  }

  const handlePollInbox = async () => {
    setPollingInbox(true)
    setReplyNotice(null)
    try {
      const res = await fetch(`${API}/api/email/imap-poll`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data = await res.json()
      if (data.status === 'LIVE_POLL_SUCCESS') {
        setReplyNotice(`📥 Live inbox poll complete — checked ${data.messages_checked} unread message(s), matched ${data.replies_matched} repl${data.replies_matched === 1 ? 'y' : 'ies'} to active threads.`)
      } else {
        setReplyNotice(`📥 ${data.message || data.status}`)
      }
      if (selectedEmail) await refreshThread(selectedEmail)
      refreshQueue()
      refreshEmailStatuses()
    } catch (err) {
      setReplyNotice(`❌ Inbox poll failed: ${err.message}`)
    } finally {
      setPollingInbox(false)
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
      setReviewMsg(data.message || `Successfully logged: ${eid} -> ${action}`)
      setCorruptActionNotes('')
      setFocusedCorruptRow(null)
      refreshQueue()
      fetchReviewQueue()
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
        body: JSON.stringify({
          max_emails: Number(maxEmails) || 0,
          resume: !freshRun,
          // Unticked rows are excluded — an empty list means "process all".
          email_ids: genPicker && genSelected.size < genPicker.length
            ? [...genSelected]
            : [],
        }),
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
  const subcategoryEntries = stats
    ? Object.entries(stats.subcategories || {}).sort((a, b) => b[1] - a[1])
    : []
  const subcategoryMax = Math.max(1, ...subcategoryEntries.map(([, n]) => n))
  const carrierEntries = stats ? Object.entries(stats.missing_bl_by_carrier || {}) : []

  const totalPages = queueData
    ? Math.max(1, Math.ceil(queueData.total / queueData.limit))
    : 1

  // Sidebar badge: everything waiting on a human (needs_review count already
  // includes corrupted files server-side)
  const hitlPendingCount =
    (queueData?.counts?.needs_review || 0) +
    (queueData?.counts?.mismatch || 0) +
    (queueData?.counts?.reply_received || 0)

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
          <div className="sidebar-section">
            <p className="sidebar-section-label">Overview</p>
            <button
              className={`sidebar-btn ${view === 'dashboard' ? 'active' : ''}`}
              onClick={() => setView('dashboard')}
            >
              <NavIcon name="dashboard" /><span className="nav-label">Dashboard</span>
            </button>
            <button
              className={`sidebar-btn ${view === 'chat' ? 'active' : ''}`}
              onClick={() => setView('chat')}
            >
              <NavIcon name="assistant" /><span className="nav-label">Assistant</span>
            </button>
          </div>

          <div className="sidebar-section">
            <p className="sidebar-section-label">Intake</p>
            <button
              className={`sidebar-btn ${view === 'queue' ? 'active' : ''}`}
              onClick={() => setView('queue')}
            >
              <NavIcon name="inbox" /><span className="nav-label">Inbox</span>
              {queueData?.counts?.all > 0 && (
                <span className="sidebar-badge">{fmtCount(queueData.counts.all)}</span>
              )}
            </button>
            <button
              className={`sidebar-btn ${view === 'getter' ? 'active' : ''}`}
              onClick={() => {
                setView('getter')
                fetchGetterStatus()
                fetchGetterEmails(1)
              }}
            >
              <NavIcon name="bolt" /><span className="nav-label">Auto Collect</span>
              {getterStatus?.ingested_count > 0 && (
                <span className="sidebar-badge">{fmtCount(getterStatus.ingested_count)}</span>
              )}
            </button>
          </div>

          <div className="sidebar-section">
            <p className="sidebar-section-label">Verification</p>
            <button
              className={`sidebar-btn ${view === 'verify' ? 'active' : ''}`}
              onClick={() => setView('verify')}
            >
              <NavIcon name="search" />
              <span className="nav-text">
                <span className="nav-label">Verify</span>
                {selectedEmail && <span className="nav-sub">{selectedEmail}</span>}
              </span>
            </button>
            <button
              className={`sidebar-btn ${view === 'review' ? 'active' : ''}`}
              onClick={() => setView('review')}
            >
              <NavIcon name="user-check" /><span className="nav-label">Human Review</span>
              {hitlPendingCount > 0 && (
                <span className="sidebar-badge attention">{fmtCount(hitlPendingCount)}</span>
              )}
            </button>
          </div>

          <div className="sidebar-section">
            <p className="sidebar-section-label">Records</p>
            <button
              className={`sidebar-btn ${view === 'cloud' ? 'active' : ''}`}
              onClick={() => { setView('cloud'); fetchCloudRecords(); }}
            >
              <NavIcon name="cloud" /><span className="nav-label">Shared Records</span>
              {cloudStats?.total_in_supabase > 0 && (
                <span className="sidebar-badge">{fmtCount(cloudStats.total_in_supabase)}</span>
              )}
            </button>
            <button
              className={`sidebar-btn ${view === 'audit' ? 'active' : ''}`}
              onClick={() => setView('audit')}
            >
              <NavIcon name="history" /><span className="nav-label">Audit Log</span>
            </button>
          </div>

          <div className="sidebar-section">
            <p className="sidebar-section-label">Tools</p>
            <button
              className={`sidebar-btn ${view === 'pipeline' ? 'active' : ''}`}
              onClick={() => setView('pipeline')}
            >
              <NavIcon name="package" /><span className="nav-label">Submissions</span>
            </button>
            <button
              className={`sidebar-btn ${view === 'stress' ? 'active' : ''}`}
              onClick={() => setView('stress')}
            >
              <NavIcon name="flask" /><span className="nav-label">Stress Lab</span>
              {stressDataset?.email_count > 0 && (
                <span className="sidebar-badge">{fmtCount(stressDataset.email_count)}</span>
              )}
            </button>
          </div>
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
              <h1>Dashboard</h1>
              <p>Verification results across all processed emails.</p>
            </div>

            {!stats ? (
              <div className="empty-state">Loading…</div>
            ) : (
              <>
                <CutoffProgressBar stats={stats} onFilter={openQueueFilter} />

                <div className="kpi-grid">
                  {kpis.map(k => {
                    const reviewKey =
                      k.label === 'Mismatch' ? 'mismatch'
                        : k.label === 'Needs Review' ? 'needs_review'
                          : k.label === 'Corrupted' ? 'corrupted'
                            : null
                    const targetFilter =
                      k.label === 'Missing BL' ? 'missing_bl'
                        : k.label === 'Resolved' ? 'resolved'
                          : null
                    const clickable = reviewKey || targetFilter
                    return (
                      <div
                        key={k.label}
                        className={`kpi-card${clickable ? ' clickable' : ''}`}
                        onClick={reviewKey
                          ? () => { setReviewFilter(reviewKey); setView('review') }
                          : targetFilter ? () => openQueueFilter(targetFilter) : undefined}
                        title={clickable ? (reviewKey ? `Open ${k.label} in Human Review` : `Open ${k.label} in Inbox`) : undefined}
                      >
                        <div className="kpi-value">{k.value ?? 0}</div>
                        <div className="kpi-label">
                          {k.label}
                        </div>
                      </div>
                    )
                  })}
                </div>

                {stats.verified_count === 0 && (
                  <div className="notice warn">
                    Run a verification or the batch pipeline to see results here.
                  </div>
                )}

                <div className="card-row">
                  <div className="section-card" style={{ marginBottom: 0 }}>
                    <div className="section-head">
                      <h3>Defects by Field</h3>
                    </div>
                    {FIELDS.map(f => (
                      <BarRow
                        key={f}
                        label={f.replace(/_/g, ' ')}
                        value={stats.defect_fields?.[f] || 0}
                        max={defectMax}
                      />
                    ))}
                  </div>

                  <div className="section-card" style={{ marginBottom: 0 }}>
                    <div className="section-head">
                      <h3>Categories</h3>
                    </div>
                    {categoryEntries.length === 0 ? (
                      <p className="empty-state">No categories recorded yet.</p>
                    ) : (
                      categoryEntries.map(([cat, n]) => (
                        <BarRow key={cat} label={cat.replace(/_/g, ' ')} value={n} max={categoryMax} />
                      ))
                    )}
                  </div>
                </div>

                <div className="section-card">
                  <div className="section-head">
                    <h3>Subcategories</h3>
                    <span className="tag">{subcategoryEntries.length} subcategories</span>
                  </div>
                  {subcategoryEntries.length === 0 ? (
                    <p className="empty-state">No subcategory data recorded yet.</p>
                  ) : (
                    <div className="card-row">
                      {subcategoryEntries.map(([subtag, n]) => (
                        <BarRow key={subtag} label={subtag} value={n} max={subcategoryMax} />
                      ))}
                    </div>
                  )}
                </div>

                <div className="section-card">
                  <div className="section-head">
                    <h3>Missing Draft BL by Carrier</h3>
                    <button
                      className="btn-secondary btn-sm"
                      onClick={() => openQueueFilter('missing_bl')}
                    >
                      View all
                    </button>
                  </div>
                  {carrierEntries.length === 0 ? (
                    <p className="empty-state">No missing bills by carrier.</p>
                  ) : (
                    <table className="data-table">
                      <thead>
                        <tr>
                          <th>Carrier</th>
                          <th className="num">Missing BLs</th>
                        </tr>
                      </thead>
                      <tbody>
                        {carrierEntries.map(([carrier, n]) => (
                          <tr
                            key={carrier}
                            className="clickable"
                            onClick={() => openQueueFilter('missing_bl')}
                          >
                            <td>{carrier}</td>
                            <td className="num">{n}</td>
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
              <div className="chat-header-actions">
                {aiConfig && (
                  <span className="chat-model-tag" title="Active AI backend">
                    <span className="chat-model-dot" />
                    {aiConfig.provider}{aiConfig.model ? ` · ${aiConfig.model}` : ''}
                  </span>
                )}
                {chatMessages.length > 0 && (
                  <button className="chat-reset-btn" onClick={resetChat}>
                    New conversation
                  </button>
                )}
              </div>
            </div>

            <div className="chat-messages">
              {chatMessages.length === 0 && (
                <div className="chat-empty">
                  <div className="chat-empty-icon">💬</div>
                  <h3>What would you like me to do?</h3>
                  <p>Ask questions, or let me take actions — I'll always ask before writing or sending anything.</p>
                  <div className="chat-suggestions">
                    {CHAT_SUGGESTIONS.map(s => (
                      <button key={s.text} className="chat-chip" onClick={() => sendChat(s.text)}>
                        <span className="chat-chip-icon">{s.icon}</span>
                        <span>{s.text}</span>
                      </button>
                    ))}
                  </div>
                </div>
              )}

              {chatMessages.map((m, i) => (
                <div key={i} className={`chat-row ${m.role}`}>
                  {m.role === 'assistant' && <div className="chat-avatar">🤖</div>}
                  <div className="chat-msg-col">
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
                    {m.ts && <div className="chat-ts">{formatChatTime(m.ts)}</div>}
                  </div>
                  {m.role === 'user' && <div className="chat-avatar user">You</div>}
                </div>
              ))}

              {chatLoading && (
                <div className="chat-row assistant">
                  <div className="chat-avatar">🤖</div>
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
              <div className="chat-input-wrap">
                <textarea
                  ref={chatInputRef}
                  value={chatInput}
                  onChange={e => { setChatInput(e.target.value); autogrowChatInput() }}
                  onKeyDown={e => {
                    if (e.key === 'Enter' && !e.shiftKey) {
                      e.preventDefault()
                      sendChat()
                    }
                  }}
                  placeholder="Ask about the inbox…"
                  disabled={chatLoading}
                  rows={1}
                />
                <button
                  className="chat-send"
                  onClick={() => sendChat()}
                  disabled={chatLoading || !chatInput.trim()}
                  title="Send (Enter)"
                >
                  Send ➤
                </button>
              </div>
              <div className="chat-composer-hint">
                Enter to send · Shift+Enter for a new line · write actions require your approval
              </div>
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
              <p>All incoming email. Filter by issue type.</p>
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
                  className="btn-secondary btn-sm"
                  onClick={handleBatchChase}
                  disabled={batchChasing || !queueData?.emails?.length}
                  style={{ marginLeft: 'auto' }}
                >
                  {batchChasing ? 'Dispatching…' : 'Chase all on this page'}
                </button>
              )}
            </div>

            {/* Debounced search */}
            <div className="action-bar">
              <input
                type="text"
                className="search-input"
                placeholder="Search by email ID, subject, or sender"
                value={queueSearch}
                onChange={(e) => {
                  setQueueSearch(e.target.value)
                  setQueuePage(1)
                }}
              />
            </div>

            {queueMsg && (
              <div className="notice ok">{queueMsg}</div>
            )}

            {loadingQueue ? (
              <div className="empty-state">Loading…</div>
            ) : !queueData?.emails?.length ? (
              <div className="empty-state">No items match this filter.</div>
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
                              <span
                                className={`category-pill category-${(item.category || '').toLowerCase()}`}
                                title={item.category_description || item.category}
                              >
                                {item.display_tag || item.category}
                              </span>
                            )}
                            <span className="tag">{item.attachments_count} docs</span>
                            {item.has_bl === false && (
                              <span className="tag warn">No BL</span>
                            )}
                          </div>
                          <p style={{ fontSize: '0.9rem', marginTop: '4px', color: 'var(--text-color)' }}>
                            {item.subject}
                          </p>
                          <p className="meta">From: {item.from}</p>
                          {item.corrupt_issue && (
                            <p className="meta danger" style={{ marginTop: '4px' }}>
                              {item.corrupt_issue}
                            </p>
                          )}
                          {item.chaser_status && (
                            <p className="meta ok" style={{ marginTop: '4px' }}>
                              Chaser: {item.chaser_status}
                            </p>
                          )}
                          {item.has_reply && item.latest_reply && (
                            <p className="meta info" style={{ marginTop: '4px' }}>
                              Reply from {item.latest_reply.from_addr || 'carrier'}
                              {item.latest_reply.body ? ` — "${item.latest_reply.body.slice(0, 90)}${item.latest_reply.body.length > 90 ? '…' : ''}"` : ''}
                            </p>
                          )}
                          {item.corrupt_action && (
                            <p className="meta" style={{ marginTop: '2px' }}>
                              Corruption action: {item.corrupt_action}
                            </p>
                          )}
                        </div>

                        <div style={{ display: 'flex', flexDirection: 'column', gap: '8px', alignItems: 'flex-end' }}>
                          <button
                            className="btn-sm"
                            onClick={(e) => {
                              e.stopPropagation()
                              openEmail(item.email_id)
                            }}
                          >
                            Open
                          </button>
                          {(qs === 'missing_bl' || queueFilter === 'missing_bl') && (
                            <button
                              className="btn-secondary btn-sm"
                              onClick={(e) => {
                                e.stopPropagation()
                                handleChase(item.email_id)
                              }}
                            >
                              Send chaser
                            </button>
                          )}
                        </div>
                      </div>

                      {qs === 'corrupted' && (
                        <div
                          style={{ display: 'flex', gap: '10px', alignItems: 'center', marginTop: '12px', borderTop: '1px solid var(--line)', paddingTop: '12px' }}
                          onClick={(e) => e.stopPropagation()}
                        >
                          <input
                            type="text"
                            className="search-input"
                            placeholder="Add a note"
                            value={focusedCorruptRow === item.email_id ? corruptActionNotes : ''}
                            onFocus={() => setFocusedCorruptRow(item.email_id)}
                            onChange={(e) => setCorruptActionNotes(e.target.value)}
                          />
                          <button
                            className="btn-secondary btn-sm"
                            onClick={() => handleCorruptAction(item.email_id, 'CARRIER_RE_REQUESTED')}
                          >
                            Request new copy
                          </button>
                          <button
                            className="btn-sm"
                            onClick={() => handleCorruptAction(item.email_id, 'MANUAL_OVERRIDE')}
                          >
                            Mark handled
                          </button>
                        </div>
                      )}
                    </div>
                  )
                })}

                {/* Pagination */}
                <div className="pager">
                  <button
                    className="btn-secondary btn-sm"
                    disabled={queuePage <= 1}
                    onClick={() => setQueuePage(p => p - 1)}
                  >
                    Prev
                  </button>
                  <span className="meta">
                    Page {queueData.page} of {totalPages} · {queueData.total} items
                  </span>
                  <button
                    className="btn-secondary btn-sm"
                    disabled={queuePage >= totalPages}
                    onClick={() => setQueuePage(p => p + 1)}
                  >
                    Next
                  </button>
                </div>
              </>
            )}
          </div>
        )}

        {/* ========================================================== */}
        {/* VIEW: AUTO EMAIL GETTER & CLASSIFIER STUDIO                */}
        {/* ========================================================== */}
        {view === 'getter' && (
          <div className="getter-container">
            {/* HERO BANNER & PIPELINE FLOW */}
            <div className="getter-hero-banner">
              <div className="getter-hero-top">
                <div>
                  <h1>⚡ Auto Email Getter & Intelligent Classifier</h1>
                  <p>
                    Continuous mailbox polling and real-time operational triage powered by <strong>Laya Decision Model (Convai Innovations)</strong>.
                    Directly connected to <strong>shiawyonglim@gmail.com</strong> via Google IMAP SSL (Port 993) & SMTP (Port 587).
                  </p>
                </div>
                <div className="getter-live-badge">
                  <span className="getter-pulse-dot"></span>
                  {getterSource === 'real'
                    ? (isLiveStreaming ? 'LIVE GMAIL STREAM (1.8s)' : 'IMAP SSL READY · 30,000+ MSGS')
                    : (isLiveStreaming ? 'LIVE POLLING ACTIVE (1.8s)' : 'DATASET READY · 520 MSGS')}
                </div>
              </div>

              {/* Source Switcher */}
              <div className="getter-source-switch">
                <button
                  className={`getter-source-btn ${getterSource === 'real' ? 'active real' : ''}`}
                  onClick={() => {
                    setGetterSource('real')
                    fetchGetterEmails(1, getterCategory, getterQueueFilter, getterSearch, 'real')
                  }}
                >
                  📬 Live Personal Gmail (shiawyonglim@gmail.com)
                </button>
                <button
                  className={`getter-source-btn ${getterSource === 'dataset' ? 'active' : ''}`}
                  onClick={() => {
                    setGetterSource('dataset')
                    fetchGetterEmails(1, getterCategory, getterQueueFilter, getterSearch, 'dataset')
                  }}
                >
                  📁 Dataset Mailbox (520 Emails)
                </button>
              </div>

              {/* 4-Step Pipeline Flow */}
              <div className="getter-pipeline-flow">
                <div className="getter-flow-step">
                  <div className="flow-step-icon">📡</div>
                  <div className="flow-step-text">
                    <span className="flow-step-title">1. Real IMAP SSL</span>
                    <span className="flow-step-desc">imap.gmail.com:993 (shiawyonglim)</span>
                  </div>
                </div>
                <div className="getter-flow-step">
                  <div className="flow-step-icon">🔍</div>
                  <div className="flow-step-text">
                    <span className="flow-step-title">2. Laya Preprocessor</span>
                    <span className="flow-step-desc">clean_email_body + email_state</span>
                  </div>
                </div>
                <div className="getter-flow-step">
                  <div className="flow-step-icon">🧠</div>
                  <div className="flow-step-text">
                    <span className="flow-step-title">3. Laya System-1</span>
                    <span className="flow-step-desc">Non-Autoregressive Decision Schema</span>
                  </div>
                </div>
                <div className="getter-flow-step">
                  <div className="flow-step-icon">🎯</div>
                  <div className="flow-step-text">
                    <span className="flow-step-title">4. Smart Router</span>
                    <span className="flow-step-desc">7-Field Audit, Billing, SI, Chaser</span>
                  </div>
                </div>
              </div>
            </div>

            {/* METRICS SUMMARY */}
            <div className="getter-metrics-grid">
              <div className="getter-metric-card">
                <span className="getter-metric-label">{getterSource === 'real' ? 'Gmail Ingested' : 'Dataset Ingested'}</span>
                <span className="getter-metric-val">{getterTotal ?? 0}</span>
                <span className="getter-metric-sub">
                  <span>{getterSource === 'real' ? `${(getterStatus?.total_available ?? 0).toLocaleString()} in mailbox` : '520 carrier emails'}</span>
                </span>
              </div>
              <div className="getter-metric-card">
                <span className="getter-metric-label">Classifier Engine</span>
                <span className="getter-metric-val" style={{ color: '#7e22ce' }}>
                  Laya v0.3.4
                </span>
                <span className="getter-metric-sub">
                  <span>Convai System-1 Decision Model</span>
                </span>
              </div>
              <div className="getter-metric-card">
                <span className="getter-metric-label">Inference Latency</span>
                <span className="getter-metric-val" style={{ color: '#15803d' }}>
                  {getterStatus?.avg_latency_ms ? `~${Math.round(getterStatus.avg_latency_ms)} ms` : 'pending'}
                </span>
                <span className="getter-metric-sub">
                  <span>Laya CPU forward pass (33ms on GPU)</span>
                </span>
              </div>
              <div className="getter-metric-card">
                <span className="getter-metric-label">Mailbox Status</span>
                <span className="getter-metric-val" style={{ fontSize: '1.05rem', color: '#1e293b' }}>
                  {getterSource === 'real' ? 'LIVE IMAP SSL' : 'STATIC DATASET'}
                </span>
                <span className="getter-metric-sub">
                  <span>{getterSource === 'real' ? 'shiawyonglim@gmail.com' : 'shipping.docs@aprilasia.com'}</span>
                </span>
              </div>
            </div>

            {/* CONTROLS & STREAM TOOLBAR */}
            <div className="getter-toolbar">
              <div className="getter-toolbar-left">
                {getterSource === 'real' ? (
                  <>
                    <button
                      className="getter-btn primary"
                      style={{ background: '#15803d', borderColor: '#15803d' }}
                      onClick={handlePollRealGmail}
                      disabled={pollingRealGmail}
                    >
                      {pollingRealGmail ? '⏳ Polling Gmail IMAP...' : '🔄 Poll Real Gmail Inbox (IMAP SSL)'}
                    </button>
                    <button
                      className="getter-btn"
                      style={{ background: '#f5f3ff', borderColor: '#c084fc', color: '#7e22ce', fontWeight: 700 }}
                      onClick={handleSendRealTestEmail}
                      disabled={sendingRealTest}
                    >
                      {sendingRealTest ? '⏳ Dispatching via SMTP...' : '✉️ Send Real Test Email to Myself & Classify'}
                    </button>
                    <button
                      className={`getter-btn ${isLiveStreaming ? 'active-stream' : ''}`}
                      onClick={() => setIsLiveStreaming(prev => !prev)}
                    >
                      {isLiveStreaming ? '⏸️ Pause Auto-Polling' : '▶️ Live Auto-Poll (1.8s)'}
                    </button>
                  </>
                ) : (
                  <>
                    <button
                      className={`getter-btn ${isLiveStreaming ? 'active-stream' : 'primary'}`}
                      onClick={() => setIsLiveStreaming(prev => !prev)}
                    >
                      {isLiveStreaming ? '⏸️ Pause Auto-Polling' : '▶️ Start Live Stream (1.8s)'}
                    </button>
                    <button
                      className="getter-btn"
                      onClick={() => handleFetchBatch(10)}
                      disabled={isLiveStreaming}
                    >
                      📥 Ingest Next Batch (+10)
                    </button>
                    <button
                      className="getter-btn"
                      onClick={() => handleFetchBatch(520)}
                      disabled={isLiveStreaming}
                    >
                      ⚡ Ingest All Available (520)
                    </button>
                    <button
                      className="getter-btn"
                      onClick={handleResetGetter}
                      disabled={isLiveStreaming}
                    >
                      🔄 Reset Stream (25)
                    </button>
                    <button
                      className="getter-btn"
                      style={{ background: '#f0fdf4', borderColor: '#86efac', color: '#166534' }}
                      onClick={() => setCustomModalOpen(true)}
                    >
                      ➕ Simulate Custom Inbound Email
                    </button>
                  </>
                )}
              </div>
              {getterMsg && (
                <div style={{ fontSize: '0.82rem', color: '#166534', fontWeight: 600 }}>
                  ✓ {getterMsg}
                </div>
              )}
            </div>

            {/* FILTERS & SEARCH */}
            <div className="getter-filters-bar">
              <div className="getter-search-row">
                <input
                  type="text"
                  className="getter-search-input"
                  placeholder="🔍 Filter ingested stream by Subject, Email ID, Booking Ref (e.g. 5AKR-), BL Ref, or Carrier..."
                  value={getterSearch}
                  onChange={e => {
                    setGetterSearch(e.target.value)
                    fetchGetterEmails(1, getterCategory, getterQueueFilter, e.target.value)
                  }}
                />
              </div>

              {/* Category Pills */}
              <div className="getter-pill-row">
                <span style={{ fontSize: '0.75rem', fontWeight: 700, color: '#64748b', textTransform: 'uppercase' }}>
                  Category:
                </span>
                {[
                  { key: 'all', label: `All Categories (${getterStatus?.ingested_count || 0})` },
                  { key: 'BL_COMPARISON', label: `BL Comparison (${getterStatus?.categories?.BL_COMPARISON || 0})` },
                  { key: 'INVOICE_QUERY', label: `Invoice Queries (${getterStatus?.categories?.INVOICE_QUERY || 0})` },
                  { key: 'SI_REQUEST', label: `SI Requests (${getterStatus?.categories?.SI_REQUEST || 0})` },
                  { key: 'GENERAL', label: `General Logistics (${getterStatus?.categories?.GENERAL || 0})` },
                  { key: 'SPAM', label: `Quarantined (${getterStatus?.categories?.SPAM || 0})` },
                ].map(pill => (
                  <button
                    key={pill.key}
                    className={`getter-pill ${getterCategory === pill.key ? 'active' : ''}`}
                    onClick={() => {
                      setGetterCategory(pill.key)
                      fetchGetterEmails(1, pill.key, getterQueueFilter, getterSearch)
                    }}
                  >
                    {pill.label}
                  </button>
                ))}
              </div>

              {/* Queue Destination Pills */}
              <div className="getter-pill-row">
                <span style={{ fontSize: '0.75rem', fontWeight: 700, color: '#64748b', textTransform: 'uppercase' }}>
                  Smart Queue:
                </span>
                {[
                  { key: 'all', label: 'All Destinations' },
                  { key: 'comparator', label: `7-Field Comparator Studio (${getterStatus?.queues?.comparator_ready || 0})` },
                  { key: 'chaser', label: `Awaiting Draft BL (${getterStatus?.queues?.awaiting_draft_bl || 0})` },
                  { key: 'billing', label: `Billing & THC Desk (${getterStatus?.queues?.billing_desk || 0})` },
                  { key: 'si', label: `SI Operations (${getterStatus?.queues?.si_operations || 0})` },
                  { key: 'general', label: `General Ops Log (${getterStatus?.queues?.general_ops || 0})` },
                ].map(pill => (
                  <button
                    key={pill.key}
                    className={`getter-pill ${getterQueueFilter === pill.key ? 'active' : ''}`}
                    onClick={() => {
                      setGetterQueueFilter(pill.key)
                      fetchGetterEmails(1, getterCategory, pill.key, getterSearch)
                    }}
                  >
                    {pill.label}
                  </button>
                ))}
              </div>
            </div>

            {/* SPLIT VIEW: INGESTED STREAM TABLE + INSPECTOR */}
            <div className="getter-split-view">
              {/* Left Column: Stream Table */}
              <div className="getter-table-card">
                <div className="getter-table-header">
                  <h3>📥 Ingested & Classified Stream ({getterTotal} items)</h3>
                  <div style={{ fontSize: '0.8rem', color: '#64748b' }}>
                    Page {getterPage} of {Math.max(1, Math.ceil(getterTotal / getterLimit))}
                  </div>
                </div>

                <div className="getter-table-wrap">
                  {getterLoading ? (
                    <div style={{ padding: '40px', textAlign: 'center', color: '#64748b' }}>
                      <p>Loading email stream from mailbox...</p>
                    </div>
                  ) : getterEmails.length === 0 ? (
                    <div style={{ padding: '40px', textAlign: 'center', color: '#64748b' }}>
                      <p>No ingested emails match the selected filters.</p>
                    </div>
                  ) : (
                    <table className="getter-table">
                      <thead>
                        <tr>
                          <th>ID</th>
                          <th>Category</th>
                          <th>Booking / BL Ref</th>
                          <th>Subject</th>
                          <th>Atts</th>
                          <th>Routing Queue</th>
                        </tr>
                      </thead>
                      <tbody>
                        {getterEmails.map(item => {
                          const isSelected = selectedGetterEmail?.email_id === item.email_id
                          const cat = item.classification.category
                          const catClass = cat.toLowerCase()
                          const prio = item.classification.priority || 'NORMAL'
                          const prioClass = prio.toLowerCase()

                          return (
                            <tr
                              key={item.email_id}
                              className={`getter-table-row ${isSelected ? 'selected' : ''}`}
                              onClick={() => setSelectedGetterEmail(item)}
                            >
                              <td style={{ fontFamily: 'monospace', fontWeight: 700 }}>
                                {item.email_id}
                                {item.source === 'real_gmail' ? (
                                  <span style={{ marginLeft: 4, fontSize: '0.64rem', color: '#15803d', background: '#dcfce7', padding: '1px 5px', borderRadius: 4, fontWeight: 700 }}>
                                    ●GMAIL
                                  </span>
                                ) : item.is_custom_simulation ? (
                                  <span style={{ marginLeft: 4, fontSize: '0.68rem', color: '#16a34a' }}>●LIVE</span>
                                ) : null}
                              </td>
                              <td>
                                <span className={`cat-badge ${catClass}`}>
                                  {cat === 'BL_COMPARISON' ? '📄 BL AUDIT'
                                    : cat === 'INVOICE_QUERY' ? '💼 INVOICE'
                                    : cat === 'SI_REQUEST' ? '📝 SI REQUEST'
                                    : cat === 'SPAM' ? '🛡️ SPAM'
                                    : '🌐 GENERAL'}
                                </span>
                              </td>
                              <td>
                                <span style={{ fontFamily: 'monospace', fontSize: '0.78rem', color: '#0f172a', fontWeight: 600 }}>
                                  {item.entities.booking_ref !== 'N/A' ? item.entities.booking_ref
                                    : item.entities.bl_ref !== 'N/A' ? item.entities.bl_ref
                                    : item.entities.invoice_no !== 'N/A' ? `INV ${item.entities.invoice_no}`
                                    : '—'}
                                </span>
                              </td>
                              <td style={{ maxWidth: '240px', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                                <span title={item.subject}>{item.subject}</span>
                              </td>
                              <td style={{ textAlign: 'center' }}>
                                <span style={{
                                  fontSize: '0.74rem',
                                  padding: '2px 7px',
                                  borderRadius: 10,
                                  background: item.attachments_count >= 2 ? '#dcfce7' : item.attachments_count === 1 ? '#fef9c3' : '#f1f5f9',
                                  color: item.attachments_count >= 2 ? '#166534' : '#475569',
                                  fontWeight: 700
                                }}>
                                  📎 {item.attachments_count}
                                </span>
                              </td>
                              <td>
                                <span style={{ fontSize: '0.78rem', color: '#475569', fontWeight: 600 }}>
                                  {item.classification.target_queue}
                                </span>
                              </td>
                            </tr>
                          )
                        })}
                      </tbody>
                    </table>
                  )}
                </div>

                {/* Pagination */}
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '12px 18px', borderTop: '1px solid #e2e8f0', background: '#fafbfc' }}>
                  <button
                    className="getter-btn"
                    disabled={getterPage <= 1}
                    onClick={() => {
                      const prevPage = getterPage - 1
                      setGetterPage(prevPage)
                      fetchGetterEmails(prevPage)
                    }}
                  >
                    ← Prev
                  </button>
                  <span style={{ fontSize: '0.82rem', color: '#64748b' }}>
                    Showing {(getterPage - 1) * getterLimit + 1} - {Math.min(getterTotal, getterPage * getterLimit)} of {getterTotal}
                  </span>
                  <button
                    className="getter-btn"
                    disabled={getterPage >= Math.ceil(getterTotal / getterLimit)}
                    onClick={() => {
                      const nextPage = getterPage + 1
                      setGetterPage(nextPage)
                      fetchGetterEmails(nextPage)
                    }}
                  >
                    Next →
                  </button>
                </div>
              </div>

              {/* Right Column: Deep Dive Inspector */}
              <div className="getter-inspector-card">
                {selectedGetterEmail ? (
                  <>
                    <div className="getter-inspector-header">
                      <div className="getter-inspector-title-row">
                        <div>
                          <span style={{ fontSize: '0.75rem', fontWeight: 700, color: '#64748b', textTransform: 'uppercase' }}>
                            Email Dossier Inspector
                          </span>
                          <h3 style={{ marginTop: 4 }}>
                            {selectedGetterEmail.email_id}
                          </h3>
                        </div>
                        <span className={`prio-badge ${(selectedGetterEmail.classification.priority || 'NORMAL').toLowerCase()}`}>
                          {selectedGetterEmail.classification.priority || 'NORMAL'} PRIORITY
                        </span>
                      </div>

                      <div style={{ fontSize: '0.82rem', color: '#475569', display: 'flex', flexDirection: 'column', gap: 4 }}>
                        <div><strong>From:</strong> {selectedGetterEmail.from}</div>
                        <div><strong>To:</strong> {selectedGetterEmail.to}</div>
                        <div><strong>Subject:</strong> {selectedGetterEmail.subject}</div>
                      </div>
                    </div>

                    <div className="getter-inspector-body">
                      {/* Classification Box */}
                      <div className="getter-section-box">
                        <div className="getter-section-title">
                          <span>🏷️ Automated Classification Dossier</span>
                        </div>
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 }}>
                          <span className={`cat-badge ${selectedGetterEmail.classification.category.toLowerCase()}`} style={{ fontSize: '0.82rem', padding: '4px 12px' }}>
                            {selectedGetterEmail.classification.display_tag}
                          </span>
                          <span style={{ fontSize: '0.78rem', fontWeight: 700, color: '#16a34a' }}>
                            Confidence: {Math.round((selectedGetterEmail.classification.confidence || 0.99) * 100)}%
                          </span>
                        </div>
                        <p style={{ fontSize: '0.82rem', color: '#475569', margin: '0 0 10px 0', lineHeight: 1.4 }}>
                          {selectedGetterEmail.classification.description}
                        </p>
                        <div style={{ fontSize: '0.75rem', color: '#64748b' }}>
                          <strong>Engine Provenance:</strong> <code>{selectedGetterEmail.classification.provenance}</code>
                        </div>
                      </div>

                      {/* Laya Decision Model Card */}
                      {selectedGetterEmail.laya_decision && (
                        <div className="getter-laya-card">
                          <div className="getter-laya-header">
                            <div className="getter-laya-title">
                              <span>🧠 Laya Decision Model (Convai Innovations)</span>
                            </div>
                            <span style={{ fontSize: '0.7rem', background: '#e9d5ff', color: '#6b21a8', padding: '2px 8px', borderRadius: 4, fontWeight: 700 }}>
                              {selectedGetterEmail.laya_decision?.inference === 'real'
                                ? 'NEURAL VERIFIED · SYSTEM-1'
                                : selectedGetterEmail.laya_decision?.inference === 'pending'
                                  ? 'RULES · NEURAL PENDING'
                                  : 'RULES ONLY'}
                            </span>
                          </div>
                          
                          <div className="getter-laya-grid">
                            <div className="getter-laya-item">
                              <div className="getter-laya-label">
                                <span>Question: Category</span>
                                <span>type: choice</span>
                              </div>
                              <div className="getter-laya-val" style={{ color: '#6b21a8' }}>
                                {selectedGetterEmail.laya_decision.answers?.category?.choice || selectedGetterEmail.classification.category}
                              </div>
                              <div style={{ fontSize: '0.72rem', color: '#16a34a', marginTop: 2 }}>
                                Calibrated Confidence: {Math.round((selectedGetterEmail.laya_decision.answers?.category?.confidence || 0.96) * 100)}%
                              </div>
                            </div>

                            <div className="getter-laya-item">
                              <div className="getter-laya-label">
                                <span>Question: Urgency</span>
                                <span>type: score (0-2.0)</span>
                              </div>
                              <div className="getter-laya-val">
                                {selectedGetterEmail.laya_decision.answers?.urgency?.level || 'ROUTINE'} ({selectedGetterEmail.laya_decision.answers?.urgency?.score ?? 0.2})
                              </div>
                              <div className="getter-meter-bar">
                                <div
                                  className="getter-meter-fill"
                                  style={{ width: `${Math.min(100, Math.max(10, ((selectedGetterEmail.laya_decision.answers?.urgency?.score ?? 0.2) / 2.0) * 100))}%` }}
                                />
                              </div>
                            </div>

                            <div className="getter-laya-item">
                              <div className="getter-laya-label">
                                <span>Question: Needs Reply</span>
                                <span>type: noul</span>
                              </div>
                              <div className="getter-laya-val" style={{ color: (selectedGetterEmail.laya_decision.answers?.needs_reply?.probability ?? 0.5) > 0.5 ? '#b45309' : '#15803d' }}>
                                {(selectedGetterEmail.laya_decision.answers?.needs_reply?.probability ?? 0.5) > 0.5 ? 'YES — ACTION REQUIRED' : 'NO — INFORMATIONAL'}
                              </div>
                              <div style={{ fontSize: '0.72rem', color: '#64748b', marginTop: 2 }}>
                                Probability: {selectedGetterEmail.laya_decision.answers?.needs_reply?.probability ?? 0.5}
                              </div>
                            </div>

                            <div className="getter-laya-item">
                              <div className="getter-laya-label">
                                <span>Question: Is Spam</span>
                                <span>type: noul</span>
                              </div>
                              <div className="getter-laya-val" style={{ color: (selectedGetterEmail.laya_decision.answers?.is_spam?.probability ?? 0) > 0.5 ? '#dc2626' : '#15803d' }}>
                                {(selectedGetterEmail.laya_decision.answers?.is_spam?.probability ?? 0) > 0.5 ? 'QUARANTINED' : 'CLEAN INBOX'}
                              </div>
                              <div style={{ fontSize: '0.72rem', color: '#64748b', marginTop: 2 }}>
                                Probability: {selectedGetterEmail.laya_decision.answers?.is_spam?.probability ?? 0.02}
                              </div>
                            </div>
                          </div>

                          {selectedGetterEmail.laya_decision.clean_state && (
                            <div style={{ background: '#ffffff', border: '1px solid #e9d5ff', borderRadius: 6, padding: '8px 10px' }}>
                              <div style={{ fontSize: '0.68rem', fontWeight: 700, color: '#6b21a8', textTransform: 'uppercase', marginBottom: 4 }}>
                                Laya Preprocessed State (Signatures & Disclaimers Stripped):
                              </div>
                              <div style={{ fontSize: '0.74rem', color: '#475569', maxHeight: '75px', overflowY: 'auto', fontFamily: 'monospace', whiteSpace: 'pre-wrap' }}>
                                {selectedGetterEmail.laya_decision.clean_state.body || selectedGetterEmail.snippet}
                              </div>
                            </div>
                          )}
                        </div>
                      )}

                      {/* Extracted Shipping Entities */}
                      <div className="getter-section-box">
                        <div className="getter-section-title">
                          <span>📦 Extracted Shipping Entities</span>
                        </div>
                        <div className="getter-entities-grid">
                          <div className="getter-entity-item">
                            <span className="getter-entity-label">Booking Ref</span>
                            <div className="getter-entity-val">{selectedGetterEmail.entities.booking_ref}</div>
                          </div>
                          <div className="getter-entity-item">
                            <span className="getter-entity-label">Carrier B/L Ref</span>
                            <div className="getter-entity-val">{selectedGetterEmail.entities.bl_ref}</div>
                          </div>
                          <div className="getter-entity-item">
                            <span className="getter-entity-label">Invoice Number</span>
                            <div className="getter-entity-val">{selectedGetterEmail.entities.invoice_no}</div>
                          </div>
                          <div className="getter-entity-item">
                            <span className="getter-entity-label">Liner Carrier</span>
                            <div className="getter-entity-val">{selectedGetterEmail.entities.carrier}</div>
                          </div>
                          <div className="getter-entity-item" style={{ gridColumn: 'span 2' }}>
                            <span className="getter-entity-label">Detected Ports / Locations</span>
                            <div className="getter-entity-val">{selectedGetterEmail.entities.ports.join(', ')}</div>
                          </div>
                        </div>
                      </div>

                      {/* Decision Signals / Why Classified */}
                      <div className="getter-section-box">
                        <div className="getter-section-title">
                          <span>🔍 Decision Signals & Trigger Reasoning</span>
                        </div>
                        <ul className="getter-signals-list">
                          {selectedGetterEmail.classification.signals?.map((sig, idx) => (
                            <li key={idx} className="getter-signal-item">
                              <span className="getter-signal-dot"></span>
                              <span>{sig}</span>
                            </li>
                          ))}
                        </ul>
                      </div>

                      {/* Downstream Smart Action Card */}
                      <div className="getter-action-banner">
                        <div className="getter-action-title">
                          <span>🚀 Downstream Automated Action</span>
                        </div>
                        <div className="getter-action-desc">
                          {selectedGetterEmail.classification.recommended_action}
                        </div>
                        <div style={{ marginTop: 8 }}>
                          {selectedGetterEmail.classification.category === 'BL_COMPARISON' && selectedGetterEmail.attachments_count >= 2 ? (
                            <button
                              className="getter-btn primary"
                              onClick={() => {
                                setSelectedEmail(selectedGetterEmail.email_id)
                                setView('verify')
                              }}
                            >
                              🔍 Open in 7-Field Verify Studio
                            </button>
                          ) : selectedGetterEmail.classification.category === 'INVOICE_QUERY' ? (
                            <button
                              className="getter-btn primary"
                              style={{ background: '#b45309', borderColor: '#b45309' }}
                              onClick={() => {
                                setSelectedEmail(selectedGetterEmail.email_id)
                                openDraftEmail(selectedGetterEmail.email_id, selectedGetterEmail)
                              }}
                            >
                              ✉️ Auto-Draft Billing / THC Reply
                            </button>
                          ) : selectedGetterEmail.classification.category === 'SI_REQUEST' ? (
                            <button
                              className="getter-btn primary"
                              style={{ background: '#047857', borderColor: '#047857' }}
                              onClick={() => {
                                setSelectedEmail(selectedGetterEmail.email_id)
                                openDraftEmail(selectedGetterEmail.email_id, selectedGetterEmail)
                              }}
                            >
                              ✉️ Auto-Draft Shipping Instructions
                            </button>
                          ) : (
                            <button
                              className="getter-btn"
                              onClick={() => {
                                setSelectedEmail(selectedGetterEmail.email_id)
                                openDraftEmail(selectedGetterEmail.email_id, selectedGetterEmail)
                              }}
                            >
                              ✉️ Auto-Draft Operational Email
                            </button>
                          )}
                        </div>
                      </div>

                      {/* Raw Body Snippet */}
                      <div className="getter-section-box">
                        <div className="getter-section-title">
                          <span>✉️ Raw Message Body</span>
                        </div>
                        <pre style={{
                          background: '#fff',
                          border: '1px solid #e2e8f0',
                          padding: 12,
                          borderRadius: 8,
                          fontSize: '0.78rem',
                          color: '#334155',
                          whiteSpace: 'pre-wrap',
                          maxHeight: '180px',
                          overflowY: 'auto',
                          fontFamily: 'monospace'
                        }}>
                          {selectedGetterEmail.full_body}
                        </pre>
                      </div>
                    </div>
                  </>
                ) : (
                  <div style={{ padding: 40, textAlign: 'center', color: '#64748b' }}>
                    <p>Select an email from the stream to view its classified dossier.</p>
                  </div>
                )}
              </div>
            </div>

            {/* CUSTOM EMAIL INGESTION MODAL */}
            {customModalOpen && (
              <div className="getter-modal-overlay" onClick={() => setCustomModalOpen(false)}>
                <div className="getter-modal-card" onClick={e => e.stopPropagation()}>
                  <div className="getter-modal-header">
                    <h3>➕ Ingest & Classify Custom Inbound Email</h3>
                    <button
                      onClick={() => setCustomModalOpen(false)}
                      style={{ background: 'none', border: 'none', fontSize: '1.2rem', cursor: 'pointer' }}
                    >
                      ✕
                    </button>
                  </div>

                  <form onSubmit={handleCustomIngestSubmit}>
                    <div className="getter-modal-body">
                      {/* Presets */}
                      <div>
                        <span style={{ fontSize: '0.78rem', fontWeight: 700, color: '#475569', textTransform: 'uppercase' }}>
                          ⚡ Quick Simulation Presets:
                        </span>
                        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginTop: 6 }}>
                          <button
                            type="button"
                            className="getter-pill"
                            onClick={() => setCustomForm({
                              from_addr: 'doc.desk@evergreen-marine.com',
                              to_addr: 'shipping.docs@aprilasia.com',
                              subject: 'DRAFT BL READY _ 5AKR-61849 _ PORT KLANG _ SIN832764835',
                              body: 'Dear Documentation Team,\n\nPlease find attached our draft Bill of Lading for verification before vessel cutoff at Port Klang.\n\nBest regards,\nEvergreen Marine Liner Desk',
                              attachments: ['attachments/email_custom_SI.txt', 'attachments/email_custom_BL.txt']
                            })}
                          >
                            Preset 1: Draft BL Arrival (2 Atts)
                          </button>
                          <button
                            type="button"
                            className="getter-pill"
                            onClick={() => setCustomForm({
                              from_addr: 'accounting@freightpartner.com',
                              to_addr: 'shipping.docs@aprilasia.com',
                              subject: 'RE_ LOCAL CHARGES FOB - 5AKR-91823 - TELEX SURRENDER FEE',
                              body: 'Hi Team,\n\nQuery on invoice 5250089123: are the THC / local port fees and telex release charges included in the ocean freight debit note? Please advise itemized breakdown asap.\n\nBest regards,\nAccounting Desk',
                              attachments: []
                            })}
                          >
                            Preset 2: Invoice & THC Query (0 Atts)
                          </button>
                          <button
                            type="button"
                            className="getter-pill"
                            onClick={() => setCustomForm({
                              from_addr: 'liner.ops@cma-cgm.com',
                              to_addr: 'shipping.docs@aprilasia.com',
                              subject: 'REQUEST SI _ 5RSG-90214 _ JAKARTA _ URGENT PORT CUTOFF',
                              body: 'URGENT: Please submit shipping instruction particulars for booking 5RSG-90214. Port cutoff is 17:00 SGT today.\n\nCMA CGM Customer Service',
                              attachments: []
                            })}
                          >
                            Preset 3: Urgent SI Request (0 Atts)
                          </button>
                        </div>
                      </div>

                      <div>
                        <label style={{ fontSize: '0.78rem', fontWeight: 600, color: '#475569', display: 'block', marginBottom: 4 }}>
                          Sender Address (From):
                        </label>
                        <input
                          type="email"
                          required
                          className="getter-search-input"
                          style={{ width: '100%' }}
                          value={customForm.from_addr}
                          onChange={e => setCustomForm({ ...customForm, from_addr: e.target.value })}
                        />
                      </div>

                      <div>
                        <label style={{ fontSize: '0.78rem', fontWeight: 600, color: '#475569', display: 'block', marginBottom: 4 }}>
                          Subject Line:
                        </label>
                        <input
                          type="text"
                          required
                          className="getter-search-input"
                          style={{ width: '100%' }}
                          value={customForm.subject}
                          onChange={e => setCustomForm({ ...customForm, subject: e.target.value })}
                        />
                      </div>

                      <div>
                        <label style={{ fontSize: '0.78rem', fontWeight: 600, color: '#475569', display: 'block', marginBottom: 4 }}>
                          Email Body:
                        </label>
                        <textarea
                          rows={4}
                          required
                          className="getter-search-input"
                          style={{ width: '100%', fontFamily: 'monospace', fontSize: '0.82rem' }}
                          value={customForm.body}
                          onChange={e => setCustomForm({ ...customForm, body: e.target.value })}
                        />
                      </div>

                      <div>
                        <label style={{ fontSize: '0.78rem', fontWeight: 600, color: '#475569', display: 'block', marginBottom: 4 }}>
                          Attachments Simulation:
                        </label>
                        <div style={{ display: 'flex', gap: 12 }}>
                          <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: '0.82rem' }}>
                            <input
                              type="radio"
                              name="atts_opt"
                              checked={customForm.attachments.length === 2}
                              onChange={() => setCustomForm({
                                ...customForm,
                                attachments: ['attachments/email_custom_SI.txt', 'attachments/email_custom_BL.txt']
                              })}
                            />
                            2 Attachments (SI + Draft BL)
                          </label>
                          <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: '0.82rem' }}>
                            <input
                              type="radio"
                              name="atts_opt"
                              checked={customForm.attachments.length === 0}
                              onChange={() => setCustomForm({ ...customForm, attachments: [] })}
                            />
                            0 Attachments (Text Inquiry / Chaser)
                          </label>
                        </div>
                      </div>
                    </div>

                    <div className="getter-modal-footer">
                      <button
                        type="button"
                        className="getter-btn"
                        onClick={() => setCustomModalOpen(false)}
                      >
                        Cancel
                      </button>
                      <button
                        type="submit"
                        className="getter-btn primary"
                        disabled={customIngesting}
                      >
                        {customIngesting ? 'Ingesting...' : '⚡ Ingest & Classify Now'}
                      </button>
                    </div>
                  </form>
                </div>
              </div>
            )}
          </div>
        )}

        {/* VIEW: VERIFY DOCUMENTS                                     */}
        {/* ========================================================== */}
        {view === 'verify' && (
          <>
            <div className="page-header">
              <h1>Verify Documents</h1>
              <p>Compare an email's SI vs draft BL — or upload paper scans.</p>
            </div>

            {/* Mode toggle: inbox email vs paper scan/upload */}
            <div className="action-bar" style={{ flexWrap: 'wrap', gap: '10px' }}>
              <button
                className={`chip ${verifyMode === 'email' ? 'active' : ''}`}
                onClick={() => setVerifyMode('email')}
              >
                📥 Inbox Email
              </button>
              <button
                className={`chip ${verifyMode === 'scan' ? 'active' : ''}`}
                onClick={() => setVerifyMode('scan')}
              >
                📷 Paper Scan / Upload
              </button>
              {verifyMode === 'scan' && (
                <>
                  <button
                    onClick={handleScanVerify}
                    disabled={scanLoading || !scanSi?.file || !scanBl?.file}
                    style={{ padding: '8px 18px', fontSize: '0.85rem' }}
                  >
                    {scanLoading ? 'Reading documents via vision AI...' : '⚡ Verify Scanned Documents'}
                  </button>
                  <span style={{ fontSize: '0.82rem', color: '#777', alignSelf: 'center' }}>
                    Photos (JPG/PNG), scans, PDF, DOCX, XLSX, TXT · max 15 MB each
                  </span>
                </>
              )}
            </div>

            {verifyMode === 'email' && (<>
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

                <button
                  onClick={handleSaveDocuments}
                  disabled={savingDocs || loading || !selectedEmail
                    || (emailInfo?.attachments?.length || 0) < 2
                    || (DOC_PLACEHOLDER_RX.test(siText) && DOC_PLACEHOLDER_RX.test(blText))}
                  title="Write the edited SI/BL text back to this email's attachment files — originals are snapshotted to _backups/ first"
                  style={{ padding: '8px 16px', fontSize: '0.9rem', background: '#e07a5f' }}
                >
                  {savingDocs ? 'Saving…' : '💾 Save Doc Edits'}
                </button>

                {docBackups?.has_original && (
                  <button
                    onClick={handleRestoreDocuments}
                    disabled={restoringDocs || loading}
                    title={`Restore pristine email + attachments from backup (${docBackups.snapshots?.length || 0} snapshot(s) kept)`}
                    style={{ padding: '8px 16px', fontSize: '0.9rem', background: 'transparent', border: '1px solid var(--border-color)', color: 'var(--text-color)', boxShadow: 'none' }}
                  >
                    {restoringDocs ? 'Restoring…' : '↺ Restore Original'}
                  </button>
                )}

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
                  {emailInfo?.category === 'INVOICE_QUERY' ? '✉️ Reply to Invoice Query' :
                   emailInfo?.category === 'SI_REQUEST' ? '✉️ Reply with SI' :
                   emailInfo?.category === 'GENERAL' ? '✉️ Reply to Inquiry' :
                   '✉️ Auto-Draft Email'}
                </button>
                <div className="keyboard-shortcuts-hint">
                  <span title="Keyboard shortcuts: Press [Q] for Previous, [W] for Next, [E] to Auto-Draft">
                    ⚡ <kbd>Q</kbd> Prev · <kbd>W</kbd> Next · <kbd>E</kbd> Draft
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

            {/* Save / restore status banner */}
            {saveDocMsg && (
              <div
                className="redirect-banner"
                style={{
                  background: saveDocMsg.startsWith('❌') ? '#f8d7da' : '#e8f5e9',
                  borderColor: saveDocMsg.startsWith('❌') ? '#f5c6cb' : '#c8e6c9',
                  color: saveDocMsg.startsWith('❌') ? '#721c24' : '#155724',
                }}
              >
                <div>{saveDocMsg}</div>
                {docBackups?.has_original && (
                  <span style={{ fontSize: '0.75rem', opacity: 0.8, whiteSpace: 'nowrap' }}>
                    🗄️ Backups: pristine original + {docBackups.snapshots?.length || 0} snapshot(s)
                  </span>
                )}
              </div>
            )}

            {/* Banner: Pulled from Supabase Cloud Cache */}
            {cloudSyncInfo && (
              <div className="cloud-cache-banner">
                <div>
                  <strong>☁️ Cloud-cached verdict:</strong> {cloudSyncInfo.status || 'OK'}{cloudSyncInfo.updated_at ? ` · synced ${new Date(cloudSyncInfo.updated_at).toLocaleTimeString()}` : ''}
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

            {/* Contextual Action Card: Inbound Invoice & Local Charges Query */}
            {emailInfo && emailInfo.category === 'INVOICE_QUERY' && (
              <div className="action-card query" style={{ background: '#fdf9f4', border: '1px solid #eadbc8', borderRadius: '10px', padding: '14px 18px', marginBottom: '16px' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: '10px' }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                    <span style={{ fontSize: '1.3rem' }}>💼</span>
                    <div>
                      <strong style={{ color: '#8c4308', display: 'block' }}>
                        Inbound Billing & Local Charges Inquiry (from {emailInfo.from})
                      </strong>
                      <span style={{ fontSize: '0.84rem', color: '#555' }}>
                        Sender is inquiring about invoice charges, THC, or telex release fees. Reply directly with the fee schedule.
                      </span>
                    </div>
                  </div>
                  <div style={{ display: 'flex', gap: '8px' }}>
                    <button
                      onClick={() => openDraftEmail(selectedEmail)}
                      style={{ background: '#8c4308', color: '#fff', border: 'none', borderRadius: '8px', padding: '8px 18px', fontSize: '0.85rem', fontWeight: 600, cursor: 'pointer' }}
                    >
                      ✉️ Reply with Charges Breakdown
                    </button>
                  </div>
                </div>
              </div>
            )}

            {/* Contextual Action Card: Inbound Shipping Instruction Request */}
            {emailInfo && emailInfo.category === 'SI_REQUEST' && (
              <div className="action-card query" style={{ background: '#f4f8fa', border: '1px solid #cfe2ec', borderRadius: '10px', padding: '14px 18px', marginBottom: '16px' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: '10px' }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                    <span style={{ fontSize: '1.3rem' }}>📄</span>
                    <div>
                      <strong style={{ color: '#006699', display: 'block' }}>
                        Inbound Shipping Instruction Request (from {emailInfo.from})
                      </strong>
                      <span style={{ fontSize: '0.84rem', color: '#555' }}>
                        Customer or freight forwarder is requesting Shipping Instructions for this booking.
                      </span>
                    </div>
                  </div>
                  <div style={{ display: 'flex', gap: '8px' }}>
                    <button
                      onClick={() => openDraftEmail(selectedEmail)}
                      style={{ background: '#006699', color: '#fff', border: 'none', borderRadius: '8px', padding: '8px 18px', fontSize: '0.85rem', fontWeight: 600, cursor: 'pointer' }}
                    >
                      ✉️ Reply with Shipping Instructions
                    </button>
                  </div>
                </div>
              </div>
            )}

            {/* Contextual Action Card: Inbound General Logistics Inquiry */}
            {emailInfo && emailInfo.category === 'GENERAL' && (
              <div className="action-card query" style={{ background: '#f8f9fa', border: '1px solid #e2e6ea', borderRadius: '10px', padding: '14px 18px', marginBottom: '16px' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: '10px' }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                    <span style={{ fontSize: '1.3rem' }}>🌐</span>
                    <div>
                      <strong style={{ color: '#495057', display: 'block' }}>
                        General Logistics Inquiry (from {emailInfo.from})
                      </strong>
                      <span style={{ fontSize: '0.84rem', color: '#555' }}>
                        General correspondence regarding vessel schedule, tracking, or booking status.
                      </span>
                    </div>
                  </div>
                  <div style={{ display: 'flex', gap: '8px' }}>
                    <button
                      onClick={() => openDraftEmail(selectedEmail)}
                      style={{ background: '#495057', color: '#fff', border: 'none', borderRadius: '8px', padding: '8px 18px', fontSize: '0.85rem', fontWeight: 600, cursor: 'pointer' }}
                    >
                      ✉️ Reply to Inquiry
                    </button>
                  </div>
                </div>
              </div>
            )}

            {/* Compact missing-document banner — the auto-prompt modal covers the draft flow */}
            {emailInfo && emailInfo.category === 'BL_COMPARISON' && missingDoc && (
              <div className="action-card chaser" style={{ padding: '14px 18px' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: '10px' }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                    <span style={{ fontSize: '1.3rem' }}>📎</span>
                    <div>
                      <strong style={{ color: '#b3560e' }}>
                        {missingDoc === 'si' ? 'Shipping Instruction missing'
                          : missingDoc === 'bl' ? 'Draft BL missing'
                          : 'No documents attached'}
                      </strong>
                      <span style={{ fontSize: '0.85rem', color: '#555', marginLeft: '8px' }}>
                        {missingDoc === 'si'
                          ? 'the sender forgot to attach it'
                          : `the ${detectCarrier(emailInfo)} desk hasn't issued it yet`}
                      </span>
                    </div>
                  </div>
                  <div style={{ display: 'flex', gap: '8px' }}>
                    <button
                      onClick={() => openDraftEmail(selectedEmail, null, null, missingDoc)}
                      style={{ background: '#e07a5f', padding: '8px 16px', fontSize: '0.85rem', fontWeight: 600 }}
                    >
                      ✉️ Draft {missingDoc === 'si' ? 'Request' : 'Chaser'} Email
                    </button>
                    {missingDoc !== 'si' && (
                      <button
                        onClick={() => handleChase(selectedEmail)}
                        style={{ background: 'transparent', border: '1px solid #e07a5f', color: '#e07a5f', boxShadow: 'none', padding: '8px 16px', fontSize: '0.85rem', fontWeight: 600 }}
                      >
                        ⚡ Quick Send
                      </button>
                    )}
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
                      onClick={() => setVerifyMode('scan')}
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
                  <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: '4px' }}>
                    <span
                      className={`category-pill category-${(emailInfo.category || '').toLowerCase()}`}
                      style={{ fontSize: '0.82rem', padding: '4px 12px' }}
                      title={emailInfo.category_description || emailInfo.category}
                    >
                      {emailInfo.display_tag || `Category: ${emailInfo.category || 'CLASSIFYING'}`}
                    </span>
                  </div>
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

            {/* EMAIL CONVERSATION THREAD — collapsible, auto-opens when messages exist */}
            {emailInfo && (
              <div className="glass-panel thread-panel">
                <div className="thread-header">
                  <div
                    onClick={() => setThreadOpen(o => !o)}
                    style={{ cursor: 'pointer', userSelect: 'none' }}
                    title={threadOpen ? 'Collapse thread' : 'Expand thread'}
                  >
                    <h3 style={{ margin: 0, color: 'var(--primary-color)', display: 'flex', alignItems: 'center', gap: '8px' }}>
                      <span style={{ fontSize: '0.8rem' }}>{threadOpen ? '▾' : '▸'}</span>
                      📨 Correspondence
                      {emailThreads.length > 0 && (
                        <span style={{ fontSize: '0.78rem', fontWeight: 700, background: '#f0f4e8', color: 'var(--primary-color)', borderRadius: '10px', padding: '1px 9px' }}>
                          {emailThreads.length}
                        </span>
                      )}
                    </h3>
                  </div>
                  <div className="thread-actions">
                    <button
                      onClick={handlePollInbox}
                      disabled={pollingInbox}
                      title="Poll the connected inbox via IMAP for unread carrier replies and auto-link them to this thread"
                    >
                      {pollingInbox ? 'Polling Inbox…' : '📥 Poll Inbox (IMAP)'}
                    </button>
                    <button
                      className="thread-sim-btn"
                      onClick={() => handleSimulateReply(selectedEmail)}
                      disabled={simulatingReply}
                      title="Simulate an inbound carrier reply (with revised Draft BL attachment) for demo/testing"
                    >
                      {simulatingReply ? 'Receiving Reply…' : '⚡ Simulate Carrier Reply'}
                    </button>
                  </div>
                </div>

                {threadOpen && replyNotice && (
                  <div className="thread-notice">{replyNotice}</div>
                )}

                {threadOpen && (
                <div className="thread-list">
                  {emailThreads.length === 0 && (
                    <p className="thread-empty">
                      No correspondence yet — outbound emails and auto-captured carrier replies appear here.
                    </p>
                  )}
                  {emailThreads.map(m => {
                    const inbound = m.direction === 'INBOUND'
                    const ts = m.received_at || m.sent_at
                    return (
                      <div key={m.id} className={`thread-row ${inbound ? 'inbound' : 'outbound'}`}>
                        <div className={`thread-bubble ${inbound ? 'inbound' : 'outbound'}`}>
                          <div className="thread-meta">
                            <strong>{inbound ? '📨 ' : '📤 '}{inbound ? (m.from_addr || 'Carrier') : `You → ${m.to_addr || 'carrier'}`}</strong>
                            {ts && <span className="thread-ts">{new Date(ts).toLocaleString()}</span>}
                          </div>
                          {m.subject && <div className="thread-subject">{m.subject}</div>}
                          {m.body && <div className="thread-body">{m.body}</div>}
                          {m.attachments?.length > 0 && (
                            <div className="thread-attach-row">
                              {m.attachments.map((a, i) => (
                                <span key={i} className={`thread-attach ${inbound ? 'inbound' : ''}`}>
                                  📎 {a.filename || a.path || 'attachment'}
                                  {a.size_kb ? ` (${a.size_kb} KB)` : ''}
                                </span>
                              ))}
                            </div>
                          )}
                          <div className="thread-foot">
                            {inbound ? 'INBOUND · AUTO-CAPTURED' : `OUTBOUND · ${m.mode || 'SMTP'}`}
                          </div>
                        </div>
                      </div>
                    )
                  })}
                </div>
                )}
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
            </>)}

            {/* Paper scan / upload mode — merged Paper Scan page */}
            {verifyMode === 'scan' && (
              <>
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
          </>
        )}

        {/* ========================================================== */}
        {/* VIEW: HUMAN-IN-THE-LOOP REVIEW                           */}
        {/* ========================================================== */}
        {view === 'review' && (() => {
          const items = reviewItems || []
          const pendingMismatch = items.filter(i => i.verdict_status === 'MISMATCH' && !i.resolved)
          const needsReview = items.filter(i => i.verdict_status === 'NEEDS_REVIEW' && !i.resolved)
          const corruptedOnly = items.filter(i =>
            i.corrupted && !i.resolved &&
            i.verdict_status !== 'NEEDS_REVIEW' && i.verdict_status !== 'MISMATCH')
          const replies = items.filter(i =>
            i.has_reply && !i.resolved &&
            i.verdict_status !== 'MISMATCH' && i.verdict_status !== 'NEEDS_REVIEW')

          const groups = [
            {
              key: 'mismatch',
              title: 'Mismatches',
              tagVariant: 'danger',
              desc: 'Genuine SI vs BL value conflicts — pick the correct value or enter a manual override in Verify Documents.',
              items: pendingMismatch,
              adjacentFilter: 'mismatch',
              actionLabel: 'Resolve discrepancy',
            },
            {
              key: 'needs_review',
              title: 'Escalated',
              tagVariant: 'warn',
              desc: 'The engine refused to auto-clear these — missing field values, missing attachments, unreadable or wrong documents.',
              items: needsReview,
              adjacentFilter: 'needs_review',
              actionLabel: 'Open & verify',
            },
            {
              key: 'corrupted',
              title: 'Corrupted files',
              tagVariant: 'danger',
              desc: 'Attachments that failed integrity checks before a verdict could be produced.',
              items: corruptedOnly,
              adjacentFilter: 'corrupted',
              actionLabel: 'Inspect',
            },
            {
              key: 'reply',
              title: 'Carrier replies',
              tagVariant: 'info',
              desc: 'Inbound replies auto-captured from carriers — confirm whether the revised draft BL clears the issue.',
              items: replies,
              adjacentFilter: 'reply_received',
              actionLabel: 'Open thread',
            },
          ]
          const groupCounts = Object.fromEntries(groups.map(g => [g.key, g.items.length]))
          const totalPending = groups.reduce((n, g) => n + g.items.length, 0)
          const visibleGroups = reviewFilter === 'all'
            ? groups
            : groups.filter(g => g.key === reviewFilter)

          const renderCard = (item, group) => {
            const qs = (item.queue_status || 'unverified').toLowerCase()
            const rm = REVIEW_REASON_META[item.review_reason]
            return (
              <div
                key={`${group.key}-${item.email_id}`}
                className={`item-card ${qs === 'corrupted' ? 'danger' : ''}`}
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
                      {rm && (
                        <span style={{ fontSize: '0.72rem', fontWeight: 700, padding: '2px 10px', borderRadius: '10px', color: rm.color, background: rm.bg }}>
                          {rm.label}
                        </span>
                      )}
                      {item.category && (
                        <span
                          className={`category-pill category-${(item.category || '').toLowerCase()}`}
                          title={item.category_description || item.category}
                        >
                          {item.display_tag || item.category}
                        </span>
                      )}
                      <span className="tag">{item.attachments_count} docs</span>
                    </div>
                    <p style={{ fontSize: '0.9rem', marginTop: '4px', color: 'var(--text-color)' }}>
                      {item.subject}
                    </p>
                    <p className="meta">From: {item.from}</p>

                    {item.defect_fields?.length > 0 && (
                      <div className="diff-chips" style={{ marginTop: '6px' }}>
                        {item.defect_fields.map(f => (
                          <span key={f} className="diff-field-chip">{f.replace(/_/g, ' ')}</span>
                        ))}
                      </div>
                    )}
                    {item.missing_fields?.length > 0 && (
                      <p className="meta warn" style={{ marginTop: '4px' }}>
                        Missing fields: {item.missing_fields.map(f => f.replace(/_/g, ' ')).join(', ')}
                      </p>
                    )}
                    {item.summary_reason && item.verdict_status === 'NEEDS_REVIEW' && (
                      <p className="meta warn" style={{ marginTop: '4px' }}>
                        {item.summary_reason}
                      </p>
                    )}
                    {item.corrupt_issue && (
                      <p className="meta danger" style={{ marginTop: '4px' }}>
                        {item.corrupt_issue}
                      </p>
                    )}
                    {item.corrupt_action && (
                      <p className="meta" style={{ marginTop: '2px' }}>
                        Corruption action logged: {item.corrupt_action}
                      </p>
                    )}
                    {item.chaser_status && (
                      <p className="meta ok" style={{ marginTop: '4px' }}>
                        Chaser: {item.chaser_status}
                      </p>
                    )}
                    {item.has_reply && item.latest_reply && (
                      <p className="meta info" style={{ marginTop: '4px' }}>
                        Reply from {item.latest_reply.from_addr || 'carrier'}
                        {item.latest_reply.body ? ` — "${item.latest_reply.body.slice(0, 90)}${item.latest_reply.body.length > 90 ? '…' : ''}"` : ''}
                      </p>
                    )}
                  </div>

                  <div style={{ display: 'flex', flexDirection: 'column', gap: '8px', alignItems: 'flex-end' }}>
                    <button
                      className="btn-sm"
                      onClick={() => openEmail(item.email_id, group.adjacentFilter)}
                    >
                      {group.actionLabel}
                    </button>
                    {item.has_bl === false && (
                      <button
                        className="btn-secondary btn-sm"
                        onClick={() => handleChase(item.email_id)}
                      >
                        Send chaser
                      </button>
                    )}
                  </div>
                </div>

                {(item.corrupted || item.review_reason === 'unreadable') && !item.corrupt_action && (
                  <div style={{ display: 'flex', gap: '10px', alignItems: 'center', marginTop: '12px', borderTop: '1px solid var(--line)', paddingTop: '12px' }}>
                    <input
                      type="text"
                      className="search-input"
                      placeholder="Add a note"
                      value={focusedCorruptRow === item.email_id ? corruptActionNotes : ''}
                      onFocus={() => setFocusedCorruptRow(item.email_id)}
                      onChange={(e) => setCorruptActionNotes(e.target.value)}
                    />
                    <button
                      className="btn-secondary btn-sm"
                      onClick={() => handleCorruptAction(item.email_id, 'CARRIER_RE_REQUESTED')}
                    >
                      Request new copy
                    </button>
                    <button
                      className="btn-sm"
                      onClick={() => handleCorruptAction(item.email_id, 'MANUAL_OVERRIDE')}
                    >
                      Mark handled
                    </button>
                  </div>
                )}
              </div>
            )
          }

          return (
            <div style={{ maxWidth: '1100px' }}>
              <div className="page-header">
                <h1>Human Review</h1>
                <p>Items the engine did not auto-clear: SI/BL mismatches, escalations, corrupted files, and carrier replies.</p>
              </div>

              <div className="action-bar" style={{ flexWrap: 'wrap', gap: '10px' }}>
                {REVIEW_FILTERS.map(f => (
                  <button
                    key={f.key}
                    className={`chip ${reviewFilter === f.key ? 'active' : ''}`}
                    onClick={() => setReviewFilter(f.key)}
                  >
                    {f.label}
                    {` · ${f.key === 'all' ? totalPending : (groupCounts[f.key] || 0)}`}
                  </button>
                ))}
                <button
                  className="btn-secondary btn-sm"
                  onClick={fetchReviewQueue}
                  style={{ marginLeft: 'auto' }}
                >
                  Refresh
                </button>
              </div>

              {reviewMsg && (
                <div className="notice ok">{reviewMsg}</div>
              )}

              {reviewLoading ? (
                <div className="empty-state">Loading…</div>
              ) : totalPending === 0 ? (
                <div className="empty-state">All clear — nothing is waiting on a human decision.</div>
              ) : (
                visibleGroups.map(g => (
                  g.items.length > 0 && (
                    <div key={g.key} style={{ marginBottom: '26px' }}>
                      <div className="section-head">
                        <div>
                          <h3>
                            {g.title}{' '}
                            <span className={`tag ${g.tagVariant}`}>{g.items.length}</span>
                          </h3>
                          <p className="section-sub">{g.desc}</p>
                        </div>
                      </div>
                      {g.items.map(item => renderCard(item, g))}
                    </div>
                  )
                ))
              )}
            </div>
          )
        })()}

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
        {/* VIEW: AUDIT LOG                                            */}
        {/* ========================================================== */}
        {view === 'audit' && (
          <div className="card-container">
            <div className="page-header">
              <h1>Audit Log</h1>
              <p>Immutable enterprise governance trail recording all automated verdicts, ocean carrier chasers, and operator overrides. <em>(Dual-persisted: local on-disk JSON <code>.cache/audit_state.json</code> + Supabase cloud database sync.)</em></p>
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

          // Generate picker derived values — which inbox emails to process
          const genFiltered = (genPicker || []).filter(i => {
            if (!genSearch) return true
            const s = genSearch.trim().toLowerCase()
            return i.email_id.toLowerCase().includes(s)
              || (i.subject || '').toLowerCase().includes(s)
              || (i.from || '').toLowerCase().includes(s)
          })
          const shownGenRows = genShowAll ? genFiltered : genFiltered.slice(0, 100)
          const visibleGenIds = genFiltered.map(i => i.email_id)
          const allGenVisibleSelected =
            visibleGenIds.length > 0 && visibleGenIds.every(id => genSelected.has(id))
          const someGenVisibleSelected = visibleGenIds.some(id => genSelected.has(id))
          const toggleGenEmail = (eid) => setGenSelected(prev => {
            const next = new Set(prev)
            if (next.has(eid)) next.delete(eid); else next.add(eid)
            return next
          })
          const toggleAllGenVisible = () => setGenSelected(prev => {
            const next = new Set(prev)
            if (allGenVisibleSelected) visibleGenIds.forEach(id => next.delete(id))
            else visibleGenIds.forEach(id => next.add(id))
            return next
          })
          const genIsSubset = genPicker !== null && genSelected.size < genPicker.length

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
                      <button
                        className="sub-primary-btn"
                        onClick={handleStartPipeline}
                        disabled={genPicker !== null && genSelected.size === 0}
                      >
                        {hasSubmission ? '▶ Resume Generation' : '▶ Generate Submission'}
                        {genIsSubset ? ` (${genSelected.size} selected)` : ''}
                      </button>
                      {hasSubmission && pipelineStatus?.finished_at && (
                        <span className="sub-last-run">Last completed run: {pipelineStatus.finished_at}</span>
                      )}
                    </div>

                    {genPicker !== null && (
                      <details className="sub-advanced sub-gen-picker">
                        <summary>
                          Select emails to process — {genSelected.size}/{genPicker.length} selected
                        </summary>
                        <div className="sub-gen-picker-body">
                          <div className="sub-export-tools">
                            <input
                              className="sub-diff-filter"
                              placeholder="Filter by id, subject or sender…"
                              value={genSearch}
                              onChange={(e) => setGenSearch(e.target.value)}
                            />
                            <span className="sub-export-count">
                              <strong>{genSelected.size}</strong> of {genPicker.length} selected
                              {genFiltered.length !== genPicker.length && ` · ${genFiltered.length} shown`}
                            </span>
                          </div>

                          <div className="diff-table-wrap sub-export-wrap">
                            <table className="diff-table sub-export-table">
                              <thead>
                                <tr>
                                  <th className="check-col">
                                    <input
                                      type="checkbox"
                                      checked={allGenVisibleSelected}
                                      ref={el => { if (el) el.indeterminate = !allGenVisibleSelected && someGenVisibleSelected }}
                                      onChange={toggleAllGenVisible}
                                      title="Select all shown"
                                    />
                                  </th>
                                  <th>Email</th>
                                  <th>Subject</th>
                                  <th>From</th>
                                  <th>Att.</th>
                                  <th>Current result</th>
                                </tr>
                              </thead>
                              <tbody>
                                {shownGenRows.map(i => {
                                  const m = emailStatusMeta(emailStatusMap[i.email_id])
                                  return (
                                    <tr
                                      key={i.email_id}
                                      className={genSelected.has(i.email_id) ? '' : 'row-excluded'}
                                      onClick={() => toggleGenEmail(i.email_id)}
                                    >
                                      <td className="check-col" onClick={e => e.stopPropagation()}>
                                        <input
                                          type="checkbox"
                                          checked={genSelected.has(i.email_id)}
                                          onChange={() => toggleGenEmail(i.email_id)}
                                        />
                                      </td>
                                      <td className="mono">{i.email_id}</td>
                                      <td>{i.subject || '—'}</td>
                                      <td>{i.from || '—'}</td>
                                      <td>{i.attachments_count ?? '—'}</td>
                                      <td>
                                        <span className="ep-status" style={{ color: m.color, background: m.bg }}>
                                          {m.label}
                                        </span>
                                      </td>
                                    </tr>
                                  )
                                })}
                              </tbody>
                            </table>
                            {genFiltered.length === 0 && (
                              <p className="diff-more">No emails match “{genSearch}”.</p>
                            )}
                            {genFiltered.length > 100 && (
                              <button className="diff-toggle" onClick={() => setGenShowAll(s => !s)}>
                                {genShowAll ? '▲ Show less' : `▼ Show all ${genFiltered.length} emails`}
                              </button>
                            )}
                          </div>

                          <div className="sub-export-foot">
                            <span className="sub-export-count">
                              Unticked emails are skipped — records already in submission.json are never removed.
                            </span>
                            <div className="sub-export-actions">
                              <button
                                className="sub-ghost-btn"
                                onClick={() => setGenSelected(new Set(genPicker.map(i => i.email_id)))}
                              >
                                Select all
                              </button>
                              <button className="sub-ghost-btn" onClick={() => setGenSelected(new Set())}>
                                Clear
                              </button>
                            </div>
                          </div>
                        </div>
                      </details>
                    )}

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

        {/* ========================================================== */}
        {/* VIEW: STRESS LAB — edge-case dataset + batch stress runner  */}
        {/* ========================================================== */}
        {view === 'stress' && (() => {
          const fmtPct = v => (v == null ? '—' : `${(v * 100).toFixed(1)}%`)
          const stressPct =
            stressStatus && stressStatus.total > 0
              ? Math.round((stressStatus.processed / stressStatus.total) * 100)
              : 0
          const typeEntries = Object.entries(stressDataset?.by_test_type || {})
          const m = stressMetrics
          const kpiMeter = v => (
            <div className="kpi-meter"><div style={{ width: `${Math.min(100, Math.max(0, (v ?? 0) * 100))}%` }} /></div>
          )
          const filteredFailures = stressFailFilter
            ? stressFailures.filter(f =>
                f.email_id.toLowerCase().includes(stressFailFilter.toLowerCase()) ||
                f.test_type.toLowerCase().includes(stressFailFilter.toLowerCase()))
            : stressFailures
          const shownFailures = stressShowAllFailures ? filteredFailures : filteredFailures.slice(0, 15)
          const verdictPill = (ok) =>
            ok == null ? null : (
              <span className="ep-status" style={ok
                ? { color: '#2e7d32', background: 'rgba(52,168,83,0.12)' }
                : { color: '#c62828', background: 'rgba(198,40,40,0.10)' }}>
                {ok ? '✓ PASS' : '✗ FAIL'}
              </span>
            )

          return (
            <div className="card-container">
              <div className="page-header">
                <h1>Stress Lab</h1>
                <p>Edge-case playground — run the pipeline over the 2,000-email stress dataset and probe individual cases.</p>
              </div>

              {/* DATASET OVERVIEW */}
              <div className="item-card">
                <div className="sub-card-head">
                  <div>
                    <h3>Stress dataset</h3>
                    <p className="step-sub">
                      Synthetic edge cases in <code>tests/stress_dataset/</code> — regenerate with
                      <code> python tests/generate_stress_dataset.py</code>.
                    </p>
                  </div>
                  <span className={`sub-file-pill ${stressDataset?.exists ? 'has-file' : ''}`}>
                    {stressDataset?.exists ? `📁 ${stressDataset.email_count} emails` : '📁 dataset missing'}
                  </span>
                </div>
                {stressDataset && !stressDataset.exists && (
                  <div className="sub-warn">
                    ⚠ No dataset found at <code>{stressDataset.dataset_dir}</code> — run the generator script first.
                  </div>
                )}
                {stressDataset?.exists && (
                  <>
                    <div className="sub-export-chips" style={{ marginBottom: '10px' }}>
                      {Object.entries(stressDataset.by_status || {}).map(([k, v]) => (
                        <span key={k} className="chip">{k}: {v}</span>
                      ))}
                    </div>
                    <div className="diff-table-wrap">
                      <table className="diff-table">
                        <thead>
                          <tr><th>Test type</th><th style={{ width: '90px' }}>Cases</th></tr>
                        </thead>
                        <tbody>
                          {typeEntries.map(([tt, n]) => (
                            <tr key={tt}>
                              <td className="mono">{tt}</td>
                              <td>{n}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </>
                )}
              </div>

              {/* RUN CONTROLS */}
              <div className="item-card">
                <div className="sub-card-head">
                  <div>
                    <h3>Run stress test</h3>
                    <p className="step-sub">
                      Runs the full pipeline (classify → edge-case checks → extract → compare) over
                      every case and scores the verdicts against ground truth. Fully local — no LLM
                      calls for text attachments.
                    </p>
                  </div>
                </div>

                {!stressRunning && (
                  <>
                    <div className="sub-cta-row">
                      <button className="sub-primary-btn" onClick={handleStartStress}
                        disabled={!stressDataset?.exists}>
                        ▶ Run stress test
                      </button>
                      {stressStatus?.finished_at && (
                        <span className="sub-last-run">Last run: {stressStatus.finished_at}</span>
                      )}
                    </div>
                    <details className="sub-advanced">
                      <summary>Advanced options</summary>
                      <div className="sub-advanced-body">
                        <div className="sub-option">
                          <label htmlFor="stress-limit">Limit</label>
                          <input id="stress-limit" type="number" min="0" value={stressLimit}
                            onChange={(e) => setStressLimit(e.target.value)} />
                          <span>emails (0 = all)</span>
                        </div>
                        <div className="sub-option">
                          <label htmlFor="stress-workers">Workers</label>
                          <input id="stress-workers" type="number" min="1" max="32" value={stressWorkers}
                            onChange={(e) => setStressWorkers(e.target.value)} />
                          <span>parallel (1–32)</span>
                        </div>
                      </div>
                    </details>
                  </>
                )}

                {stressRunning && (
                  <div className="sub-progress-panel">
                    <div className="sub-progress-meta">
                      <span className="sub-progress-current">
                        <span className="sub-pulse" />
                        Processing stress cases…
                      </span>
                      <strong>{stressPct}%</strong>
                    </div>
                    <div className="progress-track">
                      <div className="progress-fill animated" style={{ width: `${stressPct}%` }} />
                    </div>
                    <div className="sub-progress-foot">
                      <span>{stressStatus?.processed ?? 0} / {stressStatus?.total ?? 0} cases</span>
                      <button onClick={handleCancelStress} className="sub-cancel-btn">⏹ Cancel run</button>
                    </div>
                  </div>
                )}

                {stressMsg && !stressRunning && <div className="sub-msg">{stressMsg}</div>}
                {stressStatus?.error && !stressRunning && stressStatus.error !== 'cancelled' && (
                  <div className="sub-error">Error: {stressStatus.error}</div>
                )}
              </div>

              {/* RESULTS */}
              {m && (
                <div className="item-card">
                  <div className="sub-card-head">
                    <div>
                      <h3>Results</h3>
                      <p className="step-sub">
                        {m.processed}/{m.total} cases in {m.elapsed_seconds}s ·
                        ~{m.throughput_eps} emails/sec{m.crashes ? ` · ⚠ ${m.crashes} pipeline crashes` : ''}
                      </p>
                    </div>
                    <button onClick={fetchStressResults} className="sub-ghost-btn">↻ Refresh</button>
                  </div>

                  <div className="kpi-grid" style={{ marginTop: '4px' }}>
                    <div className="kpi-card">
                      <div className="kpi-value">{fmtPct(m.exact_verdict_accuracy)}</div>
                      <div className="kpi-label">Exact Verdict</div>
                      {kpiMeter(m.exact_verdict_accuracy)}
                      <div className="kpi-sub">category + status + reason + fields all match</div>
                    </div>
                    <div className="kpi-card">
                      <div className="kpi-value">{fmtPct(m.catch_rate)}</div>
                      <div className="kpi-label">Catch Rate</div>
                      {kpiMeter(m.catch_rate)}
                      <div className="kpi-sub">{m.flagged_total} cases that should be flagged</div>
                    </div>
                    <div className="kpi-card">
                      <div className="kpi-value">{fmtPct(m.clean_accuracy)}</div>
                      <div className="kpi-label">Clean Pass Rate</div>
                      {kpiMeter(m.clean_accuracy)}
                      <div className="kpi-sub">
                        {m.clean_total} expected-OK · {fmtPct(m.false_alarm_rate)} false alarms
                      </div>
                    </div>
                    <div className="kpi-card">
                      <div className="kpi-value">{fmtPct(m.field_f1)}</div>
                      <div className="kpi-label">Defect Field F1</div>
                      {kpiMeter(m.field_f1)}
                      <div className="kpi-sub">
                        P {fmtPct(m.field_precision)} · R {fmtPct(m.field_recall)}
                      </div>
                    </div>
                    <div className="kpi-card">
                      <div className="kpi-value">{fmtPct(m.category_accuracy)}</div>
                      <div className="kpi-label">Category Accuracy</div>
                      {kpiMeter(m.category_accuracy)}
                      <div className="kpi-sub">status accuracy {fmtPct(m.status_accuracy)}</div>
                    </div>
                    <div className="kpi-card">
                      <div className="kpi-value">{fmtPct(m.defect_recall)}</div>
                      <div className="kpi-label">Mismatch Recall</div>
                      {kpiMeter(m.defect_recall)}
                      <div className="kpi-sub">review recall {fmtPct(m.review_recall)}</div>
                    </div>
                  </div>

                  {/* Per-test-type breakdown */}
                  {m.per_test_type && (
                    <div className="sub-diffs" style={{ marginTop: '18px' }}>
                      <div className="sub-diffs-head">
                        <h4>Breakdown by test type</h4>
                      </div>
                      <div className="diff-table-wrap">
                        <table className="diff-table">
                          <thead>
                            <tr>
                              <th>Test type</th>
                              <th>Expected</th>
                              <th>Cases</th>
                              <th>Exact</th>
                              <th>Missed</th>
                              <th>Accuracy</th>
                            </tr>
                          </thead>
                          <tbody>
                            {Object.entries(m.per_test_type).map(([tt, b]) => (
                              <tr key={tt} className={b.missed ? 'stress-row-miss' : ''}>
                                <td className="mono">{tt}</td>
                                <td>{b.gt_status}</td>
                                <td>{b.total}</td>
                                <td>{b.exact}</td>
                                <td>{b.missed || ''}</td>
                                <td>{fmtPct(b.total ? b.exact / b.total : 0)}</td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    </div>
                  )}

                  {/* Failures */}
                  {stressFailures.length > 0 && (
                    <div className="sub-diffs">
                      <div className="sub-diffs-head">
                        <h4>
                          Failures
                          <span className="sub-diffs-count">
                            {stressFailFilter ? `${filteredFailures.length} of ${stressFailures.length}` : stressFailures.length}
                          </span>
                        </h4>
                        <input
                          className="sub-diff-filter"
                          placeholder="Filter by id or test type…"
                          value={stressFailFilter}
                          onChange={(e) => setStressFailFilter(e.target.value)}
                        />
                      </div>
                      <div className="diff-table-wrap">
                        <table className="diff-table">
                          <thead>
                            <tr>
                              <th>Email</th>
                              <th>Test type</th>
                              <th>Status (expected → predicted)</th>
                              <th>Differing fields</th>
                            </tr>
                          </thead>
                          <tbody>
                            {shownFailures.map(f => (
                              <tr key={f.email_id}>
                                <td className="mono">{f.email_id}</td>
                                <td className="mono">{f.test_type}</td>
                                <td>
                                  <span className="truth-val">
                                    {f.expected?.status}{f.expected?.review_reason ? `/${f.expected.review_reason}` : ''}
                                  </span>
                                  <span className="arrow">→</span>
                                  <span className="sub-val bad">
                                    {f.predicted?.status || '—'}{f.predicted?.review_reason ? `/${f.predicted.review_reason}` : ''}
                                  </span>
                                  {f.error && <div className="sub-error" style={{ marginTop: '4px' }}>{f.error}</div>}
                                </td>
                                <td>
                                  <div className="diff-chips">
                                    {(f.diffs || []).map(d => <span key={d} className="diff-field-chip">{d}</span>)}
                                  </div>
                                </td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                        {filteredFailures.length > 15 && (
                          <button className="diff-toggle" onClick={() => setStressShowAllFailures(s => !s)}>
                            {stressShowAllFailures ? '▲ Show less' : `▼ Show all ${filteredFailures.length} failures`}
                          </button>
                        )}
                      </div>
                    </div>
                  )}
                </div>
              )}

              {/* SINGLE CASE TESTER */}
              <div className="item-card">
                <div className="sub-card-head">
                  <div>
                    <h3>Test a single edge case</h3>
                    <p className="step-sub">
                      Pick a case from the stress dataset and run it through the pipeline —
                      compares the verdict against ground truth.
                    </p>
                  </div>
                </div>

                <div className="sub-export-tools">
                  <input
                    className="sub-diff-filter"
                    placeholder="Search email id…"
                    value={stressCaseSearch}
                    onChange={(e) => setStressCaseSearch(e.target.value)}
                  />
                  <select
                    className="sub-diff-filter stress-type-select"
                    value={stressCaseType}
                    onChange={(e) => setStressCaseType(e.target.value)}
                  >
                    <option value="">All test types</option>
                    {typeEntries.map(([tt]) => <option key={tt} value={tt}>{tt}</option>)}
                  </select>
                </div>

                <div className="stress-case-row">
                  <select
                    className="sub-diff-filter stress-case-select"
                    value={stressCaseId}
                    onChange={(e) => setStressCaseId(e.target.value)}
                    size={Math.min(8, Math.max(3, stressCases.length))}
                  >
                    {stressCases.map(c => (
                      <option key={c.email_id} value={c.email_id}>
                        {c.email_id} — {c.test_type} → {c.status}{c.review_reason ? `/${c.review_reason}` : ''}
                      </option>
                    ))}
                  </select>
                  <button
                    className="sub-primary-btn"
                    disabled={!stressCaseId || stressCaseLoading}
                    onClick={() => handleRunStressCase(stressCaseId)}
                  >
                    {stressCaseLoading ? 'Running…' : '▶ Run case'}
                  </button>
                </div>
                {stressCases.length === 0 && (
                  <p className="diff-more">No cases match the current filters.</p>
                )}

                {stressCaseResult && (
                  <div className="stress-one-result">
                    <div className="stress-one-head">
                      <span className="mono">{stressCaseResult.email_id}</span>
                      {verdictPill(stressCaseResult.match)}
                    </div>
                    {stressCaseResult.error && (
                      <div className="sub-error">Pipeline error: {stressCaseResult.error}</div>
                    )}
                    <div className="diff-table-wrap">
                      <table className="diff-table">
                        <thead>
                          <tr><th></th><th>Expected</th><th>Predicted</th></tr>
                        </thead>
                        <tbody>
                          {['category', 'status', 'review_reason'].map(k => (
                            <tr key={k}>
                              <td className="mono">{k}</td>
                              <td className="truth-val">{stressCaseResult.expected?.[k] ?? '—'}</td>
                              <td className={`sub-val ${stressCaseResult.diffs?.includes(k) ? 'bad' : ''}`}>
                                {stressCaseResult.predicted?.[k] ?? '—'}
                              </td>
                            </tr>
                          ))}
                          <tr>
                            <td className="mono">defect_fields</td>
                            <td className="truth-val">{(stressCaseResult.expected?.defect_fields || []).join(', ') || '—'}</td>
                            <td className={`sub-val ${stressCaseResult.diffs?.includes('defect_fields') ? 'bad' : ''}`}>
                              {(stressCaseResult.predicted?.defect_fields || []).join(', ') || '—'}
                            </td>
                          </tr>
                        </tbody>
                      </table>
                    </div>
                    {(stressCaseResult.si_fields && Object.keys(stressCaseResult.si_fields).length > 0) && (
                      <details style={{ marginTop: '10px', fontSize: '0.82rem' }}>
                        <summary style={{ cursor: 'pointer', opacity: 0.7 }}>Extracted fields</summary>
                        <pre style={{ marginTop: '8px', background: '#faf8f5', padding: '12px', borderRadius: '8px', overflowX: 'auto' }}>
                          {JSON.stringify({ si: stressCaseResult.si_fields, bl: stressCaseResult.bl_fields }, null, 2)}
                        </pre>
                      </details>
                    )}
                  </div>
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

      {/* Auto-prompt: a required attachment is missing — offer to draft the chaser */}
      {missingPrompt && (
        <div
          className="modal-overlay"
          onClick={() => {
            missingPromptDismissed.current.add(missingPrompt.emailId)
            setMissingPrompt(null)
          }}
        >
          <div
            className="modal-container"
            style={{ maxWidth: '460px' }}
            onClick={e => e.stopPropagation()}
          >
            <div className="modal-header">
              <h3>
                📎 {missingPrompt.doc === 'si' ? 'Shipping Instruction missing'
                  : missingPrompt.doc === 'bl' ? 'Draft BL missing'
                  : 'No documents attached'}
              </h3>
              <button
                className="modal-close-btn"
                onClick={() => {
                  missingPromptDismissed.current.add(missingPrompt.emailId)
                  setMissingPrompt(null)
                }}
              >&times;</button>
            </div>
            <div className="modal-body">
              <p style={{ fontSize: '0.92rem', color: '#444' }}>
                {missingPrompt.doc === 'si'
                  ? `The sender asked for a BL comparison but forgot to attach the Shipping Instruction — draft a request to them?`
                  : `The carrier hasn't issued the draft Bill of Lading for this shipment — draft a chaser email?`}
              </p>
              <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '10px', marginTop: '18px' }}>
                <button
                  onClick={() => {
                    missingPromptDismissed.current.add(missingPrompt.emailId)
                    setMissingPrompt(null)
                  }}
                  style={{ background: 'transparent', border: '1px solid var(--border-color)', color: 'var(--text-color)', boxShadow: 'none', padding: '8px 16px', fontSize: '0.88rem' }}
                >
                  Not now
                </button>
                <button
                  onClick={() => {
                    const { emailId, doc } = missingPrompt
                    missingPromptDismissed.current.add(emailId)
                    setMissingPrompt(null)
                    openDraftEmail(emailId, null, null, doc)
                  }}
                  style={{ background: '#e07a5f', padding: '8px 18px', fontSize: '0.88rem', fontWeight: 600 }}
                >
                  ✉️ Draft Email
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

export default App
