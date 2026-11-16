"""Command-line driver for scripted demos and verification.

Examples
--------
    python3 -m app.cli init --db var/demo.db
    python3 -m app.cli seed --db var/demo.db --device SYNTH-P --count 8
    python3 -m app.cli process --db var/demo.db
    python3 -m app.cli deliver --db var/demo.db --device SYNTH-P --version 1 \\
        --valid-from 2026-01-01T00:00:00Z --drift 0.5
    python3 -m app.cli show --db var/demo.db

The commands share the same Store the HTTP service uses, so a scripted demo
and the page can act on the same database.
"""

import argparse
import json
import sys

from measure import spec
from measure.store import PUBLISH_MODES, Store


def cmd_init(args):
    store = Store(args.db)
    for device in spec.DEVICES:
        mode = args.mode or "hold"
        store.set_publish_mode(device, mode, requeue=False)
    print("initialised", args.db)
    store.close()


def cmd_seed(args):
    store = Store(args.db)
    start = args.start_seq if args.start_seq is not None else store.next_seq(args.device)
    for i in range(args.count):
        seq = start + i
        ts = spec.from_ms(spec.to_ms(args.start_ts) + i * args.step_ms)
        raw = spec.raw_at(args.device, seq)
        store.ingest(args.device, seq, raw, ts)
    print("seeded", args.count, "readings for", args.device)
    store.close()


def cmd_deliver(args):
    store = Store(args.db)
    doc = spec.calibrated_doc(
        args.device, args.cal_id, args.version, spec.to_ms(args.valid_from),
        drift_kpa=args.drift, scale=args.scale,
        validity=[int(x) for x in args.validity.split(",")] if args.validity else None,
        note=args.note,
    )
    result = store.deliver_cal(doc)
    print(json.dumps(result, indent=2))
    store.close()


def cmd_process(args):
    store = Store(args.db)
    if args.crash_at is not None:
        from measure.store import SimulatedCrash

        def hook(_result):
            if not args.crash_at:
                return
            raise SimulatedCrash("simulated crash after computation")

        crash_used = {"done": False}
        for _ in range(args.max_runs):
            hook_this = hook if not crash_used["done"] else None
            try:
                result = store.run_once(pre_commit=hook_this)
            except SimulatedCrash:
                crash_used["done"] = True
                print("crashed with a run in flight; use 'recover'")
                continue
            if result is None:
                break
            print(result["reading_id"], result["outcome"], result["target"]["status"] if result["target"] else None)
    else:
        for result in store.drain(max_runs=args.max_runs):
            print(result["reading_id"], result["outcome"],
                  result["target"]["status"] if result["target"] else None)
    store.close()


def cmd_recover(args):
    store = Store(args.db)
    print("interrupted runs:", store.resume())
    for result in store.drain():
        print(result["reading_id"], result["outcome"])
    store.close()


def cmd_show(args):
    store = Store(args.db)
    out = {
        "metrics": store.metrics(),
        "chains": {
            cid: [
                {"version": c["payload"]["cal_version"],
                 "valid_from": c["payload"]["valid_from"],
                 "digest": c["digest"][:12]}
                for c in chain
            ]
            for cid, chain in store.calibration_chains().items()
        },
        "readings": [
            {
                "id": r["id"],
                "raw": r["raw"],
                "device_ts": r["device_ts"],
                "history": [
                    {"revision": h["revision"], "status": h["status"],
                     "value_kpa": h["value_kpa"], "cal_version": h["cal_version"],
                     "supersedes": h["supersedes"], "reason": h["reason"]}
                    for h in store.history(r["id"])
                ],
            }
            for r in store.readings(limit=args.limit)
        ],
    }
    print(json.dumps(out, indent=2))
    store.close()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="app.cli", description=__doc__)
    parser.add_argument("--db", default="var/demo.db")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init")
    p.add_argument("--mode", choices=PUBLISH_MODES)
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("seed")
    p.add_argument("--device", required=True, choices=spec.DEVICES)
    p.add_argument("--count", type=int, default=6)
    p.add_argument("--start-seq", type=int)
    p.add_argument("--start-ts", default=spec.from_ms(0))
    p.add_argument("--step-ms", type=int, default=20000)
    p.set_defaults(func=cmd_seed)

    p = sub.add_parser("deliver")
    p.add_argument("--device", required=True, choices=spec.DEVICES)
    p.add_argument("--cal-id")
    p.add_argument("--version", type=int, required=True)
    p.add_argument("--valid-from", required=True)
    p.add_argument("--drift", type=float, default=0.0)
    p.add_argument("--scale", type=float, default=1.0)
    p.add_argument("--validity")
    p.add_argument("--note")
    p.set_defaults(func=cmd_deliver)

    p = sub.add_parser("process")
    p.add_argument("--max-runs", type=int, default=1000)
    p.add_argument("--crash-at", action="store_true")
    p.set_defaults(func=cmd_process)

    p = sub.add_parser("recover")
    p.set_defaults(func=cmd_recover)

    p = sub.add_parser("show")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_show)

    args = parser.parse_args(argv)
    if args.command == "deliver" and not args.cal_id:
        args.cal_id = "CAL-" + args.device.split("-")[-1]
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
