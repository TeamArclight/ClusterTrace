"""ClusterTrace MVP: collection lineage, chain-of-custody handovers,
explainable discrepancy detection, evidence capture, and deterministic mass balance.
"""
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import email
from email.policy import default as email_default_policy
import hashlib
import io
import json
import os
import threading
import uuid

from PIL import Image, ImageDraw, UnidentifiedImageError

ROOT = Path(__file__).parent
DB_FILE = ROOT / "clustertrace.json"
UPLOADS_DIR = ROOT / "uploads"
LOCK = threading.RLock()

# Ensure uploads directory exists
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)


def get_db_path():
    env_override = os.environ.get("CLUSTERTRACE_DB")
    if env_override:
        return Path(env_override)
    return DB_FILE


def get_uploads_dir():
    env_override = os.environ.get("CLUSTERTRACE_UPLOADS")
    if env_override:
        p = Path(env_override)
        p.mkdir(parents=True, exist_ok=True)
        return p
    return UPLOADS_DIR


def now():
    return datetime.now(timezone.utc).isoformat()


def initial():
    return {
        "lots": [],
        "batches": [],
        "events": [],
        "handovers": [],
        "reviews": [],
        "evidence": []
    }


def read():
    db_path = get_db_path()
    if not db_path.exists():
        return initial()
    try:
        data = json.loads(db_path.read_text(encoding="utf-8"))
    except Exception:
        return initial()
    # Migration safeguard for existing databases
    for key in ("lots", "batches", "events", "handovers", "reviews", "evidence"):
        if key not in data:
            data[key] = []
    return data


def write(data):
    db_path = get_db_path()
    tmp = db_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(db_path)


def kg(value):
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError):
        raise ValueError("Enter a valid weight in kilograms")
    if not amount.is_finite() or amount <= 0 or amount.as_tuple().exponent < -3:
        raise ValueError("Weight must be positive with at most three decimal places")
    return amount


def format_kg(value):
    return f"{Decimal(value):.3f}"


def event(data, kind, **fields):
    evt = {"id": uuid.uuid4().hex[:12], "time": now(), "kind": kind, **fields}
    data["events"].append(evt)
    return evt


# ----------------------------------------------------------------------
# 1. ACTUAL EVIDENCE CAPTURE
# ----------------------------------------------------------------------

MAX_FILE_SIZE = 5 * 1024 * 1024  # 5 MB


def validate_image_bytes(file_bytes):
    if len(file_bytes) > MAX_FILE_SIZE:
        raise ValueError("File exceeds maximum allowed size of 5 MB")
    if len(file_bytes) == 0:
        raise ValueError("File content is empty")
    try:
        bio = io.BytesIO(file_bytes)
        with Image.open(bio) as im:
            fmt = im.format
            if fmt not in ("JPEG", "PNG", "WEBP"):
                raise ValueError(f"Unsupported image format: {fmt}. Only JPEG, PNG, and WebP are allowed.")
            im.verify()
    except (UnidentifiedImageError, ValueError) as exc:
        raise ValueError(f"Uploaded file is not a valid JPEG, PNG, or WebP image: {exc}")
    except Exception as exc:
        raise ValueError(f"Malformed or truncated image file: {exc}")

    canonical_mime = {
        "JPEG": "image/jpeg",
        "PNG": "image/png",
        "WEBP": "image/webp"
    }[fmt]
    canonical_ext = {
        "JPEG": ".jpg",
        "PNG": ".png",
        "WEBP": ".webp"
    }[fmt]
    return fmt, canonical_mime, canonical_ext


def generate_synthetic_demo_image(lot_id, picker_id, weight_kg, location, gps_str):
    width, height = 640, 380
    im = Image.new("RGB", (width, height), color=(246, 249, 246))
    draw = ImageDraw.Draw(im)

    # Outer border
    draw.rectangle([(10, 10), (width - 10, height - 10)], outline=(13, 110, 89), width=3)
    # Header banner
    draw.rectangle([(14, 14), (width - 14, 75)], fill=(225, 240, 233))
    draw.text((30, 24), "SYNTHETIC DEMO EVIDENCE", fill=(13, 110, 89))
    draw.text((30, 48), "ClusterTrace Sankalp Demo · Depicts no real collection event", fill=(70, 90, 85))

    # Content
    draw.text((30, 105), f"Sample Lot: {lot_id} ({picker_id})", fill=(20, 40, 35))
    draw.text((30, 140), "Polymer: PET (Polyethylene Terephthalate)", fill=(40, 60, 55))
    draw.text((30, 175), f"Recorded Weight: {weight_kg} kg (Self-reported field claim)", fill=(13, 110, 89))
    draw.text((30, 210), f"Location: {location}", fill=(50, 70, 65))
    draw.text((30, 245), f"Coordinates: {gps_str} (Unverified browser telemetry)", fill=(90, 110, 105))

    # Bottom watermark bar
    draw.rectangle([(14, height - 80), (width - 14, height - 14)], fill=(255, 235, 232))
    draw.text((30, height - 68), "NOTICE: Illustrative Synthetic Graphic for Demonstration", fill=(179, 38, 30))
    draw.text((30, height - 44), "Not a photograph of physical plastic waste or real recycling operations.", fill=(120, 50, 45))

    bio = io.BytesIO()
    im.save(bio, format="JPEG", quality=90)
    return bio.getvalue()


