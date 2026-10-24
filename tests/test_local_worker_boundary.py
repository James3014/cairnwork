"""#88 Plan v2 point 1: the Owner-side loop modules never touch files; only corpus_io (the
recorder) does, and every path it opens is checked to be inside its directory."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from experimental.local_worker import corpus_io, episode

LOOP_MODULES = ("agent.py", "agent_protocol.py", "endpoint.py", "episode.py", "episode_schema.py")
LOOP_MODULES += (
    "gitstate.py",
    "cli.py",
    "cuts.py",
    "redact.py",
    "schema_base.py",
    "schema_cuts.py",
    "schema_rules.py",
    "canary_check.py",
)
IO_NAMES = {"open", "read_text", "read_bytes", "write_text", "write_bytes", "listdir", "scandir"}
IO_NAMES |= {"walk", "iterdir", "glob", "rglob", "fdopen", "readlink", "mkdir", "unlink", "chmod"}
IO_NAMES |= {"__import__", "import_module", "exec", "eval"}
IO_MODULES = {"os", "subprocess", "shutil", "glob", "io", "tempfile", "importlib", "builtins"}
IO_MODULES |= {"fileinput", "mmap", "pty", "posix", "ctypes", "runpy", "zipfile", "tarfile"}


def _io_uses(tree: ast.AST) -> list[tuple[str, int]]:
    """Any reference to an I/O name: a call, an attribute, or a bare name (``o = open``)."""
    found = []
    for node in ast.walk(tree):
        name = node.attr if isinstance(node, ast.Attribute) else getattr(node, "id", None)
        if isinstance(node, (ast.Attribute, ast.Name)) and name in IO_NAMES:
            found.append((name, node.lineno))
    return found


def _io_imports(tree: ast.AST) -> list[str]:
    """Imports of an I/O module or of an I/O name, whatever the alias (``from os import
    listdir as _ls``, ``import os.path as p``, ``from builtins import open as o``)."""
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            bad += [a.name for a in node.names if a.name.split(".")[0] in IO_MODULES]
        elif isinstance(node, ast.ImportFrom):
            module = (node.module or "").split(".")[0]
            names = [a.name for a in node.names]
            if module in IO_MODULES or set(names) & IO_NAMES:
                bad.append(f"from {node.module} import {names}")
    return bad


_io_calls = _io_uses  # corpus_io's guard test below uses the same detector


def test_loop_modules_open_no_files():
    """Plan v2 point 1: only corpus_io (the recorder) touches files; the loop modules never do."""
    root = Path(episode.__file__).parent
    for name in LOOP_MODULES:
        tree = ast.parse((root / name).read_text())
        assert _io_uses(tree) == [], name
        assert _io_imports(tree) == [], name


@pytest.mark.parametrize(
    "src",
    [
        "from os import listdir as _ls\n_ls('.')\n",
        "import os.path as p\n",
        "from builtins import open as o\n",
        "from io import open as reader\n",
        "o = open\n",
        "import importlib\n",
        "m = __import__('os')\n",
        "from pathlib import Path\nPath('x').read_text()\n",
    ],
)
def test_the_boundary_detector_sees_aliases(src):
    """Review of #239, finding 6: ImportFrom and aliases are caught, not only ``import os``."""
    tree = ast.parse(src)
    assert _io_uses(tree) or _io_imports(tree)


def test_corpus_io_guards_every_path_it_opens():
    tree = ast.parse(Path(corpus_io.__file__).read_text())
    unguarded = {"load_task", "harness_version", "_wrapper_sha", "git"}  # fixed inputs, see doc
    for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        if fn.name in unguarded or not _io_calls(fn):
            continue
        calls = {getattr(n.func, "id", "") for n in ast.walk(fn) if isinstance(n, ast.Call)}
        assert "_inside" in calls, fn.name
