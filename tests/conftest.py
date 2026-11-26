import pytest

from sszt6g.config import build_world
from sszt6g.experiments import train_forecaster
from sszt6g.ledger import AuditLog
from sszt6g.orchestrator import OrchConfig, Orchestrator, ProvisioningIntent
from sszt6g.simulator import Cluster
from sszt6g.trust import issue_token, make_claim
from sszt6g.util import stable_id


@pytest.fixture(scope="session")
def world():
    return build_world()


@pytest.fixture(scope="session")
def predictor(world):
    model, _ = train_forecaster(world)
    return model


@pytest.fixture
def orch(world):
    o = Orchestrator(world.topo, Cluster(world.topo), world.policy, world.keyring, AuditLog(), ":memory:", OrchConfig())
    yield o
    o.close()


def make_intent(world, tag="a", tenant="acme-video", service="vision-inference", kind="reserve", servers=1, site_to="edge-east", site_from=None,
                at=1000.0, ttl=3_600_000.0, issued=0.0, posture="compliant", rev=None, token_tenant=None):
    rev = world.policy.revision if rev is None else rev
    tok = issue_token(world.issuer, make_claim(f"{tenant}-ops", token_tenant or tenant, issued, ttl, posture=posture, policy_revision=rev))
    return ProvisioningIntent(stable_id("int", "t", tag), tenant, service, kind, servers, site_to, site_from, 20000.0, at, tok, rev, "tenant")
