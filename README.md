# ClusterTrace

Deterministic plastic collection chain-of-custody ledger with mass-balance validation, batch lineage drill-down, and exportable supporting audit packs.

---

## Quick Start

ClusterTrace has **zero third-party dependencies** and runs on Python 3.10+ standard library.

```bash
python server.py
```

Then open your browser at **http://localhost:8000**.

### Running Automated Tests

Run the comprehensive unit and integration test suite:

```bash
python -m unittest test_clustertrace.py -v
```

The test suite covers:
- Deterministic mass-balance validation (valid outputs vs. excess claim rejections).
- Duplicate lot assignment prevention (a lot cannot be assigned to multiple batches).
- Mixed material prevention (lots of different polymer types cannot be combined into the same batch).
- Duplicate lot IDs in single requests.
- Input validation (positive weights, maximum 3 decimal places).
- Audit pack lineage verification and honest EPR disclaimer checks.
- End-to-end HTTP integration testing and file persistence (verifying `OUTPUT_REJECTED` events persist to disk across reloads).

---

## Sankalp Demo Walkthrough

ClusterTrace is designed for live demonstrations illustrating how deterministic mass balance stops phantom recycling claims.

### 1. Initialize the 340 kg Demo Scenario
Click **"▶ Run 340 kg Fraud Demo"** (or **"+ Create 340 kg Demo Scenario"**).
This safely creates a fresh, clearly labeled scenario without deleting any existing data:
- **Lot 1**: 200.000 kg PET (Picker P-101, Kolkata Ward 12)
- **Lot 2**: 140.000 kg PET (Picker P-102, Kolkata Ward 14)
- **Batch**: 340.000 kg PET input assembled from Lots 1 & 2.

### 2. Test the 500 kg Fraud Claim
In the **Recycler Workflow**, click **"⚠️ Claim 500 kg (Fraud Test)"** and submit:
- ClusterTrace strictly rejects the claim:
  ```
  HTTP 422 Unprocessable Content
  Mass-balance violation: Output claim (500.000 kg) exceeds recorded input (340.000 kg) by 160.000 kg
  ```
- The violation is permanently logged as an `OUTPUT_REJECTED` event in the audit trail, detailing the claimed mass (500 kg), available mass (340 kg), and unauthorized delta (+160 kg).

### 3. Record Legitimate Output
Click **"✅ Claim Valid 90% Yield"** (306.000 kg) and submit:
- Output is verified and confirmed.
- Mass balance check passes, recording yield (90.00%) and processing timestamp.

### 4. Inspect Lineage & Export Audit Pack
Switch to the **Brand & Audit Workflow**:
- Select the batch to inspect the recorded 3-stage chain of custody:
  1. **Collection Lots**: Source picker IDs, self-reported weights, locations, and photo references.
  2. **Aggregator Batch**: Sealed batch ID, material, and total input mass.
  3. **Recycler Output**: Output mass, processing yield, and verification status.
- Click **"Download Audit Pack (JSON)"** to export the structured audit pack.

---

## Workflow Views

The interface provides four operational workflow perspectives (single-tenant demonstration; does not require separate login credentials):

1. **📦 Picker Workflow**: Entry of raw collection lots with self-reported picker ID, polymer type, weight, location, and photo reference.
2. **🏭 Aggregator Workflow**: Inspection of unbatched inventory and assembly into sealed single-material batches with live weight calculation and mixed-material guardrails.
3. **♻️ Recycler Workflow**: Entry of recycled outputs against received batches with real-time mass-balance validation.
4. **🏷️ Brand & Audit Workflow**: Downstream brand buyer and compliance view with 3-stage lineage drill-down and audit pack export.

---

## Honest Product Claims & Boundaries

ClusterTrace adheres to strict integrity principles regarding its claims:

- **Supporting Audit Evidence Only**: All generated packs are labeled as **Supporting Traceability Audit Packs**. ClusterTrace **never** claims to be an official Extended Producer Responsibility (EPR) certificate or regulatory document.
- **Self-Reported Field Inputs**: Field weights, collection locations, and photo references are explicitly recorded as **self-reported claims**. They are not presented as independently verified by IoT scales, GPS hardware, or physical inspectors.
- **Deterministic Mass Balance**: Mass balance strictly verifies mathematical consistency against recorded inputs. It detects internal discrepancies and over-claiming within the chain of custody.

---

## Data Architecture

- Data is persisted locally in `clustertrace.json` using atomic file replacement (`.tmp` write followed by atomic rename).
- Thread-safe operations are enforced via re-entrant locks (`threading.RLock`).
- The test suite uses isolated temporary databases specified via the `CLUSTERTRACE_DB` environment variable.
