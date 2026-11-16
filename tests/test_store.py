"""Tests for the calibration-versioning receiver.

Run with:  python3 -m unittest discover -s tests -v
"""

import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest

from measure import intake, spec
from measure.store import (Conflict, SimulatedCrash, Store, PUBLISH_HOLD,
                           PUBLISH_PROVISIONAL)

T0 = "2026-01-01T00:00:00Z"
T1 = "2026-01-02T00:00:00Z"
T2 = "2026-01-03T00:00:00Z"
T3 = "2026-01-04T00:00:00Z"


def ms(ts):
    return spec.to_ms(ts)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "receiver.db")
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def doc(self, version, valid_from, device="SYNTH-P", cal_id="CAL-P",
            drift=0.0, validity=None, **kw):
        return spec.calibrated_doc(device, cal_id, version, ms(valid_from),
                                   drift_kpa=drift, validity=validity, **kw)

    def deliver(self, version, valid_from, **kw):
        return self.store.deliver_cal(self.doc(version, valid_from, **kw))

    def ingest(self, seq, ts, device="SYNTH-P", raw=None):
        if raw is None:
            raw = spec.raw_at(device, seq)
        return self.store.ingest(device, seq, raw, ts)

    def run_all(self):
        return self.store.drain()

    def latest(self, reading_id):
        return self.store.latest_publication(reading_id)


class TestRawImmutability(Base):
    def test_config_and_raw_are_stored_verbatim(self):
        reading, created = self.store.ingest("SYNTH-P", 1, 1234567, T0,
                                             {"serial": "X", "gain": "8"})
        self.assertTrue(created)
        self.assertEqual(reading["raw"], 1234567)
        self.assertEqual(reading["config"], {"serial": "X", "gain": "8"})
        self.assertEqual(reading["device_ts"], "2026-01-01T00:00:00.000Z")

    def test_redelivery_is_idempotent(self):
        first, created = self.ingest(1, T0, raw=1000000)
        again, created_again = self.store.ingest("SYNTH-P", 1, 1000000, T0,
                                                 first["config"])
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first["id"], again["id"])
        self.assertEqual(self.store.reading_count(), 1)

    def test_conflicting_redelivery_is_refused(self):
        self.ingest(1, T0, raw=1000000)
        with self.assertRaises(Conflict):
            self.store.ingest("SYNTH-P", 1, 1000001, T0)
        self.assertEqual(self.store.reading("SYNTH-P:1")["raw"], 1000000)

    def test_raw_rows_cannot_be_updated_even_by_sql(self):
        self.ingest(1, T0, raw=1000000)
        conn = sqlite3.connect(self.path)
        try:
            with self.assertRaises(sqlite3.DatabaseError):
                conn.execute("UPDATE readings SET raw = 42 WHERE id = 'SYNTH-P:1'")
            conn.rollback()
            with self.assertRaises(sqlite3.DatabaseError):
                conn.execute("DELETE FROM readings")
        finally:
            conn.close()


