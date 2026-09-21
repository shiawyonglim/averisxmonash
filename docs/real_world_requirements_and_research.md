# Real-World Maritime Shipping Documentation: Operational Requirements & Industry Research

## Executive Overview

In global container shipping and trade logistics, maritime documentation verification is a high-stakes, time-sensitive operational bottleneck. Large shared-service centers—such as **Averis**, ocean carriers (**MSC, Maersk, CMA CGM, Hapag-Lloyd, ONE, Evergreen**), and multi-modal freight forwarders—manage thousands of critical shipping communications daily across hundreds of global trade corridors.

A discrepancy between the **Shipping Instruction (SI)** submitted by the exporter/booking party and the carrier's **Draft Bill of Lading (Draft BL)** directly results in severe commercial, operational, and legal consequences:
1. **Cargo Rollover & Vessel Feeder Disconnection**: Failure to finalize documentation prior to the carrier's strict **SI Cutoff Time** and **VGM Cutoff Time** prevents issuance of the customs manifest, triggering container loading holds and rolling cargo to subsequent voyages.
2. **Demurrage & Detention (D&D) Accumulation**: Inability to release original bills or telegraphic transfers (Telex release) at the destination port incurs terminal storage and equipment demurrage fees that accumulate per container per day at transshipment hubs (e.g., Port Klang, PSA Singapore, Port of Tanjung Pelepas, Rotterdam, Los Angeles/Long Beach).
3. **Customs Fines & Regulatory Seizures**: Inaccurate manifest submissions violate advance filing rules—including the **US Customs Automated Manifest System (AMS)** / **ISF 10+2** (19 CFR § 4.7), **EU Import Control System 2 (ICS2)**, and **China Customs Advanced Manifest (CCAM)**—exposing shippers to statutory penalties that can reach thousands of dollars per bill.
4. **Letter of Credit (L/C) Discrepancy Rejections**: Under the International Chamber of Commerce (ICC) **UCP 600**, even slight typographical discrepancies in the legal entity name, notify address, or port description can cause issuing banks to reject documentary collections, delaying trade settlements.

---

## 1. Industry Standards & Legal Frameworks

The following are real, publicly documented industry initiatives and regulations. We intentionally cite no academic papers or invented metrics here — every item below can be verified directly with the issuing body.

### 1.1 International Legal & Digital Frameworks
- **Digital Container Shipping Association (DCSA) eBL Standards**:
  - DCSA publishes open, standardized data models and API definitions for electronic Bills of Lading and Shipping Instructions across ocean carriers — covering the same core trade parties and attributes this system verifies (shipper, consignee, notify party, port of loading/discharge, equipment, cargo weight).
  - DCSA's ocean-carrier members have publicly committed to 100% adoption of electronic bills of lading by 2030, under the cross-industry **FIT Alliance** commitment (BIMCO, DCSA, FIATA, ICC, SWIFT).
- **BIMCO "25by25" eBL Initiative**:
  - Industry campaign targeting 25% of global trade to be issued on electronic bills of lading by 2025 — a real, documented push to move documentation off unstructured email attachments.
- **UNCITRAL Model Law on Electronic Transferable Records (MLETR, 2017)**:
  - International framework enabling legal recognition of digital trade documents equivalent to paper originals.
  - Enacted into national law by key maritime jurisdictions: **Singapore** (*Electronic Transactions (Amendment) Act 2021*) and the **United Kingdom** (*Electronic Trade Documents Act 2023*).
- **UN/CEFACT (United Nations Centre for Trade Facilitation and Electronic Business)**:
  - Maintains the Multi-Modal Transport Reference Data Model (MMT-RDM) and standard UN/EDIFACT message types such as `IFTMIN` (instruction message) and `IFTSTA` (status report), alongside UNECE Recommendation 16 for UN/LOCODE port nomenclature (e.g., `MYPKG` for Port Klang, `SGSIN` for Singapore).
- **IMO SOLAS Convention (Chapter VI, Regulation 2 — Verified Gross Mass / VGM)**:
  - Requires the shipper to declare a container's verified gross mass — obtained either by weighing the packed container (Method 1) or by weighing contents and adding container tare (Method 2) — **before it may be loaded aboard a vessel**. In force since July 2016.
  - Enforcement tolerances for VGM declarations are set by national maritime administrations, which is why this system treats weight variance beyond a configured threshold as a material defect requiring carrier re-issuance.

---

## 2. Operational Throughput & Performance Benchmarks

Maritime operations centers work under strict cut-off windows tied to vessel feeder schedules and customs filing deadlines. The "Reasonable Target" column below states sensible operational goals — it is our own engineering assumption, **not** a cited industry SLA. The "This System" column reports what we **measured** on the organizers' 520-email dataset with the deterministic tier only (`AI_FALLBACK=0`; see `docs/conditions.md` §1 for the full scorecard).

