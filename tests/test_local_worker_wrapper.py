"""local-worker-tool path checks, and the isolation canary in direct (Seatbelt-only) mode.

The worker-mode canary needs the installed wrapper and sudoers rule; it is run by hand
(python -m experimental.local_worker.canary --mode worker) and its report sha256 is frozen in the
addendum. Here the same canary runs as the Owner so Seatbelt regressions show up in CI.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from experimental.local_worker import canary

WRAPPER = Path(canary.__file__).with_name("local-worker-tool")
TAG = "0123456789abcdef0123456789abcdef"


@pytest.fixture
def runs(tmp_path):
    runs = (tmp_path / "runs").resolve()
    (runs / TAG / "checkout" / "sub").mkdir(parents=True)
    (runs / TAG / "checkout" / "a.txt").write_text("hello\n")
    (runs / ("f" * 32)).mkdir()
    (runs / ("f" * 32) / "secret.txt").write_text("s\n")
    return runs


def run(runs, *args, stdin=b""):
    env = dict(
        os.environ,
        CAIRNWORK_LOCAL_WORKER_RUNS=str(runs),
        CAIRNWORK_LOCAL_WORKER_PYROOT=sys.base_prefix,
    )
    return subprocess.run(
        ["/bin/sh", str(WRAPPER), *args],
        input=stdin,
        capture_output=True,
        env=env,
        cwd="/",
        check=False,
    )


@pytest.mark.parametrize(
    "args",
    [
        ("read", "{runs}/" + "0" * 31, "a.txt"),  # not a run tag
        ("read", "{runs}/" + "A" * 32, "a.txt"),
        ("read", "/tmp/" + TAG, "a.txt"),  # not inside RUNS
        ("read", "{runs}/" + "f" * 32, "secret.txt"),  # no checkout
        ("read", "{runs}/" + TAG, "../a.txt"),
        ("read", "{runs}/" + TAG, "sub/../a.txt"),
        ("read", "{runs}/" + TAG, "/etc/passwd"),
        ("read", "{runs}/" + TAG, "./a.txt"),
        ("read", "{runs}/" + TAG, "-n"),
        ("read", "{runs}/" + TAG, ""),
        ("read", "{runs}/" + TAG, "sub"),  # a directory is not a file
        ("list", "{runs}/" + TAG, "a.txt"),  # a file is not a directory
        ("list", "{runs}/" + TAG, ".."),
        ("write", "{runs}/" + TAG, ".git/config"),
        ("write", "{runs}/" + TAG, "sub/.git/hooks/x"),
        ("write", "{runs}/" + TAG, "sub"),  # existing directory
        ("read", "{runs}/" + TAG, "a.txt", "extra"),
        ("sweep", "{runs}/" + TAG, "extra"),
        ("pytest", "{runs}/" + TAG, "-q"),  # no run venv
        ("exec", "{runs}/" + TAG, "a.txt"),
        ("read",),
    ],
)
def test_wrapper_refuses(runs, args):
    proc = run(runs, *(a.format(runs=runs) for a in args))
    assert proc.returncode == 64, proc.stderr
    assert b"refusing" in proc.stderr and proc.stdout == b""


def test_wrapper_refuses_symlinks(runs):
    co = runs / TAG / "checkout"
    (co / "lf").symlink_to(runs / ("f" * 32) / "secret.txt")
    (co / "ld").symlink_to(runs / ("f" * 32))
    root = str(runs / TAG)
    for args in (
        ("read", root, "lf"),
        ("read", root, "ld/secret.txt"),
        ("list", root, "ld"),
        ("write", root, "lf"),
        ("write", root, "ld/new.txt"),
    ):
        assert run(runs, *args, stdin=b"x").returncode == 64, args
    assert not (runs / ("f" * 32) / "new.txt").exists()
    assert (runs / ("f" * 32) / "secret.txt").read_text() == "s\n"


def test_overrides_are_ignored_for_the_worker_account():
    text = WRAPPER.read_text()
    guard = text.index('if [ "$(id -un)" != "$WORKER" ]; then')
    assert text.index("${CAIRNWORK_LOCAL_WORKER_RUNS:-") > guard
    assert text.count("CAIRNWORK_LOCAL_WORKER_RUNS:-") == 1


@pytest.mark.sandbox
def test_wrapper_actions_work_inside_the_checkout(runs):
    root = str(runs / TAG)
    assert run(runs, "read", root, "a.txt").stdout == b"hello\n"
    assert run(runs, "write", root, "new/dir/b.txt", stdin=b"B\n").returncode == 0
    assert (runs / TAG / "checkout" / "new" / "dir" / "b.txt").read_text() == "B\n"
    listing = run(runs, "list", root, ".").stdout.decode().split()
    assert {"a.txt", "new/", "sub/"} <= set(listing)
    assert run(runs, "sweep", root).stdout == b"LOCAL_WORKER_SWEEP clean\n"


@pytest.mark.sandbox
@pytest.mark.skipif(shutil.which("uv") is None, reason="the canary builds its run venv with uv")
def test_canary_direct_mode_denies_reads_writes_and_network(tmp_path):
    runs = (tmp_path / "runs").resolve()
    runs.mkdir()
    out = tmp_path / "out"
    rc = canary.main(
        [
            "--mode",
            "direct",
            "--out",
            str(out),
            "--runs",
            str(runs),
            "--pyroot",
            str(Path(sys.base_prefix).parent),
            "--python",
            os.path.realpath(getattr(sys, "_base_executable", sys.executable)),
            "--private-parent",
            str(tmp_path / "private"),
        ]
    )
    (report_path,) = out.glob("*.json")
    report = json.loads(report_path.read_text())
    failed = [p for c in report["checks"].values() for p in c.get("probes", []) if not p["pass"]]
    assert rc == 0 and report["overall"] == "SEATBELT_ONLY_PASS", failed
    for name in ("a_read_denied", "b_write_denied", "c_network_denied", "positive_controls"):
        assert report["checks"][name]["pass"], name
    digest = report_path.with_suffix(".json.sha256").read_text().split()[0]
    import hashlib

    assert digest == hashlib.sha256(report_path.read_bytes()).hexdigest()
    assert list(runs.iterdir()) == []  # fixture and call log removed