class TestSelectionByMeasurementTime(Base):
    def test_no_calibration_holds_until_one_is_effective(self):
        self.store.set_publish_mode("SYNTH-P", PUBLISH_HOLD)
        self.ingest(1, T0)
        result = self.store.run_once()
        self.assertEqual(result["outcome"], "held")
        self.assertIsNone(self.latest("SYNTH-P:1"))
        # A held reading leaves the queue: there is nothing to publish until
        # a delivery arrives, and that delivery re-queues every reading.
        self.assertEqual(self.store.queued(), [])
        self.assertEqual(self.store.metrics()["held_readings"], 1)

        self.deliver(1, T0)
        self.store.drain()
        published = self.latest("SYNTH-P:1")
        self.assertEqual(published["status"], "priced")
        self.assertEqual(published["revision"], 1)
        self.assertEqual(published["supersedes"], None)

    def test_provisional_publishes_unconverted_then_revises(self):
        self.store.set_publish_mode("SYNTH-P", PUBLISH_PROVISIONAL)
        self.ingest(1, T0)
        self.store.drain()
        first = self.latest("SYNTH-P:1")
        self.assertEqual(first["status"], "unconverted")
        self.assertIsNone(first["value_kpa"])

        self.deliver(1, T0)
        self.store.drain()
        history = self.store.history("SYNTH-P:1")
        self.assertEqual([h["revision"] for h in history], [1, 2])
        self.assertEqual(history[1]["supersedes"], 1)
        self.assertEqual(history[1]["status"], "priced")

    def test_a_version_starting_later_is_not_borrowed_for_an_earlier_reading(self):
        self.deliver(1, T2)  # effective only from T2
        self.ingest(1, T0)   # measured at T0
        self.store.set_publish_mode("SYNTH-P", PUBLISH_PROVISIONAL)
        self.store.drain()
        published = self.latest("SYNTH-P:1")
        self.assertEqual(published["status"], "unconverted")
        self.assertEqual(published["reason"], "NO_CALIBRATION_EFFECTIVE_AT_MEASUREMENT_TIME")
        self.assertIsNone(published["cal_version"])

    def test_arrival_order_never_decides(self):
        # v2 arrives first, v1 arrives afterwards.  v1 governs T0 because it
        # is the only version effective then; v2 governs T3.
        self.deliver(2, T2, drift=5.0)
        self.deliver(1, T0, drift=1.0)
        self.store.set_publish_mode("SYNTH-P", PUBLISH_PROVISIONAL)
        self.ingest(1, T0)
        self.ingest(2, T3)
        self.store.drain()

        early = self.latest("SYNTH-P:1")
        late = self.latest("SYNTH-P:2")
        self.assertEqual(early["cal_version"], 1)
        self.assertEqual(late["cal_version"], 2)
        self.assertNotEqual(early["sem_identity"], late["sem_identity"])

    def test_highest_version_wins_among_effective_ones(self):
        self.ingest(1, T3)
        self.deliver(1, T0, drift=1.0)
        self.deliver(2, T1, drift=2.0)
        self.deliver(3, T2, drift=3.0)
        self.store.set_publish_mode("SYNTH-P", PUBLISH_PROVISIONAL)
        self.store.drain()
        published = self.latest("SYNTH-P:1")
        self.assertEqual(published["cal_version"], 3)
        self.assertTrue(published["chosen_by"].startswith("HIGHEST_EFFECTIVE_VERSION"))

    def test_retroactive_correction_revises_earlier_readings(self):
        self.store.set_publish_mode("SYNTH-P", PUBLISH_PROVISIONAL)
        self.ingest(1, T1)
        self.deliver(1, T0, drift=1.0)
        self.store.drain()
        before = self.latest("SYNTH-P:1")

        # A correction whose coverage starts before the reading arrives late.
        self.deliver(2, T0, drift=4.0)
        self.store.drain()
        history = self.store.history("SYNTH-P:1")
        self.assertEqual(len(history), 2)
        after = history[-1]
        self.assertEqual(after["cal_version"], 2)
        self.assertEqual(after["supersedes"], 1)
        self.assertAlmostEqual(after["value_kpa"] - before["value_kpa"], 3.0, places=6)

    def test_out_of_validity_does_not_fall_back(self):
        self.ingest(1, T1, raw=10)  # far below every validity window
        self.deliver(1, T0)
        self.store.set_publish_mode("SYNTH-P", PUBLISH_PROVISIONAL)
        self.store.drain()
        published = self.latest("SYNTH-P:1")
        self.assertEqual(published["status"], "out_of_validity")
        self.assertEqual(published["reason"], "RAW_OUTSIDE_CAL_VALIDITY")
        self.assertIsNone(published["value_kpa"])
        self.assertEqual(published["cal_version"], 1)

    def test_calibration_for_other_device_is_not_used(self):
        self.deliver(1, T0, device="SYNTH-Q", cal_id="CAL-Q")
        self.store.set_publish_mode("SYNTH-P", PUBLISH_PROVISIONAL)
        self.ingest(1, T0, device="SYNTH-P")
        self.store.drain()
        self.assertEqual(self.latest("SYNTH-P:1")["status"], "unconverted")


