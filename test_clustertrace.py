"""Automated test suite for ClusterTrace.
Tests mass balance, duplicate lot assignment, mixed materials,
audit pack lineage, honest EPR disclaimers, and HTTP persistence.
"""
from decimal import Decimal
from http.server import ThreadingHTTPServer
from pathlib import Path
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

import server


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


if __name__ == "__main__":
    unittest.main()
