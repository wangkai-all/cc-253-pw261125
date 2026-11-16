"""HTTP-level tests: the page's API surface, crash and recovery included."""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from measure import spec
import app.server as server


class ApiBase(unittest.TestCase):
    """Each test gets a fresh database and its own in-process server."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "api.db")
        self.httpd, self.service = server.serve("127.0.0.1", 0, self.db)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        server.drop_service(self.db)
        self.tmp.cleanup()

    def call(self, method, path, payload=None):
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def get(self, path):
        return self.call("GET", path)

    def post(self, path, payload=None):
        return self.call("POST", path, payload if payload is not None else {})


class TestStatic(ApiBase):
    def test_page_assets(self):
        for path, marker in [("/", b"<html"), ("/app.js", b"function"),
                             ("/style.css", b"--accent")]:
            with urllib.request.urlopen("http://127.0.0.1:%d%s" % (self.port, path)) as r:
                self.assertEqual(r.status, 200)
                self.assertIn(marker, r.read())

    def test_unknown_endpoint_is_404(self):
        status, body = self.get("/api/nope")
        self.assertEqual(status, 404)


class TestIngestAndDelivery(ApiBase):
    def test_bulk_ingest_then_select_one(self):
        status, body = self.post("/api/readings/bulk", {
            "device": "SYNTH-P", "count": 5, "start_seq": 1,
            "start_ts": "2026-02-01T00:00:00Z", "step_ms": 10000,
        })
        self.assertEqual(status, 200, body)
        self.assertEqual(len(body["readings"]), 5)
        status, detail = self.get("/api/readings/SYNTH-P:3")
        self.assertEqual(status, 200)
        self.assertEqual(detail["device"], "SYNTH-P")
        self.assertEqual(detail["raw"], spec.raw_at("SYNTH-P", 3))
        self.assertIn("comparison", detail)

    def test_bad_input_is_a_400(self):
        cases = [
            {"device": "SYNTH-Z", "device_ts": "2026-01-01T00:00:00Z", "kPa": 10},
            {"device": "SYNTH-P", "device_ts": "nope", "kPa": 10},
            {"device": "SYNTH-P", "device_ts": "2026-01-01T00:00:00Z"},
            {"device": "SYNTH-P", "device_ts": "2026-01-01T00:00:00Z", "raw": 1.5},
        ]
        for payload in cases:
            status, body = self.post("/api/readings", payload)
            self.assertEqual(status, 400, payload)
            self.assertIn("message", body["error"])

    def test_conflicting_redelivery_is_409(self):
        payload = {"device": "SYNTH-Q", "seq": 1, "device_ts": "2026-03-01T00:00:00Z",
                   "kPa": 200}
        status, _ = self.post("/api/readings", payload)
        self.assertEqual(status, 200)
        payload["kPa"] = 210
        status, body = self.post("/api/readings", payload)
        self.assertEqual(status, 409)

    def test_generate_calibration_revises_readings(self):
        self.post("/api/readings", {"device": "SYNTH-Q", "seq": 900,
                                    "device_ts": "2026-04-01T00:00:00Z", "kPa": 300})
        self.post("/api/mode", {"device": "SYNTH-Q", "mode": "provisional"})
        status, body = self.post("/api/process", {"max_runs": 50})
        self.assertEqual(status, 200)
        self.assertEqual(body["runs"][0]["status"], "unconverted")

        status, body = self.post("/api/calibrations/generate", {
            "device": "SYNTH-Q", "valid_from": "2026-03-01T00:00:00Z",
            "drift_kpa": 2.0,
        })
        self.assertEqual(status, 200, body)
        self.assertEqual(body["delivery"]["decision"], "accepted")
        self.assertEqual(body["delivery"]["queued"], 1)

        self.post("/api/process", {"max_runs": 50})
        status, detail = self.get("/api/readings/SYNTH-Q:900")
        self.assertEqual(detail["latest"]["status"], "priced")
        self.assertEqual(detail["latest"]["revision"], 2)
        self.assertEqual(detail["latest"]["supersedes"], 1)

    def test_rejected_document_is_recorded(self):
        status, body = self.post("/api/calibrations", {
            "cal_id": "BAD", "cal_version": 1,
        })
        self.assertEqual(status, 200)
        self.assertEqual(body["delivery"]["decision"], "rejected")
        self.assertEqual(body["delivery"]["reason"], "MISSING_FIELD")
        status, boot = self.get("/api/bootstrap")
        self.assertIn("BAD", [d["cal_id"] for d in boot["deliveries"]])

    def test_mode_endpoint_validates(self):
        status, _ = self.post("/api/mode", {"device": "SYNTH-P", "mode": "later"})
        self.assertEqual(status, 400)
        status, body = self.post("/api/mode", {"device": "SYNTH-P", "mode": "provisional"})
        self.assertEqual(status, 200)


class TestCrashAndRecover(ApiBase):
    def test_crash_reports_and_recovery_completes(self):
        self.post("/api/mode", {"device": "SYNTH-P", "mode": "provisional"})
        self.post("/api/readings/bulk", {
            "device": "SYNTH-P", "count": 4, "start_seq": 500,
            "start_ts": "2026-05-01T00:00:00Z",
        })
        status, body = self.post("/api/process", {"max_runs": 10, "crash": True})
        self.assertEqual(status, 200)
        self.assertTrue(body["error"]["crashed"])
        self.assertEqual(body["error"]["interrupted_runs"], 1)

        status, body = self.post("/api/recover")
        self.assertEqual(status, 200)
        self.assertEqual(body["requeued"], 1)
        self.assertTrue(all(r["outcome"] == "published" for r in body["runs"]))
        for seq in range(500, 504):
            status, detail = self.get("/api/readings/SYNTH-P:%d" % seq)
            self.assertEqual(len(detail["history"]), 1)
            self.assertEqual(detail["history"][0]["revision"], 1)
            self.assertEqual(detail["history"][0]["status"], "unconverted")


class TestComparison(ApiBase):
    def test_comparison_lists_versions_and_the_naive_rule(self):
        self.post("/api/readings", {"device": "SYNTH-Q", "seq": 1200,
                                    "device_ts": "2026-06-01T00:00:00Z", "kPa": 250})
        for version, from_ts, drift in [
            (1, "2026-05-01T00:00:00Z", 0.5),
            (2, "2026-07-01T00:00:00Z", 3.0),
            (3, "2026-05-15T00:00:00Z", 1.0),
        ]:
            self.post("/api/calibrations/generate", {
                "device": "SYNTH-Q", "cal_id": "CAL-Q", "cal_version": version,
                "valid_from": from_ts, "drift_kpa": drift,
            })
        status, cmp = self.get("/api/compare?reading_id=SYNTH-Q:1200")
        self.assertEqual(status, 200)
        self.assertEqual(cmp["chosen_version"], 3)
        self.assertEqual(cmp["effective_versions"], [1, 3])
        self.assertEqual(cmp["pending_versions"], [2])
        self.assertEqual(cmp["naive_latest_received"]["cal_version"], 3)
        self.assertTrue(any(not row["effective"] for row in cmp["rows"]))

    def test_compare_without_a_reading_id(self):
        status, body = self.get("/api/compare?device=SYNTH-P")
        self.assertEqual(status, 200)
        self.assertIn("comparisons", body)


class TestFeed(ApiBase):
    def test_feed_is_append_only_and_ordered(self):
        self.post("/api/mode", {"device": "SYNTH-P", "mode": "provisional"})
        self.post("/api/readings/bulk", {
            "device": "SYNTH-P", "count": 3, "start_seq": 700,
            "start_ts": "2026-08-01T00:00:00Z",
        })
        self.post("/api/process", {"max_runs": 10})
        self.post("/api/calibrations/generate", {
            "device": "SYNTH-P", "valid_from": "2026-07-01T00:00:00Z",
        })
        self.post("/api/process", {"max_runs": 10})

        status, first = self.get("/api/feed")
        self.assertEqual(status, 200)
        ids = [p["id"] for p in first["publications"]]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(len(ids), 6)  # 3 unconverted + 3 priced revisions
        superseding = [p for p in first["publications"] if p["supersedes"]]
        self.assertEqual(len(superseding), 3)
        self.assertTrue(all(p["source"] == "supersede" for p in superseding))

        cursor = first["next_since_id"]
        status, second = self.get("/api/feed?since_id=%d" % cursor)
        self.assertEqual(second["publications"], [])
        self.assertEqual(second["next_since_id"], cursor)


if __name__ == "__main__":
    unittest.main()