class TestIntake(Base):
    def test_invalid_documents_are_recorded_not_stored(self):
        bad_cases = [
            ("{not json", "BAD_JSON"),
            ({"cal_id": "X"}, "MISSING_FIELD"),
            (self.doc(1, T0) | {"extra": 1}, "UNKNOWN_FIELD"),
            (self.doc(1, T0) | {"coefs": [1, 2, 3]}, "BAD_FIELD"),
            (self.doc(1, T0) | {"validity": [10, 5]}, "BAD_FIELD"),
            (self.doc(1, T0) | {"valid_from": "yesterday"}, "BAD_FIELD"),
            (self.doc(1, T0) | {"cal_version": 0}, "BAD_FIELD"),
            (self.doc(1, T0) | {"device": "SYNTH-Z"}, "BAD_FIELD"),
        ]
        for body, reason in bad_cases:
            result = self.store.deliver_cal(body)
            self.assertEqual(result["decision"], "rejected", body)
            self.assertEqual(result["reason"], reason, body)
        self.assertEqual(self.store.accepted_calibrations(), [])
        self.assertEqual(len(self.store.deliveries()), len(bad_cases))

    def test_version_conflict_and_duplicate(self):
        first = self.store.deliver_cal(self.doc(1, T0, drift=1.0))
        self.assertEqual(first["decision"], "accepted")
        dup = self.store.deliver_cal(self.doc(1, T0, drift=1.0))
        self.assertEqual(dup["decision"], "duplicate")
        clash = self.store.deliver_cal(self.doc(1, T0, drift=2.0))
        self.assertEqual(clash["decision"], "rejected")
        self.assertEqual(clash["reason"], "VERSION_CONFLICT")
        # The rejected version did not replace the accepted one.
        accepted = self.store.accepted_calibrations()
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0]["payload"]["coefs"][0], 101.0)

    def test_one_chain_per_device(self):
        self.store.deliver_cal(self.doc(1, T0, cal_id="CAL-A"))
        other = self.store.deliver_cal(self.doc(2, T1, cal_id="CAL-B"))
        self.assertEqual(other["decision"], "rejected")
        self.assertEqual(other["reason"], "CHAIN_CONFLICT")

    def test_gap_in_versions_is_allowed(self):
        self.store.deliver_cal(self.doc(1, T0))
        result = self.store.deliver_cal(self.doc(3, T1))
        self.assertEqual(result["decision"], "accepted")

    def test_semantic_identity_ignores_note_and_replaces(self):
        base = self.doc(1, T0)
        annotated = dict(base, note="reviewed", replaces=None)
        self.assertEqual(spec.semantic_identity(base), spec.semantic_identity(annotated))
        changed = dict(base, coefs=[base["coefs"][0] + 0.5] + base["coefs"][1:])
        self.assertNotEqual(spec.semantic_identity(base), spec.semantic_identity(changed))


