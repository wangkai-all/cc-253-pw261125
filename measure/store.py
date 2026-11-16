"""SQLite store: raw readings, calibration versions, publications, runs.

Invariants the rest of the system relies on:

1.  Raw readings and the device configuration that arrived with them are
    immutable.  Re-delivery is idempotent; a conflicting re-delivery is
    refused.
2.  Calibration deliveries are append-only.  Accepted versions form one
    linear chain per device.  Late and out-of-order versions are allowed.
3.  Selection is by *measurement time*: among accepted versions whose
    ``valid_from`` is not after the reading's device timestamp, the highest
    ``cal_version`` wins.  A later version is not borrowed for an earlier
    reading, and "arrived most recently" is never a selection rule.
4.  A conversion job is claimed atomically before it is computed.  The run
    snapshots the selection, computes without a write lock, then commits only
    if the selection is unchanged.  A stale run writes nothing and returns its
    job to the queue.  A run therefore cannot publish a result derived from
    one version on top of a result derived from another.
5.  Publications are append-only.  A change adds a revision that names the
    revision it ``supersedes``.  The past is never edited in place.
"""

import json
import os
import sqlite3
import threading
import time
import uuid

from . import intake, schema, spec

PUBLISH_HOLD = "hold"
PUBLISH_PROVISIONAL = "provisional"
PUBLISH_MODES = (PUBLISH_HOLD, PUBLISH_PROVISIONAL)

STATUS_PRICED = "priced"
STATUS_UNCONVERTED = "unconverted"
STATUS_OUT_OF_VALIDITY = "out_of_validity"

MAX_ATTEMPTS = 8


class Conflict(Exception):
    """A write was refused because it would break an invariant."""


class SimulatedCrash(BaseException):
    """Raised by the crash-injection hook to mimic a hard process death."""


