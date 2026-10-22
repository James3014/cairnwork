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
  I6  in_flight() is exactly our BOUND/DISPATCHED records (include_foreign: all of them);
  I7  an identical request under a new key and another card is refused while one is active
      (REPLAY_FORBIDDEN if COMPLETED/FAILED, else RECONCILIATION_REQUIRED).

Run: pytest tests/property/ -q   (or with mutmut to check these actually bite).
"""

from __future__ import annotations

from hypothesis import settings
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule
from hypothesis.strategies import sampled_from

from cairnwork.ownership import (
    OWNER,
    OWNERS,
    DuplicateLogicalTask,
    InvalidOwnershipValue,
    InvalidStatusTransition,
    KeyReusedWithDifferentRequest,
    OwnerMismatch,
    OwnershipLedger,
    OwnershipUnknown,
    ReconciliationRequired,
    ReplayForbidden,
    request_digest,
)

# Written by hand here, NOT imported from ownership.py: importing the transition table would make
# the test use the code under test as its own oracle, so a wrong table (e.g. a CANCELLED -> BOUND
# move reviving a cancelled task) could never be caught. Keep this in sync deliberately.
ALLOWED = {
    "BOUND": {"DISPATCHED", "FAILED", "CANCELLED", "SUPERSEDED"},
    "DISPATCHED": {"COMPLETED", "FAILED", "CANCELLED"},
    "FAILED": {"SUPERSEDED"},
    "COMPLETED": set(),
    "CANCELLED": set(),
    "SUPERSEDED": set(),
}
INACTIVE = {"SUPERSEDED", "CANCELLED"}

# Small pools so collisions (the interesting cases) happen often.
KEYS = ["k0", "k1", "k2", "k3"]
CARDS = ["cardA", "cardB"]
OWNER_LIST = sorted(OWNERS)
REQUESTS = [
    {"title": "fix a", "allowed_files": ["src/a.py"]},
    {"title": "fix b", "allowed_files": ["src/b.py"]},
    {"title": "fix a", "allowed_files": ["src/a.py"], "note": "variant"},
]
STATUSES = sorted(ALLOWED)


def _active(status: str) -> bool:
    return status not in INACTIVE


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
        # new key: an active task with the same request on another card is a replay
        twins = [
            r["status"]
            for r in self.by_key.values()
            if r["digest"] == digest and r["card"] != card and _active(r["status"])
        ]
        if twins:
            expected = (
                ReplayForbidden
                if any(t in {"COMPLETED", "FAILED"} for t in twins)
                else ReconciliationRequired
            )
            try:
                self.ledger.bind(key, req, owner, card)
            except expected:
                return
            raise AssertionError(f"I7: replay of an active request must raise {expected.__name__}")
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
        legal = status in ALLOWED.get(current, set())
        try:
            updated = self.ledger.set_status(task_id, status)
        except (InvalidStatusTransition, InvalidOwnershipValue):
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
        for lookup in (self.ledger.get, self.ledger.route):
            try:
                lookup(task_id=task_id)
            except OwnershipUnknown:
                continue
            raise AssertionError("I5: unknown task must raise OwnershipUnknown, not guess")

    @precondition(lambda self: bool(self.by_task))
    @rule(pick=sampled_from(range(8)))
    def compare_and_set_refuses_a_wrong_expected_from(self, pick):
        task_id = sorted(self.by_task)[pick % len(self.by_task)]
        row = self.by_key[self.by_task[task_id]]
        wrong = next(s for s in STATUSES if s != row["status"])
        try:
            self.ledger.set_status(task_id, "DISPATCHED", expected_from=wrong)
        except InvalidStatusTransition:
            return  # compare-and-set refuses: the task is not in `wrong`
        raise AssertionError("I-CAS: expected_from mismatch must refuse the move")

    # ----- invariants ----------------------------------------------------
    @invariant()
    def ledger_matches_model_and_one_active_per_card(self):
        # read every known task FROM THE LEDGER (not the model) so a divergent database is caught
        active_per_card: dict[str, int] = {}
        for row in self.by_key.values():
            got = self.ledger.get(task_id=row["task_id"])
            assert got.owner == row["owner"], "I2: owner changed in the ledger"
            assert got.status == row["status"], "ledger status diverged from the model"
            assert got.card_hash == row["card"], "ledger card diverged from the model"
            if _active(got.status):
                active_per_card[got.card_hash] = active_per_card.get(got.card_hash, 0) + 1
        assert all(n == 1 for n in active_per_card.values()), (
            "I1: a card has >1 active in the ledger"
        )

    @invariant()
    def in_flight_matches(self):
        expected = {
            r["task_id"] for r in self.by_key.values() if r["status"] in {"BOUND", "DISPATCHED"}
        }
        got = {rec.task_id for rec in self.ledger.in_flight(include_foreign=True)}
        assert got == expected, "I6: in_flight() diverged from model"
        ours = {r["task_id"] for r in self.by_key.values() if r["owner"] == OWNER} & expected
        assert {rec.task_id for rec in self.ledger.in_flight()} == ours, "I6: foreign row counted"

    def teardown(self) -> None:
        import shutil

        shutil.rmtree(self._dir, ignore_errors=True)


# keep CI fast but still exercise long sequences
OwnershipStateMachine = OwnershipModel.TestCase
OwnershipStateMachine.settings = settings(max_examples=200, stateful_step_count=40, deadline=None)