def save_evidence(data, file_bytes, original_filename, content_type=None, geolocation=None, is_synthetic_demo=False):
    fmt, canonical_mime, canonical_ext = validate_image_bytes(file_bytes)
    sha256_hash = hashlib.sha256(file_bytes).hexdigest()
    safe_stem = "".join(c for c in Path(original_filename).stem if c.isalnum() or c in "-_")[:24] or "photo"
    filename = f"{uuid.uuid4().hex[:12]}_{safe_stem}{canonical_ext}"
    target_path = get_uploads_dir() / filename
    target_path.write_bytes(file_bytes)

    geo_data = None
    if geolocation and isinstance(geolocation, dict):
        lat = geolocation.get("latitude")
        lng = geolocation.get("longitude")
        if lat is not None and lng is not None:
            geo_data = {
                "latitude": float(lat),
                "longitude": float(lng),
                "accuracy_m": float(geolocation.get("accuracy_m", 0.0)),
                "captured_at": geolocation.get("captured_at") or now(),
                "disclaimer": "Browser-submitted coordinate; unverified GPS telemetry"
            }

    ev_item = {
        "id": f"ev_{uuid.uuid4().hex[:10]}",
        "filename": filename,
        "original_name": original_filename,
        "url": f"/uploads/{filename}",
        "sha256": sha256_hash,
        "size_bytes": len(file_bytes),
        "mime_type": canonical_mime,
        "created_at": now(),
        "geolocation": geo_data,
        "is_synthetic_demo": is_synthetic_demo,
        "synthetic_disclaimer": "Synthetic demo evidence. Depicts no real collection event." if is_synthetic_demo else None,
        "verification_status": "Synthetic demo evidence (unverified)" if is_synthetic_demo else "Submitted evidence (unverified)"
    }
    data["evidence"].append(ev_item)
    event(data, "EVIDENCE_UPLOADED", evidence_id=ev_item["id"], sha256=sha256_hash,
          original_name=original_filename, is_synthetic=is_synthetic_demo)
    return ev_item


# ----------------------------------------------------------------------
# 2. CORE LOTS & BATCHES
# ----------------------------------------------------------------------

def lot(data, payload):
    picker = str(payload.get("picker", "")).strip()
    material = str(payload.get("material", "")).strip().upper()
    location = str(payload.get("location", "")).strip()
    if not all((picker, material, location)):
        raise ValueError("Picker ID, material and location are required")
    weight = kg(payload.get("weight"))
    weight_str = format_kg(weight)
    photo_ref = str(payload.get("photo_ref", "")).strip()
    evidence_id = payload.get("evidence_id")
    if evidence_id and not any(e["id"] == evidence_id for e in data["evidence"]):
        raise ValueError("Referenced evidence ID not found")

    # Optional price per kg for picker economics
    price_val = payload.get("price_per_kg")
    price_per_kg = format_kg(price_val) if price_val is not None and str(price_val).strip() else "15.000"

    notes = str(payload.get("notes", "")).strip()
    item = {
        "id": uuid.uuid4().hex[:12],
        "picker": picker,
        "material": material,
        "location": location,
        "photo_ref": photo_ref,
        "evidence_id": evidence_id,
        "notes": notes,
        "weight_kg": weight_str,
        "price_per_kg_inr": price_per_kg,
        "created_at": now(),
        "batch_id": None,
        "verification_status": "Self-reported field claim (unverified)"
    }
    data["lots"].append(item)
    event(data, "LOT_RECORDED", lot_id=item["id"], weight_kg=weight_str, material=material, picker=picker)
    return item


def batch(data, payload):
    ids = payload.get("lot_ids")
    if not isinstance(ids, list) or not ids or len(ids) != len(set(ids)):
        raise ValueError("Select one or more distinct lots")
    selected = [item for item in data["lots"] if item["id"] in ids]
    if len(selected) != len(ids):
        raise ValueError("One or more specified lots do not exist")
    if any(item["batch_id"] for item in selected):
        raise ValueError("Every lot must be unbatched; duplicate lot assignment is strictly rejected")
    materials = {item["material"] for item in selected}
    if len(materials) != 1:
        raise ValueError("A batch must contain exactly one material type; mixed materials are rejected")

    total_input = sum((Decimal(x["weight_kg"]) for x in selected), Decimal(0))
    input_str = format_kg(total_input)
    item = {
        "id": uuid.uuid4().hex[:12],
        "lot_ids": ids,
        "material": selected[0]["material"],
        "input_kg": input_str,
        "output_kg": None,
        "created_at": now(),
        "review_status": "NONE"
    }
    data["batches"].append(item)
    for source in selected:
        source["batch_id"] = item["id"]
    event(data, "BATCH_CREATED", batch_id=item["id"], lot_ids=ids, input_kg=item["input_kg"], material=item["material"])
    return item