class TestStaleRunNeverOverwrites(Base):
    def test_run_computed_with_old_version_is_discarded(self):
        self.store.set_publish_mode("SYNTH-P", PUBLISH_PROVISIONAL)
        self.ingest(1, T1)
        self.deliver(1, T0, drift=1.0)
        self.store.drain()
        v1 = self.latest("SYNTH-P:1")

        # Force a run to compute under v1, then deliver v2 just before it
        # commits.  The run must not publish its v1 result.
        self.store.enqueue_device("SYNTH-P", reason="test")
        correction = self.doc(2, T0, drift=6.0)

        def pre_commit(_result):
            self.store.deliver_cal(correction)

        result = self.store.run_once(pre_commit=pre_commit)
        self.assertEqual(result["outcome"], "stale")
        self.assertIsNone(result["revision"])
        self.assertEqual(self.store.history("SYNTH-P:1"), [v1])
        self.assertEqual(len(self.store.queued()), 1)

        # The reading is still queued and the next run publishes v2 only.
        self.store.drain()
        history = self.store.history("SYNTH-P:1")
        self.assertEqual([h["cal_version"] for h in history], [1, 2])
        self.assertAlmostEqual(history[1]["value_kpa"] - history[0]["value_kpa"],
                               5.0, places=6)
        self.assertEqual(history[1]["supersedes"], 1)
        self.assertEqual(history[1]["revision"], 2)

    def test_stale_run_leaves_no_mixed_version_publication(self):
        self.store.set_publish_mode("SYNTH-P", PUBLISH_PROVISIONAL)
        for seq in range(1, 6):
            self.ingest(seq, T1)
        self.deliver(1, T0, drift=0.0)
        self.store.drain()

        self.store.enqueue_device("SYNTH-P", reason="test")
        correction = self.doc(2, T0, drift=9.0)

        results = []
        first = self.store.run_once(pre_commit=lambda _r: self.store.deliver_cal(correction))
        results.append(first)
        results.extend(self.store.drain())
        stale = [r for r in results if r["outcome"] == "stale"]
        self.assertEqual(len(stale), 1)

        v2_sem = spec.semantic_identity(
            self.store.accepted_calibrations("SYNTH-P")[-1]["payload"])
        for seq in range(1, 6):
            latest = self.latest("SYNTH-P:%d" % seq)
            self.assertEqual(latest["sem_identity"], v2_sem)
            # No revision may mix arithmetic from two documents.
            for pub in self.store.history("SYNTH-P:%d" % seq):
                self.assertIn(pub["sem_identity"],
                              {spec.semantic_identity(c["payload"])
                               for c in self.store.accepted_calibrations("SYNTH-P")})

    def test_run_discarded_when_policy_changes_mid_flight(self):
        self.store.set_publish_mode("SYNTH-P", PUBLISH_PROVISIONAL)
        self.ingest(1, T1)
        self.store.drain()
        self.store.enqueue_device("SYNTH-P", reason="test")

        def pre_commit(_result):
            self.store.set_publish_mode("SYNTH-P", PUBLISH_HOLD, requeue=False)

        result = self.store.run_once(pre_commit=pre_commit)
        self.assertEqual(result["outcome"], "stale")

        # A policy switch is not itself a correction: the already published
        # value is not republished.  A later real correction is stamped with
        # the current policy.
        self.store.drain()
        self.assertEqual(len(self.store.history("SYNTH-P:1")), 1)
        self.deliver(1, T0)
        self.store.drain()
        latest = self.latest("SYNTH-P:1")
        self.assertEqual(latest["revision"], 2)
        self.assertEqual(latest["publish_mode"], "hold")


class TestCrashRecovery(Base):
    def test_interrupted_conversion_is_requeued_and_published_once(self):
        self.store.set_publish_mode("SYNTH-P", PUBLISH_PROVISIONAL)
        for seq in range(1, 4):
            self.ingest(seq, T1)
        self.deliver(1, T0)

        def crash(_result):
            raise SimulatedCrash("die before commit")

        with self.assertRaises(SimulatedCrash):
            self.store.run_once(pre_commit=crash)

        self.assertEqual(len(self.store.interrupted_runs()), 1)
        self.assertEqual(len(self.store.queued()), 3)  # one still claimed
        self.assertEqual(self.store.history("SYNTH-P:1") if
                         self.store.reading("SYNTH-P:1") else [], [])
        self.assertEqual(self.store.latest_publications(), [])

        recovered = self.store.resume()
        self.assertEqual(recovered, 1)
        self.assertEqual(self.store.interrupted_runs(), [])
        self.store.drain()

        for seq in range(1, 4):
            history = self.store.history("SYNTH-P:%d" % seq)
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["status"], "priced")
            self.assertEqual(history[0]["revision"], 1)

    def test_resume_after_restart(self):
        self.store.set_publish_mode("SYNTH-P", PUBLISH_PROVISIONAL)
        self.ingest(1, T1)
        self.deliver(1, T0)
        with self.assertRaises(SimulatedCrash):
            self.store.run_once(pre_commit=lambda _r: (_ for _ in ()).throw(
                SimulatedCrash("boom")))
        self.store.close()

        reopened = Store(self.path)
        try:
            self.assertEqual(len(reopened.interrupted_runs()), 1)
            self.assertEqual(reopened.resume(), 1)
            reopened.drain()
            history = reopened.history("SYNTH-P:1")
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["status"], "priced")
        finally:
            reopened.close()

    def test_a_failed_run_is_retried_then_abandoned(self):
        self.store.set_publish_mode("SYNTH-P", PUBLISH_PROVISIONAL)
        self.ingest(1, T1)
        self.deliver(1, T0)

        def explode(_result):
            raise RuntimeError("transient")

        for _ in range(3):
            with self.assertRaises(RuntimeError):
                self.store.run_once(pre_commit=explode)
        self.assertEqual(len(self.store.queued()), 1)
        self.store.drain()
        self.assertEqual(self.latest("SYNTH-P:1")["status"], "priced")


