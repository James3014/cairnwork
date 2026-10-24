"""Issue #240: what a verifier may do to its checkout is the same in owner and account mode.

The same candidate tests run in both modes against one ``EXPECTED`` table. Writing and cleaning up,
setting explicit times (a template cache keyed by mtime), chmod and restore, an atomic replace with
the same bytes (also from a mode-0600 temporary file, left at 0600), a tracked directory left at
0700, and deleting the top-level ``.git`` (the verifier's scratch, never judged) pass. A leftover
file (ignored by .gitignore or not), a retained modification, ``.git`` moved to a path inside the
checkout, a symlink swapped in, a new directory left without permissions, or a tracked file or
directory closed to its own owner (0000: unreadable to the owner in owner mode, so in both) is
"checkout changed". The checkout is owned by the uid the test runs as (in account mode: the
account's own copy), and every work dir, that copy included, is removed afterwards without a
cleanup ALERT. The account side is one ``run_verifiers`` call (one lock hold). A link witness
checks that links the test makes to files outside its checkout and to the run's own ini, log and
guard leave those files' contents, modes, owners and link counts as they were, and a crash leftover
(the account's copy with a mode-0 directory, plus the owner's staging clone) is removed by
recovery.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

import pytest
from conftest import commit_change
from test_recovery import GOOD, crash, stores
from test_recovery import project as project  # noqa: F401 - fixture

import cairnwork.verifiers as verifiers
from cairnwork.account_checkout import STAGE
from cairnwork.recovery import recover
from cairnwork.verifier_account import VerifierAccount
from cairnwork.verifiers import parse_verifier, run_verifiers

ACCOUNT = VerifierAccount()

_HEAD = (
    "import os, shutil, time\nfrom pathlib import Path\n"
    "ROOT = Path(__file__).resolve().parents[1]\nDATA = ROOT / 'pkg' / 'data.txt'\n"
)
DATA = "DATA"
SCENARIOS = {
    "clean": (  # a fixture under tests/examples, an in-place rewrite restored, a cache file
        "def test_clean():\n"
        "    examples = ROOT / 'tests' / 'examples' / 'new'\n"
        "    examples.mkdir(parents=True)\n"
        "    (examples / 'fixture.txt').write_text('x')\n"
        "    shutil.rmtree(ROOT / 'tests' / 'examples')\n"
        f"    data = {DATA}\n"
        "    text = data.read_text()\n"
        "    data.write_text('changed\\n')\n"
        "    data.write_text(text)\n"
        "    os.utime(data, None)\n"
        "    (ROOT / 'pkg' / '.cache').write_text('c')\n"
        "    (ROOT / 'pkg' / '.cache').unlink()\n"
    ),
    # explicit times need ownership (POSIX): what the jinja mtime-cache test does
    "utime": f"def test_utime():\n    os.utime({DATA}, (time.time() + 5, time.time() + 5))\n",
    "chmod": (
        "def test_chmod():\n"
        f"    mode = {DATA}.stat().st_mode & 0o7777\n"
        f"    os.chmod({DATA}, 0o600)\n"
        f"    os.chmod({DATA}, mode)\n"
    ),
    "replace": (
        "def test_replace():\n"
        f"    data = {DATA}\n"
        "    tmp = data.with_name('data.tmp')\n"
        "    tmp.write_bytes(data.read_bytes())\n"
        "    os.chmod(tmp, data.stat().st_mode & 0o7777)\n"
        "    os.replace(tmp, data)\n"
    ),
    "replace0600": (  # identical bytes from a mode-0600 temporary file, left at 0600
        "def test_replace0600():\n"
        f"    data = {DATA}\n"
        "    tmp = data.with_name('data.tmp')\n"
        "    tmp.write_bytes(data.read_bytes())\n"
        "    os.chmod(tmp, 0o600)\n"
        "    os.replace(tmp, data)\n"
    ),
    "dir0700": "def test_dir0700():\n    os.chmod(ROOT / 'pkg' / 'sub', 0o700)\n",
    # closed to its own owner: unreadable to the comparing owner in owner mode, so in both
    "file0000": f"def test_file0000():\n    os.chmod({DATA}, 0)\n",
    "dir0000": "def test_dir0000():\n    os.chmod(ROOT / 'pkg' / 'sub', 0)\n",
    "gitdelete": "def test_gitdelete():\n    shutil.rmtree(ROOT / '.git')\n",
    "owns": (  # the checkout is the running uid's own (account mode: the account's copy)
        f"def test_owns():\n    assert ROOT.stat().st_uid == {DATA}.stat().st_uid == os.getuid()\n"
    ),
    "leave": "def test_leave():\n    (ROOT / 'pkg' / 'left.txt').write_text('x')\n",
    "ignored": "def test_ignored():\n    (ROOT / 'pkg' / 'run.log').write_text('x')\n",
    "modify": f"def test_modify():\n    {DATA}.write_text('kept\\n')\n",
    "gitmove": "def test_gitmove():\n    os.rename(ROOT / '.git', ROOT / '.git-moved')\n",
    "swap": (
        "def test_swap():\n"
        "    os.rename(ROOT / 'pkg' / 'sub', ROOT / 'pkg' / 'sub.real')\n"
        "    os.symlink('sub.real', ROOT / 'pkg' / 'sub')\n"
        f"    {DATA}.unlink()\n"
        f"    os.symlink('/etc/hosts', {DATA})\n"
    ),
    "locked": (
        "def test_locked():\n"
        "    locked = ROOT / 'tests' / 'locked'\n"
        "    (locked / 'inner').mkdir(parents=True)\n"
        "    (locked / 'inner' / 'f').write_text('x')\n"
        "    os.chmod(locked / 'inner', 0)\n"
        "    os.chmod(locked, 0)\n"
    ),
}
# (passed, what mutated must contain): one table for both modes
EXPECTED = {
    "clean": (True, set()),
    "utime": (True, set()),
    "chmod": (True, set()),
    "replace": (True, set()),
    "owns": (True, set()),  # under the account: its own copy (account_checkout.py)
    "replace0600": (True, set()),  # mode bits other than the owner's x are not judged
    "dir0700": (True, set()),
    "file0000": (False, {"unreadable pkg/data.txt"}),
    "dir0000": (False, {"unreadable pkg/sub"}),
    "gitdelete": (True, set()),  # policy: the top-level .git is not judged (verifiers.py)
    "leave": (False, {"extra pkg/left.txt"}),
    "ignored": (False, {"extra pkg/run.log"}),  # ignore rules do not apply to the comparison
    "modify": (False, {"changed pkg/data.txt"}),
    "gitmove": (False, {"extra directory .git-moved"}),
    "swap": (False, {"changed pkg/sub", "changed pkg/data.txt", "extra directory pkg/sub.real"}),
    "locked": (False, {"unreadable tests/locked"}),
}
FILES = {"pkg/__init__.py": "", "pkg/data.txt": "v1\n", "pkg/sub/x.txt": "x\n"}
FILES[".gitignore"] = "*.log\n"


def _normal(entry: str) -> str:
    """'unreadable <absolute path>' names a path inside the run's checkout: keep the tail."""
    found = re.fullmatch(r"unreadable /.*?\.work/[^/]+/(.+)", entry)
    return f"unreadable {found.group(1)}" if found else entry


