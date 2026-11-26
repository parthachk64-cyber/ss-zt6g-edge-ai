import pytest

from sszt6g.errors import BenchmarkAdapterError
from sszt6g.ledger import AuditLog, NullLedger
from sszt6g.orchestrator import OrchConfig, Orchestrator
from sszt6g.simulator import Cluster
from sszt6g.trust import PermissiveEvaluator, Request, evaluate, issue_token, make_claim
from tests.conftest import make_intent


def req(world, tenant="acme-video", token_tenant=None, op="reserve", service="vision-inference", site="edge-east", total=3, now=1000.0, rev=None,
        issued=0.0, ttl=3_600_000.0, posture="compliant", resource_tenant=None):
    rev = world.policy.revision if rev is None else rev
    tok = issue_token(world.issuer, make_claim("u", token_tenant or tenant, issued, ttl, posture=posture, policy_revision=rev))
    return Request("r-1", tok, op, service, site, resource_tenant or tenant, total, now, rev)


def test_allows_a_fully_valid_request(world):
    d = evaluate(req(world), world.policy, 1000.0, world.keyring)
    assert d.allow and d.reasons == [] and all(d.checks.values())


def test_default_deny_without_rule(world):
    d = evaluate(req(world, tenant="nobody"), world.policy, 1000.0, world.keyring)
    assert not d.allow and "no_rule_default_deny" in d.reasons


def test_tenant_isolation(world):
    d = evaluate(req(world, tenant="acme-video", resource_tenant="city-traffic", service="traffic-analytics"), world.policy, 1000.0, world.keyring)
    assert not d.allow and "tenant_mismatch" in d.reasons


def test_expired_and_not_yet_valid_claims_denied(world):
    assert "claim_expired_or_not_yet_valid" in evaluate(req(world, issued=-7_200_000.0, ttl=3_600_000.0), world.policy, 1000.0, world.keyring).reasons
    assert "claim_expired_or_not_yet_valid" in evaluate(req(world, issued=50_000.0), world.policy, 1000.0, world.keyring).reasons


def test_stale_policy_revision_denied(world):
    newer = world.policy.bumped(world.policy.revision + 1)
    d = evaluate(req(world), newer, 1000.0, world.keyring)
    assert not d.allow and "stale_policy" in d.reasons and d.policy_revision == newer.revision


def test_degraded_posture_denied(world):
    assert "posture_not_compliant" in evaluate(req(world, posture="degraded"), world.policy, 1000.0, world.keyring).reasons


def test_operation_and_resource_scoping(world):
    assert "op_not_allowed" in evaluate(req(world, tenant="city-traffic", op="move", service="traffic-analytics"), world.policy, 1000.0, world.keyring).reasons
    assert "resource_not_allowed" in evaluate(req(world, service="traffic-analytics"), world.policy, 1000.0, world.keyring).reasons


def test_quota_enforced(world):
    d = evaluate(req(world, total=7), world.policy, 1000.0, world.keyring)
    assert not d.allow and d.reasons == ["quota_exceeded"]


def test_tampered_claim_fails_signature(world):
    r = req(world)
    r.token["claim"]["tenant"] = "city-traffic"
    assert "bad_signature" in evaluate(r, world.policy, 1000.0, world.keyring).reasons


def test_unknown_issuer_denied(world):
    r = req(world)
    r.token["kid"] = "somebody-else"
    assert "unknown_issuer" in evaluate(r, world.policy, 1000.0, world.keyring).reasons


def test_malformed_token_denies_instead_of_raising(world):
    r = req(world)
    r.token = {"claim": {}}
    d = evaluate(r, world.policy, 1000.0, world.keyring)
    assert not d.allow and d.reasons == ["malformed"]
    r.token = None
    assert evaluate(r, world.policy, 1000.0, world.keyring).reasons == ["malformed"]


def test_decision_ids_are_stable(world):
    a = evaluate(req(world), world.policy, 1000.0, world.keyring)
    b = evaluate(req(world), world.policy, 2000.0, world.keyring)
    assert a.decision_id == b.decision_id


def test_permissive_adapter_is_benchmark_only(world):
    with pytest.raises(BenchmarkAdapterError):
        Orchestrator(world.topo, Cluster(world.topo), world.policy, world.keyring, AuditLog(), ":memory:", OrchConfig(), PermissiveEvaluator())
    with pytest.raises(BenchmarkAdapterError):
        Orchestrator(world.topo, Cluster(world.topo), world.policy, world.keyring, NullLedger(), ":memory:", OrchConfig())
    o = Orchestrator(world.topo, Cluster(world.topo), world.policy, world.keyring, NullLedger(), ":memory:", OrchConfig(), PermissiveEvaluator(), True)
    assert o.submit(make_intent(world, "perm", token_tenant="city-traffic"), 1000.0).state == "applied"
