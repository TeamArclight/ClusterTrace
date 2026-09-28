"""Automated test suite for ClusterTrace.
Tests mass balance, duplicate lot assignment, mixed materials,
audit pack lineage, honest EPR disclaimers, and HTTP persistence.
"""
from decimal import Decimal
from http.server import ThreadingHTTPServer
from pathlib import Path
import hashlib
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

import server

VALID_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a\x1f\x1e\x1d\x1a\x1c\x1c $.' \",#\x1c\x1c(7),01444\x1f'9=82<.342\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xbf\x00\xff\xd9"
VALID_PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc```\x00\x00\x00\x04\x00\x01\xf6\x178U\x00\x00\x00\x00IEND\xaeB`\x82"


class TestClusterTraceCore(unittest.TestCase):
    def setUp(self):
        self.data = server.initial()

    def test_kg_validation(self):
        # Valid weights
        self.assertEqual(server.kg(100), Decimal("100"))
        self.assertEqual(server.kg("120.5"), Decimal("120.5"))
        self.assertEqual(server.kg("340.125"), Decimal("340.125"))

        # Invalid weights: non-numeric, negative, zero, or >3 decimal places
        with self.assertRaises(ValueError):
            server.kg("abc")
        with self.assertRaises(ValueError):
            server.kg("-10")
        with self.assertRaises(ValueError):
            server.kg(0)
        with self.assertRaises(ValueError):
            server.kg("50.1234")  # more than 3 decimal places

    def test_lot_creation_and_disclaimer(self):
        lot_item = server.lot(self.data, {
            "picker": "P-101",
            "material": "PET",
            "location": "Kolkata Ward 12",
            "weight": "200.000",
            "photo_ref": "bag-photo-01.jpg"
        })
        self.assertEqual(len(self.data["lots"]), 1)
        self.assertEqual(lot_item["picker"], "P-101")
        self.assertEqual(lot_item["material"], "PET")
        self.assertEqual(lot_item["weight_kg"], "200.000")
        self.assertIn("unverified", lot_item["verification_status"].lower())
        self.assertIsNone(lot_item["batch_id"])

        # Missing required fields
        with self.assertRaises(ValueError):
            server.lot(self.data, {"picker": "", "material": "PET", "location": "Kolkata", "weight": 50})

    def test_batch_valid_and_total_mass(self):
        l1 = server.lot(self.data, {"picker": "P-1", "material": "PET", "location": "A", "weight": "200"})
        l2 = server.lot(self.data, {"picker": "P-2", "material": "PET", "location": "B", "weight": "140"})

        b = server.batch(self.data, {"lot_ids": [l1["id"], l2["id"]]})
        self.assertEqual(b["input_kg"], "340.000")
        self.assertEqual(b["material"], "PET")
        self.assertEqual(l1["batch_id"], b["id"])
        self.assertEqual(l2["batch_id"], b["id"])

    def test_duplicate_lot_assignment_rejected(self):
        l1 = server.lot(self.data, {"picker": "P-1", "material": "PET", "location": "A", "weight": "100"})
        server.batch(self.data, {"lot_ids": [l1["id"]]})

        # Attempting to assign l1 to a second batch must fail
        with self.assertRaises(ValueError) as ctx:
            server.batch(self.data, {"lot_ids": [l1["id"]]})
        self.assertIn("duplicate", str(ctx.exception).lower())

    def test_duplicate_lot_ids_in_same_request_rejected(self):
        l1 = server.lot(self.data, {"picker": "P-1", "material": "PET", "location": "A", "weight": "100"})
        with self.assertRaises(ValueError) as ctx:
            server.batch(self.data, {"lot_ids": [l1["id"], l1["id"]]})
        self.assertIn("distinct", str(ctx.exception).lower())

    def test_mixed_materials_rejected(self):
        l1 = server.lot(self.data, {"picker": "P-1", "material": "PET", "location": "A", "weight": "100"})
        l2 = server.lot(self.data, {"picker": "P-2", "material": "HDPE", "location": "B", "weight": "100"})

        with self.assertRaises(ValueError) as ctx:
            server.batch(self.data, {"lot_ids": [l1["id"], l2["id"]]})
        self.assertIn("mixed", str(ctx.exception).lower())

    def test_mass_balance_valid_output(self):
        l1 = server.lot(self.data, {"picker": "P-1", "material": "PET", "location": "A", "weight": "200"})
        l2 = server.lot(self.data, {"picker": "P-2", "material": "PET", "location": "B", "weight": "140"})
        b = server.batch(self.data, {"lot_ids": [l1["id"], l2["id"]]})

        # Valid output: 306 kg (90% yield) on 340 kg input
        res, err = server.output(self.data, {"batch_id": b["id"], "output_kg": "306.000"})
        self.assertIsNone(err)
        self.assertIsNotNone(res)
        self.assertEqual(res["output_kg"], "306.000")
        self.assertEqual(res["yield_pct"], "90.00")

        # Second output attempt must fail
        with self.assertRaises(ValueError):
            server.output(self.data, {"batch_id": b["id"], "output_kg": "10"})

    def test_mass_balance_fraud_rejection(self):
        # 340 kg input
        l1 = server.lot(self.data, {"picker": "P-1", "material": "PET", "location": "A", "weight": "200"})
        l2 = server.lot(self.data, {"picker": "P-2", "material": "PET", "location": "B", "weight": "140"})
        b = server.batch(self.data, {"lot_ids": [l1["id"], l2["id"]]})

        # Fraud claim: 500 kg output on 340 kg input
        res, err = server.output(self.data, {"batch_id": b["id"], "output_kg": "500.000"})
        self.assertIsNone(res)
        self.assertIsNotNone(err)
        self.assertIn("Mass-balance violation", err)
        self.assertIn("160.000 kg", err)

        # Check that OUTPUT_REJECTED event was logged with exact discrepancy
        rejected_events = [e for e in self.data["events"] if e["kind"] == "OUTPUT_REJECTED"]
        self.assertEqual(len(rejected_events), 1)
        rev = rejected_events[0]
        self.assertEqual(rev["batch_id"], b["id"])
        self.assertEqual(rev["claimed_kg"], "500.000")
        self.assertEqual(rev["available_kg"], "340.000")
        self.assertEqual(rev["excess_kg"], "160.000")

    def test_repeatable_demo_scenario_safe(self):
        # Pre-populate custom existing lot
        custom_lot = server.lot(self.data, {"picker": "Custom-1", "material": "PP", "location": "Hub", "weight": "50"})

        # Run demo scenario 1
        res1 = server.create_demo_scenario(self.data)
        b1 = res1["batch"]
        self.assertEqual(b1["input_kg"], "340.000")
        self.assertTrue(b1.get("is_demo"))

        # Run demo scenario 2
        res2 = server.create_demo_scenario(self.data)
        b2 = res2["batch"]
        self.assertEqual(b2["input_kg"], "340.000")

        # Distinct IDs, existing data preserved
        self.assertNotEqual(b1["id"], b2["id"])
        self.assertIn(custom_lot, self.data["lots"])
        self.assertEqual(len(self.data["batches"]), 2)

    def test_audit_pack_lineage_and_honest_claims(self):
        res = server.create_demo_scenario(self.data)
        b = res["batch"]
        server.output(self.data, {"batch_id": b["id"], "output_kg": "306.000"})

        pack = server.report(self.data, b["id"])
        self.assertFalse(pack["official_epr_certificate"])
        self.assertIn("NOT an official EPR certificate", pack["compliance_disclaimer"])
        self.assertIn("self reported field claims", pack["limitation"])
        self.assertEqual(pack["batch"]["id"], b["id"])
        self.assertEqual(len(pack["source_lots"]), 2)
        self.assertTrue(any(e["kind"] == "OUTPUT_RECORDED" for e in pack["events"]))


class TestClusterTraceHTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp_dir = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.tmp_dir.name) / "test_clustertrace.json"
        os.environ["CLUSTERTRACE_DB"] = str(cls.db_path)

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.server.server_address[1]
        cls.base_url = f"http://127.0.0.1:{cls.port}"
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        os.environ.pop("CLUSTERTRACE_DB", None)
        cls.tmp_dir.cleanup()

    def request(self, path, method="GET", payload=None):
        url = f"{self.base_url}{path}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Content-Type": "application/json"} if payload is not None else {}
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req) as resp:
                status = resp.status
                body = resp.read()
                content_type = resp.headers.get("Content-Type", "")
                if "application/json" in content_type:
                    return status, json.loads(body.decode("utf-8")), resp.headers
                return status, body.decode("utf-8"), resp.headers
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8")
                try:
                    parsed = json.loads(body)
                except Exception:
                    parsed = {"error": body}
                return exc.code, parsed, exc.headers
            finally:
                exc.close()

    def test_http_demo_and_fraud_rejection_persistence(self):
        # 1. Trigger demo scenario creation via HTTP
        status, demo_res, _ = self.request("/api/demo", method="POST", payload={})
        self.assertEqual(status, 201)
        batch_id = demo_res["batch"]["id"]
        self.assertEqual(demo_res["batch"]["input_kg"], "340.000")

        # 2. Attempt fraud: 500 kg output on 340 kg batch via HTTP
        status, err_res, _ = self.request("/api/outputs", method="POST", payload={
            "batch_id": batch_id,
            "output_kg": "500.000"
        })
        self.assertEqual(status, 422)
        self.assertIn("Mass-balance violation", err_res["error"])
        self.assertIn("160.000 kg", err_res["error"])

        # 3. Persistence verification: read directly from the JSON file on disk
        persisted = json.loads(self.db_path.read_text(encoding="utf-8"))
        rejected_events = [e for e in persisted["events"] if e.get("kind") == "OUTPUT_REJECTED" and e.get("batch_id") == batch_id]
        self.assertEqual(len(rejected_events), 1)
        self.assertEqual(rejected_events[0]["claimed_kg"], "500.000")
        self.assertEqual(rejected_events[0]["excess_kg"], "160.000")

        # 4. Check via /api/state as well (reload simulation)
        status, state_res, _ = self.request("/api/state", method="GET")
        self.assertEqual(status, 200)
        self.assertTrue(any(e["kind"] == "OUTPUT_REJECTED" and e["batch_id"] == batch_id for e in state_res["events"]))

        # 5. Record legitimate output: 306 kg (90% yield)
        status, ok_res, _ = self.request("/api/outputs", method="POST", payload={
            "batch_id": batch_id,
            "output_kg": "306.000"
        })
        self.assertEqual(status, 201)
        self.assertEqual(ok_res["output_kg"], "306.000")
        self.assertEqual(ok_res["yield_pct"], "90.00")

        # 6. Audit pack download via /api/report/<batch_id>
        status, audit_pack, headers = self.request(f"/api/report/{batch_id}", method="GET")
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers.get("Content-Disposition", ""))
        self.assertFalse(audit_pack["official_epr_certificate"])
        self.assertEqual(audit_pack["batch"]["id"], batch_id)
        self.assertEqual(len(audit_pack["source_lots"]), 2)
        # Verify both the rejected attempt and successful record are present in events
        event_kinds = [e["kind"] for e in audit_pack["events"]]
        self.assertIn("OUTPUT_REJECTED", event_kinds)
        self.assertIn("OUTPUT_RECORDED", event_kinds)

    def test_http_evidence_upload_and_path_traversal_protection(self):
        # 1. Upload sample evidence via multipart/form-data
        boundary = "---------------------------ClusterTraceBoundary123"
        dummy_jpeg = VALID_JPEG
        
        body_parts = [
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"sample.jpg\"\r\nContent-Type: image/jpeg\r\n\r\n".encode("utf-8"),
            dummy_jpeg,
            f"\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"latitude\"\r\n\r\n22.5726\r\n".encode("utf-8"),
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"longitude\"\r\n\r\n88.3639\r\n".encode("utf-8"),
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"accuracy_m\"\r\n\r\n12.5\r\n".encode("utf-8"),
            f"--{boundary}--\r\n".encode("utf-8")
        ]
        body = b"".join(body_parts)

        url = f"{self.base_url}/api/upload"
        req = urllib.request.Request(url, data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}, method="POST")
        with urllib.request.urlopen(req) as resp:
            status = resp.status
            upload_res = json.loads(resp.read().decode("utf-8"))
        self.assertEqual(status, 201)
        self.assertIn("id", upload_res)
        self.assertEqual(upload_res["mime_type"], "image/jpeg")
        self.assertIsNotNone(upload_res["geolocation"])
        self.assertEqual(upload_res["geolocation"]["latitude"], 22.5726)

        # 2. Retrieve uploaded file safely
        file_url = f"{self.base_url}{upload_res['url']}"
        with urllib.request.urlopen(file_url) as file_resp:
            self.assertEqual(file_resp.status, 200)
            self.assertEqual(file_resp.headers.get("Content-Type"), "image/jpeg")
            self.assertEqual(file_resp.read(), dummy_jpeg)

        # 3. Path traversal attack attempt: must return 404
        bad_url = f"{self.base_url}/uploads/../../server.py"
        try:
            urllib.request.urlopen(bad_url)
            self.fail("Path traversal request should have failed")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 404)
            exc.close()

        # 4. Disguised text file uploaded with .jpg extension: must be rejected with 400
        disguised_body = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"disguised.jpg\"\r\nContent-Type: image/jpeg\r\n\r\n"
            f"Plain text file masquerading as an image.\r\n--{boundary}--\r\n"
        ).encode("utf-8")
        req_disguised = urllib.request.Request(url, data=disguised_body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}, method="POST")
        try:
            urllib.request.urlopen(req_disguised)
            self.fail("Disguised text upload should have failed with 400")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)
            err_data = json.loads(exc.read().decode("utf-8"))
            self.assertIn("not a valid JPEG, PNG, or WebP", err_data["error"])
            exc.close()

        # 5. Truncated image uploaded: must be rejected with 400
        truncated_body = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"corrupt.jpg\"\r\nContent-Type: image/jpeg\r\n\r\n".encode("utf-8")
            + b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01"
            + f"\r\n--{boundary}--\r\n".encode("utf-8")
        )
        req_trunc = urllib.request.Request(url, data=truncated_body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}, method="POST")
        try:
            urllib.request.urlopen(req_trunc)
            self.fail("Truncated image upload should have failed with 400")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)
            exc.close()

        # 6. Mismatched extension: valid PNG bytes submitted with .jpg filename
        valid_png = VALID_PNG
        mismatched_body = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"actual_png_named_jpg.jpg\"\r\nContent-Type: image/jpeg\r\n\r\n".encode("utf-8")
            + valid_png
            + f"\r\n--{boundary}--\r\n".encode("utf-8")
        )
        req_mismatch = urllib.request.Request(url, data=mismatched_body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}, method="POST")
        with urllib.request.urlopen(req_mismatch) as resp:
            self.assertEqual(resp.status, 201)
            res_png = json.loads(resp.read().decode("utf-8"))
            # Verified and canonicalized to PNG!
            self.assertEqual(res_png["mime_type"], "image/png")
            self.assertTrue(res_png["filename"].endswith(".png"))

    def test_http_handovers_reviews_insights(self):
        # 1. Create a lot
        status, lot_res, _ = self.request("/api/lots", method="POST", payload={
            "picker": "P-99", "material": "HDPE", "location": "Howrah Yard", "weight": "150.000", "price_per_kg": "18.000"
        })
        self.assertEqual(status, 201)
        lot_id = lot_res["id"]

        # 2. Record custody handover from picker to aggregator with slight divergence
        status, ho_res, _ = self.request("/api/handovers", method="POST", payload={
            "stage": "PICKER_TO_AGGREGATOR",
            "sender": "P-99",
            "receiver": "Howrah Aggregator",
            "claimed_weight_kg": "146.000",
            "lot_id": lot_id,
            "notes": "Moisture loss during sorting"
        })
        self.assertEqual(status, 201)
        ho_id = ho_res["id"]
        self.assertEqual(ho_res["reconciliation"]["status"], "LOSS")
        self.assertEqual(ho_res["reconciliation"]["delta_kg"], "-4.000")

        # 3. Acknowledge handover
        status, ack_res, _ = self.request("/api/handovers/acknowledge", method="POST", payload={
            "handover_id": ho_id,
            "status": "ACKNOWLEDGED",
            "acknowledged_by": "Howrah Station Lead"
        })
        self.assertEqual(status, 200)
        self.assertEqual(ack_res["acknowledgement_status"], "ACKNOWLEDGED")

        # 4. Check discrepancy detection endpoint
        status, disc_res, _ = self.request("/api/discrepancies", method="GET")
        self.assertEqual(status, 200)
        self.assertIn("findings", disc_res)
        divergence_findings = [f for f in disc_res["findings"] if f["rule_id"] == "HANDOVER_WEIGHT_DIVERGENCE"]
        self.assertTrue(len(divergence_findings) > 0)

        # 5. Record auditor review
        status, rev_res, _ = self.request("/api/reviews", method="POST", payload={
            "target_type": "LOT",
            "target_id": lot_id,
            "status": "NEEDS_FIELD_CHECK",
            "reason": "4 kg weight loss noted on handover; scale tare requires cross-calibration check.",
            "reviewer_name": "Quality Inspector Ramesh"
        })
        self.assertEqual(status, 201)
        self.assertEqual(rev_res["status"], "NEEDS_FIELD_CHECK")

        # 6. Check insights endpoint
        status, ins_res, _ = self.request("/api/insights", method="GET")
        self.assertEqual(status, 200)
        p_data = next((p for p in ins_res["picker_economics"]["pickers"] if p["picker_id"] == "P-99"), None)
        self.assertIsNotNone(p_data)
        self.assertEqual(p_data["total_weight_kg"], "150.000")
        # 150 kg * 18 INR = 2700.00 INR
        self.assertEqual(p_data["reported_transaction_value_inr"], "2700.00")


