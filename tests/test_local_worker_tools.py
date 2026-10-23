"""experimental.local_worker.tools: every tool call goes to the worker wrapper, nothing else."""

import ast
import json
from pathlib import Path

import pytest

from experimental.local_worker import tools
from experimental.local_worker.tools import WorkerIsolationError, WorkerTools

SPEC_TURN_FIELDS = {
    "turn_index",
    "role",
    "action_type",
    "tool_args",
    "exit_code",
    "stdout_bytes",
    "stdout_truncated",
    "error_category",
    "duration_ms",
}


class FakeInvoker:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, argv, stdin, timeout):
        self.calls.append((argv, stdin, timeout))
        return self.replies.pop(0) if self.replies else (0, b"", b"")


def make(tmp_path, *replies, **kw):
    inv = FakeInvoker(*replies)
    t = WorkerTools(tmp_path / "run", tmp_path / "calls.jsonl", invoker=inv, **kw)
    return t, inv


def log_lines(tmp_path):
    return [json.loads(x) for x in (tmp_path / "calls.jsonl").read_text().splitlines()]


def test_worker_mode_goes_through_sudo_as_the_worker(tmp_path):
    t, inv = make(tmp_path, (0, b"a.txt\n", b""))
    call = t.list_dir()
    argv = inv.calls[0][0]
    assert argv[:5] == [
        "/usr/bin/sudo",
        "-n",
        "-u",
        "_cairnwork_worker",
        str(tools.INSTALLED_WRAPPER),
    ]
    assert argv[5:] == ["list", str(tmp_path / "run"), "."]
    assert call.output == "a.txt\n" and call.exec_identity == "_cairnwork_worker"
    assert call.isolation == "sudo+seatbelt"


def test_direct_mode_runs_the_wrapper_copy_as_the_owner(tmp_path):
    t, inv = make(tmp_path, mode="direct", wrapper=Path("/w/local-worker-tool"))
    t.read_file("a.py")
    assert inv.calls[0][0][:3] == ["/bin/sh", "/w/local-worker-tool", "read"]
    assert log_lines(tmp_path)[0]["isolation"] == "seatbelt-only"


def test_unknown_mode_is_refused(tmp_path):
    with pytest.raises(ValueError):
        WorkerTools(tmp_path, tmp_path / "l", mode="owner")


def test_write_sends_content_on_stdin_and_logs_spec_fields(tmp_path):
    t, inv = make(tmp_path)
    t.write_file("src/x.py", "x = 2\n")
    argv, stdin, _ = inv.calls[0]
    assert argv[-2:] == [str(tmp_path / "run"), "src/x.py"] and argv[-3] == "write"
    assert stdin == b"x = 2\n"
    (rec,) = log_lines(tmp_path)
    assert SPEC_TURN_FIELDS <= rec.keys()
    assert rec["action_type"] == "write_file" and rec["role"] == "tool_result"
    assert rec["tool_args"] == {"path": "src/x.py", "content": "x = 2\n"}
    assert rec["error_category"] == "NONE" and rec["exit_code"] == 0


def test_turn_index_counts_calls_and_as_turn_adds_tokens(tmp_path):
    t, _ = make(tmp_path)
    t.list_dir()
    call = t.read_file("a")
    assert call.turn_index == 1
    turn = call.as_turn(10, 2, 7)
    assert (turn["tokens_prompt"], turn["tokens_completion"], turn["tokens_cached"]) == (10, 2, 7)
    assert "output" not in turn and "raw" not in turn


def test_large_output_is_clipped_head_and_tail_with_the_true_size(tmp_path):
    big = b"H" * 600 + b"M" * 5000 + b"T" * 600
    t, _ = make(tmp_path, (1, big, b""), max_output_bytes=1000)
    call = t.run_tests(["-q"])
    assert call.stdout_truncated and call.stdout_bytes == len(big)
    assert call.output.startswith("H" * 500) and call.output.endswith("T" * 500)
    assert "truncated" in call.output
    assert log_lines(tmp_path)[0]["stdout_truncated"] is True


