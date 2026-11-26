"""Audit ledgers.

Two profiles, deliberately named for what they establish:

* AuditLog ("tamper-evident-audit-log"): canonical-JSON entries hash-linked from a genesis value, plus a
  SEPARATELY RETAINED Ed25519-signed checkpoint. It detects alteration, reordering and truncation of
  history up to the checkpoint. It is one writer: it proves integrity, not agreement.

* QuorumLedger ("quorum-replicated-local"): N local validator replicas, each with its own Ed25519
  identity and its own chain. A block commits when at least `quorum` distinct validators have signed its
  hash. This is a crash-fault-tolerant commit rule inside one process. It is NOT Byzantine fault
  tolerant, uses no network and is NOT an implementation of the chain used in the paper.

What a quorum receipt establishes: at least `quorum` configured validator keys signed this block hash,
which commits to its predecessor hash, at this height. It does not establish wall-clock time, honest
validators, or independence beyond the key separation here.
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .errors import LedgerUnavailable, QuorumNotReached
from .keys import Identity, fixture_identity, verify_signature
from .util import canonical, sha256_hex, stable_id

GENESIS = "0" * 64


@dataclass(frozen=True)
class Receipt:
    ledger: str
    seq: int
    entry_hash: str
    head_hash: str
    state: str
    action_id: str
    extra: dict = field(default_factory=dict)

    @property
    def receipt_id(self) -> str:
        return stable_id("rcpt", self.ledger, self.seq, self.entry_hash)

    def to_dict(self) -> dict:
        return {"receipt_id": self.receipt_id, "ledger": self.ledger, "seq": self.seq, "entry_hash": self.entry_hash,
                "head_hash": self.head_hash, "state": self.state, "action_id": self.action_id, "extra": self.extra}


@dataclass
class VerifyReport:
    ok: bool
    issues: list
    entries: int
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"ok": self.ok, "issues": self.issues, "entries": self.entries, "detail": self.detail}


class NullLedger:
    """Records nothing. Benchmark-only: isolates the cost of audit commits in a simulated experiment."""

    benchmark_only = True
    profile = "none"

    def __init__(self):
        self.n = 0

    def append(self, entry: dict) -> Receipt:
        self.n += 1
        return Receipt("none", self.n, "", "", entry.get("state", ""), entry.get("action_id", ""))

    def find_state(self, action_id: str, state: str):
        return None

    def verify(self) -> VerifyReport:
        return VerifyReport(False, ["no_ledger"], 0)


class AuditLog:
    """Hash-linked, append-only, tamper-evident. Optionally persisted as JSON lines."""

    profile = "tamper-evident-audit-log"

    def __init__(self, path: Optional[Path | str] = None):
        self.path = Path(path) if path else None
        self._entries: list = []
        self.unavailable = False  # outage injection for tests
        if self.path and self.path.exists():
            self._entries = [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]

    @property
    def head(self) -> str:
        return self._entries[-1]["hash"] if self._entries else GENESIS

    def entries(self) -> list:
        return list(self._entries)

    def append(self, entry: dict) -> Receipt:
        if self.unavailable:
            raise LedgerUnavailable("audit log unavailable")
        body = {"seq": len(self._entries) + 1, "prev": self.head, "entry": entry}
        h = sha256_hex(canonical(body))
        rec = {**body, "hash": h}
        if self.path:
            with open(self.path, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(rec, sort_keys=True, separators=(",", ":")) + "\n")
        self._entries.append(rec)
        return Receipt("audit-log", rec["seq"], h, h, entry.get("state", ""), entry.get("action_id", ""))

    def find_state(self, action_id: str, state: str) -> Optional[Receipt]:
        for rec in self._entries:
            e = rec["entry"]
            if e.get("action_id") == action_id and e.get("state") == state:
                return Receipt("audit-log", rec["seq"], rec["hash"], rec["hash"], state, action_id)
        return None

    def checkpoint(self, signer: Identity) -> dict:
        body = {"seq": len(self._entries), "head": self.head}
        return {**body, "kid": signer.name, "sig": signer.sign(canonical(body))}

    def verify(self, checkpoint: Optional[dict] = None, signer_public_hex: Optional[str] = None) -> VerifyReport:
        return verify_entries(self._entries, checkpoint, signer_public_hex)


def verify_entries(entries: list, checkpoint: Optional[dict] = None, signer_public_hex: Optional[str] = None) -> VerifyReport:
    issues: list = []
    prev = GENESIS
    for i, rec in enumerate(entries, 1):
        try:
            body = {"seq": rec["seq"], "prev": rec["prev"], "entry": rec["entry"]}
            if rec["seq"] != i:
                issues.append(f"seq_mismatch@{i}")
            if rec["prev"] != prev:
                issues.append(f"prev_mismatch@{i}")
            if sha256_hex(canonical(body)) != rec["hash"]:
                issues.append(f"hash_mismatch@{i}")
            prev = rec["hash"]
        except (KeyError, TypeError):
            issues.append(f"malformed@{i}")
    if checkpoint is not None:
        body = {"seq": checkpoint.get("seq"), "head": checkpoint.get("head")}
        if signer_public_hex is None or not verify_signature(signer_public_hex, canonical(body), str(checkpoint.get("sig", ""))):
            issues.append("checkpoint_signature_invalid")
        elif checkpoint["seq"] > len(entries):
            issues.append(f"truncated: checkpoint covers {checkpoint['seq']} entries, log has {len(entries)}")
        elif checkpoint["seq"] > 0 and entries[checkpoint["seq"] - 1].get("hash") != checkpoint["head"]:
            issues.append("checkpoint_head_mismatch")
    return VerifyReport(not issues, issues, len(entries), {"checkpoint": checkpoint is not None})


# ------------------------------------------------------------------ quorum profile

def vote_message(block_hash: str) -> bytes:
    return b"sszt6g-vote-v1:" + block_hash.encode("ascii")


def block_hash(height: int, prev: str, proposer: str, entries: list) -> str:
    return sha256_hex(canonical({"height": height, "prev": prev, "proposer": proposer, "entries": entries}))


class Validator:
    def __init__(self, name: str, directory: Optional[Path] = None):
        self.name = name
        self.identity = fixture_identity("validator:" + name)
        self.online = True
        self.chain: list = []
        self.path = (Path(directory) / f"validator-{name}.jsonl") if directory else None
        if self.path and self.path.exists():
            self.chain = [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]

    @property
    def head(self) -> str:
        return self.chain[-1]["hash"] if self.chain else GENESIS

    def vote_if_valid(self, height: int, prev: str, proposer: str, entries: list, bhash: str) -> Optional[str]:
        """Independently re-derive the hash and check it extends THIS validator's own chain."""
        if not self.online:
            return None
        if height != len(self.chain) + 1 or prev != self.head or block_hash(height, prev, proposer, entries) != bhash:
            return None
        return self.identity.sign(vote_message(bhash))

    def commit(self, block: dict) -> None:
        self.chain.append(copy.deepcopy(block))  # each replica owns its copy: no aliasing between validators
        if self.path:
            with open(self.path, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(block, sort_keys=True, separators=(",", ":")) + "\n")


