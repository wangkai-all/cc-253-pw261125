"""Synthetic measurement model, calibration arithmetic and document validation.

All data is synthetic.  A device reports 24-bit ADC counts.  A calibration
document says how a raw count becomes kilopascals:

    u   = (raw - z0) / 1_000_000
    kPa = a0 + a1*u + a2*u^2 + a3*u^3

A document also declares:

* ``validity``   the raw-count window in which it may price a reading;
* ``valid_from`` the measurement timestamp from which it is effective;
* ``device``     the transmitter it describes.

Effectiveness is always judged on measurement time, never arrival time.
"""

import hashlib
import json
import math
import re
from datetime import datetime, timedelta, timezone

RAW_LO = 0
RAW_HI = 16777215
RAW_SPAN = 1_000_000.0
MAX_VERSION = 100000
CAL_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class Profile:
    """The true, fixed response of one synthetic transmitter."""

    def __init__(self, key, z0, coefs, validity, label):
        self.key = key
        self.z0 = int(z0)
        self.a = tuple(float(c) for c in coefs)
        self.valid_lo, self.valid_hi = int(validity[0]), int(validity[1])
        self.label = label

    def value(self, raw):
        a0, a1, a2, a3 = self.a
        u = (raw - self.z0) / RAW_SPAN
        return a0 + a1 * u + a2 * u * u + a3 * u * u * u

    def raw_for(self, kpa):
        lo, hi = float(RAW_LO), float(RAW_HI)
        f_lo = self.value(lo) - kpa
        f_hi = self.value(hi) - kpa
        if not (min(f_lo, f_hi) <= 0 <= max(f_lo, f_hi)):
            raise ValueError("value %r is outside the span of %s" % (kpa, self.key))
        for _ in range(120):
            mid = (lo + hi) / 2.0
            f_mid = self.value(mid) - kpa
            if f_mid == 0:
                return int(round(mid))
            if (f_mid > 0) == (f_lo > 0):
                lo = mid
            else:
                hi = mid
        return int(round((lo + hi) / 2.0))

    def config(self):
        return {
            "serial": self.key + "-0042",
            "firmware": "2.4.1",
            "adc_bits": 24,
            "gain": "4",
            "sample_period_ms": 1000,
        }


PROFILES = {
    "SYNTH-P": Profile(
        "SYNTH-P", 524288, (100.0, 30.0, 0.5, 0.01), (50000, 16500000),
        "synthetic gauge, 100-570 kPa band",
    ),
    "SYNTH-Q": Profile(
        "SYNTH-Q", 480000, (90.0, 32.0, -0.6, 0.015), (50000, 16000000),
        "synthetic gauge, 120-460 kPa band",
    ),
}
DEVICES = tuple(sorted(PROFILES))


def is_monotonic(coefs, z0):
    """True when the cubic's derivative has no root inside the ADC span."""
    _, a1, a2, a3 = coefs
    u_lo = (RAW_LO - z0) / RAW_SPAN
    u_hi = (RAW_HI - z0) / RAW_SPAN
    qa, qb, qc = 3.0 * a3, 2.0 * a2, a1
    roots = []
    if abs(qa) < 1e-15:
        if abs(qb) > 1e-15:
            roots = [-qc / qb]
    else:
        disc = qb * qb - 4 * qa * qc
        if disc >= 0:
            r = math.sqrt(disc)
            roots = [(-qb - r) / (2 * qa), (-qb + r) / (2 * qa)]
    return all(not (u_lo < r < u_hi) for r in roots)


for _p in PROFILES.values():
    assert is_monotonic(_p.a, _p.z0), _p.key
    assert _p.valid_lo < _p.valid_hi


# -- timestamps ------------------------------------------------------------

def parse_ts(text):
    if not isinstance(text, str) or not (1 <= len(text) <= 40):
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt


def to_ms(text):
    dt = parse_ts(text)
    if dt is None:
        raise ValueError("not an ISO-8601 timestamp with an offset: %r" % (text,))
    return int(round(dt.timestamp() * 1000))


def from_ms(ms):
    dt = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (int(ms) % 1000)


def parse_epoch(text):
    """Parse an epoch-millisecond input, or None."""
    if isinstance(text, bool):
        return None
    if isinstance(text, int):
        return text
    if isinstance(text, str) and text.strip().isdigit() and len(text.strip()) <= 18:
        return int(text.strip())
    return None


# -- calibration documents -------------------------------------------------

CAL_FIELDS = frozenset({
    "cal_id", "cal_version", "device", "z0", "coefs", "validity",
    "valid_from", "replaces", "note",
})
CAL_REQUIRED = ("cal_id", "cal_version", "device", "z0", "coefs", "validity", "valid_from")


