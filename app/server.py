"""Small HTTP service and JSON API for the synthetic receiver.

No framework: http.server with a deliberately tiny router.  The service holds
one Store per database file; SQLite connections are thread-local, and every
write is guarded by an explicit immediate transaction.
"""

import json
import os
import threading
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from measure import spec
from measure.store import PUBLISH_MODES, Conflict, SimulatedCrash, Store

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
DEFAULT_DB = os.environ.get("MEASURE_DB", os.path.join(os.getcwd(), "var", "receiver.db"))

_services = {}
_services_lock = threading.Lock()


class Service:
    def __init__(self, db_path):
        self.db_path = db_path
        self.store = Store(db_path)
        # Recover work a previous process left in flight before answering.
        self.store.resume()

    def info(self):
        return {
            "db": self.db_path,
            "devices": [
                {
                    "key": d,
                    "label": spec.PROFILES[d].label,
                    "span_kpa": [round(spec.PROFILES[d].value(spec.RAW_LO), 1),
                                 round(spec.PROFILES[d].value(spec.RAW_HI), 1)],
                    "validity": [spec.PROFILES[d].valid_lo, spec.PROFILES[d].valid_hi],
                    "z0": spec.PROFILES[d].z0,
                    "coefs": list(spec.PROFILES[d].a),
                    "mode": self.store.publish_mode(d),
                    "readings": self.store.reading_count(d),
                }
                for d in spec.DEVICES
            ],
            "publish_modes": list(PUBLISH_MODES),
        }


def get_service(db_path=None):
    """One service (and one Store) per database path, per process."""
    path = os.path.abspath(db_path or DEFAULT_DB)
    with _services_lock:
        if path not in _services:
            _services[path] = Service(path)
        return _services[path]


def drop_service(db_path=None):
    path = os.path.abspath(db_path or DEFAULT_DB)
    with _services_lock:
        service = _services.pop(path, None)
    if service is not None:
        service.store.close()


# -- payload helpers -------------------------------------------------------

def json_body(handler):
    length = int(handler.headers.get("Content-Length") or 0)
    if length > 2_000_000:
        raise ApiError(413, "request body too large")
    raw = handler.rfile.read(length) if length else b"{}"
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ApiError(400, "request body is not valid JSON")
    if not isinstance(body, dict):
        raise ApiError(400, "request body must be a JSON object")
    return body