def output(data, payload):
    batch_id = payload.get("batch_id")
    item = next((b for b in data["batches"] if b["id"] == batch_id), None)
    if item is None:
        raise ValueError("Batch not found")
    amount = kg(payload.get("output_kg"))
    if item["output_kg"] is not None:
        raise ValueError("Output is already recorded for this batch")

    input_amount = Decimal(item["input_kg"])
    amount_str = format_kg(amount)
    input_str = format_kg(input_amount)
    if amount > input_amount:
        delta = amount - input_amount
        delta_str = format_kg(delta)
        reason = f"Output claim ({amount_str} kg) exceeds recorded input ({input_str} kg) by {delta_str} kg"
        event(data, "OUTPUT_REJECTED",
              batch_id=item["id"],
              claimed_kg=amount_str,
              available_kg=input_str,
              excess_kg=delta_str,
              reason=reason)
        return None, f"Mass-balance violation: {reason}"

    item["output_kg"] = amount_str
    item["processed_at"] = now()
    yield_pct = round((amount / input_amount) * Decimal(100), 2)
    item["yield_pct"] = str(yield_pct)
    event(data, "OUTPUT_RECORDED",
          batch_id=item["id"],
          output_kg=amount_str,
          input_kg=input_str,
          yield_pct=str(yield_pct))
    return item, None


# ----------------------------------------------------------------------
# 3. CHAIN-OF-CUSTODY HANDOVERS
# ----------------------------------------------------------------------

def record_handover(data, payload):
    stage = str(payload.get("stage", "")).strip().upper()
    if stage not in ("PICKER_TO_AGGREGATOR", "AGGREGATOR_TO_RECYCLER"):
        raise ValueError("Invalid stage. Must be PICKER_TO_AGGREGATOR or AGGREGATOR_TO_RECYCLER")
    sender = str(payload.get("sender", "")).strip()
    receiver = str(payload.get("receiver", "")).strip()
    if not sender or not receiver:
        raise ValueError("Sender and receiver are required")

    claimed_weight = kg(payload.get("claimed_weight_kg"))
    claimed_str = format_kg(claimed_weight)

    lot_id = payload.get("lot_id")
    batch_id = payload.get("batch_id")
    evidence_id = payload.get("evidence_id")
    notes = str(payload.get("notes", "")).strip()

    reconciliation = None
    if lot_id:
        target_lot = next((l for l in data["lots"] if l["id"] == lot_id), None)
        if not target_lot:
            raise ValueError("Target lot not found")
        lot_weight = Decimal(target_lot["weight_kg"])
        delta = claimed_weight - lot_weight
        reconciliation = {
            "source_type": "LOT",
            "source_id": lot_id,
            "source_kg": target_lot["weight_kg"],
            "handover_claimed_kg": claimed_str,
            "delta_kg": format_kg(delta),
            "status": "MATCH" if delta == 0 else ("INCREASE" if delta > 0 else "LOSS")
        }
    elif batch_id:
        target_batch = next((b for b in data["batches"] if b["id"] == batch_id), None)
        if not target_batch:
            raise ValueError("Target batch not found")
        batch_input = Decimal(target_batch["input_kg"])
        delta = claimed_weight - batch_input
        reconciliation = {
            "source_type": "BATCH",
            "source_id": batch_id,
            "source_kg": target_batch["input_kg"],
            "handover_claimed_kg": claimed_str,
            "delta_kg": format_kg(delta),
            "status": "MATCH" if delta == 0 else ("INCREASE" if delta > 0 else "LOSS")
        }

    item = {
        "id": f"ho_{uuid.uuid4().hex[:10]}",
        "stage": stage,
        "sender": sender,
        "receiver": receiver,
        "claimed_weight_kg": claimed_str,
        "lot_id": lot_id,
        "batch_id": batch_id,
        "evidence_id": evidence_id,
        "notes": notes,
        "acknowledgement_status": "PENDING",
        "acknowledgement_type": "Self-entered receipt (unauthenticated party)",
        "is_signed_or_authenticated": False,
        "disclaimer": "Recorded as a self-entered handover acknowledgement; not an independently authenticated or signed receipt.",
        "acknowledged_at": None,
        "acknowledged_by": None,
        "reconciliation": reconciliation,
        "created_at": now()
    }
    data["handovers"].append(item)
    event(data, "HANDOVER_RECORDED", handover_id=item["id"], stage=stage,
          sender=sender, receiver=receiver, claimed_weight_kg=claimed_str)
    return item


def acknowledge_handover(data, payload):
    handover_id = payload.get("handover_id")
    item = next((h for h in data["handovers"] if h["id"] == handover_id), None)
    if not item:
        raise ValueError("Handover not found")
    status = str(payload.get("status", "")).strip().upper()
    if status not in ("ACKNOWLEDGED", "DISPUTED"):
        raise ValueError("Status must be ACKNOWLEDGED or DISPUTED")
    acknowledged_by = str(payload.get("acknowledged_by", "")).strip()
    if not acknowledged_by:
        raise ValueError("Acknowledged-by name is required")

    item["acknowledgement_status"] = status
    item["acknowledgement_type"] = "Self-entered receipt (unauthenticated party)"
    item["disclaimer"] = "Recorded as a self-entered handover acknowledgement; not an independently authenticated or signed receipt."
    item["acknowledged_at"] = now()
    item["acknowledged_by"] = acknowledged_by
    item["acknowledgement_notes"] = str(payload.get("notes", "")).strip()
    event(data, "HANDOVER_ACKNOWLEDGED", handover_id=item["id"], status=status,
          acknowledged_by=acknowledged_by, acknowledgement_type="Self-entered receipt")
    return item


