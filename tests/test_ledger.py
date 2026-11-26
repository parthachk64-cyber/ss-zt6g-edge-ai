import json

import pytest

from sszt6g.errors import LedgerUnavailable, QuorumNotReached
from sszt6g.ledger import AuditLog, QuorumLedger, verify_entries
from sszt6g.util import canonical, sha256_hex
from sszt6g.keys import fixture_identity

SIGNER = fixture_identity("test-signer")


def filled(n=5, path=None):
    log = AuditLog(path)
    for i in range(n):
        log.append({"action_id": f"a{i}", "state": "proposed", "v": i})
    return log


def test_clean_log_verifies_with_checkpoint():
    log = filled()
    cp = log.checkpoint(SIGNER)
    assert log.verify(cp, SIGNER.public_hex).ok


def test_modified_entry_is_detected():
    log = filled()
    ents = log.entries()
    ents[2]["entry"]["v"] = 999
    rep = verify_entries(ents)
    assert not rep.ok and any(i.startswith("hash_mismatch@3") for i in rep.issues)


def test_reordered_entries_detected():
    ents = filled().entries()
    ents[1], ents[2] = ents[2], ents[1]
    assert not verify_entries(ents).ok


def test_rewritten_chain_with_recomputed_hashes_caught_only_by_checkpoint():
    log = filled()
    cp = log.checkpoint(SIGNER)
    ents = log.entries()
    ents[1]["entry"]["v"] = 12345
    prev = "0" * 64
    for e in ents:  # attacker recomputes every hash: the chain alone now looks consistent
        e["prev"] = prev
        e["hash"] = sha256_hex(canonical({"seq": e["seq"], "prev": e["prev"], "entry": e["entry"]}))
        prev = e["hash"]
    assert verify_entries(ents).ok
    rep = verify_entries(ents, cp, SIGNER.public_hex)
    assert not rep.ok and "checkpoint_head_mismatch" in rep.issues


def test_truncation_detected_against_checkpoint():
    log = filled()
    cp = log.checkpoint(SIGNER)
    rep = verify_entries(log.entries()[:3], cp, SIGNER.public_hex)
    assert not rep.ok and any("truncated" in i for i in rep.issues)


def test_forged_checkpoint_rejected():
    log = filled()
    cp = log.checkpoint(SIGNER)
    cp["seq"] = 2
    assert "checkpoint_signature_invalid" in verify_entries(log.entries(), cp, SIGNER.public_hex).issues
    assert not verify_entries(log.entries(), log.checkpoint(SIGNER), fixture_identity("other").public_hex).ok


def test_appends_after_checkpoint_still_verify():
    log = filled(3)
    cp = log.checkpoint(SIGNER)
    log.append({"action_id": "z", "state": "x"})
    assert log.verify(cp, SIGNER.public_hex).ok


def test_persistence_and_reload(tmp_path):
    p = tmp_path / "log.jsonl"
    log = filled(4, p)
    again = AuditLog(p)
    assert again.head == log.head and again.verify().ok
    again.append({"action_id": "n", "state": "y"})
    assert AuditLog(p).verify().entries == 5


def test_outage_raises_and_records_nothing():
    log = filled(2)
    log.unavailable = True
    with pytest.raises(LedgerUnavailable):
        log.append({"action_id": "x", "state": "y"})
    assert len(log.entries()) == 2


def test_find_state_returns_receipt():
    log = filled(3)
    assert log.find_state("a1", "proposed").seq == 2 and log.find_state("a1", "applied") is None


# ---------------------------------------------------------------- quorum profile
def test_quorum_commits_and_receipt_lists_votes(tmp_path):
    q = QuorumLedger(4, directory=tmp_path)
    r = q.append({"action_id": "a", "state": "committed"})
    assert r.extra["quorum"] == 3 and len(r.extra["votes"]) == 4 and q.verify().ok


def test_quorum_tolerates_one_offline_validator_then_it_catches_up(tmp_path):
    q = QuorumLedger(4, directory=tmp_path)
    q.set_online("v4", False)
    q.append({"action_id": "a", "state": "x"})
    rep = q.verify()
    assert rep.ok and rep.detail["lagging"] == ["v4"]
    q.set_online("v4", True)
    assert q.verify().detail["lagging"] == []


def test_quorum_not_reached_with_two_offline():
    q = QuorumLedger(4)
    q.set_online("v3", False)
    q.set_online("v4", False)
    with pytest.raises(QuorumNotReached):
        q.append({"action_id": "a", "state": "x"})
    assert q.verify().entries == 0


def test_replica_disagreement_is_reported():
    q = QuorumLedger(4)
    for i in range(3):
        q.append({"action_id": f"a{i}", "state": "x"})
    q.validators[1].chain[1]["entries"][0]["state"] = "altered"
    rep = q.verify()
    assert not rep.ok and any(i.startswith("hash_mismatch@v2:2") for i in rep.issues) and any(i.startswith("replica_divergence@2") for i in rep.issues)


def test_forged_votes_do_not_count():
    q = QuorumLedger(4)
    q.append({"action_id": "a", "state": "x"})
    for v in q.validators[0].chain[0]["votes"]:
        v["sig"] = "00" * 64
    assert any(i.startswith("insufficient_valid_votes@v1") for i in q.verify().issues)


def test_restart_reloads_committed_blocks(tmp_path):
    q = QuorumLedger(4, directory=tmp_path)
    q.append({"action_id": "a", "state": "x"})
    q2 = QuorumLedger(4, directory=tmp_path)
    r = q2.append({"action_id": "b", "state": "y"})
    assert r.seq == 2 and q2.verify().ok and q2.find_state("a", "x") is not None


def test_replica_that_missed_a_commit_syncs_after_restart(tmp_path):
    q = QuorumLedger(4, directory=tmp_path)
    q.append({"action_id": "a", "state": "x"})
    q.validators[3].chain.clear()
    (tmp_path / "validator-v4.jsonl").write_text("")
    q2 = QuorumLedger(4, directory=tmp_path)
    assert q2.verify().detail["lagging"] == [] and len(q2.validators[3].chain) == 1


def test_block_on_one_replica_with_quorum_votes_is_committed(tmp_path):
    q = QuorumLedger(4, directory=tmp_path)
    q.append({"action_id": "a", "state": "x"})
    for name in ("v2", "v3", "v4"):  # a crash left the block on only one replica
        (tmp_path / f"validator-{name}.jsonl").write_text("")
    q2 = QuorumLedger(4, directory=tmp_path)
    assert [len(v.chain) for v in q2.validators] == [1, 1, 1, 1]


def test_quorum_find_state():
    q = QuorumLedger(4)
    q.append({"action_id": "a", "state": "applied"})
    assert q.find_state("a", "applied").ledger == "quorum" and q.find_state("a", "zzz") is None