class TestAppendOnlyPublications(Base):
    def test_publications_cannot_be_updated_or_deleted(self):
        self.store.set_publish_mode("SYNTH-P", PUBLISH_PROVISIONAL)
        self.ingest(1, T1)
        self.deliver(1, T0)
        self.store.drain()
        conn = sqlite3.connect(self.path)
        try:
            with self.assertRaises(sqlite3.DatabaseError):
                conn.execute("UPDATE publications SET value_kpa = 0")
            conn.rollback()
            with self.assertRaises(sqlite3.DatabaseError):
                conn.execute("DELETE FROM publications")
            conn.rollback()
        finally:
            conn.close()
        self.assertEqual(len(self.store.history("SYNTH-P:1")), 1)

    def test_revision_chain_is_linear_and_names_its_predecessor(self):
        self.store.set_publish_mode("SYNTH-P", PUBLISH_PROVISIONAL)
        self.ingest(1, T1)
        for version, drift in [(1, 0.0), (2, 2.0), (3, 5.0)]:
            self.deliver(version, T0, drift=drift)
            self.store.drain()
        history = self.store.history("SYNTH-P:1")
        self.assertEqual([h["revision"] for h in history], [1, 2, 3])
        self.assertEqual([h["supersedes"] for h in history], [None, 1, 2])
        self.assertEqual([h["source"] for h in history],
                         ["initial", "supersede", "supersede"])
        values = [round(h["value_kpa"], 3) for h in history]
        self.assertEqual(values, sorted(values))

    def test_no_revision_when_nothing_changed(self):
        self.store.set_publish_mode("SYNTH-P", PUBLISH_PROVISIONAL)
        self.ingest(1, T1)
        self.deliver(1, T0)
        self.store.drain()
        # A calibration delivery that is not effective at this measurement
        # time triggers a run but must not fabricate a revision.
        self.store.deliver_cal(self.doc(2, "2027-01-01T00:00:00Z"))
        outcomes = [r["outcome"] for r in self.store.drain()]
        self.assertEqual(outcomes, ["unchanged"])
        self.assertEqual(len(self.store.history("SYNTH-P:1")), 1)

    def test_note_only_change_does_not_republish(self):
        self.store.set_publish_mode("SYNTH-P", PUBLISH_PROVISIONAL)
        self.ingest(1, T1)
        base = self.doc(1, T0)
        self.store.deliver_cal(base)
        self.store.drain()
        # Same arithmetic, different note -> duplicate path, no new revision.
        self.store.deliver_cal(dict(base, note="annotation"))
        self.assertEqual(self.store.deliver_cal(dict(base, note="annotation"))["decision"],
                         "duplicate")
        self.store.drain()
        self.assertEqual(len(self.store.history("SYNTH-P:1")), 1)