# ----------------------------------------------------------------------
# 4. EXPLAINABLE DISCREPANCY DETECTION
# ----------------------------------------------------------------------

def detect_discrepancies(data, batch_id=None):
    findings = []

    batches_to_check = data["batches"]
    if batch_id:
        batches_to_check = [b for b in data["batches"] if b["id"] == batch_id]

    # Rule 1: Mass balance check on batches
    for b in batches_to_check:
        input_amount = Decimal(b["input_kg"])
        # Check against rejected events as well
        rejected_events = [e for e in data["events"] if e.get("kind") == "OUTPUT_REJECTED" and e.get("batch_id") == b["id"]]
        for rev in rejected_events:
            claimed_amount = Decimal(rev["claimed_kg"])
            delta = claimed_amount - input_amount
            findings.append({
                "rule_id": "MASS_BALANCE_EXCEEDED",
                "rule_name": "Reported Output Exceeds Recorded Input",
                "severity": "HIGH",
                "is_block": True,
                "affected_entity": {"type": "BATCH", "id": b["id"]},
                "inputs": {
                    "batch_id": b["id"],
                    "recorded_input_kg": str(input_amount),
                    "claimed_output_kg": str(claimed_amount)
                },
                "calculation": f"{claimed_amount} kg claimed - {input_amount} kg recorded input = +{delta} kg excess",
                "finding": f"Recycler claimed {claimed_amount} kg output against {input_amount} kg batch input ({delta} kg discrepancy).",
                "recommended_human_check": "Compare recycler weighbridge receipt with aggregator dispatch manifest and inspect scale tare."
            })

    # Rule 2: Duplicate evidence SHA-256 hash across distinct records
    hash_map = {}
    for ev in data["evidence"]:
        h = ev["sha256"]
        hash_map.setdefault(h, []).append(ev)

    for sha, items in hash_map.items():
        if len(items) > 1:
            findings.append({
                "rule_id": "DUPLICATE_EVIDENCE_HASH",
                "rule_name": "Cryptographic Hash Reused Across Distinct Evidence Items",
                "severity": "HIGH",
                "is_block": False,
                "affected_entity": {"type": "EVIDENCE", "id": items[0]["id"]},
                "inputs": {
                    "sha256": sha,
                    "evidence_ids": [x["id"] for x in items],
                    "filenames": [x["filename"] for x in items]
                },
                "calculation": f"Identical SHA-256 digest ({sha[:12]}...) found on {len(items)} evidence submissions",
                "finding": f"The exact same photo hash was uploaded {len(items)} times across different events.",
                "recommended_human_check": "Review evidence photos to determine if an operator submitted stock or recycled photos."
            })

    # Rule 3: Handover weight divergence
    for ho in data["handovers"]:
        rec = ho.get("reconciliation")
        if rec and rec.get("delta_kg") and Decimal(rec["delta_kg"]) != 0:
            delta = abs(Decimal(rec["delta_kg"]))
            source_kg = Decimal(rec["source_kg"])
            pct_diff = round((delta / source_kg) * 100, 2) if source_kg > 0 else 0
            if pct_diff > 2:
                findings.append({
                    "rule_id": "HANDOVER_WEIGHT_DIVERGENCE",
                    "rule_name": "Significant Custody Transfer Weight Divergence",
                    "severity": "MEDIUM",
                    "is_block": False,
                    "affected_entity": {"type": "HANDOVER", "id": ho["id"]},
                    "inputs": {
                        "handover_id": ho["id"],
                        "sender": ho["sender"],
                        "receiver": ho["receiver"],
                        "source_kg": rec["source_kg"],
                        "handover_claimed_kg": rec["handover_claimed_kg"],
                        "divergence_kg": str(delta)
                    },
                    "calculation": f"|{rec['handover_claimed_kg']} - {rec['source_kg']}| = {delta} kg ({pct_diff}% divergence)",
                    "finding": f"Handover weight between {ho['sender']} and {ho['receiver']} diverges by {delta} kg ({pct_diff}%).",
                    "recommended_human_check": "Check for moisture loss in transit, transport spillage, or differing scale calibrations."
                })

    # Rule 4: Missing evidence references on lots
    for l in data["lots"]:
        if not l.get("photo_ref") and not l.get("evidence_id"):
            findings.append({
                "rule_id": "MISSING_EVIDENCE",
                "rule_name": "Collection Lot Without Photographic Evidence",
                "severity": "LOW",
                "is_block": False,
                "affected_entity": {"type": "LOT", "id": l["id"]},
                "inputs": {"lot_id": l["id"], "picker": l["picker"]},
                "calculation": "No evidence ID or photo reference string provided",
                "finding": f"Lot {l['id']} was submitted without any collection photo reference.",
                "recommended_human_check": "Ask field supervisor to upload collection receipt or bag photo."
            })

    return {
        "engine_disclaimer": "Deterministic mathematical and cryptographic rule engine; not an AI system. Detects data inconsistencies and does not guarantee that original self-reported field submissions are genuine.",
        "findings": findings
    }


