import pytest

from sszt6g.errors import ContractError
from sszt6g.interfaces import accept_management, envelope, to_wire, validate

TEL = {"ts_ms": 5000.0, "service": "svc", "site": "edge-east", "ingress_arrivals": 3, "routed_arrivals": 3, "queue_depth": 0, "allocated": 2, "busy": 1}


def test_producer_consumer_round_trip(world):
    from tests.conftest import make_intent
    from sszt6g.orchestrator import ProvisioningIntent
    it = make_intent(world, "rt")
    wire = to_wire("provisioning_intent", it.to_dict(), "msg-intent-rt")
    back = ProvisioningIntent.from_dict(validate(wire, "provisioning_intent"))
    assert back == it
    assert validate(to_wire("telemetry", TEL, "msg-tel-1")) == TEL


def test_unsupported_versions_rejected():
    for v in ("2.0", "1.1", "0.9", "1"):
        with pytest.raises(ContractError, match="unsupported_version"):
            validate(envelope("telemetry", TEL, "msg-1", version=v))


def test_missing_and_extra_fields_rejected():
    body = dict(TEL)
    del body["site"]
    with pytest.raises(ContractError, match="missing_field:site"):
        validate(envelope("telemetry", body, "msg-1"))
    with pytest.raises(ContractError, match="unexpected_field"):
        validate(envelope("telemetry", {**TEL, "label": 1}, "msg-1"))


def test_wrong_types_rejected_including_bool_as_number():
    with pytest.raises(ContractError, match="bad_type:queue_depth"):
        validate(envelope("telemetry", {**TEL, "queue_depth": "3"}, "msg-1"))
    with pytest.raises(ContractError, match="bad_type:queue_depth"):
        validate(envelope("telemetry", {**TEL, "queue_depth": True}, "msg-1"))


def test_invalid_identifiers_rejected():
    for bad in ("", "a", "has space", "-lead", "x" * 200, 5):
        with pytest.raises(ContractError, match="invalid_identifier"):
            validate(envelope("telemetry", TEL, bad))


def test_data_plane_message_refused_on_management_channel():
    with pytest.raises(ContractError, match="data_plane_message_on_management_channel"):
        accept_management({"schema": "sszt6g.inference_request", "version": "1.0", "id": "req-1", "body": {}})


def test_unknown_schema_and_non_objects():
    for bad in (None, [], "x", {"schema": "other.x", "version": "1.0", "id": "abc", "body": {}}, {"schema": "sszt6g.nope", "version": "1.0", "id": "abc", "body": {}}):
        with pytest.raises(ContractError):
            validate(bad)
    with pytest.raises(ContractError, match="missing_envelope_field"):
        validate({"schema": "sszt6g.telemetry"})


def test_expected_kind_mismatch_and_intent_kind():
    with pytest.raises(ContractError, match="unexpected_kind"):
        validate(envelope("telemetry", TEL, "msg-1"), "action_result")
    from tests.conftest import make_intent
    from sszt6g.config import build_world
    d = make_intent(build_world(), "k").to_dict()
    with pytest.raises(ContractError, match="invalid_intent_kind"):
        validate(envelope("provisioning_intent", {**d, "kind": "teleport"}, "msg-2"))
    with pytest.raises(ContractError, match="negative_servers"):
        validate(envelope("provisioning_intent", {**d, "servers": -1}, "msg-3"))
