import json
import sqlite3
import subprocess

import pytest

pytest_plugins = ["verify_helpers"]


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def tamper_stored_request(db, mutate):
    """Rewrite the stored request of the only notary row (the append-only trigger is dropped)."""
    conn = sqlite3.connect(db)
    conn.execute("DROP TRIGGER core_verifications_no_update")
    request = json.loads(conn.execute("SELECT request_json FROM core_verifications").fetchone()[0])
    mutate(request)
    conn.execute("UPDATE core_verifications SET request_json = ?", (json.dumps(request),))
    conn.commit()
    conn.close()


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "test@example.invalid")
    git(path, "config", "user.name", "test")
    (path / "src").mkdir()
    (path / "src" / "a.py").write_text("x = 1\n")
    (path / "src" / "b.py").write_text("y = 1\n")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "base")
    return path


def commit_change(repo, files=None, delete=(), rename=None, message="candidate"):
    for name, text in (files or {}).items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    for name in delete:
        git(repo, "rm", "-q", name)
    if rename:
        git(repo, "mv", *rename)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def pytest_collection_modifyitems(config, items):
    """Tests marked 'sandbox' need real macOS Seatbelt. CAIRNWORK_REQUIRE_SANDBOX=1 (the
    macOS CI job) makes a missing sandbox an error; elsewhere those tests are skipped."""
    import os

    import cairnwork_sandbox
    from cairnwork.verifier_account import VerifierAccount

    if not VerifierAccount().available():
        if os.environ.get("CAIRNWORK_REQUIRE_VERIFIER_ACCOUNT") == "1":
            raise pytest.UsageError(
                "CAIRNWORK_REQUIRE_VERIFIER_ACCOUNT=1 but the account is unusable"
            )
        skip_account = pytest.mark.skip(reason="needs the cairnwork-verifier account")
        for item in items:
            if "verifier_account" in item.keywords:
                item.add_marker(skip_account)
    if cairnwork_sandbox.select() is not None:
        return
    if os.environ.get("CAIRNWORK_REQUIRE_SANDBOX") == "1":
        raise pytest.UsageError("CAIRNWORK_REQUIRE_SANDBOX=1 but sandbox-exec is unavailable")
    skip = pytest.mark.skip(reason="needs macOS sandbox-exec")
    for item in items:
        if "sandbox" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _no_origin_in_test_repos(request, monkeypatch):
    """Test repositories have no origin; tests of the lineage check set the key to true.
    A test marked no_origin_patch sees the real default."""
    if request.node.get_closest_marker("no_origin_patch"):
        return
    from cairnwork import intake

    monkeypatch.setitem(intake.DEFAULT_CONFIG, "require_base_on_main", False)