# ----------------------------------------------------------------------
# 5. AUDITOR REVIEW QUEUE
# ----------------------------------------------------------------------

def record_review(data, payload):
    target_type = str(payload.get("target_type", "BATCH")).strip().upper()
    target_id = str(payload.get("target_id", "")).strip()
    status = str(payload.get("status", "")).strip().upper()
    reason = str(payload.get("reason", "")).strip()
    reviewer_name = str(payload.get("reviewer_name", "")).strip()

    if status not in ("OPEN", "NEEDS_FIELD_CHECK", "RESOLVED", "REJECTED"):
        raise ValueError("Status must be OPEN, NEEDS_FIELD_CHECK, RESOLVED, or REJECTED")
    if not reason:
        raise ValueError("A mandatory audit reason must be provided")
    if not reviewer_name:
        raise ValueError("Reviewer name is required")

    if target_type == "BATCH":
        target = next((b for b in data["batches"] if b["id"] == target_id), None)
        if not target:
            raise ValueError("Target batch not found")
        target["review_status"] = status
    elif target_type == "LOT":
        target = next((l for l in data["lots"] if l["id"] == target_id), None)
        if not target:
            raise ValueError("Target lot not found")
        target["review_status"] = status
    else:
        raise ValueError("Invalid target type")

    rev_item = {
        "id": f"rev_{uuid.uuid4().hex[:10]}",
        "target_type": target_type,
        "target_id": target_id,
        "status": status,
        "reason": reason,
        "reviewer_name": reviewer_name,
        "created_at": now(),
        "disclaimer": "Auditor review recorded in demo workflow; not an authorized regulatory EPR compliance decision."
    }
    data["reviews"].append(rev_item)
    event(data, "AUDIT_REVIEW_RECORDED", review_id=rev_item["id"],
          target_type=target_type, target_id=target_id, status=status, reviewer=reviewer_name)
    return rev_item


# ----------------------------------------------------------------------
# 6. PICKER INCOME AND MATERIAL-FLOW INSIGHTS
# ----------------------------------------------------------------------

def calculate_insights(data):
    # Picker economics
    pickers = {}
    for l in data["lots"]:
        p_id = l["picker"]
        if p_id not in pickers:
            pickers[p_id] = {
                "picker_id": p_id,
                "lots_count": 0,
                "total_weight_kg": Decimal(0),
                "reported_transaction_value_inr": Decimal(0),
                "materials": {}
            }
        pickers[p_id]["lots_count"] += 1
        w = Decimal(l["weight_kg"])
        price = Decimal(l.get("price_per_kg_inr", "15.000"))
        pickers[p_id]["total_weight_kg"] += w
        pickers[p_id]["reported_transaction_value_inr"] += (w * price)
        mat = l["material"]
        pickers[p_id]["materials"][mat] = pickers[p_id]["materials"].get(mat, Decimal(0)) + w

    picker_list = []
    for p in pickers.values():
        picker_list.append({
            "picker_id": p["picker_id"],
            "lots_count": p["lots_count"],
            "total_weight_kg": format_kg(p["total_weight_kg"]),
            "reported_transaction_value_inr": f"{p['reported_transaction_value_inr']:.2f}",
            "materials": {m: format_kg(v) for m, v in p["materials"].items()}
        })

    # Material flow summary
    total_input = sum((Decimal(b["input_kg"]) for b in data["batches"]), Decimal(0))
    total_output = sum((Decimal(b["output_kg"]) for b in data["batches"] if b["output_kg"] is not None), Decimal(0))

    # Flagged discrepancy mass from rejected events
    rejected_events = [e for e in data["events"] if e.get("kind") == "OUTPUT_REJECTED"]
    flagged_discrepancy_kg = sum((Decimal(e.get("excess_kg", "0")) for e in rejected_events), Decimal(0))

    # Reviewed batches
    reviewed_batches = [b for b in data["batches"] if b.get("review_status") in ("RESOLVED", "NEEDS_FIELD_CHECK", "REJECTED")]
    reviewed_kg = sum((Decimal(b["input_kg"]) for b in reviewed_batches), Decimal(0))

    # Polymer breakdown
    materials_summary = {}
    for b in data["batches"]:
        mat = b["material"]
        if mat not in materials_summary:
            materials_summary[mat] = {"batches": 0, "input_kg": Decimal(0), "output_kg": Decimal(0)}
        materials_summary[mat]["batches"] += 1
        materials_summary[mat]["input_kg"] += Decimal(b["input_kg"])
        if b["output_kg"] is not None:
            materials_summary[mat]["output_kg"] += Decimal(b["output_kg"])

    polymer_flow = {
        m: {
            "batches": v["batches"],
            "input_kg": format_kg(v["input_kg"]),
            "output_kg": format_kg(v["output_kg"])
        } for m, v in materials_summary.items()
    }

    return {
        "picker_economics": {
            "disclaimer": "Reported transaction value (unconfirmed payment; no UPI or banking integration)",
            "pickers": picker_list
        },
        "material_flow": {
            "total_input_kg": format_kg(total_input),
            "reported_output_kg": format_kg(total_output),
            "flagged_discrepancy_kg": format_kg(flagged_discrepancy_kg),
            "reviewed_quantity_kg": format_kg(reviewed_kg),
            "polymers": polymer_flow,
            "disclaimer": "Internal mass-balance tallies; not officially certified recycling figures or carbon credits."
        }
    }


