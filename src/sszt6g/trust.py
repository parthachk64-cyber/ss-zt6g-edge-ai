"""Zero-trust management-plane decisions: every operation is evaluated, default deny.

Authorization decisions are NOT anomaly alerts. A denial means "policy does not allow this", not "this was
an attack"; the detector in detection.py is evaluated separately against labels.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .keys import Identity, verify_signature
from .util import canonical, stable_id

POSTURES = ("compliant", "degraded", "unknown")


@dataclass
class TenantRule:
    ops: frozenset
    services: frozenset
    sites: frozenset  # empty = every site
    max_servers: int


@dataclass
class PolicyStore:
    revision: int
    rules: dict  # tenant -> TenantRule

    @staticmethod
    def from_dict(d: dict) -> "PolicyStore":
        rules = {t: TenantRule(frozenset(r.get("ops", [])), frozenset(r.get("services", [])), frozenset(r.get("sites", [])), int(r.get("max_servers", 0)))
                 for t, r in d.get("tenants", {}).items()}
        return PolicyStore(int(d.get("revision", 1)), rules)

    def bumped(self, revision: int) -> "PolicyStore":
        return PolicyStore(revision, dict(self.rules))


class Keyring:
    """Trusted issuer public keys: kid -> public key hex."""

    def __init__(self, keys: Optional[dict] = None):
        self.keys = dict(keys or {})

    def add(self, ident: Identity) -> None:
        self.keys[ident.name] = ident.public_hex

    def get(self, kid: str) -> Optional[str]:
        return self.keys.get(kid)


def make_claim(subject: str, tenant: str, issued_ms: float, ttl_ms: float, *, posture: str = "compliant",
               policy_revision: int = 1, nonce: Optional[str] = None) -> dict:
    return {"subject": subject, "tenant": tenant, "posture": posture, "policy_revision": policy_revision,
            "issued_ms": issued_ms, "expires_ms": issued_ms + ttl_ms, "nonce": nonce or stable_id("n", subject, issued_ms)}


def issue_token(issuer: Identity, claim: dict) -> dict:
    return {"claim": claim, "kid": issuer.name, "sig": issuer.sign(canonical(claim))}


@dataclass
class Request:
    request_id: str
    token: dict
    operation: str
    service: str
    site: str
    resource_tenant: str
    servers_total_after: int
    ts_ms: float
    policy_revision: int


@dataclass
class Decision:
    decision_id: str
    allow: bool
    reasons: list
    checks: dict
    policy_revision: int
    evidence_ids: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"decision_id": self.decision_id, "allow": self.allow, "reasons": list(self.reasons), "checks": dict(self.checks),
                "policy_revision": self.policy_revision, "evidence_ids": list(self.evidence_ids)}


def _deny(request_id: str, rev: int, reason: str) -> Decision:
    return Decision(stable_id("dec", request_id, rev, [reason]), False, [reason], {}, rev, [request_id])


def evaluate(request: Request, store: PolicyStore, now_ms: float, keyring: Keyring) -> Decision:
    """Evaluate one management operation. Never raises on malformed input: it denies."""
    rid = getattr(request, "request_id", "unknown")
    try:
        tok = request.token
        claim, kid, sig = tok["claim"], tok["kid"], tok["sig"]
        subject, tenant, posture = claim["subject"], claim["tenant"], claim["posture"]
        issued, expires, nonce = float(claim["issued_ms"]), float(claim["expires_ms"]), str(claim["nonce"])
    except (KeyError, TypeError, ValueError, AttributeError):
        return _deny(rid, store.revision, "malformed")

    pub = keyring.get(kid)
    checks = {
        "issuer_known": pub is not None,
        "signature_valid": bool(pub is not None and verify_signature(pub, canonical(claim), sig)),
        "claim_current": issued <= now_ms < expires,
        "policy_current": request.policy_revision >= store.revision,
        "tenant_match": tenant == request.resource_tenant,
        "posture_compliant": posture == "compliant",
    }
    rule = store.rules.get(tenant)
    checks["rule_exists"] = rule is not None
    checks["op_allowed"] = bool(rule and request.operation in rule.ops)
    checks["resource_allowed"] = bool(rule and request.service in rule.services and (not rule.sites or request.site in rule.sites))
    checks["within_quota"] = bool(rule and request.servers_total_after <= rule.max_servers)

    reason_map = [
        ("issuer_known", "unknown_issuer"), ("signature_valid", "bad_signature"), ("claim_current", "claim_expired_or_not_yet_valid"),
        ("policy_current", "stale_policy"), ("tenant_match", "tenant_mismatch"), ("posture_compliant", "posture_not_compliant"),
        ("rule_exists", "no_rule_default_deny"), ("op_allowed", "op_not_allowed"), ("resource_allowed", "resource_not_allowed"),
        ("within_quota", "quota_exceeded"),
    ]
    reasons = [r for (k, r) in reason_map if not checks[k]]
    allow = not reasons
    evidence = [f"issuer:{kid}", f"nonce:{nonce}", f"request:{rid}", f"policy-rev:{store.revision}", f"subject:{subject}"]
    return Decision(stable_id("dec", rid, store.revision, reasons), allow, reasons, checks, store.revision, evidence)


class PermissiveEvaluator:
    """Allow-all adapter used ONLY to isolate the cost of zero-trust checks in a simulated benchmark."""

    benchmark_only = True

    def __call__(self, request: Request, store: PolicyStore, now_ms: float, keyring: Keyring) -> Decision:
        return Decision(stable_id("dec", request.request_id, store.revision, ["permissive"]), True, [], {"permissive": True}, store.revision, [])


def static_eligible(request: Request, store: PolicyStore, keyring: Keyring, now_ms: float) -> bool:
    """Eligibility fixed from policy alone, before any controller runs: could this intent ever be allowed?"""
    return evaluate(request, store, now_ms, keyring).allow
