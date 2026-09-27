"""ClusterTrace local MVP: collection lineage and deterministic mass balance."""
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import os
import threading
import uuid

ROOT = Path(__file__).parent
DB_FILE = ROOT / "clustertrace.json"
LOCK = threading.RLock()


def get_db_path():
    env_override = os.environ.get("CLUSTERTRACE_DB")
    if env_override:
        return Path(env_override)
    return DB_FILE


def now():
    return datetime.now(timezone.utc).isoformat()


def initial():
    return {"lots": [], "batches": [], "events": []}


def read():
    db_path = get_db_path()
    return json.loads(db_path.read_text(encoding="utf-8")) if db_path.exists() else initial()


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


def event(data, kind, **fields):
    evt = {"id": uuid.uuid4().hex[:12], "time": now(), "kind": kind, **fields}
    data["events"].append(evt)
    return evt


def format_kg(value):
    return f"{Decimal(value):.3f}"


def lot(data, payload):
    picker = str(payload.get("picker", "")).strip()
    material = str(payload.get("material", "")).strip().upper()
    location = str(payload.get("location", "")).strip()
    if not all((picker, material, location)):
        raise ValueError("Picker ID, material and location are required")
    weight = kg(payload.get("weight"))
    weight_str = format_kg(weight)
    photo_ref = str(payload.get("photo_ref", "")).strip()
    notes = str(payload.get("notes", "")).strip()
    item = {
        "id": uuid.uuid4().hex[:12],
        "picker": picker,
        "material": material,
        "location": location,
        "photo_ref": photo_ref,
        "notes": notes,
        "weight_kg": weight_str,
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
        "created_at": now()
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


def create_demo_scenario(data):
    """Generates a fresh, clearly labeled 340 kg demo batch without deleting existing data."""
    demo_seq = sum(1 for b in data["batches"] if b.get("is_demo")) + 1
    tag = f"Demo #{demo_seq}"
    a = lot(data, {
        "picker": f"P-101 ({tag})",
        "material": "PET",
        "location": "Kolkata Ward 12 (Self-reported)",
        "weight": "200.000",
        "photo_ref": f"photo-bag-p101-{uuid.uuid4().hex[:6]}.jpg"
    })
    b = lot(data, {
        "picker": f"P-102 ({tag})",
        "material": "PET",
        "location": "Kolkata Ward 14 (Self-reported)",
        "weight": "140.000",
        "photo_ref": f"photo-bag-p102-{uuid.uuid4().hex[:6]}.jpg"
    })
    batch_item = batch(data, {"lot_ids": [a["id"], b["id"]]})
    batch_item["is_demo"] = True
    batch_item["label"] = f"Sankalp 340 kg Demo Batch ({tag})"
    event(data, "DEMO_SCENARIO_CREATED", batch_id=batch_item["id"], input_kg="340.000", tag=tag)
    return {"batch": batch_item, "lots": [a, b]}


def report(data, batch_id):
    item = next((b for b in data["batches"] if b["id"] == batch_id), None)
    if item is None:
        raise ValueError("Batch not found")
    source_lots = [l for l in data["lots"] if l["id"] in item["lot_ids"]]
    events = [e for e in data["events"] if e.get("batch_id") == batch_id or e.get("lot_id") in item["lot_ids"]]
    
    input_kg = Decimal(item["input_kg"])
    output_kg = Decimal(item["output_kg"]) if item["output_kg"] is not None else None
    yield_pct = str(round((output_kg / input_kg) * 100, 2)) if output_kg is not None else None
    
    return {
        "title": "ClusterTrace supporting traceability audit pack",
        "official_epr_certificate": False,
        "compliance_disclaimer": "This document is a supporting traceability audit pack for chain-of-custody verification. It is NOT an official EPR certificate or regulatory compliance document.",
        "generated_at": now(),
        "batch": item,
        "source_lots": source_lots,
        "events": events,
        "mass_balance_summary": {
            "material": item["material"],
            "recorded_input_kg": item["input_kg"],
            "recorded_output_kg": item["output_kg"],
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
    return {
        "batch": item,
        "source_lots": source_lots,
        "events": events
    }


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
        else:
            self.send_json(404, {"error": "Not found"})

    def do_POST(self):
        clean_path = self.path.split("?")[0]
        if clean_path not in ("/api/lots", "/api/batches", "/api/outputs", "/api/demo"):
            return self.send_json(404, {"error": "Not found"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 100000:
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
                elif clean_path == "/api/demo":
                    result = create_demo_scenario(data)
                    write(data)
                    self.send_json(201, result)
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            self.send_json(400, {"error": str(exc)})

    def log_message(self, format, *args):
        # Suppress noisy request logs during unit testing unless DEBUG is set
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
