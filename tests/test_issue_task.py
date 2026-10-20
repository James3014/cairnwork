import json

import pytest

from cairnwork.issue_task import IssueRefused, parse_task_block, queue_trusted

GOOD = {
    "difficulty": "easy",
    "allowed_files": ["src/stats.py"],
    "verification": ["python3 -m pytest -q tests/test_stats.py"],
    "capability_ids": ["x"],
    "summary": "fix   average",
}


def body(block=GOOD, extra=""):
    return f"Bug text\n\n```eia-task\n{json.dumps(block)}\n```\n{extra}"


def test_a_good_block_parses():
    task = parse_task_block(body())
    assert task.allowed_files == ("src/stats.py",)
    assert task.verifiers == (("python3", "-m", "pytest", "-q", "tests/test_stats.py"),)
    assert task.summary == "fix average" and len(task.block_sha256) == 64


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("no block", "TASK_BLOCK_REQUIRED"),
        (body() + body(), "TASK_BLOCK_REQUIRED"),
        (body({**GOOD, "extra": 1}), "TASK_BLOCK_KEYS_INVALID"),
        (body({**GOOD, "difficulty": "hard"}), "TASK_DIFFICULTY_NOT_TAKEN"),
        (body({**GOOD, "allowed_files": []}), "TASK_ALLOWED_FILES_INVALID"),
        (body({**GOOD, "allowed_files": ["../x.py"]}), "TASK_ALLOWED_FILES_INVALID"),
        (body({**GOOD, "allowed_files": ["a/.hidden/x.py"]}), "TASK_ALLOWED_FILES_INVALID"),
        (body({**GOOD, "allowed_files": ["pyproject.toml"]}), "TASK_ALLOWED_FILES_PROTECTED"),
        (body({**GOOD, "allowed_files": ["src/conftest.py"]}), "TASK_ALLOWED_FILES_PROTECTED"),
        (body({**GOOD, "allowed_files": [".github/x.yml"]}), "TASK_ALLOWED_FILES_INVALID"),
        (body({**GOOD, "allowed_files": ["Tasks/card.md"]}), "TASK_ALLOWED_FILES_PROTECTED"),
        (
            body({**GOOD, "verification": ["sh -c 'git diff --check a b'"]}),
            "TASK_VERIFICATION_UNSUPPORTED",
        ),
        (
            body({**GOOD, "verification": ["python3 -m pytest -c x.ini tests/test_a.py"]}),
            "TASK_VERIFICATION_UNSUPPORTED",
        ),
        (body({**GOOD, "verification": ["python3 -m pytest -q"]}), "TASK_VERIFICATION_UNSUPPORTED"),
        (body(extra="```eia-contract\n{}\n```"), "TASK_BODY_HAS_FOREIGN_CONTRACT"),
        (body({**GOOD, "capability_ids": []}), "TASK_CAPABILITY_IDS_INVALID"),
    ],
)
def test_refused_blocks(text, code):
    with pytest.raises(IssueRefused) as caught:
        parse_task_block(text)
    assert caught.value.code == code


USER = {"login": "James3014", "__typename": "User"}
BOT = {"login": "james3014-nexus-agent", "__typename": "Bot"}
STRANGER = {"login": "someone", "__typename": "User"}
LABELED = {"event": "labeled", "label": "q", "actor": "James3014", "at": "2026-10-20T07:00:00Z"}


def trust(events=(LABELED,), author=USER, nodes=(), total=None):
    edits = {"nodes": list(nodes), "totalCount": len(nodes) if total is None else total}
    return queue_trusted(label="q", events=events, author=author, edits=edits)


def test_trusted_queue():
    assert trust() == ""
    assert trust(author=BOT) == ""
    early = {"editedAt": "2026-10-20T06:00:00Z", "editor": STRANGER}
    assert trust(nodes=[early]) == ""  # an edit before the label was reviewed by the labeler


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"events": ()}, "QUEUE_LABEL_EVENT_MISSING"),
        ({"events": ({**LABELED, "actor": "someone"},)}, "QUEUE_LABELED_BY_UNTRUSTED"),
        ({"author": STRANGER}, "AUTHOR_UNTRUSTED"),
        ({"author": {"login": "james3014-nexus-agent", "__typename": "User"}}, "AUTHOR_UNTRUSTED"),
        (
            {"nodes": [{"editedAt": "2026-10-20T08:00:00Z", "editor": STRANGER}]},
            "BODY_EDITED_BY_UNTRUSTED",
        ),
        ({"total": 5}, "EDIT_HISTORY_INCOMPLETE"),
        (
            {"nodes": [{"editedAt": "2026-10-20T07:00:00Z", "editor": STRANGER}]},
            "BODY_EDITED_BY_UNTRUSTED",
        ),
        ({"events": ({**LABELED, "at": "garbage"},)}, "QUEUE_LABEL_TIME_UNREADABLE"),
        (
            {"nodes": [{"editedAt": "not-a-time", "editor": STRANGER}]},
            "BODY_EDITED_BY_UNTRUSTED",
        ),
        (
            {
                "events": (
                    LABELED,
                    {"event": "renamed", "actor": "someone", "at": "2026-10-20T09:00:00Z"},
                )
            },
            "TITLE_EDITED_BY_UNTRUSTED",
        ),
    ],
)
def test_untrusted_queue(kwargs, reason):
    assert trust(**kwargs) == reason


def test_the_latest_label_event_counts():
    relabeled = {**LABELED, "actor": "someone", "at": "2026-10-20T10:00:00Z"}
    assert trust(events=(LABELED, relabeled)) == "QUEUE_LABELED_BY_UNTRUSTED"