class Store:
    def __init__(self, path, clock=None):
        self.path = path
        self.clock = clock or (lambda: int(time.time() * 1000))
        self._local = threading.local()
        self._init_lock = threading.Lock()
        absolute = os.path.abspath(path)
        os.makedirs(os.path.dirname(absolute), exist_ok=True)
        with self._init_lock:
            conn = self._connect()
            try:
                conn.executescript(schema.SCHEMA)
                conn.execute(
                    "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
                    (str(schema.SCHEMA_VERSION),),
                )
                conn.commit()
            finally:
                conn.close()

    # -- plumbing ----------------------------------------------------------

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=30.0, isolation_level=None,
                               check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000")
        # WAL + NORMAL: commits survive process death, which is what the
        # crash demo exercises.  Only an OS/power failure can lose the last
        # transactions, and no test depends on that.
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def conn(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect()
            self._local.conn = conn
        return conn

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    def close(self):
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def _begin(self):
        conn = self.conn()
        conn.execute("BEGIN IMMEDIATE")
        return conn

    @staticmethod
    def _rollback(conn):
        if conn.in_transaction:
            conn.rollback()

    def now_ms(self):
        return int(self.clock())

    @staticmethod
    def _iso(ms):
        return spec.from_ms(ms)

    # -- settings ----------------------------------------------------------

    def set_publish_mode(self, device, mode, requeue=True):
        if mode not in PUBLISH_MODES:
            raise ValueError("publish_mode must be one of %s" % (PUBLISH_MODES,))
        if device not in spec.PROFILES:
            raise ValueError("unknown device %r" % (device,))
        conn = self._begin()
        try:
            conn.execute(
                "INSERT INTO settings(device, publish_mode) VALUES(?, ?) "
                "ON CONFLICT(device) DO UPDATE SET publish_mode = excluded.publish_mode",
                (device, mode),
            )
            self._event(conn, "publish_mode", device=device, detail={"mode": mode})
            conn.commit()
        except BaseException:
            self._rollback(conn)
            raise
        if requeue:
            # Switching to hold must not recompute values that were already
            # published; switching to provisional must publish held readings.
            self.enqueue_device(device, reason="publish_mode_changed",
                                only_unpublished=(mode == PUBLISH_HOLD))

    def publish_mode(self, device):
        row = self.conn().execute(
            "SELECT publish_mode FROM settings WHERE device = ?", (device,)
        ).fetchone()
        return row["publish_mode"] if row else PUBLISH_HOLD

    def publish_modes(self):
        return {d: self.publish_mode(d) for d in spec.DEVICES}

    # -- readings ----------------------------------------------------------

    def ingest(self, device, seq, raw, device_ts, config=None):
        """Store one raw reading.  Returns ``(reading, created)``."""
        if device not in spec.PROFILES:
            raise ValueError("unknown device %r" % (device,))
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
            raise ValueError("seq must be a positive integer")
        if not isinstance(raw, int) or isinstance(raw, bool) or not (
            spec.RAW_LO <= raw <= spec.RAW_HI
        ):
            raise ValueError("raw must be an ADC count in 0..16777215")
        if spec.parse_ts(device_ts) is None:
            raise ValueError("device_ts must be an ISO-8601 timestamp with an offset")
        if config is None:
            config = spec.PROFILES[device].config()
        if not isinstance(config, dict):
            raise ValueError("config must be a JSON object")

        reading_id = "%s:%d" % (device, seq)
        config_json = json.dumps(config, sort_keys=True, separators=(",", ":"))
        device_ts = spec.from_ms(spec.to_ms(device_ts))
        device_ts_ms = spec.to_ms(device_ts)
        ms = self.now_ms()

        conn = self._begin()
        try:
            existing = conn.execute(
                "SELECT * FROM readings WHERE id = ?", (reading_id,)
            ).fetchone()
            if existing is not None:
                same = (
                    existing["raw"] == raw
                    and existing["device_ts_ms"] == device_ts_ms
                    and existing["config_json"] == config_json
                )
                conn.rollback()
                if not same:
                    raise Conflict(
                        "%s exists with a different raw payload; readings are immutable"
                        % reading_id
                    )
                return self._reading_row(existing), False

            conn.execute(
                "INSERT INTO readings(id, device, seq, raw, device_ts, device_ts_ms, "
                "received_at, received_ms, config_json) VALUES(?,?,?,?,?,?,?,?,?)",
                (reading_id, device, seq, raw, device_ts, device_ts_ms,
                 self._iso(ms), ms, config_json),
            )
            conn.execute(
                "INSERT INTO jobs(reading_id, enqueued_ms) VALUES(?, ?)",
                (reading_id, ms),
            )
            self._event(conn, "ingest", device=device, reading_id=reading_id,
                        detail={"seq": seq, "raw": raw})
            conn.commit()
        except BaseException:
            self._rollback(conn)
            raise
        return self.reading(reading_id), True

    def reading(self, reading_id):
        row = self.conn().execute("SELECT * FROM readings WHERE id = ?", (reading_id,)).fetchone()
        return self._reading_row(row) if row else None

    def readings(self, device=None, limit=1000, offset=0):
        sql, args = "SELECT * FROM readings", []
        if device:
            sql += " WHERE device = ?"
            args.append(device)
        sql += " ORDER BY device, seq LIMIT ? OFFSET ?"
        args.extend([limit, offset])
        return [self._reading_row(r) for r in self.conn().execute(sql, args)]

    def reading_count(self, device=None):
        if device:
            return self.conn().execute(
                "SELECT COUNT(*) c FROM readings WHERE device = ?", (device,)
            ).fetchone()["c"]
        return self.conn().execute("SELECT COUNT(*) c FROM readings").fetchone()["c"]

    def next_seq(self, device):
        row = self.conn().execute(
            "SELECT MAX(seq) AS m FROM readings WHERE device = ?", (device,)
        ).fetchone()
        return (row["m"] or 0) + 1

    # -- calibration intake ------------------------------------------------

    def deliver_cal(self, body):
        """Record one calibration delivery.  Returns the delivery verdict."""
        parsed, _ = intake.parse_body(body)
        ms = self.now_ms()
        conn = self._begin()
        try:
            verdict = intake.examine(body, lambda p: self._chain_state(conn, p))
            payload = verdict.get("payload")
            conn.execute(
                "INSERT INTO calibrations(cal_id, cal_version, device, decision, reason, detail, "
                "payload_json, digest, received_at, received_ms) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    (payload or {}).get("cal_id") or _loose_cal_id(parsed),
                    (payload or {}).get("cal_version"),
                    (payload or {}).get("device"),
                    verdict["decision"], verdict["reason"], verdict["detail"],
                    json.dumps(payload, sort_keys=True) if payload else None,
                    verdict["digest"], self._iso(ms), ms,
                ),
            )
            seq = conn.execute("SELECT last_insert_rowid() AS s").fetchone()["s"]
            self._event(conn, "cal_delivery", device=(payload or {}).get("device"),
                        detail={"seq": seq, "decision": verdict["decision"],
                                "reason": verdict["reason"],
                                "cal_id": (payload or {}).get("cal_id"),
                                "cal_version": (payload or {}).get("cal_version")})
            conn.commit()
        except BaseException:
            self._rollback(conn)
            raise

        device = (payload or {}).get("device")
        result = {
            "seq": seq, "decision": verdict["decision"], "reason": verdict["reason"],
            "detail": verdict["detail"], "cal_id": (payload or {}).get("cal_id"),
            "cal_version": (payload or {}).get("cal_version"), "device": device,
        }
        if verdict["decision"] == intake.ACCEPTED and device:
            result["queued"] = self.enqueue_device(device, reason="calibration_delivery")
        return result

    def _chain_state(self, conn, payload):
        for row in conn.execute(
            "SELECT cal_id, device FROM calibrations WHERE decision = 'accepted' "
            "AND (cal_id = ? OR device = ?) GROUP BY cal_id",
            (payload["cal_id"], payload["device"]),
        ).fetchall():
            if row["cal_id"] == payload["cal_id"] and row["device"] != payload["device"]:
                return "cal_id_other_device"
            if row["device"] == payload["device"] and row["cal_id"] != payload["cal_id"]:
                return "device_other_chain"
        digest = spec.doc_digest(payload)
        sem = spec.semantic_identity(payload)
        for row in conn.execute(
            "SELECT cal_version, digest, payload_json FROM calibrations "
            "WHERE decision = 'accepted' AND cal_id = ?",
            (payload["cal_id"],),
        ).fetchall():
            if row["cal_version"] == payload["cal_version"]:
                if row["digest"] == digest:
                    return "duplicate"
                try:
                    stored = json.loads(row["payload_json"])
                except (TypeError, ValueError):
                    stored = None
                if stored is not None and spec.semantic_identity(stored) == sem:
                    # Same version, same arithmetic: only provenance/notes
                    # differ.  The accepted document stays authoritative.
                    return "duplicate"
                return "conflict"
        return "new"

    def accepted_calibrations(self, device=None, conn=None):
        conn = conn or self.conn()
        sql = "SELECT * FROM calibrations WHERE decision = 'accepted'"
        args = []
        if device:
            sql += " AND device = ?"
            args.append(device)
        sql += " ORDER BY cal_version"
        return [self._cal_row(r) for r in conn.execute(sql, args)]

    def deliveries(self, limit=300):
        return [
            self._cal_row(r) for r in self.conn().execute(
                "SELECT * FROM calibrations ORDER BY seq DESC LIMIT ?", (limit,)
            )
        ]

    def calibration_chains(self):
        chains = {}
        for cal in self.accepted_calibrations():
            chains.setdefault(cal["cal_id"], []).append(cal)
        for chain in chains.values():
            chain.sort(key=lambda c: c["cal_version"], reverse=True)
        return chains

    # -- selection ---------------------------------------------------------

    def selection(self, device, device_ts_ms, conn=None):
        """Choose the calibration that governs one measurement time.

        This is the only place the choice is made.  The result explains
        itself: which versions were effective, which start later, and why the
        winner won.
        """
        accepted = self.accepted_calibrations(device, conn)
        effective, pending = [], []
        for cal in accepted:
            if spec.to_ms(cal["payload"]["valid_from"]) <= device_ts_ms:
                effective.append(cal)
            else:
                pending.append(cal)
        if not effective:
            return {
                "cal": None,
                "chosen_by": "NO_VERSION_EFFECTIVE_AT_MEASUREMENT_TIME" if pending
                             else "NO_CALIBRATION",
                "effective": [],
                "pending": [_version_ref(c) for c in pending],
                "sem": None,
            }
        chosen = max(effective, key=lambda c: c["cal_version"])
        reason = "HIGHEST_EFFECTIVE_VERSION"
        if len(effective) > 1:
            reason += "_OF_%d" % len(effective)
        return {
            "cal": chosen,
            "chosen_by": reason,
            "effective": [_version_ref(c) for c in sorted(effective, key=lambda c: c["cal_version"])],
            "pending": [_version_ref(c) for c in sorted(pending, key=lambda c: c["cal_version"])],
            "sem": spec.semantic_identity(chosen["payload"]),
        }

    # -- desired publication ----------------------------------------------

    def target(self, reading, mode=None, has_history=False, conn=None):
        """What should currently be published for a reading, if anything.

        ``hold`` suppresses the *first* publication of an unpriced reading
        until a calibration is effective at its measurement time.  Once a
        reading has been published, a correction is always published -- a
        policy switch cannot strand a stale value downstream.
        """
        mode = mode or self.publish_mode(reading["device"])
        suppress = mode == PUBLISH_HOLD and not has_history
        sel = self.selection(reading["device"], reading["device_ts_ms"], conn)
        cal = sel["cal"]
        if cal is None:
            if suppress:
                return None
            return {
                "status": STATUS_UNCONVERTED, "value_kpa": None,
                "reason": "NO_CALIBRATION_EFFECTIVE_AT_MEASUREMENT_TIME"
                          if sel["pending"] else "NO_CALIBRATION_FOR_DEVICE",
                "cal_seq": None, "cal_id": None, "cal_version": None,
                "cal_doc_digest": None, "sem_identity": None,
                "publish_mode": mode, "chosen_by": sel["chosen_by"],
                "selection": sel,
            }
        evaluation = spec.evaluate(cal["payload"], reading["raw"])
        base = {
            "cal_seq": cal["seq"], "cal_id": cal["payload"]["cal_id"],
            "cal_version": cal["payload"]["cal_version"],
            "cal_doc_digest": cal["digest"], "sem_identity": sel["sem"],
            "publish_mode": mode, "chosen_by": sel["chosen_by"],
            "selection": sel,
        }
        if not evaluation["priced"]:
            if suppress:
                return None
            return dict(base, status=STATUS_OUT_OF_VALIDITY, value_kpa=None,
                        reason="RAW_OUTSIDE_CAL_VALIDITY")
        return dict(base, status=STATUS_PRICED, value_kpa=evaluation["value_kpa"],
                    reason="OK")

    # -- queue -------------------------------------------------------------

    def enqueue_device(self, device, reason="explicit", only_unpublished=False):
        """Queue readings of one device for (re)conversion.

        ``only_unpublished`` is used when switching to ``hold``: values that
        consumers already hold must not be recomputed merely because the
        release policy changed.
        """
        conn = self._begin()
        try:
            ms = self.now_ms()
            sql = "SELECT id FROM readings WHERE device = ?"
            if only_unpublished:
                sql += " AND id NOT IN (SELECT DISTINCT reading_id FROM publications)"
            rows = conn.execute(sql, (device,)).fetchall()
            for row in rows:
                conn.execute(
                    "INSERT INTO jobs(reading_id, enqueued_ms) VALUES(?, ?) "
                    "ON CONFLICT(reading_id) DO UPDATE SET enqueued_ms = excluded.enqueued_ms, "
                    "claimed_token = NULL, claimed_ms = NULL",
                    (row["id"], ms),
                )
            self._event(conn, "enqueue", device=device,
                        detail={"reason": reason, "readings": len(rows)})
            conn.commit()
        except BaseException:
            self._rollback(conn)
            raise
        return len(rows)

    def enqueue_all(self):
        return sum(self.enqueue_device(d, reason="manual") for d in spec.DEVICES)

    def queued(self, device=None):
        sql = ("SELECT j.reading_id, j.enqueued_ms, j.attempts, j.claimed_token, "
               "r.device, r.seq FROM jobs j JOIN readings r ON r.id = j.reading_id")
        args = []
        if device:
            sql += " WHERE r.device = ?"
            args.append(device)
        sql += " ORDER BY j.enqueued_ms, r.device, r.seq"
        return [dict(r) for r in self.conn().execute(sql, args)]

    # -- conversion runs ---------------------------------------------------

    def run_once(self, token=None, pre_commit=None):
        """Claim and process one queued reading.  Returns a result dict or None.

        A run is: claim -> snapshot -> compute (no write lock) -> conditional
        commit.  ``pre_commit(result)`` runs after the computation and before
        the conditional commit; tests and the crash demo use it to interrupt
        the process at the most dangerous point.
        """
        token = token or uuid.uuid4().hex
        claimed = self._claim(token)
        if claimed is None:
            return None
        return self._run(claimed, token, pre_commit)

    def _claim(self, token):
        conn = self._begin()
        try:
            row = conn.execute(
                "SELECT j.reading_id FROM jobs j JOIN readings r ON r.id = j.reading_id "
                "WHERE j.claimed_token IS NULL "
                "ORDER BY j.enqueued_ms, r.device, r.seq LIMIT 1"
            ).fetchone()
            if row is None:
                conn.rollback()
                return None
            updated = conn.execute(
                "UPDATE jobs SET claimed_token = ?, claimed_ms = ?, attempts = attempts + 1 "
                "WHERE reading_id = ? AND claimed_token IS NULL",
                (token, self.now_ms(), row["reading_id"]),
            ).rowcount
            if updated != 1:
                conn.rollback()
                return None
            conn.commit()
        except BaseException:
            self._rollback(conn)
            raise
        return self.reading(row["reading_id"])

    def _run(self, reading, token, pre_commit=None):
        started = self.now_ms()
        selection = self.selection(reading["device"], reading["device_ts_ms"])
        snapshot_sem = selection["sem"]
        mode = self.publish_mode(reading["device"])
        target = self.target(reading, mode, has_history=self._latest_for(reading["id"]) is not None)

        conn = self._begin()
        try:
            conn.execute(
                "INSERT INTO runs(token, reading_id, device, started_ms, state, "
                "snapshot_cal_seq, snapshot_sem, snapshot_chosen_by, snapshot_mode) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (token, reading["id"], reading["device"], started, "running",
                 selection["cal"]["seq"] if selection["cal"] else None,
                 snapshot_sem, selection["chosen_by"], mode),
            )
            conn.commit()
        except BaseException:
            self._rollback(conn)
            raise

        result = {
            "token": token, "reading_id": reading["id"], "device": reading["device"],
            "mode": mode, "chosen_by": selection["chosen_by"],
            "snapshot_version": selection["cal"]["payload"]["cal_version"]
                                if selection["cal"] else None,
            "target": target, "outcome": None, "revision": None,
        }

        try:
            if pre_commit is not None:
                pre_commit(result)
            conn = self._begin()
            try:
                now_sel = self.selection(reading["device"], reading["device_ts_ms"])
                now_mode = self.publish_mode(reading["device"])
                if now_sel["sem"] != snapshot_sem or now_mode != mode:
                    outcome, revision = "stale", None
                    # Hand the job back; the next run uses the new selection.
                    # Only drop the claim if it is still ours.
                    conn.execute(
                        "UPDATE jobs SET claimed_token = NULL, claimed_ms = NULL, "
                        "enqueued_ms = ? WHERE reading_id = ? AND claimed_token = ?",
                        (self.now_ms(), reading["id"], token),
                    )
                else:
                    latest = self._latest_for(reading["id"], conn)
                    if target is None:
                        outcome, revision = "held", None
                        conn.execute("DELETE FROM jobs WHERE reading_id = ?", (reading["id"],))
                    elif self._same_as_latest(latest, target):
                        outcome, revision = "unchanged", latest["revision"]
                        conn.execute("DELETE FROM jobs WHERE reading_id = ?", (reading["id"],))
                    else:
                        revision = (latest["revision"] + 1) if latest else 1
                        supersedes = latest["revision"] if latest else None
                        outcome = "published"
                        self._insert_publication(conn, reading, revision, target,
                                                 supersedes, mode)
                        conn.execute("DELETE FROM jobs WHERE reading_id = ?", (reading["id"],))
                conn.execute(
                    "UPDATE runs SET state='finished', finished_ms=?, outcome=?, revision=? "
                    "WHERE token=?", (self.now_ms(), outcome, revision, token),
                )
                self._event(
                    conn, "convert", device=reading["device"], reading_id=reading["id"],
                    detail={"outcome": outcome, "mode": mode,
                            "cal_version": target["cal_version"] if target else None,
                            "chosen_by": now_sel["chosen_by"]},
                )
                conn.commit()
            except BaseException:
                self._rollback(conn)
                raise
            result["outcome"] = outcome
            result["revision"] = revision
            return result
        except SimulatedCrash:
            # A hard kill: the job stays claimed and the run stays 'running'
            # until recovery runs, exactly like a dead worker process.
            raise
        except BaseException as exc:
            conn = self._begin()
            try:
                conn.execute(
                    "UPDATE runs SET state='failed', finished_ms=?, outcome=? WHERE token=?",
                    (self.now_ms(), type(exc).__name__, token),
                )
                job = conn.execute(
                    "SELECT attempts FROM jobs WHERE reading_id = ?", (reading["id"],)
                ).fetchone()
                if job is not None and job["attempts"] >= MAX_ATTEMPTS:
                    conn.execute("DELETE FROM jobs WHERE reading_id = ?", (reading["id"],))
                    self._event(conn, "job_abandoned", device=reading["device"],
                                reading_id=reading["id"],
                                detail={"attempts": job["attempts"], "error": type(exc).__name__})
                else:
                    conn.execute(
                        "UPDATE jobs SET claimed_token = NULL, claimed_ms = NULL "
                        "WHERE reading_id = ? AND claimed_token = ?", (reading["id"], token),
                    )
                conn.commit()
            except BaseException:
                self._rollback(conn)
            raise

    def drain(self, max_runs=5000):
        results = []
        for _ in range(max_runs):
            result = self.run_once()
            if result is None:
                break
            results.append(result)
        return results

    def _latest_for(self, reading_id, conn=None):
        conn = conn or self.conn()
        row = conn.execute(
            "SELECT * FROM publications WHERE reading_id = ? ORDER BY revision DESC LIMIT 1",
            (reading_id,),
        ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _same_as_latest(latest, target):
        """True when the target adds nothing to what is already published.

        The publication policy is metadata about how the value was released;
        switching it does not by itself justify a new revision.
        """
        if latest is None:
            return False
        return (
            latest["status"] == target["status"]
            and latest["sem_identity"] == target["sem_identity"]
            and latest["reason"] == target["reason"]
            and latest["cal_version"] == target["cal_version"]
            and _same_float(latest["value_kpa"], target["value_kpa"])
        )

    def _insert_publication(self, conn, reading, revision, target, supersedes, mode):
        ms = self.now_ms()
        conn.execute(
            "INSERT INTO publications(reading_id, revision, status, device, seq, raw, "
            "device_ts, device_ts_ms, value_kpa, reason, chosen_by, cal_seq, cal_id, "
            "cal_version, cal_doc_digest, sem_identity, publish_mode, source, supersedes, "
            "created_at, created_ms) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                reading["id"], revision, target["status"], reading["device"],
                reading["seq"], reading["raw"], reading["device_ts"],
                reading["device_ts_ms"], target["value_kpa"], target["reason"],
                target["chosen_by"], target["cal_seq"], target["cal_id"],
                target["cal_version"], target["cal_doc_digest"],
                target["sem_identity"], mode,
                "supersede" if supersedes else "initial", supersedes,
                self._iso(ms), ms,
            ),
        )

    # -- publications ------------------------------------------------------

    def latest_publications(self, device=None):
        sql = (
            "SELECT p.* FROM publications p JOIN ("
            " SELECT reading_id, MAX(revision) AS rev FROM publications GROUP BY reading_id"
            ") m ON m.reading_id = p.reading_id AND m.rev = p.revision"
        )
        args = []
        if device:
            sql += " WHERE p.device = ?"
            args.append(device)
        sql += " ORDER BY p.device, p.seq"
        return [dict(r) for r in self.conn().execute(sql, args)]

    def latest_publication(self, reading_id):
        return self._latest_for(reading_id)

    def history(self, reading_id):
        return [
            dict(r) for r in self.conn().execute(
                "SELECT * FROM publications WHERE reading_id = ? ORDER BY revision",
                (reading_id,),
            )
        ]

    def revision_count_for(self, reading_id):
        return self.conn().execute(
            "SELECT COUNT(*) c FROM publications WHERE reading_id = ?", (reading_id,)
        ).fetchone()["c"]

    def all_histories(self):
        """Every publication grouped by reading, oldest revision first."""
        grouped = {}
        for row in self.conn().execute(
            "SELECT * FROM publications ORDER BY reading_id, revision"
        ):
            grouped.setdefault(row["reading_id"], []).append(dict(row))
        return grouped

    def publication_stats(self):
        row = self.conn().execute(
            "SELECT COUNT(*) AS revisions, COUNT(DISTINCT reading_id) AS readings FROM publications"
        ).fetchone()
        revised = self.conn().execute(
            "SELECT COUNT(*) c FROM (SELECT reading_id FROM publications "
            "GROUP BY reading_id HAVING MAX(revision) > 1)"
        ).fetchone()["c"]
        return {"revisions": row["revisions"],
                "published_readings": row["readings"],
                "revised_readings": revised}

    # -- recovery ----------------------------------------------------------

    def interrupted_runs(self):
        return [
            dict(r) for r in self.conn().execute(
                "SELECT * FROM runs WHERE state = 'running' ORDER BY started_ms"
            )
        ]

    def recent_runs(self, limit=100):
        return [
            dict(r) for r in self.conn().execute(
                "SELECT * FROM runs ORDER BY started_ms DESC LIMIT ?", (limit,)
            )
        ]

    def resume(self):
        """Recover work left behind by a dead conversion process.

        A run row is committed before computation starts and the job stays
        claimed while it runs, so a hard kill leaves a ``running`` row and a
        claimed job.  The publication was never committed (publication,
        completion and job deletion share one transaction), so recovery marks
        the run interrupted and releases its job.
        """
        conn = self._begin()
        try:
            rows = conn.execute(
                "SELECT token, reading_id FROM runs WHERE state='running'"
            ).fetchall()
            ms = self.now_ms()
            for row in rows:
                conn.execute(
                    "UPDATE runs SET state='interrupted', finished_ms=?, "
                    "outcome='process_interrupted' WHERE token=?", (ms, row["token"]),
                )
                conn.execute(
                    "UPDATE jobs SET claimed_token = NULL, claimed_ms = NULL, enqueued_ms = ? "
                    "WHERE reading_id = ?", (ms, row["reading_id"]),
                )
            if rows:
                self._event(conn, "resume", detail={"interrupted_runs": len(rows)})
            conn.commit()
        except BaseException:
            self._rollback(conn)
            raise
        return len(rows)

    # -- reporting ---------------------------------------------------------

    def metrics(self):
        pubs = self.latest_publications()
        by_status = {}
        for p in pubs:
            by_status[p["status"]] = by_status.get(p["status"], 0) + 1
        stats = self.publication_stats()
        queue = self.queued()
        latency = {}
        for p in pubs:
            reading = self.reading(p["reading_id"])
            first = self.conn().execute(
                "SELECT created_ms FROM publications WHERE reading_id = ? ORDER BY revision LIMIT 1",
                (p["reading_id"],),
            ).fetchone()
            if not reading or not first:
                continue
            latency.setdefault(p["device"], []).append(
                max(0, first["created_ms"] - reading["received_ms"])
            )
        latency_out = {}
        for device, values in latency.items():
            values.sort()
            latency_out[device] = {"n": len(values), "median_ms": values[len(values) // 2],
                                   "max_ms": values[-1]}
        pending = []
        held = []
        published = {p["reading_id"]: p for p in pubs}
        for reading in self.readings(limit=10000):
            latest = published.get(reading["id"]) or self._latest_for(reading["id"])
            target = self.target(reading, has_history=latest is not None)
            if target is None:
                if latest is None:
                    held.append(reading["id"])
                continue
            if latest is None or not self._same_as_latest(latest, target):
                pending.append(reading["id"])
        return {
            "readings": self.reading_count(),
            "queued_jobs": len(queue),
            "claimed_jobs": sum(1 for q in queue if q["claimed_token"]),
            "revisions": stats["revisions"],
            "published_readings": stats["published_readings"],
            "revised_readings": stats["revised_readings"],
            "by_status": by_status,
            "publish_modes": self.publish_modes(),
            "latency": latency_out,
            "pending_readings": len(pending),
            "held_readings": len(held),
            "needs_attention": (pending + held)[:50],
            "interrupted_runs": len(self.interrupted_runs()),
        }

    def events(self, limit=200):
        return [
            dict(r) for r in self.conn().execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
            )
        ]

    def _event(self, conn, kind, device=None, reading_id=None, detail=None):
        conn.execute(
            "INSERT INTO events(ms, kind, device, reading_id, detail_json) VALUES(?,?,?,?,?)",
            (self.now_ms(), kind, device, reading_id,
             json.dumps(detail, sort_keys=True) if detail else None),
        )

    # -- snapshots ---------------------------------------------------------

    def snapshot(self):
        """Portable copy of the inputs a replay needs (readings + calibrations)."""
        readings = [
            {"device": r["device"], "seq": r["seq"], "raw": r["raw"],
             "device_ts": r["device_ts"], "config": r["config"],
             "received_ms": r["received_ms"]}
            for r in self.readings(limit=100000)
        ]
        cals = [
            {"payload": d["payload"], "received_ms": d["received_ms"]}
            for d in self.deliveries(limit=100000)
            if d["decision"] == intake.ACCEPTED
        ]
        return {"readings": readings, "calibrations": cals,
                "publish_modes": self.publish_modes()}

    # -- row mapping -------------------------------------------------------

    @staticmethod
    def _reading_row(row):
        d = dict(row)
        d["config"] = json.loads(d.pop("config_json"))
        return d

    @staticmethod
    def _cal_row(row):
        d = dict(row)
        d["payload"] = json.loads(d.pop("payload_json")) if d["payload_json"] else None
        return d


def _version_ref(cal):
    return {
        "seq": cal["seq"],
        "cal_id": cal["payload"]["cal_id"],
        "cal_version": cal["payload"]["cal_version"],
        "valid_from": cal["payload"]["valid_from"],
        "validity": cal["payload"]["validity"],
        "digest": cal["digest"][:12],
    }


def _same_float(a, b):
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) < 1e-9


def _loose_cal_id(doc):
    if isinstance(doc, dict) and isinstance(doc.get("cal_id"), str):
        return doc["cal_id"][:64]
    return None