# ----------------------------------------------------------------------
# 7. VISUAL 60-SECOND GUIDED DEMO SCENARIO
# ----------------------------------------------------------------------

def create_demo_scenario(data):
    """Generates a complete 60-second guided Sankalp demo scenario:
    200 kg + 140 kg PET lots -> 340 kg Batch -> Attempted 500 kg Recycler claim
    -> 160 kg Discrepancy Flagged -> Auditor Review.
    Safe to run repeatedly; does not delete existing data.
    """
    demo_seq = sum(1 for b in data["batches"] if b.get("is_demo")) + 1
    tag = f"Demo #{demo_seq}"

    # Generate valid illustrative synthetic demo images locally
    img1_bytes = generate_synthetic_demo_image("P-101", f"Picker P-101 ({tag})", "200.000", "Kolkata Ward 12, Hub A (Self-reported)", "22.5726° N, 88.3639° E")
    img2_bytes = generate_synthetic_demo_image("P-102", f"Picker P-102 ({tag})", "140.000", "Kolkata Ward 14, Hub B (Self-reported)", "22.5697° N, 88.3697° E")

    ev1 = save_evidence(data, img1_bytes, f"synthetic_demo_p101_{uuid.uuid4().hex[:6]}.jpg", geolocation={
        "latitude": 22.5726, "longitude": 88.3639, "accuracy_m": 12.5, "captured_at": now()
    }, is_synthetic_demo=True)
    ev2 = save_evidence(data, img2_bytes, f"synthetic_demo_p102_{uuid.uuid4().hex[:6]}.jpg", geolocation={
        "latitude": 22.5697, "longitude": 88.3697, "accuracy_m": 15.0, "captured_at": now()
    }, is_synthetic_demo=True)

    # Lot 1 (200 kg) & Lot 2 (140 kg)
    a = lot(data, {
        "picker": f"P-101 ({tag})",
        "material": "PET",
        "location": "Kolkata Ward 12, Hub A (Self-reported)",
        "weight": "200.000",
        "photo_ref": f"evidence://{ev1['filename']}",
        "evidence_id": ev1["id"],
        "price_per_kg": "16.000"
    })
    b = lot(data, {
        "picker": f"P-102 ({tag})",
        "material": "PET",
        "location": "Kolkata Ward 14, Hub B (Self-reported)",
        "weight": "140.000",
        "photo_ref": f"evidence://{ev2['filename']}",
        "evidence_id": ev2["id"],
        "price_per_kg": "15.500"
    })

    # Handover 1: Picker P-101 -> Aggregator Hub
    ho1 = record_handover(data, {
        "stage": "PICKER_TO_AGGREGATOR",
        "sender": f"P-101 ({tag})",
        "receiver": "Kolkata Central Aggregator Hub",
        "claimed_weight_kg": "200.000",
        "lot_id": a["id"],
        "evidence_id": ev1["id"],
        "notes": "Demo collection handoff note (self-entered, unverified)"
    })
    acknowledge_handover(data, {
        "handover_id": ho1["id"],
        "status": "ACKNOWLEDGED",
        "acknowledged_by": "Kolkata Hub Manager (Self-entered demo receipt)"
    })

    # Handover 2: Picker P-102 -> Aggregator Hub
    ho2 = record_handover(data, {
        "stage": "PICKER_TO_AGGREGATOR",
        "sender": f"P-102 ({tag})",
        "receiver": "Kolkata Central Aggregator Hub",
        "claimed_weight_kg": "140.000",
        "lot_id": b["id"],
        "evidence_id": ev2["id"],
        "notes": "Demo transfer handoff note (self-entered, unverified)"
    })
    acknowledge_handover(data, {
        "handover_id": ho2["id"],
        "status": "ACKNOWLEDGED",
        "acknowledged_by": "Kolkata Hub Manager (Self-entered demo receipt)"
    })

    # Batch 340 kg
    batch_item = batch(data, {"lot_ids": [a["id"], b["id"]]})
    batch_item["is_demo"] = True
    batch_item["label"] = f"Sankalp 340 kg Demo Batch ({tag})"

    # Handover 3: Aggregator Hub -> Recycler Plant
    ho3 = record_handover(data, {
        "stage": "AGGREGATOR_TO_RECYCLER",
        "sender": "Kolkata Central Aggregator Hub",
        "receiver": "GreenTech Polymers Recycling Ltd",
        "claimed_weight_kg": "340.000",
        "batch_id": batch_item["id"],
        "notes": "Demo transfer note #CT-DEMO-340 (self-entered, unverified)"
    })
    acknowledge_handover(data, {
        "handover_id": ho3["id"],
        "status": "ACKNOWLEDGED",
        "acknowledged_by": "GreenTech Inward Supervisor (Self-entered demo receipt)"
    })

    event(data, "DEMO_SCENARIO_CREATED", batch_id=batch_item["id"], input_kg="340.000", tag=tag)

    return {
        "batch": batch_item,
        "lots": [a, b],
        "handovers": [ho1, ho2, ho3],
        "evidence": [ev1, ev2]
    }