@pytest.mark.parametrize(
    ("action", "rc", "out", "category"),
    [
        ("run_test", 1, b"1 failed", "TEST_FAILED"),
        ("run_test", 2, b"E   SyntaxError: invalid", "SYNTAX_ERROR"),
        ("run_test", 5, b"no tests ran", "NO_TESTS"),
        ("run_test", 3, b"internal", "TOOL_ERROR"),
        ("read_file", 64, b"refusing", "REFUSED"),
        ("read_file", 1, b"cat: x", "TOOL_ERROR"),
        ("read_file", None, b"", "TIMEOUT"),
        ("read_file", 0, b"", "NONE"),
    ],
)
def test_error_category(action, rc, out, category):
    assert tools.error_category(action, rc, out) == category


def test_timeout_sweeps_the_run(tmp_path):
    t, inv = make(tmp_path, (None, b"partial", b""), (0, b"LOCAL_WORKER_SWEEP clean\n", b""))
    call = t.run_tests(["-q"])
    assert call.error_category == "TIMEOUT"
    assert inv.calls[1][0][-2:] == ["sweep", str(tmp_path / "run")]
    assert [r["action_type"] for r in log_lines(tmp_path)] == ["run_test", "sweep"]


def test_sudo_failure_fails_closed(tmp_path):
    t, _ = make(tmp_path, (1, b"", b"sudo: a password is required\n"))
    with pytest.raises(WorkerIsolationError):
        t.read_file("a.py")


def test_edit_reads_and_writes_through_the_worker(tmp_path):
    t, inv = make(tmp_path, (0, b"a = 1\nb = 2\n", b""), (0, b"", b""))
    call = t.edit_file("m.py", "b = 2", "b = 3")
    assert [c[0][5] for c in inv.calls] == ["read", "write"]
    assert inv.calls[1][1] == b"a = 1\nb = 3\n"
    assert call.action_type == "edit_file" and call.exit_code == 0
    assert len(log_lines(tmp_path)) == 1


@pytest.mark.parametrize("text", [b"x\n", b"b = 2\nb = 2\n", b"\xff\xfe"])
def test_edit_without_a_single_match_writes_nothing(tmp_path, text):
    t, inv = make(tmp_path, (0, text, b""))
    call = t.edit_file("m.py", "b = 2", "b = 3")
    assert len(inv.calls) == 1 and call.exit_code == 1


def test_edit_passes_a_refused_read_through(tmp_path):
    t, inv = make(tmp_path, (64, b"", b"local-worker-tool: refusing: x\n"))
    call = t.edit_file("../x", "a", "b")
    assert len(inv.calls) == 1 and call.error_category == "REFUSED"


def test_tools_module_opens_only_its_log():
    """The loop may hand the model only worker output: tools.py never reads a file itself."""
    tree = ast.parse(Path(tools.__file__).read_text())
    file_access = {
        "open",
        "read_text",
        "read_bytes",
        "write_text",
        "write_bytes",
        "listdir",
        "scandir",
        "iterdir",
        "glob",
        "rglob",
        "walk",
    }
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if name in file_access:
                hits.append(ast.unparse(fn))
    assert hits == ["self.log_path.open"]


def test_wrapper_that_did_not_start_fails_closed(tmp_path):
    t, _ = make(tmp_path, (127, b"", b"env: sandbox-exec: No such file or directory\n"))
    with pytest.raises(WorkerIsolationError):
        t.read_file("a.py")


def test_every_test_run_is_followed_by_a_sweep(tmp_path):
    t, inv = make(tmp_path, (0, b"1 passed", b""))
    t.run_tests(["-q"])
    t.read_file("a.py")
    assert [c[0][5] for c in inv.calls] == ["pytest", "sweep", "read"]


def test_edit_timeout_sweeps(tmp_path):
    t, inv = make(tmp_path, (0, b"a = 1\n", b""), (None, b"", b""))
    call = t.edit_file("m.py", "a = 1", "a = 2")
    assert call.error_category == "TIMEOUT"
    assert [c[0][5] for c in inv.calls] == ["read", "write", "sweep"]


def test_subprocess_invoker_times_out_and_bounds_capture(monkeypatch):
    monkeypatch.setattr(tools, "CAPTURE_LIMIT", 1000)
    invoke = tools.subprocess_invoker()
    rc, out, err = invoke(["/bin/sh", "-c", "head -c 5000 /dev/zero; echo e >&2"], None, 10)
    assert rc == 0 and len(out) < 1100 and b"not captured" in out and err == b"e\n"
    # A grandchild holding stdout open must not hang the Owner after the timeout.
    rc, _, _ = invoke(["/bin/sh", "-c", "(sleep 30 &) ; sleep 30"], None, 0.5)
    assert rc is None
