# ClusterTrace

Deterministic plastic collection chain-of-custody ledger with mass-balance validation, cryptographic evidence hashing, custody handovers, explainable discrepancy detection, and exportable supporting audit packs.

Built for the Sankalp competition demonstration.

---

## Quick Start

ClusterTrace requires Python 3.10+ and uses the lightweight, standard `Pillow` library for secure content-based image verification and local synthetic demo graphic generation.

```bash
pip install Pillow
python server.py
```

Then open your browser at **http://localhost:8000**.

### Running the Automated Test Suite

Run the focused unit and HTTP integration tests:

```bash
python -m unittest test_clustertrace.py -v
```

All tests execute in under 1 second.

---

## 60-Second Guided Sankalp Demo Story

At the top of the interface, judges and users are greeted by a guided **60-Second Demo Story** showcasing how ClusterTrace stops phantom recycling claims:

1. **Stage 1 (Evidence & Collection)**: Pickers P-101 (200.000 kg) and P-102 (140.000 kg) record raw PET collection lots with synthetic demo evidence images, SHA-256 digests, and browser geolocation coordinates.
2. **Stage 2 (Aggregation)**: The Central Aggregation Hub merges the two lots into a sealed 340.000 kg PET batch, accompanied by self-entered custody handovers.
3. **Stage 3 (Deterministic Mass Balance)**: A recycling facility submits an unauthorized claim for 500.000 kg of recycled output. ClusterTrace deterministically blocks the claim, logging a **+160.000 kg Mass Discrepancy** in the permanent event trail.
4. **Stage 4 (Auditor Review Queue)**: The flagged discrepancy is routed to the auditor queue. An auditor reviews the mathematical calculation and logs a review decision (`NEEDS_FIELD_CHECK`) with a mandatory audit reason.

Click **"▶ Play 60-Second Demo Story"** to watch the story advance automatically or inspect each stage interactively.

---

## Five Core Features

### 1. Actual Evidence Capture
- Uploads collection and handover photos from desktop or mobile (`.jpg`, `.jpeg`, `.png`, `.webp` up to 5 MB).
- **Content-Based Validation**: Uploads are verified by inspecting image byte headers with `Pillow`. Disguised text, malformed files, and truncated streams are strictly blocked, regardless of claimed file extensions or MIME headers.
- Files are securely stored locally in `uploads/` (outside Git, ignored by `.gitignore`), with sanitized unique filenames and path traversal protection.
- Computes cryptographic SHA-256 digests linked directly to the event and batch audit pack.
- Supports optional browser geolocation (`navigator.geolocation`) showing capture accuracy (e.g. `±12.5m`) and timestamp after explicit user consent.
- All evidence is transparently labeled as **submitted evidence (unverified)**; synthetic demo graphics are explicitly marked as **synthetic demo evidence (depicting no real collection event)** in the UI, metadata, and audit pack exports.

### 2. Chain-of-Custody Handovers
- Records transfers between supply chain actors:
  - **Picker → Aggregator** (inward collection)
  - **Aggregator → Recycler** (consignment dispatch)
- Reconciles handover claimed weight against source lots or batches:
  - Detects and flags moisture losses, tare variances, or unexpected gains.
  - **Never silently alters the original collection lot weight.**
- Records receiver acknowledgement lifecycle (`PENDING` → `ACKNOWLEDGED` or `DISPUTED`). All acknowledgements are explicitly labeled as **self-entered demo receipts**; they do not use digital signatures or independently authenticated receivers.

### 3. Explainable Discrepancy Detection
Deterministic mathematical and cryptographic rules analyze all records and produce human-readable findings:
- **`MASS_BALANCE_EXCEEDED`** (High Severity, Hard Block): Output claims exceeding recorded input are blocked.
- **`DUPLICATE_EVIDENCE_HASH`** (High Severity, Review Flag): Detects if an identical photo SHA-256 hash was submitted across multiple collection events.
- **`HANDOVER_WEIGHT_DIVERGENCE`** (Medium Severity, Review Flag): Flags transfer weight divergences > 2%.
- **`MISSING_EVIDENCE`** (Low Severity, Review Flag): Highlights records lacking photographic references.
- Every finding details the rule name, input parameters, exact mathematical calculation, affected lot/batch ID, and recommended human check.
- Transparently disclosed: **Deterministic rule engine; not an AI system.**

