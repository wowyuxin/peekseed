"""Dataset review settings + dataset-level authorization (M3 CP5, wire §9/§10).

These are peekseed-side, additive files under
``$PEEKSEED_HOME/datasets/<id>/review/``; ``dataset.json`` and
``DATASET_SCHEMA`` are untouched so an old checkout can ignore them and a
rollback never loses settings:

    review/settings.json          auto-review thresholds/caps/cutoff/breaker
    review/authorizations.jsonl   append-only authorization ledger (no secret)
    review/auto_confirmations.jsonl  mirror of peelfeed's authoritative ledger

Secrets never enter these files: an authorization stores only provider/link/
model/scope and (optionally) the *name* of an API-key env var.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path

from . import contracts

__all__ = [
    "DEFAULT_SETTINGS",
    "active_authorization",
    "authorization_status",
    "authorizations",
    "create_authorization",
    "load",
    "mirror_auto_confirmation",
    "revoke_authorization",
    "save",
    "settings_path",
]

SCHEMA_VERSION = 1

DEFAULT_SETTINGS = {
    "schema_version": SCHEMA_VERSION,
    "auto_review": {"enabled": False},
    "thresholds": {
        "apply": {"enabled": False, "value": 90},
        "keep": {"enabled": False, "value": 90},
    },
    "caps": {"cap_rule_disagree": 50, "cap_uncertain": 70},
    "minimum_validated_cutoff": None,
    "breaker": {"window_days": 30, "window_records": 200,
                "min_samples": 20, "line": 0.95},
    "category_overrides": {},
}

_TOP_KEYS = frozenset(DEFAULT_SETTINGS)
_THRESHOLD_KEYS = frozenset(("enabled", "value"))
_CAP_KEYS = frozenset(("cap_rule_disagree", "cap_uncertain"))
_BREAKER_KEYS = frozenset(("window_days", "window_records", "min_samples",
                           "line"))


class ReviewSettingsError(ValueError):
    """Invalid review settings/authorization input (HTTP 400 at the API)."""


def review_dir(directory):
    return Path(directory) / "review"


def settings_path(directory):
    return review_dir(directory) / "settings.json"


def authorizations_path(directory):
    return review_dir(directory) / "authorizations.jsonl"


def auto_confirmations_path(directory):
    return review_dir(directory) / "auto_confirmations.jsonl"


def _number(value, name, *, minimum=0.0, maximum=100.0):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReviewSettingsError("%s must be a number" % name)
    value = float(value)
    if not minimum <= value <= maximum:
        raise ReviewSettingsError(
            "%s must be between %s and %s" % (name, minimum, maximum))
    return value


def _integer(value, name, *, minimum=1):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ReviewSettingsError("%s must be an integer" % name)
    if value < minimum:
        raise ReviewSettingsError("%s must be >= %s" % (name, minimum))
    return value


def _bool(value, name):
    if not isinstance(value, bool):
        raise ReviewSettingsError("%s must be a boolean" % name)
    return value


def _validate_threshold(value, name):
    if not isinstance(value, dict):
        raise ReviewSettingsError("%s must be an object" % name)
    unknown = set(value) - _THRESHOLD_KEYS
    if unknown:
        raise ReviewSettingsError(
            "%s has unknown key(s): %s" % (name, ", ".join(sorted(unknown))))
    return {
        "enabled": _bool(value.get("enabled", False),
                         name + ".enabled"),
        "value": _number(value.get("value", 90), name + ".value"),
    }


def validate_settings(payload):
    """Validate one complete settings object and fill defaults."""
    if not isinstance(payload, dict):
        raise ReviewSettingsError("review settings must be a JSON object")
    unknown = set(payload) - _TOP_KEYS
    if unknown:
        raise ReviewSettingsError(
            "unknown review setting(s): %s" % ", ".join(sorted(unknown)))
    merged = json.loads(json.dumps(DEFAULT_SETTINGS))
    if "auto_review" in payload:
        auto = payload.get("auto_review")
        if not isinstance(auto, dict) or set(auto) - {"enabled"}:
            raise ReviewSettingsError(
                "auto_review must be {enabled: bool}")
        merged["auto_review"] = {
            "enabled": _bool(auto.get("enabled", False),
                             "auto_review.enabled")}
    if "thresholds" in payload:
        thresholds = payload.get("thresholds")
        if not isinstance(thresholds, dict) or set(thresholds) - {
                "apply", "keep"}:
            raise ReviewSettingsError(
                "thresholds must contain apply/keep")
        for key in ("apply", "keep"):
            if key in thresholds:
                merged["thresholds"][key] = _validate_threshold(
                    thresholds[key], "thresholds." + key)
    if "caps" in payload:
        caps = payload.get("caps")
        if not isinstance(caps, dict) or set(caps) - _CAP_KEYS:
            raise ReviewSettingsError("caps must contain %s" %
                                      ", ".join(sorted(_CAP_KEYS)))
        for key in _CAP_KEYS:
            if key in caps:
                merged["caps"][key] = int(_number(caps[key], "caps." + key))
    if "minimum_validated_cutoff" in payload:
        value = payload.get("minimum_validated_cutoff")
        merged["minimum_validated_cutoff"] = (
            None if value is None else _number(value,
                                               "minimum_validated_cutoff"))
    if "breaker" in payload:
        breaker = payload.get("breaker")
        if not isinstance(breaker, dict) or set(breaker) - _BREAKER_KEYS:
            raise ReviewSettingsError(
                "breaker must contain %s" % ", ".join(sorted(_BREAKER_KEYS)))
        merged["breaker"] = {
            "window_days": _integer(
                breaker.get("window_days", 30), "breaker.window_days"),
            "window_records": _integer(
                breaker.get("window_records", 200), "breaker.window_records"),
            "min_samples": _integer(
                breaker.get("min_samples", 20), "breaker.min_samples"),
            "line": _number(breaker.get("line", 0.95), "breaker.line",
                            minimum=0.0, maximum=1.0),
        }
    if "category_overrides" in payload:
        overrides = payload.get("category_overrides")
        if not isinstance(overrides, dict):
            raise ReviewSettingsError("category_overrides must be an object")
        for code, value in overrides.items():
            if not isinstance(code, str) or not code:
                raise ReviewSettingsError(
                    "category_overrides keys must be non-empty strings")
            if not isinstance(value, dict):
                raise ReviewSettingsError(
                    "category_overrides.%s must be an object" % code)
            unknown = set(value) - {"thresholds", "enabled"}
            if unknown:
                raise ReviewSettingsError(
                    "category_overrides.%s has unknown key(s)" % code)
            entry = {}
            if "enabled" in value:
                entry["enabled"] = _bool(
                    value["enabled"], "category_overrides.%s.enabled" % code)
            if "thresholds" in value:
                thresholds = value["thresholds"]
                if not isinstance(thresholds, dict) or set(thresholds) - {
                        "apply", "keep"}:
                    raise ReviewSettingsError(
                        "category_overrides.%s.thresholds must contain "
                        "apply/keep" % code)
                entry["thresholds"] = {
                    key: _validate_threshold(
                        thresholds[key],
                        "category_overrides.%s.thresholds.%s" % (code, key))
                    for key in thresholds}
            overrides[code] = entry
        merged["category_overrides"] = dict(overrides)
    merged["schema_version"] = SCHEMA_VERSION
    return merged


def load(directory):
    """The persisted settings merged over defaults (never raises on missing)."""
    path = settings_path(directory)
    try:
        payload = contracts.read_json(path, default=None)
    except contracts.ContractError:
        payload = None
    if not isinstance(payload, dict):
        return json.loads(json.dumps(DEFAULT_SETTINGS))
    try:
        return validate_settings(payload)
    except ReviewSettingsError:
        return json.loads(json.dumps(DEFAULT_SETTINGS))


def save(directory, payload):
    """Validate and atomically persist one complete settings object (0600)."""
    validated = validate_settings(payload)
    path = settings_path(directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    contracts.write_json_atomic(path, validated, mode=0o600)
    return validated


# ---------------------------------------------------------------- authorizations
def _append_jsonl(path, record):
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, sort_keys=False) + "\n"
    with path.open("a", encoding="utf-8") as stream:
        stream.write(line)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return record


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


def authorizations(directory):
    """Collapse the append-only ledger to one current record per id."""
    latest = {}
    for record in _read_jsonl(authorizations_path(directory)):
        identifier = str(record.get("authorization_id") or "")
        if identifier:
            latest[identifier] = record
    return [latest[key] for key in sorted(latest)]


def _expiry(value):
    if value in (None, ""):
        return None
    try:
        moment = datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise ReviewSettingsError(
            "expires_at must be UTC ISO-8601 (YYYY-MM-DDTHH:MM:SSZ)")
    return moment.replace(tzinfo=timezone.utc)


def authorization_status(record, *, now=None, provider=None):
    """``active|expired|revoked|provider_changed`` for one authorization."""
    if not isinstance(record, dict):
        return "missing"
    if str(record.get("status") or "") != "active":
        return "revoked"
    moment = _expiry(record.get("expires_at"))
    current = now or datetime.now(timezone.utc)
    if moment is not None and moment <= current:
        return "expired"
    if isinstance(provider, dict):
        scope = record.get("provider") or {}
        for key in ("decision", "vlm"):
            wanted = scope.get(key) or {}
            actual = provider.get(key) or {}
            if wanted.get("link_id") != actual.get("link_id") \
                    or wanted.get("model") != actual.get("model"):
                return "provider_changed"
    return "active"


def active_authorization(directory, *, provider=None):
    """The newest active authorization (or ``None`` + reason via statuses)."""
    records = authorizations(directory)
    for record in reversed(records):
        if authorization_status(record, provider=provider) == "active":
            return record
    return None


def create_authorization(directory, payload, *, dataset_id, provider,
                         actor="user"):
    """Append one dataset-level authorization (no secret)."""
    if not isinstance(payload, dict):
        raise ReviewSettingsError("authorization must be a JSON object")
    unknown = set(payload) - {"scope", "expires_at", "note"}
    if unknown:
        raise ReviewSettingsError(
            "unknown authorization field(s): %s" % ", ".join(sorted(unknown)))
    scope = payload.get("scope") or {}
    if not isinstance(scope, dict):
        raise ReviewSettingsError("authorization.scope must be an object")
    allowed_scope = {"text", "image", "decision", "vlm"}
    unknown_scope = set(scope) - allowed_scope
    if unknown_scope:
        raise ReviewSettingsError(
            "unknown authorization scope key(s): %s"
            % ", ".join(sorted(unknown_scope)))
    expires_at = payload.get("expires_at")
    _expiry(expires_at)
    material = "|".join([
        str(dataset_id), str(provider.get("decision", {}).get("link_id")),
        str(provider.get("decision", {}).get("model")),
        str(provider.get("vlm", {}).get("link_id")),
        str(provider.get("vlm", {}).get("model")),
        json.dumps(scope, sort_keys=True, ensure_ascii=False),
        secrets.token_hex(4),
    ])
    record = {
        "schema_version": SCHEMA_VERSION,
        "authorization_id": "auth-" + hashlib.sha256(
            material.encode("utf-8")).hexdigest()[:16],
        "dataset_id": str(dataset_id),
        "actor": str(actor),
        "created_at": contracts.utc_now(),
        "expires_at": expires_at or None,
        "status": "active",
        "scope": {key: bool(scope.get(key, False)) for key in allowed_scope},
        "provider": {
            "decision": {
                "link_id": provider.get("decision", {}).get("link_id"),
                "model": provider.get("decision", {}).get("model"),
                "protocol": provider.get("decision", {}).get(
                    "protocol") or "typesafe",
            },
            "vlm": {
                "link_id": provider.get("vlm", {}).get("link_id"),
                "model": provider.get("vlm", {}).get("model"),
            },
        },
        "note": str(payload.get("note") or "")[:300],
    }
    _append_jsonl(authorizations_path(directory), record)
    return record


def mirror_auto_confirmation(directory, record):
    """Append one peekseed audit mirror row (peelfeed stays authoritative)."""
    if not isinstance(record, dict):
        raise ReviewSettingsError("auto confirmation must be an object")
    identifier = str(record.get("auto_confirmation_id") or "")
    if not identifier:
        raise ReviewSettingsError("auto_confirmation_id is required")
    row = {
        "schema_version": SCHEMA_VERSION,
        "auto_confirmation_id": identifier,
        "dataset_id": str(record.get("dataset_id") or ""),
        "peel_id": str(record.get("peel_id") or ""),
        "base_plan_version": int(record.get("base_plan_version") or 0),
        "entry_ids": list(record.get("entry_ids") or []),
        "item_ids": list(record.get("item_ids") or []),
        "actor": str(record.get("actor") or "user"),
        "created_at": contracts.utc_now(),
        "outcome": str(record.get("outcome") or "confirmed"),
        "batch_id": record.get("batch_id"),
        "revert_of": record.get("revert_of"),
    }
    _append_jsonl(auto_confirmations_path(directory), row)
    return row


def revoke_authorization(directory, authorization_id, *, actor="user"):
    """Append a revocation row; the original row is never rewritten."""
    identifier = str(authorization_id or "").strip()
    if not identifier:
        raise ReviewSettingsError("authorization_id is required")
    records = [record for record in authorizations(directory)
               if record.get("authorization_id") == identifier]
    if not records:
        raise ReviewSettingsError(
            "unknown authorization %s" % identifier)
    current = records[-1]
    if current.get("status") != "active":
        return current
    record = dict(current)
    record.update({"status": "revoked", "revoked_at": contracts.utc_now(),
                   "revoked_by": str(actor)})
    _append_jsonl(authorizations_path(directory), record)
    return record