class ApiError(Exception):
    def __init__(self, status, message, extra=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra or {}


def require(body, name, types=None):
    if name not in body:
        raise ApiError(400, "missing field: %s" % name)
    value = body[name]
    if types is not None and not isinstance(value, types):
        raise ApiError(400, "field %s has the wrong type" % name)
    return value


def parse_ts_input(value):
    if isinstance(value, str):
        if spec.parse_ts(value) is None:
            raise ApiError(400, "timestamp must be ISO-8601 with an offset, e.g. 2026-01-01T00:00:00Z")
        return spec.from_ms(spec.to_ms(value))
    ms = spec.parse_epoch(value)
    if ms is None:
        raise ApiError(400, "timestamp must be ISO-8601 or epoch milliseconds")
    return spec.from_ms(ms)


def reading_payload(store, reading, include_history=False):
    latest = store.latest_publication(reading["id"])
    history = store.history(reading["id"]) if include_history else None
    target = store.target(reading, has_history=latest is not None)
    revision_count = len(history) if history is not None else store.revision_count_for(reading["id"])
    out = {
        "id": reading["id"],
        "device": reading["device"],
        "seq": reading["seq"],
        "raw": reading["raw"],
        "device_ts": reading["device_ts"],
        "device_ts_ms": reading["device_ts_ms"],
        "received_at": reading["received_at"],
        "config": reading["config"],
        "latest": latest,
        "revisions": revision_count,
        "target_status": target["status"] if target else None,
        "target_version": target["cal_version"] if target else None,
        "mode": store.publish_mode(reading["device"]),
        "up_to_date": target is None or (latest is not None
                                         and store._same_as_latest(latest, target)),
    }
    if include_history:
        out["history"] = history
    return out


def comparison(store, reading):
    """Every accepted version, evaluated for this reading, plus the naive rule.

    ``latest_received`` is what a receiver that applies "the most recently
    delivered file" would produce -- included to make the difference visible,
    not because it is a valid answer.
    """
    device = reading["device"]
    device_ts_ms = reading["device_ts_ms"]
    accepted = store.accepted_calibrations(device)
    rows = []
    chosen = None
    produced = set()
    for cal in accepted:
        eff = spec.to_ms(cal["payload"]["valid_from"]) <= device_ts_ms
        evaluation = spec.evaluate(cal["payload"], reading["raw"])
        in_window = spec.in_validity(cal["payload"], reading["raw"])
        rows.append({
            "seq": cal["seq"],
            "cal_id": cal["payload"]["cal_id"],
            "cal_version": cal["payload"]["cal_version"],
            "valid_from": cal["payload"]["valid_from"],
            "validity": cal["payload"]["validity"],
            "received_at": cal["received_at"],
            "effective": eff,
            "in_validity": in_window,
            "value_kpa": evaluation["value_kpa"],
            "status": "priced" if evaluation["priced"] else "out_of_validity",
            "deviation_kpa": (
                spec.endorse(cal["payload"], reading["raw"]) if evaluation["priced"] else None
            ),
        })
        if eff and evaluation["priced"]:
            produced.add(round(evaluation["value_kpa"], 6))
    selection = store.selection(device, device_ts_ms)
    if selection["cal"] is not None:
        chosen = selection["cal"]["payload"]["cal_version"]
    naive = None
    if accepted:
        last = max(accepted, key=lambda c: (c["received_ms"], c["seq"]))
        evaluation = spec.evaluate(last["payload"], reading["raw"])
        naive = {
            "cal_version": last["payload"]["cal_version"],
            "received_at": last["received_at"],
            "value_kpa": evaluation["value_kpa"],
            "status": "priced" if evaluation["priced"] else "rejected",
            "matches_chosen": last["payload"]["cal_version"] == chosen,
        }
    return {
        "reading": {
            "id": reading["id"], "device": device, "seq": reading["seq"],
            "raw": reading["raw"], "device_ts": reading["device_ts"],
        },
        "chosen_version": chosen,
        "chosen_by": selection["chosen_by"],
        "effective_versions": [r["cal_version"] for r in rows if r["effective"]],
        "pending_versions": [r["cal_version"] for r in rows if not r["effective"]],
        "rows": rows,
        "naive_latest_received": naive,
        "distinct_effective_values": len(produced),
        "current": store.latest_publication(reading["id"]),
    }


# -- endpoints -------------------------------------------------------------

def api_bootstrap(service, body):
    store = service.store
    return {
        "info": service.info(),
        "summary": store.metrics(),
        "tradeoff": tradeoff(store),
        "deliveries": [
            {
                "seq": d["seq"], "decision": d["decision"], "reason": d["reason"],
                "detail": d["detail"], "cal_id": d["cal_id"], "cal_version": d["cal_version"],
                "device": d["device"], "received_at": d["received_at"],
            }
            for d in store.deliveries(limit=100)
        ],
        "chains": {
            cal_id: [
                {
                    "seq": c["seq"], "cal_version": c["payload"]["cal_version"],
                    "device": c["device"], "valid_from": c["payload"]["valid_from"],
                    "validity": c["payload"]["validity"],
                    "coefs": c["payload"]["coefs"],
                    "z0": c["payload"]["z0"],
                    "received_at": c["received_at"], "note": c["payload"].get("note"),
                }
                for c in chain
            ]
            for cal_id, chain in store.calibration_chains().items()
        },
        "queue": store.queued(),
        "readings": [reading_payload(store, r) for r in reversed(store.readings(limit=120))],
        "runs": store.recent_runs(limit=30),
        "events": store.events(limit=60),
    }


def api_reading(service, reading_id):
    store = service.store
    reading = store.reading(reading_id)
    if reading is None:
        raise ApiError(404, "no reading %s" % reading_id)
    payload = reading_payload(store, reading, include_history=True)
    payload["selection"] = store.selection(reading["device"], reading["device_ts_ms"])
    payload["target"] = store.target(reading, has_history=payload["latest"] is not None)
    payload["comparison"] = comparison(store, reading)
    return payload


def api_readings(service, query):
    device = query.get("device")
    readings = service.store.readings(device=device, limit=int(query.get("limit", 200)))
    return {"readings": [reading_payload(service.store, r) for r in reversed(readings)]}


def api_ingest(service, body):
    store = service.store
    device = require(body, "device", str)
    if device not in spec.PROFILES:
        raise ApiError(400, "unknown device %r" % device)
    ts_text = parse_ts_input(require(body, "device_ts"))
    config = body.get("config")
    if config is not None and not isinstance(config, dict):
        raise ApiError(400, "config must be a JSON object")

    if "raw" in body:
        raw = body["raw"]
        if not isinstance(raw, int) or isinstance(raw, bool):
            raise ApiError(400, "raw must be an integer")
    elif "kPa" in body:
        kpa = body["kPa"]
        if not isinstance(kpa, (int, float)) or isinstance(kpa, bool):
            raise ApiError(400, "kPa must be a number")
        try:
            raw = spec.PROFILES[device].raw_for(float(kpa))
        except ValueError as exc:
            raise ApiError(400, str(exc))
    else:
        raise ApiError(400, "provide either raw or kPa")

    config_snapshot = spec.PROFILES[device].config()
    if config is not None:
        if "temperature_c" in config and not isinstance(config["temperature_c"], (int, float)):
            raise ApiError(400, "config.temperature_c must be a number")
        config_snapshot.update(config)

    seq = body.get("seq")
    if seq is None:
        seq = store.next_seq(device)
    try:
        reading, created = store.ingest(device, seq, raw, ts_text, config_snapshot)
    except Conflict as exc:
        raise ApiError(409, str(exc))
    except ValueError as exc:
        raise ApiError(400, str(exc))
    return {"reading": reading_payload(store, reading, include_history=True), "created": created}


def api_ingest_bulk(service, body):
    """Send several device readings in one call (backfill / parallel flock)."""
    store = service.store
    device = require(body, "device", str)
    count = require(body, "count", int)
    if not (1 <= count <= 500):
        raise ApiError(400, "count must be between 1 and 500")
    start_seq = body.get("start_seq")
    if start_seq is None:
        start_seq = store.next_seq(device)
    start_ts = parse_ts_input(require(body, "start_ts"))
    step_ms = int(body.get("step_ms", 20_000))
    if not (0 <= step_ms <= 86_400_000):
        raise ApiError(400, "step_ms out of range")
    mode = body.get("value_mode", "kPa")
    if mode not in ("kPa", "raw"):
        raise ApiError(400, "value_mode must be kPa or raw")
    config = spec.PROFILES[device].config()
    if isinstance(body.get("config"), dict):
        config.update(body["config"])

    created, duplicates = [], 0
    for i in range(count):
        seq = start_seq + i
        ts_ms = spec.to_ms(start_ts) + i * step_ms
        if mode == "kPa":
            raw = spec.raw_at(device, seq)
        else:
            raw = (spec.PROFILES[device].z0 + (i * 7919)) % (spec.RAW_HI + 1)
        try:
            reading, is_new = store.ingest(device, seq, raw, spec.from_ms(ts_ms), config)
        except Conflict as exc:
            raise ApiError(409, str(exc))
        created.append({"id": reading["id"], "created": is_new})
        duplicates += 0 if is_new else 1
    return {"readings": created, "duplicates": duplicates,
            "start_seq": start_seq, "device": device}


def api_deliver_cal(service, body):
    document = body.get("document", body)
    if not isinstance(document, dict):
        raise ApiError(400, "document must be a JSON object")
    result = service.store.deliver_cal(document)
    return {"delivery": result}


def api_generate_cal(service, body):
    store = service.store
    device = require(body, "device", str)
    if device not in spec.PROFILES:
        raise ApiError(400, "unknown device %r" % device)
    cal_id = body.get("cal_id", "CAL-%s" % device.split("-")[-1])
    version = body.get("cal_version")
    if version is None:
        existing = [c["payload"]["cal_version"] for c in store.accepted_calibrations(device)]
        version = (max(existing) if existing else 0) + 1
    if not isinstance(version, int) or isinstance(version, bool):
        raise ApiError(400, "cal_version must be an integer")
    valid_from = parse_ts_input(require(body, "valid_from"))
    drift = float(body.get("drift_kpa", 0.0))
    scale = float(body.get("scale", 1.0))
    validity = body.get("validity")
    if validity is not None and not (isinstance(validity, list) and len(validity) == 2):
        raise ApiError(400, "validity must be [lo, hi]")
    doc = spec.calibrated_doc(
        device, cal_id, version, spec.to_ms(valid_from), drift_kpa=drift, scale=scale,
        validity=validity, replaces=body.get("replaces"), note=body.get("note"),
    )
    result = store.deliver_cal(doc)
    return {"document": doc, "delivery": result}


def api_mode(service, body):
    device = require(body, "device", str)
    mode = require(body, "mode", str)
    if mode not in PUBLISH_MODES:
        raise ApiError(400, "mode must be one of %s" % (PUBLISH_MODES,))
    try:
        service.store.set_publish_mode(device, mode)
    except ValueError as exc:
        raise ApiError(400, str(exc))
    return {"device": device, "mode": mode, "summary": service.store.metrics()}


def api_process(service, body):
    max_runs = int(body.get("max_runs", 50))
    max_runs = max(1, min(max_runs, 5000))
    crash = bool(body.get("crash"))
    if crash and not service.store.queued():
        # A crash demo needs work in flight; requeue everything if idle.
        service.store.enqueue_all()
    inject = body.get("inject_cal")
    injected = None

    results, error = [], None
    for i in range(max_runs):
        pre = None
        if i == 0 and isinstance(inject, dict):
            def pre(_result, doc=inject):
                nonlocal injected
                injected = service.store.deliver_cal(doc)
        elif i == 0 and crash:
            def pre(_result):
                raise SimulatedCrash("simulated process death before commit")
        try:
            result = service.store.run_once(pre_commit=pre)
        except SimulatedCrash as exc:
            error = {
                "crashed": True, "message": str(exc),
                "interrupted_runs": len(service.store.interrupted_runs()),
                "queue": len(service.store.queued()),
            }
            break
        if result is None:
            break
        results.append({
            "reading_id": result["reading_id"],
            "outcome": result["outcome"],
            "revision": result["revision"],
            "mode": result["mode"],
            "chosen_by": result["chosen_by"],
            "snapshot_version": result["snapshot_version"],
            "status": result["target"]["status"] if result["target"] else None,
            "cal_version": result["target"]["cal_version"] if result["target"] else None,
        })
    return {"runs": results, "error": error, "injected": injected,
            "summary": service.store.metrics(), "queue": service.store.queued()}


def api_recover(service, body):
    store = service.store
    interrupted = store.interrupted_runs()
    requeued = store.resume()
    drained = store.drain()
    return {
        "interrupted": [
            {"token": r["token"], "reading_id": r["reading_id"], "started_ms": r["started_ms"]}
            for r in interrupted
        ],
        "requeued": requeued,
        "runs": [{"reading_id": r["reading_id"], "outcome": r["outcome"],
                  "revision": r["revision"]} for r in drained],
        "summary": store.metrics(),
    }


def api_compare(service, query):
    reading_id = query.get("reading_id")
    if reading_id:
        reading = service.store.reading(reading_id)
        if reading is None:
            raise ApiError(404, "no reading %s" % reading_id)
        return comparison(service.store, reading)
    device = query.get("device")
    readings = service.store.readings(device=device, limit=50)
    return {"comparisons": [comparison(service.store, r) for r in readings]}


def api_feed(service, query):
    """The downstream-facing stream: append-only publication rows.

    Each row states the revision it supersedes, so a consumer can replace its
    view instead of discovering that a value silently changed.
    """
    since = int(query.get("since_id", 0) or 0)
    device = query.get("device")
    limit = max(1, min(int(query.get("limit", 200) or 200), 1000))
    sql = ("SELECT id, reading_id, revision, supersedes, source, status, device, seq, "
           "raw, device_ts, value_kpa, reason, cal_id, cal_version, publish_mode, "
           "sem_identity, created_at FROM publications WHERE id > ?")
    args = [since]
    if device:
        sql += " AND device = ?"
        args.append(device)
    sql += " ORDER BY id LIMIT ?"
    args.append(limit)
    rows = [dict(r) for r in service.store.conn().execute(sql, args)]
    latest = {}
    for row in rows:
        latest[row["reading_id"]] = row["revision"]
    return {
        "publications": rows,
        "next_since_id": rows[-1]["id"] if rows else since,
        "reading_revision": latest,
    }


def api_events(service, query):
    return {"events": service.store.events(limit=int(query.get("limit", 200))),
            "runs": service.store.recent_runs(limit=50),
            "queue": service.store.queued()}


def tradeoff(store):
    """Measured price of each publication policy, from actual publications.

    A cohort is grouped by the policy under which a reading was *first*
    published.  Delaying is measured as time from receipt to first
    publication; revising is measured as extra revisions and how long the
    final value took.
    """
    histories = store.all_histories()
    cohorts = {}
    for reading in store.readings(limit=100000):
        history = histories.get(reading["id"], [])
        if not history:
            continue
        first = history[0]
        final = history[-1]
        bucket = cohorts.setdefault(first["publish_mode"], {
            "readings": 0, "first_latencies": [], "final_latencies": [],
            "revised": 0, "unpriced": 0, "revisions": 0, "values": [],
        })
        bucket["readings"] += 1
        bucket["revisions"] += len(history)
        bucket["first_latencies"].append(max(0, first["created_ms"] - reading["received_ms"]))
        bucket["final_latencies"].append(max(0, final["created_ms"] - reading["received_ms"]))
        changed = len(history) > 1 and (
            final["value_kpa"] != first["value_kpa"]
            or final["status"] != first["status"]
        )
        if changed:
            bucket["revised"] += 1
        if final["status"] != "priced":
            bucket["unpriced"] += 1
        if final["value_kpa"] is not None:
            bucket["values"].append(final["value_kpa"])

    out = {}
    for mode, bucket in cohorts.items():
        def med(values):
            values = sorted(values)
            return values[len(values) // 2] if values else None
        out[mode] = {
            "readings": bucket["readings"],
            "median_first_ms": med(bucket["first_latencies"]),
            "median_final_ms": med(bucket["final_latencies"]),
            "revised": bucket["revised"],
            "revised_share": round(bucket["revised"] / bucket["readings"], 4),
            "unpriced": bucket["unpriced"],
            "revisions": bucket["revisions"],
            "revision_overhead": round(bucket["revisions"] / bucket["readings"] - 1, 4),
        }
    out["_unpublished_hold"] = sum(
        1 for reading in store.readings(limit=100000)
        if reading["id"] not in histories and store.publish_mode(reading["device"]) == "hold"
    )
    return out


# -- HTTP ------------------------------------------------------------------

ROUTES = [
    ("GET", "/api/bootstrap", lambda svc, body, q: api_bootstrap(svc, body)),
    ("GET", "/api/readings", lambda svc, body, q: api_readings(svc, q)),
    ("GET", "/api/readings/", lambda svc, body, q, rid: api_reading(svc, rid)),
    ("GET", "/api/compare", lambda svc, body, q: api_compare(svc, q)),
    ("GET", "/api/events", lambda svc, body, q: api_events(svc, q)),
    ("GET", "/api/feed", lambda svc, body, q: api_feed(svc, q)),
    ("POST", "/api/readings", lambda svc, body, q: api_ingest(svc, body)),
    ("POST", "/api/readings/bulk", lambda svc, body, q: api_ingest_bulk(svc, body)),
    ("POST", "/api/calibrations", lambda svc, body, q: api_deliver_cal(svc, body)),
    ("POST", "/api/calibrations/generate", lambda svc, body, q: api_generate_cal(svc, body)),
    ("POST", "/api/mode", lambda svc, body, q: api_mode(svc, body)),
    ("POST", "/api/process", lambda svc, body, q: api_process(svc, body)),
    ("POST", "/api/recover", lambda svc, body, q: api_recover(svc, body)),
]


class Handler(BaseHTTPRequestHandler):
    server_version = "MeasureReceiver/1.0"

    def log_message(self, fmt, *args):
        if os.environ.get("MEASURE_QUIET") == "1":
            return
        super().log_message(fmt, *args)

    # -- routing -----------------------------------------------------------

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = {k: v[-1] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        if path.startswith("/api/"):
            return self._api(method, path, query)
        if method == "GET":
            return self._static(path)
        self._json(404, {"error": {"message": "not found"}})

    def _api(self, method, path, query):
        service = self.server.service
        body = {}
        if method == "POST":
            try:
                body = json_body(self)
            except ApiError as exc:
                return self._error(exc)
        for route_method, prefix, handler in ROUTES:
            if route_method != method:
                continue
            try:
                if prefix.endswith("/") and path.startswith(prefix) and len(path) > len(prefix):
                    rid = urllib.parse.unquote(path[len(prefix):])
                    if "/" in rid:
                        continue
                    return self._json(200, handler(service, body, query, rid))
                if path == prefix:
                    return self._json(200, handler(service, body, query))
            except ApiError as exc:
                return self._error(exc)
            except Conflict as exc:
                return self._error(ApiError(409, str(exc)))
            except Exception as exc:  # pragma: no cover - defensive
                return self._unexpected(exc)
        self._json(404, {"error": {"message": "no such endpoint: %s" % path}})

    def _unexpected(self, exc):
        traceback.print_exc()
        self._json(500, {"error": {"message": "internal error: %s" % exc}})

    def _error(self, exc):
        payload = {"error": {"message": exc.message}}
        payload["error"].update(exc.extra)
        self._json(exc.status, payload)

    def _json(self, status, payload):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _static(self, path):
        if path in ("/", "/index.html"):
            name, kind = "index.html", "text/html; charset=utf-8"
        elif path == "/app.js":
            name, kind = "app.js", "application/javascript; charset=utf-8"
        elif path == "/style.css":
            name, kind = "style.css", "text/css; charset=utf-8"
        else:
            return self._json(404, {"error": {"message": "not found"}})
        full = os.path.join(WEB_DIR, name)
        try:
            with open(full, "rb") as fh:
                data = fh.read()
        except OSError:
            return self._json(404, {"error": {"message": "missing %s" % name}})
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


def serve(host="127.0.0.1", port=8787, db_path=None):
    service = get_service(db_path)
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    server.service = service
    return server, service


def main():  # pragma: no cover - manual entry point
    import argparse

    parser = argparse.ArgumentParser(description="synthetic calibration-version receiver")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--db", default=DEFAULT_DB)
    args = parser.parse_args()
    server, service = serve(args.host, args.port, args.db)
    print("receiver listening on http://%s:%d  (db: %s)" % (args.host, args.port, service.db_path))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
