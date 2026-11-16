"""Second-review tests: edge cases the main suite only touches indirectly."""

import json
import os
import tempfile
import unittest

from measure import intake, spec
from measure.store import PUBLISH_HOLD, PUBLISH_PROVISIONAL, Store

T0 = "2026-01-01T00:00:00Z"
T1 = "2026-01-02T00:00:00Z"
T2 = "2026-01-03T00:00:00Z"

class EdgeBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "edge.db"))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def doc(self, version, valid_from, device="SYNTH-P", cal_id="CAL-P", drift=0.0):
        return spec.calibrated_doc(device, cal_id, version, spec.to_ms(valid_from),
                                   drift_kpa=drift)

    def ingest(self, seq, ts, device="SYNTH-P", raw=None):
        raw = spec.raw_at(device, seq) if raw is None else raw
        return self.store.ingest(device, seq, raw, ts)


class TestOrderingAndBoundaries(EdgeBase):
    def test_valid_from_is_inclusive(self):
        reading = self.ingest(1, T2)[0]
        self.store.set_publish_mode("SYNTH-P", PUBLISH_PROVISIONAL)
        # valid_from exactly equal to the measurement time is effective.
        self.store.deliver_cal(self.doc(1, T2))
        self.store.drain()
        self.assertEqual(self.store.latest_publication(reading["id"])["cal_version"], 1)

    def test_boundary_one_millisecond_later_is_not_effective(self):
        reading = self.ingest(1, T2)[0]
        self.store.set_publish_mode("SYNTH-P", PUBLISH_PROVISIONAL)
        due = spec.from_ms(spec.to_ms(T2) + 1)
        self.store.deliver_cal(self.doc(1, due))
        self.store.drain()
        published = self.store.latest_publication(reading["id"])
        self.assertEqual(published["status"], "unconverted")
        self.assertIsNone(published["cal_version"])

    def test_validity_edges_inclusive(self):
        self.store.set_publish_mode("SYNTH-P", PUBLISH_PROVISIONAL)
        validity = [5_000_000, 9_000_000]
        for seq, raw in [(1, 5_000_000), (2, 9_000_000), (3, 4_999_999), (4, 9_000_001)]:
            self.ingest(seq, T1, raw=raw)
        self.store.deliver_cal(spec.calibrated_doc("SYNTH-P", "CAL-P", 1,
                                                   spec.to_ms(T0), validity=validity))
        self.store.drain()
        statuses = {seq: self.store.latest_publication("SYNTH-P:%d" % seq)["status"]
                    for seq in range(1, 5)}
        self.assertEqual(statuses, {1: "priced", 2: "priced",
                                    3: "out_of_validity", 4: "out_of_validity"})

    def test_out_of_validity_holds_when_policy_is_hold(self):
        self.store.set_publish_mode("SYNTH-P", PUBLISH_HOLD)
        self.ingest(1, T1, raw=10)
        self.store.deliver_cal(self.doc(1, T0))
        outcomes = [r["outcome"] for r in self.store.drain()]
        self.assertEqual(outcomes, ["held"])
        self.assertEqual(self.store.history("SYNTH-P:1"), [])


class TestVersionChainEdges(EdgeBase):
    def test_same_number_different_case_ids_are_independent(self):
        self.assertEqual(self.store.deliver_cal(self.doc(1, T0, cal_id="cal-p"))["decision"],
                         "accepted")
        other = self.store.deliver_cal(self.doc(1, T0, cal_id="CAL-P"))
        # Different cal_id for the same device is a chain conflict, not a
        # silent second chain.
        self.assertEqual(other["decision"], "rejected")
        self.assertEqual(other["reason"], "CHAIN_CONFLICT")

    def test_highest_version_is_not_necessarily_the_latest_arrival(self):
        self.store.set_publish_mode("SYNTH-P", PUBLISH_PROVISIONAL)
        self.ingest(1, T2)
        self.store.deliver_cal(self.doc(5, T0, drift=5.0))
        self.store.deliver_cal(self.doc(6, T2, drift=6.0))
        self.store.deliver_cal(self.doc(4, T0, drift=4.0))
        self.store.drain()
        self.assertEqual(self.store.latest_publication("SYNTH-P:1")["cal_version"], 6)

    def test_rejects_revision_zero_and_negative(self):
        for bad in (0, -1):
            result = self.store.deliver_cal(self.doc(1, T0) | {"cal_version": bad})
            self.assertEqual(result["decision"], "rejected")

    def test_rejects_replace_pointing_forward(self):
        result = self.store.deliver_cal(self.doc(1, T0) | {"replaces": 2})
        self.assertEqual(result["decision"], "rejected")
        self.assertEqual(result["reason"], "BAD_FIELD")

    def test_rejects_non_monotonic_polynomial(self):
        payload = self.doc(1, T0)
        payload["coefs"] = [134.0, 6948.0, 5275.0, -4898.0]
        result = self.store.deliver_cal(payload)
        self.assertEqual(result["decision"], "rejected")
        self.assertEqual(result["reason"], "NON_MONOTONIC")

    def test_timestamp_normalised_to_utc(self):
        payload = self.doc(1, T0)
        payload["valid_from"] = "2026-01-01T08:00:00+08:00"
        self.store.deliver_cal(payload)
        accepted = self.store.accepted_calibrations()
        self.assertEqual(accepted[0]["payload"]["valid_from"], "2026-01-01T00:00:00.000Z")

    def test_duplicate_detected_across_non_canonical_json(self):
        payload = self.doc(1, T0)
        text = json.dumps(payload, indent=2, sort_keys=True)
        first = self.store.deliver_cal(text)
        second = self.store.deliver_cal(json.dumps(payload, separators=(",", ":")))
        self.assertEqual(first["decision"], "accepted")
        self.assertEqual(second["decision"], "duplicate")