### 4. Auditor Review Queue
- Flagged batches and discrepancies are gathered into an audit case queue.
- Auditors can record formal review decisions: `OPEN`, `NEEDS_FIELD_CHECK`, `RESOLVED`, or `REJECTED`.
- Enforces a mandatory written finding rationale and self-entered reviewer name.
- Review decisions are appended as `AUDIT_REVIEW_RECORDED` events in the event trail; previous claims and discrepancy findings are strictly preserved.

### 5. Picker Income & Material-Flow Insights
- **Waste Picker Economics**: Calculates total collected weight and **reported transaction value** in Indian Rupees (₹ INR) based on recorded collection weights and field rates (e.g. ₹16/kg).
  - Explicitly labeled: *Reported transaction value reflects claimed collection rates; unconfirmed payment (no banking or UPI integration).*
- **Material-Flow Reconciliation**: Compares total recorded input, reported recycler output, flagged discrepancy mass, and reviewed quantities across polymer types (PET, HDPE, PP, LDPE).
  - Explicitly labeled: *Internal mass-balance ledger tallies; not officially certified recycling figures or carbon credits.*

---

## Honest Product Claims & System Boundaries

| Dimension | Implemented (Working) | Simulated (Demo Mode) | Future Integration |
| :--- | :--- | :--- | :--- |
| **Mass Balance** | Deterministic Decimal math; output > input strictly blocked. | — | Automated continuous scale telemetry. |
| **Evidence & Photos** | File upload, safe storage, SHA-256 hashing, browser GPS. | Coordinates & photos are submitted claims (unverified). | Hardware-signed camera tokens, automated EXIF tamper analysis. |
| **Custody Handovers** | Weight reconciliation against source lots; loss/gain flags. | Single-tenant UI; receiver acknowledgement is manual demo action. | Digital signatures / SMS OTP handshakes between actors. |
| **Workflow Roles** | 4 operational perspectives (Picker, Aggregator, Recycler, Brand). | No user login, passwords, or role-based access control. | Multi-tenant auth, OAuth / Single Sign-On. |
| **Discrepancy Engine** | Deterministic rule calculations, inputs, human checks. | — | Historical pattern baselines, seasonal yield calibration. |
| **Auditor Review** | Case queue, mandatory audit reason, persistent review events. | Reviewer name is self-entered demo input. | Formal third-party auditor credentialing & digital sign-off. |
| **Picker Economics** | Weight x rate calculation, reported transaction value (INR). | Unconfirmed payment; no UPI or banking integration. | Direct UPI / bank transfer disbursement rails. |
| **Compliance Pack** | Supporting Traceability Audit Pack (JSON export). | — | Government portal API integration (e.g., CPCB EPR portal). |

> [!IMPORTANT]
> **Regulatory Boundary**: ClusterTrace generates a **Supporting Traceability Audit Pack** for voluntary chain-of-custody verification. It is **NOT an official Extended Producer Responsibility (EPR) certificate**.
> **No Exaggerated Tech Claims**: ClusterTrace does **NOT** use blockchain, does **NOT** issue regulatory carbon credits, and does **NOT** claim automated AI fraud detection.

---

## Data Architecture & Security

- **Persistence**: Atomic file replacement (`clustertrace.json.tmp` → `clustertrace.json`) protected by re-entrant locks (`threading.RLock`).
- **File Safety**: Path traversal attacks (e.g. `/uploads/../../server.py`) are strictly validated and blocked. File uploads are capped at 5 MB and restricted to valid image types.
- **HTML Escaping**: All user-entered data rendered in the DOM is sanitized against script injection (`safe()`).
- **Data Migration**: Existing databases automatically migrate to include new schema keys (`handovers`, `reviews`, `evidence`) without data loss.
