"""Versioned LOCAL message contracts for the management plane.

These are this companion's own JSON contracts. They are mapped conceptually to standards in
docs/standards-map.md; nothing here is compliant with, or tested against, any standard's interface.
Data-plane inference requests are a different message family and are refused on the management channel.
"""
from __future__ import annotations

import re
from typing import Any

from .errors import ContractError

SCHEMA_PREFIX = "sszt6g."
SUPPORTED_VERSIONS = {"1.0"}  # exact match on major.minor: this companion defines only 1.0
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\-]{2,95}$")

_NUM = (int, float)

# kind -> {field: (accepted types, required)}
FIELDS: dict = {
    "telemetry": {
        "ts_ms": (_NUM, True), "service": (str, True), "site": (str, True), "ingress_arrivals": (int, True),
        "routed_arrivals": (int, True), "queue_depth": (int, True), "allocated": (int, True), "busy": (int, True),
    },
    "provisioning_intent": {
        "intent_id": (str, True), "tenant": (str, True), "service": (str, True), "kind": (str, True), "servers": (int, True),
        "site_to": ((str, type(None)), True), "site_from": ((str, type(None)), True), "sla_ms": (_NUM, True),
        "submitted_ms": (_NUM, True), "policy_revision": (int, True), "origin": (str, True), "token": (dict, True),
    },
    "policy_decision": {
        "decision_id": (str, True), "allow": (bool, True), "reasons": (list, True), "checks": (dict, True),
        "policy_revision": (int, True), "evidence_ids": (list, True),
    },
    "action_result": {
        "action_id": (str, True), "intent_id": (str, True), "state": (str, True), "reason": ((str, type(None)), True),
        "duplicate": (bool, True), "submitted_ms": (_NUM, True), "verified_ms": ((int, float, type(None)), True),
    },
    "audit_receipt": {
        "receipt_id": (str, True), "ledger": (str, True), "seq": (int, True), "entry_hash": (str, True),
        "head_hash": (str, True), "state": (str, True), "action_id": (str, True), "extra": (dict, True),
    },
}
MANAGEMENT_KINDS = set(FIELDS)
DATA_PLANE_KINDS = {"inference_request"}
VALID_INTENT_KINDS = {"reserve", "move", "retain"}


def envelope(kind: str, body: dict, msg_id: str, version: str = "1.0") -> dict:
    return {"schema": SCHEMA_PREFIX + kind, "version": version, "id": msg_id, "body": body}


def _type_ok(value: Any, accepted) -> bool:
    if isinstance(value, bool) and bool not in (accepted if isinstance(accepted, tuple) else (accepted,)):
        return False  # bool is an int subclass; reject it where a number is expected
    return isinstance(value, accepted)


def validate(msg: Any, expected_kind: str | None = None) -> dict:
    """Validate an envelope and return its body. Raises ContractError with a specific code."""
    if not isinstance(msg, dict):
        raise ContractError("not_an_object")
    for key in ("schema", "version", "id", "body"):
        if key not in msg:
            raise ContractError(f"missing_envelope_field:{key}")
    schema = msg["schema"]
    if not isinstance(schema, str) or not schema.startswith(SCHEMA_PREFIX):
        raise ContractError("unknown_schema")
    kind = schema[len(SCHEMA_PREFIX):]
    if kind in DATA_PLANE_KINDS:
        raise ContractError("data_plane_message_on_management_channel")
    if kind not in MANAGEMENT_KINDS:
        raise ContractError("unknown_schema")
    if expected_kind and kind != expected_kind:
        raise ContractError(f"unexpected_kind:{kind}")
    if msg["version"] not in SUPPORTED_VERSIONS:
        raise ContractError(f"unsupported_version:{msg['version']}")
    if not isinstance(msg["id"], str) or not ID_RE.match(msg["id"]):
        raise ContractError("invalid_identifier")
    body = msg["body"]
    if not isinstance(body, dict):
        raise ContractError("body_not_object")
    spec = FIELDS[kind]
    for name, (types, required) in spec.items():
        if name not in body:
            if required:
                raise ContractError(f"missing_field:{name}")
            continue
        if not _type_ok(body[name], types):
            raise ContractError(f"bad_type:{name}")
    extra = set(body) - set(spec)
    if extra:
        raise ContractError(f"unexpected_field:{sorted(extra)[0]}")
    if kind == "provisioning_intent":
        if body["kind"] not in VALID_INTENT_KINDS:
            raise ContractError("invalid_intent_kind")
        if body["servers"] < 0:
            raise ContractError("negative_servers")
        if not ID_RE.match(body["intent_id"]):
            raise ContractError("invalid_identifier")
    return body


def accept_management(msg: Any) -> dict:
    """Entry point of the management channel: contract-validate, refuse data-plane traffic."""
    return validate(msg)


def to_wire(kind: str, body: dict, msg_id: str) -> dict:
    msg = envelope(kind, body, msg_id)
    validate(msg, kind)
    return msg
