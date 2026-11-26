"""Seeded fixture generators and the dataset registry.

Everything generated here is SYNTHETIC. It certifies software behaviour, not field performance. Job demand,
security labels and orchestration outcomes live in separate files and no outcome, label or scenario name is
ever written into a detector-input file.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Optional

from .config import ATTACKER_ISSUER_NAME, World
from .keys import fixture_identity
from .orchestrator import ProvisioningIntent
from .trust import Request, evaluate, issue_token, make_claim
from .util import canonical, file_sha256, seeded_rng, stable_id
from .workload import Job, Scenario, generate_jobs

TENANT_SERVICE = {"acme-video": ("vision-inference", "edge-east"), "city-traffic": ("traffic-analytics", "edge-west"),
                  "metro-health": ("triage-vision", "edge-east")}
SUBJECTS = {t: [f"{t.split('-')[0]}-op{k}" for k in (1, 2, 3)] for t in TENANT_SERVICE}
OP_WEIGHTS = {"read_telemetry": 0.80, "reserve": 0.14, "retain": 0.05, "move": 0.01}
EPISODE_MS = 1_800_000.0
TOKEN_TTL_MS = 3_600_000.0
ATTACK_KINDS = ("expired_identity", "tenant_mismatch", "replay", "bad_signature", "credential_misuse")


# ----------------------------------------------------------------------------- demand (job) fixtures
def write_arrivals_csv(path: Path, jobs: list) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["job_id", "t_ms", "ingress", "tenant", "service", "work_ms", "deadline_ms"])
        for j in jobs:
            w.writerow([j.job_id, j.t_ms, j.ingress, j.tenant, j.service, round(j.work_ms, 3), j.deadline_ms])


def read_arrivals_csv(path: Path) -> list:
    from .errors import DataError
    need = {"job_id", "t_ms", "ingress", "tenant", "service", "work_ms", "deadline_ms"}
    with open(path, newline="", encoding="utf-8") as fh:
        rd = csv.DictReader(fh)
        if not rd.fieldnames or not need <= set(rd.fieldnames):
            raise DataError(f"arrivals file needs columns {sorted(need)}; found {rd.fieldnames}")
        out, last = [], -1.0
        for i, r in enumerate(rd, 2):
            try:
                j = Job(r["job_id"], float(r["t_ms"]), r["ingress"], r["tenant"], r["service"], float(r["work_ms"]), float(r["deadline_ms"]))
            except ValueError as e:
                raise DataError(f"row {i}: {e}") from e
            if j.t_ms < last:
                raise DataError(f"row {i}: timestamps must be non-decreasing")
            last = j.t_ms
            out.append(j)
    return out


# ----------------------------------------------------------------------------- security fixtures
def _weighted(rng, weights: dict) -> str:
    keys = list(weights)
    return keys[int(rng.choice(len(keys), p=[weights[k] for k in keys]))]


def _request_fields(tenant: str, op: str, rng, total_cap: int) -> tuple:
    service, site = TENANT_SERVICE[tenant]
    total = int(rng.integers(2, max(3, total_cap))) if op in ("reserve", "move") else 0
    return service, site, total


def generate_security(world: World, seed: int, episodes: int, per_episode: int) -> tuple:
    """Returns (events, labels, splits). Events carry detector inputs only."""
    issuer, rogue = world.issuer, fixture_identity(ATTACKER_ISSUER_NAME)
    events, labels = [], []
    for ep in range(episodes):
        rng = seeded_rng("security", seed, ep)
        start = ep * EPISODE_MS
        rows: list = []  # (ts, event_dict, label dict)
        tokens: dict = {}

        def token_for(subj: str, tenant: str, posture: str = "compliant", offset: Optional[float] = None) -> dict:
            key = (subj, tenant, posture, offset)
            if key not in tokens:
                off = float(rng.uniform(0, 1_200_000)) if offset is None else offset
                tokens[key] = issue_token(issuer, make_claim(subj, tenant, start - off, TOKEN_TTL_MS, posture=posture, policy_revision=1, nonce=stable_id("t", subj, ep, posture, off)))
            return tokens[key]

        def add(ts: float, subj: str, tenant: str, op: str, label: int, attack: str = "", benign: str = "", tok: Optional[dict] = None,
                resource_tenant: Optional[str] = None, nonce: Optional[str] = None):
            service, site, total = _request_fields(resource_tenant or tenant, op, rng, 7)
            ev = {"ts_ms": round(ts, 3), "token": tok or token_for(subj, tenant), "operation": op, "service": service, "site": site,
                  "resource_tenant": resource_tenant or tenant, "servers_total_after": total, "policy_revision": 1,
                  "request_nonce": nonce or stable_id("rq", ep, len(rows), round(ts, 3), subj)}
            rows.append((ts, ev, {"label": label, "attack_kind": attack, "benign_kind": benign}))

        n_bg = int(per_episode * 0.62)
        for _ in range(n_bg):
            tenant = list(TENANT_SERVICE)[int(rng.integers(0, 3))]
            add(start + rng.uniform(0, EPISODE_MS), SUBJECTS[tenant][int(rng.integers(0, 3))], tenant, _weighted(rng, OP_WEIGHTS), 0)
        # benign burst: legitimate rollout, many common-op requests from one subject
        bt = list(TENANT_SERVICE)[int(rng.integers(0, 3))]
        bs, t0 = SUBJECTS[bt][int(rng.integers(0, 3))], start + rng.uniform(0, EPISODE_MS - 120_000)
        for _ in range(int(per_episode * 0.12)):
            add(t0 + rng.uniform(0, 40_000), bs, bt, "read_telemetry" if rng.random() < 0.85 else "reserve", 0, benign="burst")
        # benign noise: degraded posture, near-expiry token, rare op
        for _ in range(int(per_episode * 0.03)):
            t = list(TENANT_SERVICE)[int(rng.integers(0, 3))]
            s = SUBJECTS[t][int(rng.integers(0, 3))]
            add(start + rng.uniform(0, EPISODE_MS), s, t, "read_telemetry", 0, benign="degraded_posture", tok=token_for(s, t, "degraded"))
        for _ in range(int(per_episode * 0.04)):
            t = list(TENANT_SERVICE)[int(rng.integers(0, 3))]
            s = SUBJECTS[t][int(rng.integers(0, 3))]
            add(start + rng.uniform(1_200_000, EPISODE_MS), s, t, _weighted(rng, OP_WEIGHTS), 0, benign="near_expiry", tok=token_for(s, t, offset=1_700_000.0 + float(rng.uniform(0, 100_000))))
        for _ in range(int(per_episode * 0.02)):
            t = list(TENANT_SERVICE)[int(rng.integers(0, 3))]
            add(start + rng.uniform(0, EPISODE_MS), SUBJECTS[t][int(rng.integers(0, 3))], t, "move", 0, benign="rare_op")
        # attacks
        for _ in range(max(2, int(per_episode * 0.03))):
            t = list(TENANT_SERVICE)[int(rng.integers(0, 3))]
            s = SUBJECTS[t][int(rng.integers(0, 3))]
            add(start + rng.uniform(0, EPISODE_MS), s, t, _weighted(rng, OP_WEIGHTS), 1, attack="expired_identity", tok=token_for(s, t, offset=float(rng.uniform(4_000_000, 6_000_000))))
        for _ in range(max(2, int(per_episode * 0.035))):
            t = list(TENANT_SERVICE)[int(rng.integers(0, 3))]
            other = [x for x in TENANT_SERVICE if x != t][int(rng.integers(0, 2))]
            add(start + rng.uniform(0, EPISODE_MS), SUBJECTS[t][int(rng.integers(0, 3))], t, _weighted(rng, OP_WEIGHTS), 1, attack="tenant_mismatch", resource_tenant=other)
        victims = [e for e in rows if e[2]["label"] == 0 and e[2]["benign_kind"] == ""]
        for _ in range(max(2, int(per_episode * 0.035))):
            src = victims[int(rng.integers(0, len(victims)))]
            ev = dict(src[1])
            rows.append((min(start + EPISODE_MS - 1, src[0] + float(rng.uniform(5_000, 300_000))), ev, {"label": 1, "attack_kind": "replay", "benign_kind": ""}))
        for _ in range(max(1, int(per_episode * 0.015))):
            t = list(TENANT_SERVICE)[int(rng.integers(0, 3))]
            s = SUBJECTS[t][int(rng.integers(0, 3))]
            good = token_for(s, t)
            forged = {"claim": dict(good["claim"], tenant=list(TENANT_SERVICE)[(list(TENANT_SERVICE).index(t) + 1) % 3]), "kid": good["kid"], "sig": good["sig"]}
            add(start + rng.uniform(0, EPISODE_MS), s, t, _weighted(rng, OP_WEIGHTS), 1, attack="bad_signature", tok=forged)
        for _ in range(max(2, int(per_episode * 0.015))):  # credential misuse: valid, allowed, but unusual and clustered
            t = list(TENANT_SERVICE)[int(rng.integers(0, 3))]
            s = SUBJECTS[t][int(rng.integers(0, 3))]
            t0 = start + rng.uniform(0, EPISODE_MS - 60_000)
            for _k in range(int(rng.integers(4, 8))):
                add(t0 + rng.uniform(0, 25_000), s, t, "retain" if rng.random() < 0.7 else "reserve", 1, attack="credential_misuse")
        rows.sort(key=lambda r: (r[0], r[1]["request_nonce"]))
        for k, (_ts, ev, lab) in enumerate(rows):
            eid = f"ev-{ep:02d}-{k:04d}"
            events.append({"event_id": eid, **ev})
            labels.append({"event_id": eid, "episode": ep, **lab})
    return events, labels, None


def assign_splits(labels: list, train_eps: int, dev_eps: int) -> list:
    out = []
    for lab in labels:
        ep = lab["episode"]
        out.append({"event_id": lab["event_id"], "episode": ep, "split": "train" if ep < train_eps else ("dev" if ep < train_eps + dev_eps else "test")})
    return out


def write_security(dirpath: Path, events: list, labels: list, splits: list) -> None:
    with open(dirpath / "security_events.jsonl", "w", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e, sort_keys=True, separators=(",", ":")) + "\n")
    for name, rows, cols in (("security_labels.csv", labels, ["event_id", "episode", "label", "attack_kind", "benign_kind"]),
                             ("security_splits.csv", splits, ["event_id", "episode", "split"])):
        with open(dirpath / name, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            w.writerows([{c: r[c] for c in cols} for r in rows])


def read_security(dirpath: Path) -> tuple:
    events = [json.loads(line) for line in (dirpath / "security_events.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    labels = {r["event_id"]: r for r in csv.DictReader(open(dirpath / "security_labels.csv", newline="", encoding="utf-8"))}
    splits = {r["event_id"]: r["split"] for r in csv.DictReader(open(dirpath / "security_splits.csv", newline="", encoding="utf-8"))}
    return events, labels, splits


# ----------------------------------------------------------------------------- tenant intents
def generate_intents(world: World, seed: int, horizon_ms: int = 120_000) -> tuple:
    """Tenant provisioning intents plus an eligibility table fixed from policy alone (never from outcomes)."""
    cfg = world.settings["intents"]
    rng = seeded_rng("intents", seed)
    issuer = world.issuer
    intents: list = []

    def tok(subject: str, tenant: str, issued: float = 0.0, ttl: float = 3_600_000.0) -> dict:
        return issue_token(issuer, make_claim(subject, tenant, issued, ttl, policy_revision=world.policy.revision))

    def make(i: int, kind: str, tenant: str, service: str, servers: int, site: Optional[str], token: dict, at: float) -> ProvisioningIntent:
        return ProvisioningIntent(stable_id("int", "tenant", seed, i), tenant, service, kind, servers, site, None if kind != "move" else "edge-east",
                                  world.settings["orchestrator"]["sla_ms"], round(at, 1), token, world.policy.revision, "tenant")

    k = 0
    for j in range(int(cfg["eligible"])):
        site = "edge-west" if j % 6 != 5 else "edge-east"
        intents.append(make(k, "reserve", "city-traffic", "traffic-analytics", 1, site, tok("city-ops", "city-traffic"), float(rng.uniform(5_000, horizon_ms - 25_000)))); k += 1
    bad = [("reserve", "city-traffic", "traffic-analytics", 1, tok("city-ops", "city-traffic", issued=-7_200_000.0, ttl=3_600_000.0)),
           ("reserve", "city-traffic", "traffic-analytics", 1, tok("acme-ops", "acme-video")),
           ("move", "city-traffic", "traffic-analytics", 1, tok("city-ops", "city-traffic")),
           ("reserve", "city-traffic", "traffic-analytics", 9, tok("city-ops", "city-traffic"))]
    for kind, tenant, service, servers, token in bad[: int(cfg["ineligible"])]:
        intents.append(make(k, kind, tenant, service, servers, "edge-west", token, float(rng.uniform(5_000, horizon_ms - 25_000)))); k += 1
    elig = {}
    for it in intents:
        req = Request(it.intent_id, it.token, it.kind, it.service, it.site_to or "", world.topo.service(it.service).tenant_id, it.servers, it.submitted_ms, it.policy_revision)
        d = evaluate(req, world.policy, it.submitted_ms, world.keyring)  # static: total_after = the intent's own size
        elig[it.intent_id] = {"eligible": d.allow, "static_reasons": d.reasons}
    dup_src = [i for i in intents if elig[i.intent_id]["eligible"]][: int(cfg["duplicates"])]
    for src in dup_src:  # re-delivery of the same intent id later in the run
        intents.append(ProvisioningIntent(**{**src.to_dict(), "submitted_ms": round(src.submitted_ms + float(rng.uniform(3_000, 15_000)), 1)}))
    intents.sort(key=lambda i: (i.submitted_ms, i.intent_id))
    return intents, elig


def write_intents(dirpath: Path, intents: list, elig: dict) -> None:
    with open(dirpath / "intents.jsonl", "w", encoding="utf-8") as fh:
        for it in intents:
            fh.write(json.dumps(it.to_dict(), sort_keys=True, separators=(",", ":")) + "\n")
    with open(dirpath / "intent_eligibility.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["intent_id", "eligible", "static_reasons"])
        for iid, e in sorted(elig.items()):
            w.writerow([iid, int(e["eligible"]), "|".join(e["static_reasons"])])


def read_intents(dirpath: Path) -> tuple:
    intents = [ProvisioningIntent.from_dict(json.loads(line)) for line in (dirpath / "intents.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    elig = {r["intent_id"]: {"eligible": bool(int(r["eligible"])), "static_reasons": [x for x in r["static_reasons"].split("|") if x]}
            for r in csv.DictReader(open(dirpath / "intent_eligibility.csv", newline="", encoding="utf-8"))}
    return intents, elig


# ----------------------------------------------------------------------------- build + registry
def build_fixtures(world: World, out: Path, demo_scenario: Scenario) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    sec = world.settings["security"]
    write_arrivals_csv(out / "arrivals_ramp_burst_seed1001.csv", generate_jobs(demo_scenario))
    events, labels, _ = generate_security(world, sec["seed"], sec["episodes"], sec["events_per_episode"])
    splits = assign_splits(labels, sec["train_episodes"], sec["dev_episodes"])
    write_security(out, events, labels, splits)
    intents, elig = generate_intents(world, world.settings["seeds"]["demo"])
    write_intents(out, intents, elig)
    return build_registry(out)


def _csv_stats(path: Path) -> int:
    return sum(1 for _ in open(path, encoding="utf-8")) - 1


REGISTRY_ENTRIES = {
    "arrivals_ramp_burst_seed1001.csv": dict(id="synthetic-job-demand", role="job demand (data plane)", fields=["job_id", "t_ms", "ingress", "tenant", "service", "work_ms", "deadline_ms"],
                                             observation_times="simulation milliseconds from 0 to 120000", labels="none", splits="none (single demo scenario; benchmarks regenerate held-out seeds)",
                                             exclusions="none"),
    "security_events.jsonl": dict(id="synthetic-security-events", role="detector inputs (management plane)", fields=["event_id", "ts_ms", "token", "operation", "service", "site", "resource_tenant", "servers_total_after", "policy_revision", "request_nonce"],
                                  observation_times="simulation milliseconds; 12 episodes of 1,800,000 ms", labels="separate file security_labels.csv", splits="separate file security_splits.csv: whole episodes, chronological",
                                  exclusions="no label, attack kind, episode id or scenario name appears in this file"),
    "security_labels.csv": dict(id="synthetic-security-labels", role="labels (never a detector input)", fields=["event_id", "episode", "label", "attack_kind", "benign_kind"],
                                observation_times="n/a", labels="label 1 = attack; attack_kind and benign_kind are for analysis only", splits="see security_splits.csv", exclusions="none"),
    "security_splits.csv": dict(id="synthetic-security-splits", role="split membership", fields=["event_id", "episode", "split"],
                                observation_times="n/a", labels="none", splits="train episodes 0-6, dev 7-8, test 9-11", exclusions="none"),
    "intents.jsonl": dict(id="synthetic-intents", role="tenant provisioning intents (management plane inputs)", fields=["intent_id", "tenant", "service", "kind", "servers", "site_to", "site_from", "sla_ms", "submitted_ms", "token", "policy_revision", "origin"],
                          observation_times="simulation milliseconds", labels="eligibility in intent_eligibility.csv", splits="none", exclusions="none"),
    "intent_eligibility.csv": dict(id="synthetic-intent-eligibility", role="eligibility fixed from policy alone, before any controller runs", fields=["intent_id", "eligible", "static_reasons"],
                                   observation_times="n/a", labels="eligible = 1 if static policy evaluation allows the intent", splits="none", exclusions="not derived from any run outcome"),
}


def build_registry(fixture_dir: Path) -> dict:
    files = []
    for name, meta in REGISTRY_ENTRIES.items():
        p = fixture_dir / name
        files.append({"file": f"examples/fixtures/{name}", "sha256": file_sha256(p), "rows": _csv_stats(p) if p.suffix == ".csv" else sum(1 for _ in open(p, encoding="utf-8")), **meta})
    return {
        "registry_version": "1.0",
        "datasets": [{
            "origin": "generated by this repository (sszt6g.fixtures) from fixed seeds; no external or original-paper data",
            "access_terms": "MIT licence, same as the repository; contains no personal or provider data",
            "version": "fixtures-1.0", "kind": "synthetic", "files": files,
        }],
        "original_paper_datasets": {
            "status": "not identified",
            "note": ("The paper describes a simulated testbed (OMNeT++, MATLAB Simulink, a Kubernetes Edge-AI cluster; 25 edge nodes in three domains; "
                     "50-500 Mbps per node; 5-20 intrusion attempts per minute; 100 iterations) but names no workload or security dataset and releases "
                     "no traces, code, labels, splits or class balance. Nothing here substitutes for them."),
        },
    }