| Operational Dimension | Reasonable Target (assumption) | This System (measured) | Operational Mechanism |
| :--- | :--- | :--- | :--- |
| **Batch Ingestion Velocity** | Minutes, not hours, for a ~500-email queue | Full 520-email inbox processed in seconds on a laptop | High-throughput in-memory token indexing & non-blocking I/O |
| **Per-Document Decision Latency** | Interactive — a few seconds per document pair | Milliseconds per document (Tier-1 deterministic engine) | Compiled regex heuristics & normalized string distance algorithms |
| **Stage-1 Classification Accuracy** | High — misrouted email stalls the queue | **100%** on the organizers' set (measured) | Deterministic precedence cascade, LLM fallback only for unmatched cases |
| **Field-Level Extraction F1** | High — extraction errors become false defects | **0.9859** (P = 1.000, R = 0.972, deterministic tier) | Tier-1 regex extraction; Tier-2 LLM fills only the blanks |
| **Defect Flagging Recall** | Catch every material discrepancy | **46/46** defects flagged; **44/46** exact field-level match | Strict non-regression benchmark across all 7 mandatory trade fields |
| **Defect False Alarm Rate** | Low — false positives erode operator trust | **0** false alarms on the gold set (measured) | Maritime style tolerance engine (suffixes, spacing, punctuation) |
| **Weighted Benchmark Score** | — | **97.54%** = 0.50×0.9565 + 0.30×1.0000 + 0.20×0.9859 (measured) | Official weighted scoring of classification, status, and field accuracy |
| **Escalation Discipline** | Escalate when unsure; never guess | 25 `NEEDS_REVIEW` flags vs 20 gold — 5 honest extra escalations where a field could not be extracted rather than a wrong "OK" claim | Fail-safe design: an unverifiable field escalates instead of fabricating a verdict |
| **Out-of-Distribution Generalization** | Rules alone should not be trusted blindly | **11/12** on our hand-written OOD set, rules only — the one miss (`SI_REQUEST` phrased unlike the training patterns) is exactly what the LLM tier catches | Two-tier architecture: deterministic first, LLM fallback for the residual |
| **Cutoff Velocity Tracking** | Live view of the chaser queue | Live Cutoff Monitor in the UI | Real-time velocity bar, clearance rate, and remaining chaser queue |
| **Audit Trail & Governance** | Reconstructable decision history | Dual-persistence audit log + `verification_details.json` sidecar | Per-field provenance (`rule` / `model` / `missing`), rule evaluation, and user overrides |

---

## 3. The 7 Core Verification Rules & Standard Trade Tolerances

In container shipping, discrepancies must be separated into **material legal defects** (which invalidate customs filing or title transfer) and **standard commercial style variations** (which are universally accepted by ocean carriers and port terminals).

### Rule 1: Shipper & Consignee Entities
- **Material Discrepancy**: Different registered company identity, conflicting entity registration number, or differing legal jurisdiction/country.
- **Accepted Trade Tolerances**:
  - Corporate suffix variations: `SDN BHD` $\leftrightarrow$ `SDN. BHD.`, `PTE LTD` $\leftrightarrow$ `PTE. LTD.`, `LLC` $\leftrightarrow$ `L.L.C.`, `CO., LTD` $\leftrightarrow$ `COMPANY LIMITED`, `S.L.` $\leftrightarrow$ `SL`.
  - Standard street/address notations: `#02-00` vs `LEVEL 2`, commas, semicolons, and hyphenation variations.

### Rule 2: Notify Party
- **Legal Certification**: If marked `"SAME AS CONSIGNEE"` or textually identical to the consignee party, it is legally valid under standard bills of lading.
- **Material Discrepancy**: When an explicit third-party notify entity is declared, any mismatch in legal entity name or overseas agent constitutes a defect requiring customer clarification.

### Rule 3: Port of Loading (POL) & Port of Discharge (POD)
- **Standardization**: Validated against UN/LOCODE standards (UNECE Rec. 16).
- **Accepted Trade Tolerances**:
  - `PORT KLANG (WESTPORT), MALAYSIA (MYPKG)` $\leftrightarrow$ `PORT KLANG` or `MYPKG`.
  - `SINGAPORE, SINGAPORE (SGSIN)` $\leftrightarrow$ `SINGAPORE PORT` or `SGSIN`.
  - `TANJUNG PELEPAS, MALAYSIA (MYTPP)` $\leftrightarrow$ `TANJUNG PELEPAS` or `MYTPP`.
- **Material Discrepancy**: Routing to an entirely different terminal, city, or transshipment hub (e.g., `CALLAO (PECLL)` vs `JEBEL ALI (AEJEA)`).

### Rule 4: Container Equipment Count
- **Material Discrepancy**: Any numeric mismatch between total equipment units declared on the SI and the draft BL.
- **Accepted Trade Tolerances**:
  - Equipment notations: `2 x 40'HC` $\leftrightarrow$ `2 CONTAINERS (40' HIGH CUBE)` $\leftrightarrow$ `40HQ x 2` normalized to integer count `2`.

### Rule 5: Cargo Gross Weight (KG / MT) & SOLAS Tolerance
- **Configurable Tolerance**: Any weight variance is flagged by default; a small tolerance for tare-weight rounding or scale precision can be enabled via `WEIGHT_TOLERANCE_PCT`.
- **Unit Conversions**: Metric Tonnes (`MT`, `MTS`) are normalized at $1\text{ MT} = 1,000\text{ KG}$.
- **Material Discrepancy**: Weight variance beyond the configured tolerance is treated as a material defect — consistent with the SOLAS VGM principle that declared container mass must be accurate — requiring carrier re-issuance.

### Rule 6: Edge-Case Escalation Protocols (`NEEDS_REVIEW`)
- **Missing Draft BL**: When the carrier response lacks the draft BL attachment, the case is routed to the **Chaser Queue** for one-click follow-up dispatch.
- **Wrong Document Type**: Submissions containing packing lists or commercial invoices instead of draft ocean bills are escalated to human review.
- **Unreadable / Corrupted Attachments**: Corrupted files or scanned raster images lacking an OCR text layer are flagged for vision extraction or re-upload.
- **Blank / Incomplete Values**: Missing mandatory fields (e.g., gross weight marked `TBD` or `PENDING`) are automatically held for operator confirmation.

### Rule 7: Discrepancy Precedence Mandate
- If a document has both a formatting warning and a material discrepancy (such as mismatched gross weight or differing consignee), the engine strictly assigns status **`MISMATCH`**. Cargo and regulatory integrity always take precedence over operational informational warnings.
