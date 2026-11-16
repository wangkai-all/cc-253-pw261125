"""Calibration delivery intake.

A calibration document is a versioned description of one transmitter's
response.  Validation is pure.  Classification against accepted history is
what keeps "which version governs this reading?" answerable:

* identical ``(cal_id, cal_version, document)`` already accepted -> duplicate
* same ``(cal_id, cal_version)`` but different arithmetic       -> conflict
* a ``cal_id`` bound to one device, or a device with two chains -> conflict
* a new version number                                          -> accepted

Versions may arrive out of order.  Acceptance does not mean "use me": it means
the document is well-formed and does not contradict accepted history.
Selection happens later, per measurement time.
"""

import json

from . import spec

ACCEPTED = "accepted"
DUPLICATE = "duplicate"
REJECTED = "rejected"


def parse_body(body):
    """Return (object, error).  Accepts bytes, str, or an already-parsed object."""
    if isinstance(body, (bytes, bytearray)):
        try:
            body = body.decode("utf-8")
        except UnicodeDecodeError:
            return None, "body is not UTF-8"
    if not isinstance(body, str):
        return body, None
    try:
        return json.loads(body), None
    except ValueError as exc:
        return None, "body is not valid JSON: %s" % exc


def _reason_for(errors):
    head = errors[0]
    if head.startswith("payload is not"):
        return "BAD_SHAPE"
    if head.startswith("unknown field"):
        return "UNKNOWN_FIELD"
    if head.startswith("missing field"):
        return "MISSING_FIELD"
    if head.startswith("coefs must be monotonic"):
        return "NON_MONOTONIC"
    return "BAD_FIELD"


def examine(body, chain_state):
    """Classify one delivery.

    ``chain_state(payload)`` is supplied by the store and returns ``"new"``,
    ``"duplicate"``, ``"conflict"``, ``"cal_id_other_device"`` or
    ``"device_other_chain"`` for a valid document, based only on previously
    accepted deliveries.  It is called inside the same write transaction as
    the insert.
    """
    doc, err = parse_body(body)
    if err:
        return _rejected("BAD_JSON", err)

    payload, errors = spec.validate_cal(doc)
    if errors:
        return _rejected(_reason_for(errors), "; ".join(errors))

    state = chain_state(payload)
    if state == "duplicate":
        return {
            "decision": DUPLICATE,
            "reason": "DUPLICATE",
            "detail": "an identical document was already accepted",
            "payload": payload,
            "digest": spec.doc_digest(payload),
        }
    if state == "conflict":
        return _rejected(
            "VERSION_CONFLICT",
            "cal_id %s version %s already exists with different content"
            % (payload["cal_id"], payload["cal_version"]),
            payload=payload,
        )
    if state == "cal_id_other_device":
        return _rejected(
            "CAL_ID_CONFLICT",
            "cal_id %s is already bound to another device" % payload["cal_id"],
            payload=payload,
        )
    if state == "device_other_chain":
        return _rejected(
            "CHAIN_CONFLICT",
            "device %s already has a calibration chain with another cal_id"
            % payload["device"],
            payload=payload,
        )
    return {
        "decision": ACCEPTED,
        "reason": None,
        "detail": None,
        "payload": payload,
        "digest": spec.doc_digest(payload),
    }


def _rejected(reason, detail, payload=None):
    return {
        "decision": REJECTED,
        "reason": reason,
        "detail": detail,
        "payload": payload,
        "digest": spec.doc_digest(payload) if payload else None,
    }