def _run(repo, run_root, account, run_id, scenarios):
    files = {f"tests/test_{name}.py": _HEAD + body for name, body in scenarios.items()}
    head = commit_change(repo, {**FILES, **files})
    specs = tuple(
        parse_verifier(name, ["python3", "-m", "pytest", f"tests/test_{name}.py"])
        for name in scenarios
    )
    python = None if account is not None else sys.executable
    runs = run_verifiers(repo, head, specs, run_root, run_id, python=python, account=account)
    for run in runs:
        assert run.ran, run.log_path.read_text()  # the guard saw the test run and pass
        assert run.returncode == 0, run.log_path.read_text()  # the candidate test succeeded
    # in account mode only account.remove can delete the copy: this pins that it did
    assert not list(run_root.glob(f"{run_id}-v*.work")), "a work dir or the copy was left behind"
    assert not list(run_root.glob(f"{run_id}-v*.work/{STAGE}")), "a staging clone was left"
    return {run.spec.verifier_id: run for run in runs}


def _check(runs):
    for name, (passed, must_contain) in EXPECTED.items():
        run = runs[name]
        assert run.observation.passed is passed, (name, run.mutated, run.log_path.read_text())
        assert must_contain <= {_normal(m) for m in run.mutated}, (name, run.mutated)
        assert ("checkout changed" in run.why) is not passed, (name, run.why)


