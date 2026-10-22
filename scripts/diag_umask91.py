"""TEMPORARY diagnostic for Issue #91 (strict caller umask breaks the Linux account run).

Reproduces the per-verifier setup under ``umask 077`` the way ``run_verifiers`` does — a clone
into a setgid ``work`` dir under the shared run root, then ``grant_read`` — and prints, from the
owner's view, the mode/owner/group of every directory from the run root down to
``checkout/tests/test_ok.py``, then, as the verifier account, whether it can traverse and read
that file. This is run by the ``test-linux-verifier-account`` CI job to locate which path the
account cannot reach; it is removed once the fix lands.
"""

from __future__ import annotations

import grp
import os
import pwd
import subprocess
import sys
import tempfile
from pathlib import Path

from cairnwork.gitcmd import run_git
from cairnwork.verifier_account import VerifierAccount
from cairnwork.workspace import create_attempt_clone


def _perm(path: Path) -> str:
    try:
        st = os.stat(path)
    except OSError as exc:
        return f"  ??? {exc} {path}"
    user = pwd.getpwuid(st.st_uid).pw_name if _name(pwd.getpwuid, st.st_uid) else st.st_uid
    group = grp.getgrgid(st.st_gid).gr_name if _name(grp.getgrgid, st.st_gid) else st.st_gid
    return f"  {oct(st.st_mode & 0o7777):>6} {user}:{group} {path}"


def _name(fn, ident) -> bool:
    try:
        fn(ident)
        return True
    except KeyError:
        return False


def _chain(leaf: Path, stop: Path) -> list[Path]:
    chain, cur = [], leaf
    while True:
        chain.append(cur)
        if cur == stop or cur == cur.parent:
            break
        cur = cur.parent
    return list(reversed(chain))


def main() -> int:
    os.umask(0o077)  # the failing condition: a caller started with a strict umask
    account = VerifierAccount()
    base = Path(tempfile.mkdtemp(prefix="diag91-src-"))
    run_git(base, "init", "--quiet", "-b", "main")
    run_git(base, "config", "user.email", "d@d")
    run_git(base, "config", "user.name", "d")
    (base / "tests").mkdir()
    (base / "tests" / "test_ok.py").write_text("def test_ok():\n    assert 1 + 1 == 2\n")
    run_git(base, "add", "-A")
    run_git(base, "commit", "--quiet", "-m", "seed")
    head = run_git(base, "rev-parse", "HEAD").strip()

    work = account.run_root / "diag91.work"
    work.mkdir(mode=0o700)
    os.chmod(work, 0o3770)
    checkout = create_attempt_clone(base, head, work, "diag" + os.urandom(4).hex()).path
    account.grant_read(checkout)
    target = checkout / "tests" / "test_ok.py"

    print("=== owner-view permission chain (run_root -> target) ===", flush=True)
    for p in _chain(target, account.run_root):
        print(_perm(p), flush=True)

    print(f"=== as the account ({account.user}) ===", flush=True)
    probe = f"cd {checkout} && ls -la tests/ && cat tests/test_ok.py; echo RC=$?"
    argv = account.wrap(["/bin/sh", "-c", probe], None)
    done = subprocess.run(argv, capture_output=True, text=True, timeout=60, check=False)  # noqa: S603
    print(done.stdout, flush=True)
    print("STDERR:", done.stderr, flush=True)
    print("account exit:", done.returncode, flush=True)

    account.remove(work)
    return 0


if __name__ == "__main__":
    sys.exit(main())
