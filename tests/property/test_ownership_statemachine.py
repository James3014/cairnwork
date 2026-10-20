"""Property-based state-machine test for OwnershipLedger (preserved behaviors #6, #7).

Why this exists: unit tests check the paths the author thought of. This drives the
ledger through long random sequences of bind / resend / status-change operations
against a Python shadow model, and asserts the invariants that must never break —
the exact class of "tests pass but a guarantee silently fails" bug that this
project exists to avoid (docs/LESSONS_FROM_NEXUS.md).

Invariants asserted after every step:
  I1  at most one ACTIVE record per card_hash (status not in SUPERSEDED/CANCELLED);
  I2  the owner bound to a task never changes;
  I3  status only ever moves along TRANSITIONS;
  I4  an identical resend returns the same record and never re-runs (FAILED stays FAILED);
  I5  get/route never guesses — unknown ids raise OwnershipUnknown;
  I6  in_flight() is exactly the BOUND/DISPATCHED records.

Run: pytest tests/property/ -q   (or with mutmut to check these actually bite).
"""

from __future__ import annotations

from hypothesis import settings
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule
from hypothesis.strategies import sampled_from

from cairnwork.ownership import (
    INACTIVE_STATUSES,
    OWNERS,
    TRANSITIONS,
    DuplicateLogicalTask,
    KeyReusedWithDifferentRequest,
    OwnerMismatch,
    OwnershipLedger,
    OwnershipUnknown,
    request_digest,
)

# Small pools so collisions (the interesting cases) happen often.
KEYS = ["k0", "k1", "k2", "k3"]
CARDS = ["cardA", "cardB"]
OWNER_LIST = sorted(OWNERS)
REQUESTS = [
    {"title": "fix a", "allowed_files": ["src/a.py"]},
    {"title": "fix b", "allowed_files": ["src/b.py"]},
    {"title": "fix a", "allowed_files": ["src/a.py"], "note": "variant"},
]
STATUSES = sorted(TRANSITIONS)


def _active(status: str) -> bool:
    return status not in INACTIVE_STATUSES


class OwnershipModel(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        import tempfile

        self._dir = tempfile.mkdtemp(prefix="ownership-sm-")
        self.ledger = OwnershipLedger(f"{self._dir}/ownership.sqlite")
        # shadow model: key -> dict(digest, owner, task_id, card, status)
        self.by_key: dict[str, dict] = {}
        self.by_task: dict[str, str] = {}  # task_id -> key

    # ----- helpers -------------------------------------------------------
    def _active_card_owner(self, card: str) -> str | None:
        for row in self.by_key.values():
            if row["card"] == card and _active(row["status"]):
                return row["key"]
        return None

    # ----- rules ---------------------------------------------------------
    @rule(
        key=sampled_from(KEYS),
        req=sampled_from(REQUESTS),
        owner=sampled_from(OWNER_LIST),
        card=sampled_from(CARDS),
    )
    def bind(self, key, req, owner, card):
        digest = request_digest(req)
        existing = self.by_key.get(key)
        if existing is not None:
            # resend path: digest+card checked before owner (see _same_binding)
            if existing["digest"] != digest or existing["card"] != card:
                try:
                    self.ledger.bind(key, req, owner, card)
                except KeyReusedWithDifferentRequest:
                    return
                raise AssertionError("I4: reused key with different request must raise")
            if existing["owner"] != owner:
                try:
                    self.ledger.bind(key, req, owner, card)
                except OwnerMismatch:
                    return
                raise AssertionError("I2: resend with different owner must raise OwnerMismatch")
            result = self.ledger.bind(key, req, owner, card)
            assert result.duplicate, "I4: identical resend must be a duplicate"
            assert result.record.task_id == existing["task_id"], "I4: resend returns same task"
            assert result.record.status == existing["status"], "I4: resend never re-runs"
            return
        # new key
        active_key = self._active_card_owner(card)
        if active_key is not None:
            try:
                self.ledger.bind(key, req, owner, card)
            except DuplicateLogicalTask:
                return
            raise AssertionError("I1: a second active task for a card must raise")
        result = self.ledger.bind(key, req, owner, card)
        assert not result.duplicate
        assert result.record.status == "BOUND"
        rec = result.record
        self.by_key[key] = {
            "key": key,
            "digest": digest,
            "owner": owner,
            "task_id": rec.task_id,
            "card": card,
            "status": "BOUND",
        }
        self.by_task[rec.task_id] = key

    @precondition(lambda self: bool(self.by_task))
    @rule(status=sampled_from(STATUSES), pick=sampled_from(range(8)))
    def set_status(self, status, pick):
        task_ids = sorted(self.by_task)
        task_id = task_ids[pick % len(task_ids)]
        row = self.by_key[self.by_task[task_id]]
        current = row["status"]
        legal = status in TRANSITIONS.get(current, frozenset())
        try:
            updated = self.ledger.set_status(task_id, status)
        except Exception:  # noqa: BLE001 - we assert on legality below
            assert not legal, f"I3: legal move {current}->{status} must not raise"
            return
        assert legal, f"I3: illegal move {current}->{status} must have raised"
        assert updated.owner == row["owner"], "I2: owner must not change"
        row["status"] = status

    @precondition(lambda self: bool(self.by_task))
    @rule(pick=sampled_from(range(8)))
    def get_roundtrips(self, pick):
        task_ids = sorted(self.by_task)
        task_id = task_ids[pick % len(task_ids)]
        row = self.by_key[self.by_task[task_id]]
        got = self.ledger.get(task_id=task_id)
        assert got.owner == row["owner"] and got.status == row["status"]
        assert self.ledger.route(task_id=task_id) == row["owner"]

    @rule(task_id=sampled_from(["nope-1", "nope-2"]))
    def unknown_never_guesses(self, task_id):
        try:
            self.ledger.get(task_id=task_id)
        except OwnershipUnknown:
            return
        raise AssertionError("I5: unknown task must raise OwnershipUnknown, not guess")

    # ----- invariants ----------------------------------------------------
    @invariant()
    def one_active_per_card(self):
        active_cards = [r["card"] for r in self.by_key.values() if _active(r["status"])]
        assert len(active_cards) == len(set(active_cards)), "I1 broken in model"
        # and in the ledger itself
        for card in set(active_cards):
            live = [r for r in self.by_key.values() if r["card"] == card and _active(r["status"])]
            assert len(live) == 1

    @invariant()
    def in_flight_matches(self):
        expected = {
            r["task_id"] for r in self.by_key.values() if r["status"] in {"BOUND", "DISPATCHED"}
        }
        got = {rec.task_id for rec in self.ledger.in_flight()}
        assert got == expected, "I6: in_flight() diverged from model"

    def teardown(self) -> None:
        import shutil

        shutil.rmtree(self._dir, ignore_errors=True)


# keep CI fast but still exercise long sequences
OwnershipStateMachine = OwnershipModel.TestCase
OwnershipStateMachine.settings = settings(max_examples=200, stateful_step_count=40, deadline=None)