@pytest.fixture
def own_names():
    """Name prefixes this test creates in the account's run root; only those are removed after
    it. The run root is shared with other sessions' runs: never remove "whatever is new". If a
    failure left owner directories shut (0o700), reopen them first so the account's rm gets in."""
    prefixes: list[str] = []
    yield prefixes
    if not prefixes or not ACCOUNT.run_root.is_dir():
        return
    for name in os.listdir(ACCOUNT.run_root):
        if not any(name.startswith(prefix) for prefix in prefixes):
            continue
        path = ACCOUNT.run_root / name
        for top, dirs, _ in os.walk(path):
            for entry in [top, *(os.path.join(top, d) for d in dirs)]:
                if not os.path.islink(entry) and os.lstat(entry).st_uid == os.getuid():
                    os.chmod(entry, os.lstat(entry).st_mode | 0o777)
        ACCOUNT.remove(path)
        shutil.rmtree(path, ignore_errors=True)


def _mode_run_root(mode, tmp_path):
    if mode == "owner":
        return tmp_path / "runs", None
    return ACCOUNT.run_root, ACCOUNT


MODES = [
    pytest.param("owner", marks=pytest.mark.sandbox),
    pytest.param("account", marks=[pytest.mark.sandbox, pytest.mark.verifier_account]),
]


@pytest.mark.parametrize("mode", MODES)
def test_checkout_writes_match_between_modes(repo, tmp_path, own_names, capsys, mode):
    run_root, account = _mode_run_root(mode, tmp_path)
    run_id = "w" + os.urandom(6).hex()
    own_names.append(f"{run_id}-")
    _check(_run(repo, run_root, account, run_id, SCENARIOS))
    assert "ALERT verifier cleanup" not in capsys.readouterr().err


_LINKS = (
    "import errno, hashlib, json\n"
    "def _seen(path):\n"
    "    try:\n"
    "        info = os.lstat(path)\n"
    "    except OSError as exc:\n"
    "        return {'lstat': errno.errorcode[exc.errno]}\n"
    "    try:\n"
    "        digest = hashlib.sha256(open(path, 'rb').read()).hexdigest()\n"
    "    except OSError as exc:\n"
    "        digest = errno.errorcode[exc.errno]\n"
    "    keys = ('mode', 'nlink', 'uid')\n"
    "    return {'sha256': digest, **{k: getattr(info, 'st_' + k) for k in keys}}\n"
    "def test_links(request, capsys):\n"
    "    # explicit paths: the run's private dir is the ini's (the sandbox may not list run_root)\n"
    "    ini = Path(str(request.config.inipath))\n"
    "    guard = ini.parent / 'guard' / '_cairnwork_report_guard.py'\n"
    "    targets = os.environ['CW_TARGETS'].split(os.pathsep)\n"
    "    targets += [str(ini), str(ini.parent / 'verifier.log'), str(guard)]\n"
    "    seen = [_seen(t) for t in targets]\n"
    "    made = []\n"
    "    for i, target in enumerate(targets):\n"
    "        for make, kind in ((os.link, 'hl'), (os.symlink, 'sl')):\n"
    "            try:\n"
    "                make(target, ROOT / f'{kind}{i}')\n"
    "                made.append([i, kind, 'ok'])\n"
    "            except OSError as exc:\n"
    "                made.append([i, kind, errno.errorcode[exc.errno]])\n"
    "    report = json.dumps({'targets': targets, 'seen': seen, 'made': made})\n"
    "    with capsys.disabled():  # straight into the verifier log\n"
    "        print('CW-LINKS ' + report, flush=True)\n"
)


def _fingerprint(path):
    info = os.lstat(path)
    digest = hashlib.sha256(open(path, "rb").read()).hexdigest()
    return digest, info.st_mode, info.st_nlink, info.st_uid, info.st_gid


