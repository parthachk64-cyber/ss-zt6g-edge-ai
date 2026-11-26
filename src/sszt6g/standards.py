"""Standards mapping registry. docs/standards-map.md is generated from this table.

Levels, strictest last:
  conceptual_mapping    this code's behaviour is READ AS an instance of the cited clause; nothing is asserted about the standard's own interface
  local_contract_test   a local executable test checks the field or behaviour named here
  not_implemented       cited so the boundary is explicit; no code exists
  not_retrieved         the specification text was not available; no clause is claimed
There is deliberately NO level for "interoperability demonstrated against an independent implementation":
none was performed, and a custom JSON contract passing a local test is never called compliant.
"""
from __future__ import annotations

LEVELS = ("conceptual_mapping", "local_contract_test", "not_implemented", "not_retrieved")

ZSM009 = "ETSI GS ZSM 009-1 V1.1.1 (2021-06)"
ZSM002 = "ETSI GS ZSM 002 V1.1.1 (2019-08)"
TS28533 = "3GPP TS 28.533 V18.2.0 (Release 18), published as ETSI TS 128 533 V18.2.0 (2024-07)"

MAPPINGS: list = [
    dict(id="M1", body="ETSI ZSM", document=ZSM009, clause="7.2.2.2 Primary flow (M2A, A2D, D2E, E2M)",
         concept="Closed loop of four stages: monitoring, analysis, decision, execution, with each stage's output feeding the next.",
         implementation="telemetry.py snapshot (monitor) -> prediction.py forecast (analyse) -> planners.py plan + trust.py authorise (decide) -> orchestrator.py apply (execute) -> verify_pending feeds the next tick.",
         test="tests/test_orchestrator.py::test_full_intent_prediction_to_report", level="conceptual_mapping",
         gap="The stage boundaries are in-process function calls, not the ZSM integration fabric or any ZSM service interface."),
    dict(id="M2", body="ETSI ZSM", document=ZSM009, clause="5.2 General requirements, [CL-general-8]",
         concept="The actions taken by closed loops shall be logged.",
         implementation="Every state (proposed, authorized or denied, committed, applied, verified, rolled back, escalated) appends a receipt to the ledger.",
         test="tests/test_orchestrator.py::test_every_state_has_a_ledger_receipt", level="local_contract_test",
         gap="Retention period configuration ([CL-general-9]) is not implemented."),
    dict(id="M3", body="ETSI ZSM", document=ZSM009, clause="5.2 General requirements, [CL-general-14]",
         concept="Decisions based on AI/ML made by a closed loop should be monitored to support administrative audit trails.",
         implementation="The plan evidence written to the ledger carries the snapshot id, forecast ids, demand estimate and the model id.",
         test="tests/test_orchestrator.py::test_plan_evidence_carries_snapshot_and_forecast_ids", level="local_contract_test",
         gap="Only one local forecaster; no model-governance lifecycle."),
    dict(id="M4", body="ETSI ZSM", document=ZSM009, clause="8.1.5.3 Interactions based on intents",
         concept="External parties instruct a closed loop declaratively (an intent) rather than by commands.",
         implementation="interfaces.py provisioning_intent v1: tenant, service, kind, servers, SLA; the orchestrator chooses placement and timing.",
         test="tests/test_interfaces.py::test_producer_consumer_round_trip", level="local_contract_test",
         gap="The intent vocabulary is this companion's own. No ZSM or 3GPP intent model is used."),
    dict(id="M5", body="ETSI ZSM", document=ZSM002, clause="4.2.10 Principle 10: Intent-based interfaces",
         concept="Interfaces express consumer requests in declarative form and hide technology-specific detail.",
         implementation="Same as M4: the intent states a goal (capacity for a service), not a placement command.",
         test="tests/test_interfaces.py::test_producer_consumer_round_trip", level="conceptual_mapping",
         gap="Declarative scope is narrow: capacity and move only."),
    dict(id="M6", body="ETSI ZSM", document=ZSM002, clause="4.2.5 Principle 05: Closed-loop management automation",
         concept="Management automation through closed loops.", implementation="orchestrator.py ClosedLoopController.",
         test="tests/test_orchestrator.py::test_full_intent_prediction_to_report", level="conceptual_mapping",
         gap="Single loop; no loop coordination (ZSM 009-1 clause 8.2)."),
    dict(id="M7", body="ETSI ZSM", document=ZSM002, clause="5.4 Security requirements, [Sec-03]",
         concept="Integrity of management data at rest, in transit and in use.",
         implementation="ledger.py hash-linked audit log with a signed checkpoint: integrity of the AUDIT RECORD at rest.",
         test="tests/test_ledger.py::test_modified_entry_is_detected", level="conceptual_mapping",
         gap="Covers one data item (the audit log) at rest only. Nothing about data in transit or in use; no confidentiality."),
    dict(id="M8", body="3GPP", document=TS28533, clause="4.8 Management capability support in multiple tenant environment",
         concept="A tenant is a group of management-service consumers associated with the management capabilities they may access.",
         implementation="trust.py PolicyStore: per-tenant allowed operations, services, sites and a server quota; cross-tenant requests are denied.",
         test="tests/test_trust.py::test_tenant_isolation", level="local_contract_test",
         gap="No 3GPP tenant information model; no SLA-based capability exposure."),
    dict(id="M9", body="3GPP", document=TS28533, clause="4.9.2 Authorization service",
         concept="An authorization service grants permissions on management services to consumers.",
         implementation="trust.evaluate: every operation is checked (identity, tenant, resource, operation, claim expiry, posture, policy revision); default deny.",
         test="tests/test_trust.py::test_default_deny_without_rule", level="local_contract_test",
         gap="Not the 3GPP authorization service; claims are local Ed25519 tokens, not an operator IAM."),
    dict(id="M10", body="3GPP", document=TS28533, clause="4.1 Management Services (MnS) and 5.1.1 producers, consumers and exposure",
         concept="Service-based management: producers expose management services that consumers use.",
         implementation="The orchestrator plays producer of a provisioning service; a tenant or the closed loop is the consumer.",
         test="tests/test_interfaces.py::test_producer_consumer_round_trip", level="conceptual_mapping",
         gap="No MnS definition, no NRM, no 3GPP management operations (TS 28.532 and related are not implemented or retrieved)."),
    dict(id="M11", body="3GPP / ETSI NFV", document=TS28533 + "; refers to ETSI GS NFV-IFA013 and NFV-IFA008 (V4.3.1)", clause="5.2 Management interactions with NFV MANO",
         concept="The 3GPP management system shall be capable of consuming NFV MANO interfaces (for example Os-Ma-nfvo, Ve-Vnfm-em).",
         implementation="None.", test="", level="not_implemented",
         gap="No NFV-MANO interface exists here. If NFV-MANO is intended, the relevant ETSI NFV documents must be identified and read separately."),
    dict(id="M12", body="3GPP", document="3GPP TS 28.312 (intent driven management services), cited by TS 28.533", clause="not retrieved",
         concept="Standardised intent-driven management services.", implementation="None.", test="", level="not_retrieved",
         gap="Specification not read. No clause is claimed. Candidate for a future, properly sourced intent mapping."),
    dict(id="M13", body="O-RAN Alliance", document="O-RAN SMO and O1 interface specifications", clause="not retrieved",
         concept="The abstract names O-RAN; an SMO with an O1 management interface would be the plausible management role.",
         implementation="None.", test="", level="not_retrieved",
         gap="O-RAN specifications were not retrieved. No O-RAN interface is implemented or claimed. Passing a local JSON test would NOT make anything O1-compliant."),
]


def validate_registry() -> list:
    """Self-check used by tests: every mapping has a known level, and implemented levels name a test."""
    problems = []
    for m in MAPPINGS:
        if m["level"] not in LEVELS:
            problems.append(f"{m['id']}: unknown level {m['level']}")
        if m["level"] in ("local_contract_test",) and not m["test"]:
            problems.append(f"{m['id']}: local_contract_test requires a test")
        if "compliant" in (m["implementation"] + m["concept"]).lower() and "not" not in (m["implementation"] + m["gap"]).lower():
            problems.append(f"{m['id']}: wording suggests compliance")
    return problems