class QuorumLedger:
    profile = "quorum-replicated-local"

    def __init__(self, validators: int = 4, quorum: Optional[int] = None, directory: Optional[Path | str] = None):
        self.dir = Path(directory) if directory else None
        if self.dir:
            self.dir.mkdir(parents=True, exist_ok=True)
        self.validators = [Validator(f"v{i + 1}", self.dir) for i in range(validators)]
        self.quorum = quorum if quorum is not None else validators // 2 + 1
        self.pubkeys = {v.name: v.identity.public_hex for v in self.validators}
        self.recover()

    # -- helpers
    def _valid_block(self, block: dict) -> bool:
        votes = block.get("votes", [])
        good = {v["validator"] for v in votes if self.pubkeys.get(v["validator"]) and verify_signature(self.pubkeys[v["validator"]], vote_message(block["hash"]), v["sig"])}
        return len(good) >= self.quorum and block_hash(block["height"], block["prev"], block["proposer"], block["entries"]) == block["hash"]

    def recover(self) -> None:
        """Bring every online validator up to the longest chain whose blocks carry a valid quorum of votes.
        A block present on any replica with >= quorum valid votes is, by definition, committed."""
        best: list = []
        for v in self.validators:
            ok, prev = [], GENESIS
            for b in v.chain:
                if b["prev"] != prev or not self._valid_block(b):
                    break
                ok.append(b)
                prev = b["hash"]
            if len(ok) > len(best):
                best = ok
        for v in self.validators:
            if v.online and len(v.chain) < len(best) and v.chain == best[: len(v.chain)]:
                for b in best[len(v.chain):]:
                    v.commit(b)

    def set_online(self, name: str, online: bool) -> None:
        for v in self.validators:
            if v.name == name:
                v.online = online
        if online:
            self.recover()

    # -- ledger API
    def _lagging(self) -> bool:
        longest = max(len(v.chain) for v in self.validators)
        return any(v.online and len(v.chain) < longest for v in self.validators)

    def append(self, entry: dict) -> Receipt:
        if self._lagging():  # full re-validation only when a replica actually needs to catch up
            self.recover()
        online = [v for v in self.validators if v.online]
        if len(online) < self.quorum:
            raise QuorumNotReached(f"{len(online)} validators online, quorum is {self.quorum}")
        ref = max(online, key=lambda v: len(v.chain))
        height, prev = len(ref.chain) + 1, ref.head
        proposer = ref.name
        bhash = block_hash(height, prev, proposer, [entry])
        votes = []
        for v in self.validators:
            sig = v.vote_if_valid(height, prev, proposer, [entry], bhash)
            if sig:
                votes.append({"validator": v.name, "sig": sig})
        if len(votes) < self.quorum:
            raise QuorumNotReached(f"{len(votes)} valid votes, quorum is {self.quorum}")
        block = {"height": height, "prev": prev, "proposer": proposer, "entries": [entry], "hash": bhash, "votes": votes}
        for v in self.validators:
            if any(x["validator"] == v.name for x in votes):
                v.commit(block)
        return Receipt("quorum", height, sha256_hex(canonical(entry)), bhash, entry.get("state", ""), entry.get("action_id", ""),
                       {"votes": [x["validator"] for x in votes], "quorum": self.quorum, "validators": len(self.validators)})

    def find_state(self, action_id: str, state: str) -> Optional[Receipt]:
        for b in max((v.chain for v in self.validators), key=len, default=[]):
            for e in b["entries"]:
                if e.get("action_id") == action_id and e.get("state") == state:
                    return Receipt("quorum", b["height"], sha256_hex(canonical(e)), b["hash"], state, action_id)
        return None

    def verify(self) -> VerifyReport:
        issues: list = []
        for v in self.validators:
            prev = GENESIS
            for i, b in enumerate(v.chain, 1):
                if b["height"] != i:
                    issues.append(f"height_mismatch@{v.name}:{i}")
                if b["prev"] != prev:
                    issues.append(f"prev_mismatch@{v.name}:{i}")
                if block_hash(b["height"], b["prev"], b["proposer"], b["entries"]) != b["hash"]:
                    issues.append(f"hash_mismatch@{v.name}:{i}")
                if not self._valid_block(b):
                    issues.append(f"insufficient_valid_votes@{v.name}:{i}")
                prev = b["hash"]
        max_h = max((len(v.chain) for v in self.validators), default=0)
        for h in range(1, max_h + 1):
            hashes = {v.name: block_hash(v.chain[h - 1]["height"], v.chain[h - 1]["prev"], v.chain[h - 1]["proposer"], v.chain[h - 1]["entries"])
                      for v in self.validators if len(v.chain) >= h}  # recomputed, so edited content cannot hide behind a stored hash
            if len(set(hashes.values())) > 1:
                issues.append(f"replica_divergence@{h}:{sorted(hashes)}")
        lagging = [v.name for v in self.validators if len(v.chain) < max_h]
        return VerifyReport(not issues, issues, max_h, {"lagging": lagging, "quorum": self.quorum, "validators": len(self.validators)})