@pytest.mark.parametrize("mode", MODES)
def test_links_out_of_the_checkout_change_nothing_outside_it(
    repo, tmp_path, own_names, monkeypatch, mode, request
):
    """Links (hard and symbolic) the test makes from its checkout to the source repo, an unrelated
    run's file, an owner-only state file, and the run's own ini, log and guard (explicit paths,
    each attempt recorded in the log): after the run and cleanup the outside files' contents,
    modes, owners and link counts are as before, and the run's own files are as the test first saw
    them (the log, which grows, keeps its mode and owner) with one link each."""
    run_root, account = _mode_run_root(mode, tmp_path)
    run_id = "w" + os.urandom(6).hex()
    own_names.append(f"{run_id}-")
    run_root.mkdir(parents=True, exist_ok=True)
    # another run's file the account can read: not in the shared run root, whose new names other
    # sessions' test fixtures sweep (test_verifier_account._clean_shared_run_root)
    base = tmp_path if account is None else ACCOUNT.root
    unrelated = base / f".cw240-unrelated-{run_id}" / "f.txt"
    unrelated.parent.mkdir()
    request.addfinalizer(lambda: shutil.rmtree(unrelated.parent, ignore_errors=True))
    unrelated.write_text("other run\n")
    os.chmod(unrelated, 0o640)
    journal = tmp_path / "state" / "journal.sqlite"
    journal.parent.mkdir()
    journal.write_text("journal\n")
    os.chmod(journal, 0o600)
    outside = [repo / "src" / "a.py", unrelated, journal]
    before = {t: _fingerprint(t) for t in outside}
    monkeypatch.setenv("CW_TARGETS", os.pathsep.join(map(str, outside)))
    monkeypatch.setattr(verifiers, "_ENV_KEEP", (*verifiers._ENV_KEEP, "CW_TARGETS"))
    run = _run(repo, run_root, account, run_id, {"links": _LINKS})["links"]
    assert not run.observation.passed  # the links themselves are extra paths
    assert {t: _fingerprint(t) for t in outside} == before
    private = run.log_path.parent
    own = [private / "pytest.ini", run.log_path, private / "guard" / "_cairnwork_report_guard.py"]
    lines = [x for x in run.log_path.read_text().splitlines() if x.startswith("CW-LINKS ")]
    assert len(lines) == 1, run.log_path.read_text()
    report = json.loads(lines[0].removeprefix("CW-LINKS "))
    expected = [*outside, *own]
    assert [os.path.realpath(t) for t in report["targets"]] == [
        os.path.realpath(t) for t in expected
    ]
    attempts = {(i, kind): result for i, kind, result in report["made"]}
    assert set(attempts) == {(i, kind) for i in range(len(expected)) for kind in ("hl", "sl")}
    assert all(attempts[i, "sl"] == "ok" for i in range(len(expected))), attempts
    for i, path in enumerate(own):
        seen, info = report["seen"][len(outside) + i], os.lstat(path)
        # the owner is checked from outside: under bwrap the account's user namespace shows the
        # unmapped owner uid as the overflow uid (65534), so the uid seen inside is not comparable
        assert seen["mode"] == info.st_mode, path
        assert info.st_uid == os.getuid(), path
        assert seen["nlink"] == info.st_nlink == 1, (path, attempts)
        if path != run.log_path:  # the log grows during the run; it stays owner-only
            assert seen["sha256"] == _fingerprint(path)[0], path
    assert os.lstat(run.log_path).st_mode & 0o077 == 0


@pytest.mark.sandbox
@pytest.mark.verifier_account
def test_recovery_removes_a_crashed_account_checkout_and_staging_clone(
    project, tmp_path, own_names
):
    """A crash leaves the account's copy (with a directory the account closed to itself) and the
    owner's staging clone. Recovery removes both."""
    crash(project, tmp_path, "CANDIDATE", GOOD)
    ledger, notary, journal = stores(tmp_path)
    attempt = journal.entries(ledger.in_flight()[0].task_id)[-1].attempt_id
    own_names.append(f"{attempt}-")
    work = ACCOUNT.run_root / f"{attempt}-q-v0.work"
    work.mkdir(mode=0o700)
    os.chmod(work, 0o3770)  # as verifiers._run_all makes it
    stage = work / STAGE / f"{attempt}-q-v0" / "tests"
    stage.mkdir(parents=True)
    (stage / "test_x.py").write_text("x\n")
    ACCOUNT.grant_read(work / STAGE)
    checkout = work / f"{attempt}-q-v0"
    code = (
        f"import os; p = {str(checkout / 'tests' / 'locked')!r}; os.makedirs(p + '/inner');"
        " open(p + '/inner/f', 'w').write('x'); os.chmod(p + '/inner', 0); os.chmod(p, 0)"
    )
    with ACCOUNT.exclusive():
        done = subprocess.run(  # noqa: S603 - fixed argv
            ACCOUNT.wrap([ACCOUNT.python, "-P", "-c", code], None),
            cwd=ACCOUNT.root, capture_output=True, text=True, timeout=60, check=False,
        )  # fmt: skip
    assert done.returncode == 0, done.stderr
    assert os.lstat(checkout).st_uid != os.getuid()  # the account's copy
    (decision,) = recover(
        ledger=ledger, notary=notary, journal=journal, run_root=ACCOUNT.run_root,
        verifier_account=ACCOUNT,
    )  # fmt: skip
    assert decision.detail == "", decision.detail
    assert not os.path.lexists(work)