def validate_cal(doc):
    """Return ``(canonical_payload, errors)``.  Never raises for bad input."""
    errors = []
    if not isinstance(doc, dict):
        return None, ["payload is not a JSON object"]

    unknown = sorted(set(doc) - CAL_FIELDS)
    if unknown:
        errors.append("unknown field(s): " + ", ".join(unknown))
    missing = [f for f in CAL_REQUIRED if f not in doc]
    if missing:
        errors.append("missing field(s): " + ", ".join(missing))
    if errors:
        return None, errors

    if not isinstance(doc["cal_id"], str) or not CAL_ID_RE.match(doc["cal_id"]):
        errors.append("cal_id must match %s" % CAL_ID_RE.pattern)
    version = doc["cal_version"]
    if not _is_int(version) or not (1 <= version <= MAX_VERSION):
        errors.append("cal_version must be an integer in 1..%d" % MAX_VERSION)
    if doc["device"] not in PROFILES:
        errors.append("device must be one of " + ", ".join(DEVICES))

    z0 = doc["z0"]
    if not _is_int(z0) or not (RAW_LO <= z0 <= RAW_HI):
        errors.append("z0 must be an integer ADC count in 0..16777215")
    coefs = doc["coefs"]
    if (not isinstance(coefs, list) or len(coefs) != 4
            or not all(_is_num(c) and abs(c) <= 10000 for c in coefs)):
        errors.append("coefs must be four finite numbers within +/-10000")
    validity = doc["validity"]
    if (not isinstance(validity, list) or len(validity) != 2
            or not all(_is_int(v) for v in validity)
            or not (RAW_LO <= validity[0] < validity[1] <= RAW_HI)):
        errors.append("validity must be [lo, hi] with 0 <= lo < hi <= 16777215")
    if parse_ts(doc["valid_from"]) is None:
        errors.append("valid_from must be an ISO-8601 timestamp with an offset")
    if doc.get("replaces") is not None:
        r = doc["replaces"]
        if not _is_int(r) or r < 1 or (_is_int(version) and r >= version):
            errors.append("replaces must be a positive integer lower than cal_version")
    if doc.get("note") is not None:
        if not isinstance(doc["note"], str) or len(doc["note"]) > 400:
            errors.append("note must be a string of at most 400 characters")

    if not errors and not is_monotonic([float(c) for c in coefs], int(z0)):
        errors.append("coefs must be monotonic across the full ADC span")
    if errors:
        return None, errors

    payload = {
        "cal_id": doc["cal_id"],
        "cal_version": int(version),
        "device": doc["device"],
        "z0": int(z0),
        "coefs": [float(c) for c in coefs],
        "validity": [int(validity[0]), int(validity[1])],
        "valid_from": from_ms(to_ms(doc["valid_from"])),
        "replaces": int(doc["replaces"]) if doc.get("replaces") is not None else None,
        "note": doc["note"] if doc.get("note") is not None else None,
    }
    return payload, []


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _is_num(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def canonical_digest(obj):
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def doc_digest(payload):
    """Identity of the whole delivered document."""
    return canonical_digest(payload)


def semantic_identity(payload):
    """Identity of the arithmetic and applicability a conversion depends on.

    ``replaces`` and ``note`` are provenance: a conversion already in flight
    when only those changed is still numerically valid.  Every field that can
    change a number, a validity decision, or the effective date is included.
    """
    return canonical_digest({
        "cal_id": payload["cal_id"],
        "cal_version": payload["cal_version"],
        "device": payload["device"],
        "z0": payload["z0"],
        "coefs": payload["coefs"],
        "validity": payload["validity"],
        "valid_from": payload["valid_from"],
    })


# -- conversion ------------------------------------------------------------

def in_validity(payload, raw):
    return payload["validity"][0] <= raw <= payload["validity"][1]


def evaluate(payload, raw):
    """Evaluate one calibration against one raw count.

    Out-of-validity is final for that calibration: it does not trigger a
    fallback to another version.
    """
    if not in_validity(payload, raw):
        return {"priced": False, "value_kpa": None}
    return {"priced": True, "value_kpa": round(_polynomial(payload, raw), 6)}


def _polynomial(payload, raw):
    z0 = payload["z0"]
    a0, a1, a2, a3 = payload["coefs"]
    u = (raw - z0) / RAW_SPAN
    return a0 + a1 * u + a2 * u * u + a3 * u * u * u


def endorse(payload, raw):
    """How far a calibration's own result sits from the synthetic truth."""
    truth = PROFILES[payload["device"]].value(raw)
    return round(_polynomial(payload, raw) - truth, 6)


# -- synthetic signals -----------------------------------------------------

def pressure_at(device, seq):
    """Deterministic true pressure (kPa) for a sequence number."""
    if device == "SYNTH-P":
        lo, hi, phase = 110.0, 570.0, 0.4
    else:
        lo, hi, phase = 120.0, 460.0, 2.1
    frac = 0.5 + 0.4 * math.sin(seq * 0.21 + phase) + 0.08 * math.sin(seq * 0.037 + phase)
    frac = min(1.0, max(0.0, frac))
    return lo + (hi - lo) * frac


def raw_at(device, seq):
    return PROFILES[device].raw_for(pressure_at(device, seq))


def calibrated_doc(device, cal_id, version, valid_from_ms, drift_kpa=0.0,
                   scale=1.0, validity=None, replaces=None, note=None):
    """Build a plausible calibration document for a synthetic device.

    ``drift_kpa`` and ``scale`` are deliberate imperfections: a document
    derived this way is close to, but not identical with, the true profile.
    """
    profile = PROFILES[device]
    a0, a1, a2, a3 = profile.a
    payload = {
        "cal_id": cal_id,
        "cal_version": int(version),
        "device": device,
        "z0": profile.z0,
        "coefs": [round(a0 + drift_kpa, 6), round(a1 * scale, 6), a2, a3],
        "validity": list(validity or (profile.valid_lo, profile.valid_hi)),
        "valid_from": from_ms(valid_from_ms),
        "replaces": replaces,
        "note": note,
    }
    return payload