# ----------------------------------------------------------------------
# 8. AUDIT PACK GENERATION & BATCH DETAIL
# ----------------------------------------------------------------------

def report(data, batch_id):
    item = next((b for b in data["batches"] if b["id"] == batch_id), None)
    if item is None:
        raise ValueError("Batch not found")
    source_lots = [l for l in data["lots"] if l["id"] in item["lot_ids"]]
    events = [e for e in data["events"] if e.get("batch_id") == batch_id or e.get("lot_id") in item["lot_ids"]]
    batch_handovers = [h for h in data["handovers"] if h.get("batch_id") == batch_id or h.get("lot_id") in item["lot_ids"]]
    batch_reviews = [r for r in data["reviews"] if r.get("target_id") == batch_id]
    discrepancies = detect_discrepancies(data, batch_id)["findings"]

    input_kg = Decimal(item["input_kg"])
    output_kg = Decimal(item["output_kg"]) if item["output_kg"] is not None else None
    yield_pct = str(round((output_kg / input_kg) * 100, 2)) if output_kg is not None else None

    # Link evidence objects
    all_ev_ids = {l.get("evidence_id") for l in source_lots if l.get("evidence_id")}
    all_ev_ids.update({h.get("evidence_id") for h in batch_handovers if h.get("evidence_id")})
    linked_evidence = [e for e in data["evidence"] if e["id"] in all_ev_ids]

    return {
        "title": "ClusterTrace Supporting Traceability Audit Pack",
        "official_epr_certificate": False,
        "is_synthetic_demo": bool(item.get("is_demo", False)),
        "synthetic_demo_disclaimer": "This batch contains synthetic demo records created for evaluation; it depicts no real physical collection or recycling." if item.get("is_demo") else None,
        "compliance_disclaimer": "This document is a supporting traceability audit pack for chain-of-custody verification. It is NOT an official EPR certificate or regulatory compliance document.",
        "generated_at": now(),
        "batch": item,
        "source_lots": source_lots,
        "handovers": batch_handovers,
        "evidence": linked_evidence,
        "discrepancy_findings": discrepancies,
        "reviews": batch_reviews,
        "events": events,
        "recorded_event_trail": events,
        "mass_balance_summary": {
            "material": item["material"],
            "recorded_input_kg": item["input_kg"],
            "reported_output_kg": item["output_kg"],
            "yield_percent": yield_pct,
            "status": "COMPLETED" if item["output_kg"] is not None else "IN_PROCESS"
        },
        "limitation": "Source weights, locations, and photo references are self reported field claims; independent verification or physical scale telemetry is not included."
    }


def batch_detail(data, batch_id):
    item = next((b for b in data["batches"] if b["id"] == batch_id), None)
    if item is None:
        raise ValueError("Batch not found")
    source_lots = [l for l in data["lots"] if l["id"] in item["lot_ids"]]
    events = [e for e in data["events"] if e.get("batch_id") == batch_id or e.get("lot_id") in item["lot_ids"]]
    batch_handovers = [h for h in data["handovers"] if h.get("batch_id") == batch_id or h.get("lot_id") in item["lot_ids"]]
    batch_reviews = [r for r in data["reviews"] if r.get("target_id") == batch_id]
    discrepancy_data = detect_discrepancies(data, batch_id)

    # Link evidence
    all_ev_ids = {l.get("evidence_id") for l in source_lots if l.get("evidence_id")}
    all_ev_ids.update({h.get("evidence_id") for h in batch_handovers if h.get("evidence_id")})
    linked_evidence = [e for e in data["evidence"] if e["id"] in all_ev_ids]

    return {
        "batch": item,
        "is_synthetic_demo": bool(item.get("is_demo", False)),
        "synthetic_demo_disclaimer": "This batch contains synthetic demo records created for evaluation; it depicts no real physical collection or recycling." if item.get("is_demo") else None,
        "source_lots": source_lots,
        "handovers": batch_handovers,
        "evidence": linked_evidence,
        "discrepancies": discrepancy_data["findings"],
        "reviews": batch_reviews,
        "events": events
    }