class TestReadingEdges(EdgeBase):
    def test_seq_must_be_positive_and_unique_per_device(self):
        with self.assertRaises(ValueError):
            self.store.ingest("SYNTH-P", 0, 1000, T0)
        a, _ = self.store.ingest("SYNTH-P", 1, 1000, T0)
        b, created = self.store.ingest("SYNTH-Q", 1, 1000, T0)
        self.assertTrue(created)
        self.assertNotEqual(a["id"], b["id"])

    def test_config_difference_makes_redelivery_a_conflict(self):
        self.store.ingest("SYNTH-P", 1, 1000, T0, {"gain": "4"})
        with self.assertRaises(Exception):
            self.store.ingest("SYNTH-P", 1, 1000, T0, {"gain": "8"})

    def test_timestamp_equivalent_forms_are_the_same_reading(self):
        first, _ = self.store.ingest("SYNTH-P", 1, 1000, "2026-01-01T00:00:00Z")
        second, created = self.store.ingest("SYNTH-P", 1, 1000, "2026-01-01T08:00:00+08:00",
                                            first["config"])
        self.assertFalse(created)
        self.assertEqual(first["id"], second["id"])

    def test_next_seq_skips_gaps_and_uses_maximum(self):
        self.ingest(3, T0)
        self.ingest(7, T1)
        self.assertEqual(self.store.next_seq("SYNTH-P"), 8)
        self.assertEqual(self.store.next_seq("SYNTH-Q"), 1)


class TestSnapshotAndReplay(EdgeBase):
    def test_snapshot_contains_inputs_only(self):
        self.store.set_publish_mode("SYNTH-P", PUBLISH_PROVISIONAL)
        self.ingest(1, T1)
        self.store.deliver_cal(self.doc(1, T0))
        self.store.deliver_cal({"cal_id": "BAD"})
        self.store.drain()
        snap = self.store.snapshot()
        self.assertEqual(len(snap["readings"]), 1)
        self.assertEqual(len(snap["calibrations"]), 1)
        self.assertEqual(snap["publish_modes"]["SYNTH-P"], PUBLISH_PROVISIONAL)
        self.assertNotIn("publications", snap)

    def test_replay_into_a_fresh_store_reproduces_publications(self):
        self.store.set_publish_mode("SYNTH-P", PUBLISH_PROVISIONAL)
        for seq in range(1, 4):
            self.ingest(seq, T1)
        self.store.deliver_cal(self.doc(1, T0, drift=0.0))
        self.store.drain()
        self.store.deliver_cal(self.doc(2, T0, drift=2.0))
        self.store.drain()
        original = {
            seq: [(h["status"], h["value_kpa"], h["cal_version"])
                  for h in self.store.history("SYNTH-P:%d" % seq)][-1]
            for seq in range(1, 4)
        }

        snap = self.store.snapshot()
        with tempfile.TemporaryDirectory() as other:
            replay = Store(os.path.join(other, "replay.db"))
            try:
                # Replay in the recorded arrival order, processing the queue
                # after every input, so intermediate revisions reproduce.
                events = (
                    [("cal", c) for c in snap["calibrations"]]
                    + [("reading", r) for r in snap["readings"]]
                )
                events.sort(key=lambda e: e[1]["received_ms"])
                for device, mode in snap["publish_modes"].items():
                    replay.set_publish_mode(device, mode, requeue=False)
                for kind, item in events:
                    if kind == "cal":
                        self.assertEqual(replay.deliver_cal(item["payload"])["decision"],
                                         "accepted")
                    else:
                        replay.ingest(item["device"], item["seq"], item["raw"],
                                      item["device_ts"], item["config"])
                    replay.drain()
                # Revision *count* depends on how often conversion ran between
                # arrivals; the final published value must not.
                replayed = {
                    seq: [(h["status"], h["value_kpa"], h["cal_version"])
                          for h in replay.history("SYNTH-P:%d" % seq)][-1]
                    for seq in range(1, 4)
                }
            finally:
                replay.close()
        self.assertEqual(replayed, original)


if __name__ == "__main__":
    unittest.main()
