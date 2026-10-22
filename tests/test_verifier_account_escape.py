"""#37: a launchd-started system daemon (distnoted on a full-Xcode host) must not be counted as a
process the candidate left behind, but anything merely disguised as one must still be counted.

These tests drive ``_escaped_count`` with injected process data, so the exemption logic is checked
on every host; the real full-Xcode behaviour is exercised by the sandbox test in
test_verifier_account.py.
"""

from pathlib import Path

import cairnwork.verifier_account as va

DAEMON = "/usr/sbin/distnoted"  # the one real launchd daemon seen on full-Xcode hosts


def _account(monkeypatch, procs, real, apple=(DAEMON,)):
    # The daemon exemption is macOS-only (proc_pidpath); force it on so the logic is tested on any
    # host (CI's test jobs run on Linux). A separate test covers the off-Darwin path.
    monkeypatch.setattr(va.sys, "platform", "darwin")
    monkeypatch.setattr(va.VerifierAccount, "_list", lambda self: procs)
    monkeypatch.setattr(va.VerifierAccount, "_real_paths", lambda self, pids: real)
    monkeypatch.setattr(va, "_apple_signed", lambda path: path in apple)
    return va.VerifierAccount(user="x", root=Path("/tmp/does-not-matter"))


def test_real_launchd_daemon_is_exempt(monkeypatch):
    acct = _account(monkeypatch, [(100, 1)], {100: DAEMON})
    assert acct._escaped_count() == 0


def test_disguised_daemon_is_still_counted(monkeypatch):
    # `exec -a /usr/sbin/distnoted sleep`: argv[0] says distnoted, but the REAL path is /bin/sleep,
    # Apple-signed yet not allow-listed — it must still count (the issue's "Done when").
    acct = _account(monkeypatch, [(200, 1)], {200: "/bin/sleep"}, apple=(DAEMON, "/bin/sleep"))
    assert acct._escaped_count() == 1


def test_daemon_path_but_not_launchd_parent_is_counted(monkeypatch):
    # the candidate ran distnoted itself (parent is the candidate, not launchd): not exempt.
    acct = _account(monkeypatch, [(100, 999)], {100: DAEMON})
    assert acct._escaped_count() == 1


def test_daemon_path_but_not_apple_signed_is_counted(monkeypatch):
    acct = _account(monkeypatch, [(100, 1)], {100: DAEMON}, apple=())
    assert acct._escaped_count() == 1


def test_mix_counts_only_the_escapees(monkeypatch):
    procs = [(100, 1), (200, 1), (300, 200)]  # real daemon, disguised-as-daemon, plain escapee
    real = {100: DAEMON, 200: "/bin/sleep", 300: "/private/tmp/evil"}
    acct = _account(monkeypatch, procs, real, apple=(DAEMON, "/bin/sleep"))
    assert acct._escaped_count() == 2


def test_no_processes_is_zero(monkeypatch):
    acct = _account(monkeypatch, [], {})
    assert acct._escaped_count() == 0


def test_unknown_process_list_is_cannot_tell(monkeypatch):
    acct = _account(monkeypatch, None, {})
    assert acct._escaped_count() == -1


def test_unreadable_real_paths_fail_closed(monkeypatch):
    # if the real paths cannot be read, the count is unknown, never zero (fail closed).
    acct = _account(monkeypatch, [(100, 1)], None)
    assert acct._escaped_count() == -1


def test_a_pid_with_no_resolved_path_is_counted(monkeypatch):
    # proc_pidpath returned nothing for this pid (it exited mid-scan): count it, do not exempt.
    acct = _account(monkeypatch, [(100, 1)], {})
    assert acct._escaped_count() == 1


def test_off_darwin_counts_every_process_without_proc_pidpath(monkeypatch):
    # Off macOS there is no launchd distnoted and no proc_pidpath: every process counts, and the
    # real-path helper must never be called (it would crash). A would-be daemon still counts.
    acct = _account(monkeypatch, [(100, 1)], {100: DAEMON})
    monkeypatch.setattr(va.sys, "platform", "linux")

    def _boom(self, pids):
        raise AssertionError("proc_pidpath must not run off Darwin")

    monkeypatch.setattr(va.VerifierAccount, "_real_paths", _boom)
    assert acct._escaped_count() == 1