class TestClusterTraceFiveFeaturesCore(unittest.TestCase):
    def setUp(self):
        self.data = server.initial()

    def test_evidence_capture_and_hashing(self):
        sample_bytes = VALID_JPEG
        ev = server.save_evidence(self.data, sample_bytes, "collection_bag.jpg", "image/jpeg", {
            "latitude": 22.5, "longitude": 88.3, "accuracy_m": 10.0
        })
        expected_sha = hashlib.sha256(sample_bytes).hexdigest()
        self.assertEqual(ev["sha256"], expected_sha)
        self.assertEqual(ev["verification_status"], "Submitted evidence (unverified)")
        self.assertEqual(ev["geolocation"]["latitude"], 22.5)

        # Empty file rejection
        with self.assertRaises(ValueError):
            server.save_evidence(self.data, b"", "empty.jpg", "image/jpeg")

        # Disallowed file type rejection
        with self.assertRaises(ValueError):
            server.save_evidence(self.data, b"malicious", "script.exe", "application/x-msdownload")

        # Oversized file (>5MB) rejection
        with self.assertRaises(ValueError):
            server.save_evidence(self.data, b"x" * (5 * 1024 * 1024 + 1), "big.jpg", "image/jpeg")

    def test_handover_reconciliation_preserves_original_weight(self):
        l = server.lot(self.data, {"picker": "P-1", "material": "PET", "location": "Ward 5", "weight": "100.000"})
        ho = server.record_handover(self.data, {
            "stage": "PICKER_TO_AGGREGATOR",
            "sender": "P-1",
            "receiver": "Aggregator",
            "claimed_weight_kg": "97.000",
            "lot_id": l["id"]
        })
        # Handover reconciliation flagged loss
        self.assertEqual(ho["reconciliation"]["status"], "LOSS")
        self.assertEqual(ho["reconciliation"]["delta_kg"], "-3.000")
        # Original collection lot weight NEVER silently altered
        self.assertEqual(l["weight_kg"], "100.000")

    def test_duplicate_evidence_hash_discrepancy_detection(self):
        content = VALID_JPEG
        ev1 = server.save_evidence(self.data, content, "bag1.jpg", "image/jpeg")
        ev2 = server.save_evidence(self.data, content, "bag2.jpg", "image/jpeg")
        self.assertEqual(ev1["sha256"], ev2["sha256"])

        disc = server.detect_discrepancies(self.data)
        dup_rules = [f for f in disc["findings"] if f["rule_id"] == "DUPLICATE_EVIDENCE_HASH"]
        self.assertEqual(len(dup_rules), 1)
        self.assertIn("Identical", dup_rules[0]["calculation"])

    def test_auditor_review_decision_mandatory_reason(self):
        l = server.lot(self.data, {"picker": "P-1", "material": "PET", "location": "Ward 5", "weight": "100.000"})
        # Mandatory reason required
        with self.assertRaises(ValueError):
            server.record_review(self.data, {
                "target_type": "LOT", "target_id": l["id"], "status": "RESOLVED", "reason": "", "reviewer_name": "Auditor"
            })

        rev = server.record_review(self.data, {
            "target_type": "LOT", "target_id": l["id"], "status": "RESOLVED",
            "reason": "Physical inspection verified tare weight calibration.",
            "reviewer_name": "Lead Auditor"
        })
        self.assertEqual(rev["status"], "RESOLVED")
        self.assertEqual(l["review_status"], "RESOLVED")
        self.assertTrue(any(e["kind"] == "AUDIT_REVIEW_RECORDED" for e in self.data["events"]))

    def test_picker_income_and_material_flow_insights(self):
        server.lot(self.data, {"picker": "P-10", "material": "PET", "location": "Loc A", "weight": "100.000", "price_per_kg": "16.000"})
        server.lot(self.data, {"picker": "P-10", "material": "PET", "location": "Loc B", "weight": "50.000", "price_per_kg": "16.000"})
        ins = server.calculate_insights(self.data)
        p10 = next((p for p in ins["picker_economics"]["pickers"] if p["picker_id"] == "P-10"), None)
        self.assertIsNotNone(p10)
        self.assertEqual(p10["total_weight_kg"], "150.000")
        # 150 kg * 16 INR = 2400.00 INR
        self.assertEqual(p10["reported_transaction_value_inr"], "2400.00")
        self.assertIn("unconfirmed payment", ins["picker_economics"]["disclaimer"].lower())


if __name__ == "__main__":
    unittest.main()

