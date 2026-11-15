"""Cross-peel calibration aggregation + breaker state (M3 CP5, wire §8).

Peelfeed emits one raw calibration row per adjudicate result
(``extract/review/calibration.jsonl``). Peekseed owns the cross-peel store:

    $PEEKSEED_HOME/calibration/records.jsonl   append-only, deduped by record_id
    $PEEKSEED_HOME/calibration/breaker.json    per-category breaker state
    $PEEKSEED_HOME/calibration/state.json      active version/cutoff snapshot

The records are read-only evidence: a missing truth field means "not evaluated
yet" (CP1/CP2 not run), never "correct". ``observed_correctness`` is the only
source of accuracy; ``acceptance`` is the separate user-acceptance signal.
"""
from __future__ import annotations

import json
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import contracts

__all__ = [
    "aggregate",
    "breaker",
    "ingest",
    "paths",
    "records",
    "summary",
]

SCHEMA_VERSION = 1
RECORD_ID_KEYS = ("record_id",)


def paths(home):
    root = Path(home) / "calibration"
    return {
        "root": root,
        "records": root / "records.jsonl",
        "breaker": root / "breaker.json",
        "state": root / "state.json",
    }


def _read_jsonl(path):
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    records = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict):
            records.append(payload)
    return records


def _append_jsonl(path, rows):
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False,
                                    sort_keys=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    try:
        path.chmod(0o600)
    except OSError:
        pass


def records(home):
    return _read_jsonl(paths(home)["records"])


def _dataset_dirs(home):
    root = Path(home) / "datasets"
    if not root.is_dir():
        return []
    return sorted(path for path in root.iterdir() if path.is_dir())


def _peel_ids(dataset_dir):
    try:
        metadata = contracts.read_json(dataset_dir / "dataset.json")
    except contracts.ContractError:
        return []
    found = []
    for entry in metadata.get("sources") or []:
        block = entry.get("extraction") if isinstance(entry, dict) else None
        if isinstance(block, dict) and block.get("peel_id"):
            found.append(str(block["peel_id"]))
    return found


def ingest(home, shared_dir, *, dataset_id=None):
    """Import new peelfeed calibration rows into the cross-peel store.

    Idempotent by ``record_id``; rows keep their peel/dataset identity and the
    original version binding. Never calls a provider.
    """
    store = paths(home)
    existing_ids = {str(row.get("record_id")) for row in records(home)}
    fresh = []
    for directory in _dataset_dirs(home):
        identifier = directory.name
        if dataset_id is not None and identifier != dataset_id:
            continue
        for peel_id in _peel_ids(directory):
            peel_dir = Path(shared_dir) / "peels" / peel_id
            calibration = peel_dir / "extract" / "review" / "calibration.jsonl"
            for row in _read_jsonl(calibration):
                record_id = str(row.get("record_id") or "")
                if not record_id or record_id in existing_ids:
                    continue
                row = dict(row)
                row.setdefault("dataset_id", identifier)
                row.setdefault("peel_id", peel_id)
                row.setdefault("schema_version", SCHEMA_VERSION)
                fresh.append(row)
                existing_ids.add(record_id)
    _append_jsonl(store["records"], fresh)
    return {"imported": len(fresh), "records": len(existing_ids),
            "path": str(store["records"])}


