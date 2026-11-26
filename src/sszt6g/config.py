"""Configuration: topology, policy and experiment settings, loaded from configs/*.yaml with built-in defaults."""
from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

from .keys import Identity, fixture_identity
from .trust import Keyring, PolicyStore, issue_token, make_claim
from .workload import Topology, default_topology

ISSUER_NAME = "fixture-issuer-a"
ATTACKER_ISSUER_NAME = "fixture-rogue-issuer"
CHECKPOINT_SIGNER = "audit-checkpoint-signer"

DEFAULT_POLICY = {
    "revision": 1,
    "tenants": {
        "acme-video": {"ops": ["reserve", "move", "retain", "read_telemetry"], "services": ["vision-inference"], "sites": [], "max_servers": 6},
        "city-traffic": {"ops": ["reserve", "retain", "read_telemetry"], "services": ["traffic-analytics"], "sites": [], "max_servers": 8},
        "metro-health": {"ops": ["reserve", "retain", "read_telemetry"], "services": ["triage-vision"], "sites": ["edge-east"], "max_servers": 3},
    },
}

DEFAULT_SETTINGS = {
    "seeds": {"train": list(range(1, 25)), "dev": [501, 502, 503, 504], "test": list(range(1001, 1009)), "demo": 1001},
    "forecaster": {"alpha": 3.0, "lags": 6},
    "planner": {"target_util": 0.7, "trigger_util": 0.8, "queue_trigger": 6.0, "max_step": 3},
    "orchestrator": {"ttl_ms": 30000.0, "max_snapshot_age_ms": 15000.0, "sla_ms": 20000.0, "cooldown_ms": 10000.0},
    "faults": {"lost_activation_frac": 0.1, "salt": "sszt6g-fault-v1"},
    "test_scenarios": [
        {"kind": "ramp_burst", "peak": 24.0, "ramp_s": 20.0},
        {"kind": "step_burst", "peak": 24.0, "ramp_s": 0.0},
        {"kind": "steady_low", "peak": 6.0, "ramp_s": 0.0},
    ],
    "intents": {"eligible": 6, "ineligible": 4, "duplicates": 2},
    "security": {"episodes": 12, "events_per_episode": 150, "train_episodes": 7, "dev_episodes": 2, "seed": 4242},
    "overhead": {"appends": 400, "evaluations": 3000},
}


@dataclass
class World:
    topo: Topology
    policy: PolicyStore
    keyring: Keyring
    issuer: Identity
    checkpoint_signer: Identity
    tokens: dict  # tenant -> long-lived controller token
    settings: dict


def repo_root() -> Path:
    here = Path(__file__).resolve().parents[2]
    if (here / "pyproject.toml").exists():
        return here
    cwd = Path.cwd()
    for p in [cwd, *cwd.parents]:
        if (p / "pyproject.toml").exists():
            return p
    return cwd


def _load_yaml(path: Path, default: dict) -> dict:
    if path.exists():
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    return copy.deepcopy(default)


def build_world(config_dir: Optional[Path] = None) -> World:
    cdir = Path(config_dir) if config_dir else repo_root() / "configs"
    topo = Topology.from_dict(_load_yaml(cdir / "topology.yaml", default_topology().to_dict()))
    policy = PolicyStore.from_dict(_load_yaml(cdir / "policy.yaml", DEFAULT_POLICY))
    settings = _load_yaml(cdir / "settings.yaml", DEFAULT_SETTINGS)
    issuer = fixture_identity(ISSUER_NAME)
    keyring = Keyring()
    keyring.add(issuer)
    tokens = {t: issue_token(issuer, make_claim(f"controller:{t}", t, 0.0, 3_600_000.0, policy_revision=policy.revision)) for t in topo.tenant_quota}
    return World(topo, policy, keyring, issuer, fixture_identity(CHECKPOINT_SIGNER), tokens, settings)


def write_default_configs(config_dir: Path) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "topology.yaml").write_text(yaml.safe_dump(default_topology().to_dict(), sort_keys=False), encoding="utf-8")
    (config_dir / "policy.yaml").write_text(yaml.safe_dump(DEFAULT_POLICY, sort_keys=False), encoding="utf-8")
    (config_dir / "settings.yaml").write_text(yaml.safe_dump(DEFAULT_SETTINGS, sort_keys=False), encoding="utf-8")
