"""Unprivileged checks of the macOS worker-account scripts (Issue #19 Phase B).

The scripts themselves need sudo; these tests only parse them and run their pure helper functions
(extracted from the script text) in a zsh subshell, against real directories and a stubbed dscl.
"""

import grp
import os
import pwd
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
SETUP = SCRIPTS / "setup_worker_user.sh"
ROLLBACK = SCRIPTS / "setup_worker_user_rollback.sh"
_ZSH_FOUND = shutil.which("zsh")
ZSH: str = _ZSH_FOUND or "zsh"

needs_zsh = pytest.mark.skipif(_ZSH_FOUND is None, reason="zsh not installed")
needs_bsd_stat = pytest.mark.skipif(sys.platform != "darwin", reason="BSD stat -f is macOS-only")


def extract_function(name):
    """Return `name() { ... }` from the setup script (the body ends at a line that is `}`)."""
    lines = SETUP.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{name}() {{"))
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start : end + 1])


def run_zsh(body, env=None):
    prog = 'die() { echo "DIE: $*"; exit 1; }\n' + body
    return subprocess.run(
        [ZSH, "-f", "-c", prog], capture_output=True, text=True, env=env, check=False
    )


@needs_zsh
@pytest.mark.parametrize("script", [SETUP, ROLLBACK])
def test_scripts_parse(script):
    assert subprocess.run([ZSH, "-n", str(script)], check=False).returncode == 0


def test_every_mode_check_uses_four_digit_modes():
    # '%Lp' alone drops setuid/setgid/sticky, so a 2710 directory prints 710 (the bug fixed here).
    for script in (SETUP, ROLLBACK):
        for line in script.read_text().splitlines():
            if "stat -f" in line and "%Lp" in line:
                assert "%Mp%Lp" in line or "omode=" in line, line
    calls = [ln for ln in SETUP.read_text().splitlines() if ln.startswith("verify_dir ")]
    assert len(calls) == 3
    for call in calls:
        assert call.split()[-1] in {"0755", "2710"}, call


@needs_zsh
@needs_bsd_stat
def test_verify_dir_accepts_setgid_2710_and_rejects_wrong_modes(tmp_path):
    user = pwd.getpwuid(os.getuid()).pw_name
    group = grp.getgrgid(os.getgid()).gr_name  # a group we are in, so chmod keeps setgid
    runs = tmp_path / "runs"
    runs.mkdir()
    os.chmod(runs, 0o2710)
    plain = tmp_path / "plain"
    plain.mkdir()
    os.chmod(plain, 0o710)
    fn = extract_function("verify_dir")

    ok = run_zsh(f"{fn}\nverify_dir {runs} {user} {group} 2710 && echo PASS")
    assert ok.stdout.strip() == "PASS", ok.stdout + ok.stderr
    # the same directory without setgid must fail, and so must the old 3-digit expectation
    assert "DIE:" in run_zsh(f"{fn}\nverify_dir {plain} {user} {group} 2710").stdout
    assert "DIE:" in run_zsh(f"{fn}\nverify_dir {runs} {user} {group} 710").stdout
    assert "DIE:" in run_zsh(f"{fn}\nverify_dir {runs} {user} {group} 0755").stdout


@needs_zsh
def test_free_id_picks_first_id_free_as_both_uid_and_gid(tmp_path):
    stub = tmp_path / "bin"
    stub.mkdir()
    (stub / "dscl").write_text(
        "#!/bin/sh\n"
        'case "$3" in\n'
        "  /Users) printf 'root 0\\nsvc 450\\njames 501\\n' ;;\n"
        "  /Groups) printf 'wheel 0\\ngsvc 451\\nstaff 20\\n' ;;\n"
        "esac\n"
    )
    (stub / "dscl").chmod(0o755)
    env = dict(os.environ, PATH=f"{stub}:/usr/bin:/bin")
    r = run_zsh(f"{extract_function('free_id')}\nfree_id", env=env)
    assert r.stdout.strip() == "452", r.stdout + r.stderr


@needs_zsh
def test_free_id_fails_when_quiet_range_is_full(tmp_path):
    stub = tmp_path / "bin"
    stub.mkdir()
    (stub / "dscl").write_text(
        '#!/bin/sh\nif [ "$3" = /Users ]; then i=450; while [ $i -lt 500 ]; do echo "u$i $i"; '
        "i=$((i+1)); done; fi\n"
    )
    (stub / "dscl").chmod(0o755)
    env = dict(os.environ, PATH=f"{stub}:/usr/bin:/bin")
    r = run_zsh(f"{extract_function('free_id')}\nfree_id; echo rc=$?", env=env)
    assert r.returncode != 0 and "no id in 450..499" in r.stderr