# ----------------------------------------------------------------------
# 9. HTTP HANDLER
# ----------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def send_json(self, status, body, download_filename=None):
        raw = json.dumps(body, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        if download_filename:
            self.send_header("Content-Disposition", f'attachment; filename="{download_filename}"')
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        clean_path = self.path.split("?")[0]
        if clean_path == "/":
            raw = (ROOT / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        elif clean_path == "/api/state":
            with LOCK:
                self.send_json(200, read())
        elif clean_path == "/api/insights":
            with LOCK:
                self.send_json(200, calculate_insights(read()))
        elif clean_path == "/api/discrepancies":
            with LOCK:
                self.send_json(200, detect_discrepancies(read()))
        elif clean_path.startswith("/api/batch/"):
            batch_id = clean_path.split("/")[-1]
            with LOCK:
                try:
                    self.send_json(200, batch_detail(read(), batch_id))
                except ValueError as exc:
                    self.send_json(404, {"error": str(exc)})
        elif clean_path.startswith("/api/report/"):
            batch_id = clean_path.split("/")[-1]
            with LOCK:
                try:
                    rep = report(read(), batch_id)
                    filename = f"clustertrace-audit-{batch_id}.json"
                    self.send_json(200, rep, download_filename=filename)
                except ValueError as exc:
                    self.send_json(404, {"error": str(exc)})
        elif clean_path.startswith("/uploads/"):
            # Serve uploaded evidence with path traversal protection
            filename = clean_path.replace("/uploads/", "", 1)
            uploads_dir = get_uploads_dir().resolve()
            try:
                file_path = (uploads_dir / filename).resolve()
                if not file_path.is_relative_to(uploads_dir) or not file_path.is_file():
                    return self.send_json(404, {"error": "File not found"})
                
                ext = file_path.suffix.lower()
                mime = "image/jpeg" if ext in (".jpg", ".jpeg") else ("image/png" if ext == ".png" else "image/webp")
                raw = file_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "public, max-age=86400")
                self.end_headers()
                self.wfile.write(raw)
            except Exception:
                self.send_json(404, {"error": "File not found"})
        else:
            self.send_json(404, {"error": "Not found"})

    def do_POST(self):
        clean_path = self.path.split("?")[0]
        content_type_header = self.headers.get("Content-Type", "")

        # Multipart form upload
        if clean_path == "/api/upload":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length > MAX_FILE_SIZE + 65536:
                    return self.send_json(413, {"error": "File exceeds 5 MB limit"})
                raw_body = self.rfile.read(length)

                header_bytes = f"Content-Type: {content_type_header}\r\n\r\n".encode("utf-8")
                msg = email.message_from_bytes(header_bytes + raw_body, policy=email_default_policy)

                file_bytes = None
                filename = "photo.jpg"
                file_mime = "image/jpeg"
                geo = {}

                for part in msg.iter_parts():
                    cd_name = part.get_param("name", header="content-disposition")
                    if cd_name in ("file", "photo"):
                        file_bytes = part.get_payload(decode=True)
                        filename = part.get_filename() or "photo.jpg"
                        file_mime = part.get_content_type()
                    elif cd_name == "latitude":
                        geo["latitude"] = part.get_payload(decode=True).decode("utf-8").strip()
                    elif cd_name == "longitude":
                        geo["longitude"] = part.get_payload(decode=True).decode("utf-8").strip()
                    elif cd_name == "accuracy_m":
                        geo["accuracy_m"] = part.get_payload(decode=True).decode("utf-8").strip()

                if not file_bytes:
                    return self.send_json(400, {"error": "No file uploaded in form field 'file' or 'photo'"})

                with LOCK:
                    data = read()
                    result = save_evidence(data, file_bytes, filename, file_mime, geolocation=geo)
                    write(data)
                    return self.send_json(201, result)
            except ValueError as exc:
                return self.send_json(400, {"error": str(exc)})
            except Exception as exc:
                return self.send_json(500, {"error": str(exc)})

        # JSON endpoints
        valid_paths = ("/api/lots", "/api/batches", "/api/outputs", "/api/demo",
                       "/api/handovers", "/api/handovers/acknowledge", "/api/reviews")
        if clean_path not in valid_paths:
            return self.send_json(404, {"error": "Not found"})

        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 200000:
                return self.send_json(413, {"error": "Request too large"})
            payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            if not isinstance(payload, dict):
                raise ValueError("Expected a JSON object")

            with LOCK:
                data = read()
                if clean_path == "/api/lots":
                    result = lot(data, payload)
                    write(data)
                    self.send_json(201, result)
                elif clean_path == "/api/batches":
                    result = batch(data, payload)
                    write(data)
                    self.send_json(201, result)
                elif clean_path == "/api/outputs":
                    result, error = output(data, payload)
                    write(data)  # Always persist, even when rejected so OUTPUT_REJECTED is saved!
                    if error:
                        return self.send_json(422, {"error": error})
                    self.send_json(201, result)
                elif clean_path == "/api/handovers":
                    result = record_handover(data, payload)
                    write(data)
                    self.send_json(201, result)
                elif clean_path == "/api/handovers/acknowledge":
                    result = acknowledge_handover(data, payload)
                    write(data)
                    self.send_json(200, result)
                elif clean_path == "/api/reviews":
                    result = record_review(data, payload)
                    write(data)
                    self.send_json(201, result)
                elif clean_path == "/api/demo":
                    result = create_demo_scenario(data)
                    write(data)
                    self.send_json(201, result)
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            self.send_json(400, {"error": str(exc)})

    def log_message(self, format, *args):
        if os.environ.get("CLUSTERTRACE_DEBUG"):
            super().log_message(format, *args)


def run_server(port=8000, host="127.0.0.1"):
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"ClusterTrace running at http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    run_server()
