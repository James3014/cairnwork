import subprocess

import pytest


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


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