# ------------------------------------------------------------ Clopper-Pearson
def _betacf(a, b, x):
    """Continued fraction for the incomplete beta function (NR §6.4)."""
    max_iter = 200
    eps = 3e-12
    fpmin = 1e-300
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < fpmin:
        d = fpmin
    d = 1.0 / d
    h = d
    for m in range(1, max_iter + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            return h
    return h


def _betai(a, b, x):
    """Regularized incomplete beta I_x(a,b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    log_bt = (math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
              + a * math.log(x) + b * math.log(1.0 - x))
    bt = math.exp(log_bt)
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def _beta_quantile(q, a, b):
    """Inverse regularized incomplete beta via bisection (0<q<1)."""
    low, high = 0.0, 1.0
    for _ in range(80):
        mid = (low + high) / 2.0
        if _betai(a, b, mid) < q:
            low = mid
        else:
            high = mid
    return (low + high) / 2.0


def clopper_pearson(successes, total, alpha=0.05):
    """Two-sided Clopper–Pearson interval for a binomial proportion."""
    if total <= 0:
        return None
    successes = max(0, min(int(successes), int(total)))
    if successes == 0:
        low = 0.0
    else:
        low = _beta_quantile(alpha / 2.0, successes, total - successes + 1)
    if successes == total:
        high = 1.0
    else:
        high = _beta_quantile(1.0 - alpha / 2.0, successes + 1,
                              total - successes)
    return [round(low, 6), round(high, 6)]


# --------------------------------------------------------------------- stats
def _truth(row, key):
    value = row.get(key)
    if isinstance(value, dict):
        value = value.get("decision")
    return value


def _evaluated(row):
    return row.get("observed_correctness") is not None


def _accepted(row):
    return row.get("acceptance") is not None


def _bucket_key(row):
    decision = str(row.get("decision") or _truth(row, "blind_truth")
                   or ("keep" if str(row.get("source") or "").endswith("keep")
                       else "apply"))
    return "{}|{}|{}|{}".format(
        str(row.get("partition") or "calibration"),
        str(row.get("source") or "decision"),
        str(row.get("modality") or "text"),
        decision)


def _empty_bucket(key):
    return {"key": key, "total": 0, "evaluated": 0, "correct": 0,
            "accepted": 0, "acceptance_n": 0, "harmful": 0, "lost_labels": 0,
            "ci": None}


def aggregate(rows, *, partition=None):
    """Per-category counts, accuracy/acceptance and Clopper–Pearson CIs."""
    buckets = {}
    for row in rows:
        if partition is not None and str(row.get("partition") or
                                          "calibration") != partition:
            continue
        key = _bucket_key(row)
        bucket = buckets.setdefault(key, _empty_bucket(key))
        bucket["total"] += 1
        if _evaluated(row):
            bucket["evaluated"] += 1
            if bool(row.get("observed_correctness")):
                bucket["correct"] += 1
        if _accepted(row):
            bucket["acceptance_n"] += 1
            if bool(row.get("acceptance")):
                bucket["accepted"] += 1
        if row.get("harmful_error"):
            bucket["harmful"] += 1
        try:
            bucket["lost_labels"] += int(row.get("lost_labels") or 0)
        except (TypeError, ValueError):
            pass
    for bucket in buckets.values():
        bucket["accuracy"] = (round(bucket["correct"] / bucket["evaluated"], 6)
                              if bucket["evaluated"] else None)
        bucket["acceptance_rate"] = (
            round(bucket["accepted"] / bucket["acceptance_n"], 6)
            if bucket["acceptance_n"] else None)
        bucket["ci"] = clopper_pearson(bucket["correct"],
                                       bucket["evaluated"])
    return sorted(buckets.values(), key=lambda bucket: bucket["key"])


def _parse_time(value):
    if not value:
        return None
    try:
        moment = datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None
    return moment.replace(tzinfo=timezone.utc)


def breaker(rows, settings=None):
    """Breaker state from the configured window (wire §8.4)."""
    settings = settings or {}
    breaker_settings = settings.get("breaker") or {}
    window_days = int(breaker_settings.get("window_days") or 30)
    window_records = int(breaker_settings.get("window_records") or 200)
    min_samples = int(breaker_settings.get("min_samples") or 20)
    line = float(breaker_settings.get("line") or 0.95)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=window_days)
    recent = []
    for row in rows:
        moment = _parse_time(row.get("created_at"))
        if moment is None or moment >= cutoff:
            recent.append(row)
    recent = recent[-window_records:]
    evaluated = [row for row in recent if _evaluated(row)]
    harmful = sum(1 for row in recent if row.get("harmful_error"))
    correct = sum(1 for row in evaluated if bool(
        row.get("observed_correctness")))
    if harmful:
        state = "paused_harmful"
    elif len(evaluated) < min_samples:
        state = "insufficient"
    elif correct / len(evaluated) < line:
        state = "paused_accuracy"
    else:
        state = "ok"
    return {
        "state": state,
        "window_days": window_days,
        "window_records": window_records,
        "min_samples": min_samples,
        "line": line,
        "records": len(recent),
        "evaluated": len(evaluated),
        "correct": correct,
        "harmful_errors": harmful,
        "accuracy": (round(correct / len(evaluated), 6)
                     if evaluated else None),
        "ci": clopper_pearson(correct, len(evaluated)),
    }


def summary(home, shared_dir, *, dataset_id=None, settings=None):
    imported = ingest(home, shared_dir, dataset_id=dataset_id)
    rows = records(home)
    if dataset_id is not None:
        rows = [row for row in rows
                if str(row.get("dataset_id") or "") == dataset_id]
    state = breaker(rows, settings)
    store = paths(home)
    state_payload = {
        "schema_version": SCHEMA_VERSION,
        "dataset_id": dataset_id,
        "breaker": state,
        "records": len(rows),
        "evaluated": sum(1 for row in rows if _evaluated(row)),
        "active_version_bundle_hash": (rows[-1].get("version_bundle_hash")
                                       if rows else None),
    }
    store["root"].mkdir(parents=True, exist_ok=True)
    contracts.write_json_atomic(store["breaker"], state, mode=0o600)
    contracts.write_json_atomic(store["state"], state_payload, mode=0o600)
    return {
        "schema_version": SCHEMA_VERSION,
        "dataset_id": dataset_id,
        "imported": imported["imported"],
        "records": len(rows),
        "evaluated": state_payload["evaluated"],
        "calibration": aggregate(rows, partition="calibration"),
        "holdout": aggregate(rows, partition="holdout"),
        "breaker": state,
        "version_bundle_hash": state_payload["active_version_bundle_hash"],
        "empty": not rows,
        "note": ("no calibration records yet; auto drafts stay behind the "
                 "validated cutoff (CP1/CP2 not run)") if not rows else None,
    }