class TestPolicySwitch(Base):
    def test_switching_to_hold_does_not_strand_a_stale_value(self):
        self.store.set_publish_mode("SYNTH-P", PUBLISH_PROVISIONAL)
        self.ingest(1, T1)
        self.store.drain()
        self.assertEqual(self.latest("SYNTH-P:1")["status"], "unconverted")

        self.store.set_publish_mode("SYNTH-P", PUBLISH_HOLD)
        self.deliver(1, T0)
        self.store.drain()
        latest = self.latest("SYNTH-P:1")
        self.assertEqual(latest["status"], "priced")
        self.assertEqual(latest["revision"], 2)

    def test_switching_to_provisional_publishes_held_readings(self):
        self.store.set_publish_mode("SYNTH-P", PUBLISH_HOLD)
        self.ingest(1, T1)
        self.store.drain()
        self.assertIsNone(self.latest("SYNTH-P:1"))
        self.store.set_publish_mode("SYNTH-P", PUBLISH_PROVISIONAL)
        self.store.drain()
        self.assertEqual(self.latest("SYNTH-P:1")["status"], "unconverted")


class TestConcurrency(Base):
    def test_parallel_workers_claim_each_reading_once(self):
        self.store.set_publish_mode("SYNTH-P", PUBLISH_PROVISIONAL)
        for seq in range(1, 41):
            self.ingest(seq, T1)
        self.deliver(1, T0)

        errors = []
        barrier = threading.Barrier(4)

        def worker():
            store = Store(self.path)
            try:
                barrier.wait()
                for _ in range(200):
                    if store.run_once() is None:
                        break
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                store.close()

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertEqual(self.store.queued(), [])
        latest = self.store.latest_publications()
        self.assertEqual(len(latest), 40)
        self.assertEqual(sorted(p["seq"] for p in latest), list(range(1, 41)))
        self.assertTrue(all(p["status"] == "priced" for p in latest))
        self.assertEqual(self.store.publication_stats()["revisions"], 40)

    def test_parallel_delivery_and_conversion_never_mixes_versions(self):
        self.store.set_publish_mode("SYNTH-P", PUBLISH_PROVISIONAL)
        for seq in range(1, 21):
            self.ingest(seq, T1)
        self.deliver(1, T0)

        errors = []
        stop = threading.Event()

        def converter():
            store = Store(self.path)
            try:
                while not stop.is_set():
                    if store.run_once() is None:
                        time.sleep(0.002)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                store.close()

        def deliverer():
            store = Store(self.path)
            try:
                for version in range(2, 8):
                    store.deliver_cal(self.doc(version, T0, drift=float(version)))
                    time.sleep(0.003)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                store.close()

        converter_thread = threading.Thread(target=converter)
        converter_thread.start()
        deliverer_thread = threading.Thread(target=deliverer)
        deliverer_thread.start()
        deliverer_thread.join()
        time.sleep(0.05)
        stop.set()
        converter_thread.join()

        self.assertEqual(errors, [])
        self.store.drain()
        sem_to_version = {
            spec.semantic_identity(c["payload"]): c["payload"]["cal_version"]
            for c in self.store.accepted_calibrations("SYNTH-P")
        }
        for seq in range(1, 21):
            history = self.store.history("SYNTH-P:%d" % seq)
            self.assertTrue(history)
            versions = [sem_to_version.get(h["sem_identity"]) for h in history]
            self.assertTrue(all(v is not None for v in versions), versions)
            # Revisions are monotone in version: a run computed from an older
            # document never lands after one computed from a newer document.
            self.assertEqual(versions, sorted(versions), versions)
            self.assertEqual(versions[-1], 7)
            self.assertEqual([h["revision"] for h in history],
                             list(range(1, len(history) + 1)))


class TestTradeoffMeasurement(Base):
    def test_latency_and_revision_cost_are_measured_per_policy(self):
        clock = {"now": ms(T1)}
        store = Store(self.path, clock=lambda: clock["now"])
        try:
            store.set_publish_mode("SYNTH-P", PUBLISH_PROVISIONAL)
            store.ingest("SYNTH-P", 1, spec.raw_at("SYNTH-P", 1), T1)
            clock["now"] += 250
            store.drain()
            clock["now"] += 1000
            store.deliver_cal(self.doc(1, T0))
            store.drain()

            metrics = store.metrics()
            self.assertEqual(metrics["latency"]["SYNTH-P"]["n"], 1)
            self.assertEqual(metrics["latency"]["SYNTH-P"]["median_ms"], 250)
            self.assertEqual(metrics["revised_readings"], 1)
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
