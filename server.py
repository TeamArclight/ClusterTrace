"""ClusterTrace local MVP: collection lineage and deterministic mass balance."""
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import threading
import uuid

ROOT = Path(__file__).parent
DB = ROOT / "clustertrace.json"
LOCK = threading.RLock()


def now():
    return datetime.now(timezone.utc).isoformat()


def initial():
    return {"lots": [], "batches": [], "events": []}


def read():
    return json.loads(DB.read_text()) if DB.exists() else initial()


def write(data):
    tmp = DB.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(DB)


def kg(value):
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError):
        raise ValueError("Enter a valid weight")
    if not amount.is_finite() or amount <= 0 or amount.as_tuple().exponent < -3:
        raise ValueError("Weight must be positive with at most three decimals")
    return amount


def event(data, kind, **fields):
    data["events"].append({"id": uuid.uuid4().hex[:12], "time": now(), "kind": kind, **fields})


def lot(data, payload):
    picker = str(payload.get("picker", "")).strip()
    material = str(payload.get("material", "")).strip()
    location = str(payload.get("location", "")).strip()
    if not all((picker, material, location)):
        raise ValueError("Picker, material and location are required")
    weight = kg(payload.get("weight"))
    item = {"id": uuid.uuid4().hex[:12], "picker": picker, "material": material,
            "location": location, "photo_ref": str(payload.get("photo_ref", "")).strip(),
            "weight_kg": str(weight), "created_at": now(), "batch_id": None}
    data["lots"].append(item)
    event(data, "LOT_RECORDED", lot_id=item["id"], weight_kg=str(weight))
    return item


def batch(data, payload):
    ids = payload.get("lot_ids")
    if not isinstance(ids, list) or not ids or len(ids) != len(set(ids)):
        raise ValueError("Select one or more distinct lots")
    selected = [item for item in data["lots"] if item["id"] in ids]
    if len(selected) != len(ids) or any(item["batch_id"] for item in selected):
        raise ValueError("Every lot must exist and be unbatched")
    materials = {item["material"] for item in selected}
    if len(materials) != 1:
        raise ValueError("A batch must contain one material type")
    item = {"id": uuid.uuid4().hex[:12], "lot_ids": ids,
            "material": selected[0]["material"],
            "input_kg": str(sum((Decimal(x["weight_kg"]) for x in selected), Decimal(0))),
            "output_kg": None, "created_at": now()}
    data["batches"].append(item)
    for source in selected:
        source["batch_id"] = item["id"]
    event(data, "BATCH_CREATED", batch_id=item["id"], lot_ids=ids, input_kg=item["input_kg"])
    return item


def output(data, payload):
    item = next((b for b in data["batches"] if b["id"] == payload.get("batch_id")), None)
    if item is None:
        raise ValueError("Batch not found")
    amount = kg(payload.get("output_kg"))
    if item["output_kg"] is not None:
        raise ValueError("Output is already recorded for this batch")
    if amount > Decimal(item["input_kg"]):
        event(data, "OUTPUT_REJECTED", batch_id=item["id"], claimed_kg=str(amount),
              available_kg=item["input_kg"], reason="Output exceeds recorded input")
        return None, "Output exceeds recorded input of " + item["input_kg"] + " kg"
    item["output_kg"] = str(amount)
    item["processed_at"] = now()
    event(data, "OUTPUT_RECORDED", batch_id=item["id"], output_kg=str(amount))
    return item, None


def report(data, batch_id):
    item = next((b for b in data["batches"] if b["id"] == batch_id), None)
    if item is None:
        raise ValueError("Batch not found")
    return {"title": "ClusterTrace supporting traceability audit pack",
            "official_epr_certificate": False, "generated_at": now(),
            "batch": item,
            "source_lots": [l for l in data["lots"] if l["id"] in item["lot_ids"]],
            "events": [e for e in data["events"] if e.get("batch_id") == batch_id or e.get("lot_id") in item["lot_ids"]],
            "limitation": "Source weights and identities are self reported; independent verification is not included."}


class Handler(BaseHTTPRequestHandler):
    def send_json(self, status, body, download=False):
        raw = json.dumps(body, indent=2).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        if download:
            self.send_header("Content-Disposition", 'attachment; filename="clustertrace-audit.json"')
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/":
            raw = (ROOT / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        elif self.path == "/api/state":
            with LOCK:
                self.send_json(200, read())
        elif self.path.startswith("/api/report/"):
            with LOCK:
                try:
                    self.send_json(200, report(read(), self.path.split("/")[-1]), True)
                except ValueError as exc:
                    self.send_json(404, {"error": str(exc)})
        else:
            self.send_json(404, {"error": "Not found"})

    def do_POST(self):
        if self.path not in ("/api/lots", "/api/batches", "/api/outputs", "/api/demo"):
            return self.send_json(404, {"error": "Not found"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 100000:
                return self.send_json(413, {"error": "Request too large"})
            payload = json.loads(self.rfile.read(length)) if length else {}
            if not isinstance(payload, dict):
                raise ValueError("Expected a JSON object")
            with LOCK:
                data = read()
                if self.path == "/api/lots":
                    result = lot(data, payload)
                elif self.path == "/api/batches":
                    result = batch(data, payload)
                elif self.path == "/api/outputs":
                    result, error = output(data, payload)
                    write(data)
                    if error:
                        return self.send_json(422, {"error": error})
                else:
                    a = lot(data, {"picker": "P-101", "material": "PET", "location": "Kolkata",
                                   "weight": 200, "photo_ref": "demo-photo-1"})
                    b = lot(data, {"picker": "P-102", "material": "PET", "location": "Kolkata",
                                   "weight": 140, "photo_ref": "demo-photo-2"})
                    result = batch(data, {"lot_ids": [a["id"], b["id"]]})
                write(data)
                self.send_json(201, result)
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            self.send_json(400, {"error": str(exc)})


if __name__ == "__main__":
    print("ClusterTrace running at http://localhost:8000")
    ThreadingHTTPServer(("127.0.0.1", 8000), Handler).serve_forever()
