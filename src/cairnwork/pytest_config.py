"""pytest settings for a verifier run, taken from the task's base commit (never the candidate).

A target repository's tests often need its pytest configuration (an ``asyncio_mode``,
``pythonpath`` entries, markers). The verifier still runs with Cairnwork's own ini
(verifiers.py); this module fills that ini from the configuration file found at the
repository root of the *base* commit, read with git, so a candidate that edits
``pytest.ini`` or ``pyproject.toml`` changes nothing (those files are also protected
verifier inputs). Only the keys in ``BASE_KEYS`` are copied; every other key is
dropped and named in a comment of the generated ini. ``addopts`` is never copied
(it can load plugins, change the rootdir or the ini); only an ``--import-mode`` and
options that make the run stricter (``-W``, ``--strict-markers``, ``--strict-config``)
found in it are kept, so these settings are not more lenient than the target's own CI.
Plugins are never autoloaded: ``PLUGINS`` are loaded with an explicit ``-p``, and only
when the verifier's interpreter has them installed; a kept key that needs a missing
plugin (``timeout``, ``asyncio_*``) fails the run instead of being dropped.

Only the root-level file is read, in pytest's discovery order; a configuration file in
a test directory of the target is not looked at. A malformed or non-regular (symlink)
configuration file at the base fails the run (``PytestConfigError``).
"""

from __future__ import annotations

import configparser
import json
import os
import re
import shlex
import shutil
import subprocess
import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from cairnwork.gitcmd import run_git

# Copied from the base configuration. Each one only decides which tests exist and how
# they run, never where configuration, plugins or reports come from:
#   python_files/_classes/_functions  which names are tests;
#   testpaths, norecursedirs          used only for directory arguments (verifiers name
#                                     files), copied so the ini mirrors the base;
#   markers                           registers mark names (matters with strict markers);
#   filterwarnings                    how warnings are reported ("error" can only fail);
#   xfail_strict, strict_xfail,       strictness the target's own CI has: they can only
#   strict, strict_markers,           turn a pass into a failure, never the other way;
#   strict_config
#   asyncio_*                         pytest-asyncio needs them to run async tests;
#   timeout                           pytest-timeout limit per test; it can only end a
#                                     test sooner, Cairnwork's own timeout still applies.
# A kept key whose plugin the interpreter lacks fails the run (PytestConfigError) rather
# than being dropped: dropping it would run the tests more leniently than the target's CI.
# pythonpath is handled apart: its entries must stay inside the checkout.
BASE_KEYS = (
    "python_files",
    "python_classes",
    "python_functions",
    "testpaths",
    "norecursedirs",
    "markers",
    "filterwarnings",
    "xfail_strict",
    "strict",
    "strict_xfail",
    "strict_markers",
    "strict_config",
    "asyncio_mode",
    "asyncio_default_fixture_loop_scope",
    "asyncio_default_test_loop_scope",
    "timeout",
)
# Explicit -p plugins: (importable top-level name, plugin module, keys that need it).
PLUGINS = (
    ("pytest_asyncio", "pytest_asyncio.plugin", "asyncio_"),
    ("pytest_timeout", "pytest_timeout", "timeout"),
    ("anyio", "anyio.pytest_plugin", None),
)
IMPORT_MODES = frozenset({"prepend", "append", "importlib"})
# From the base addopts only these survive (everything else is dropped): the import
# mode, and options that make the run stricter (-W warning filters, strict markers/config).
STRICT_OPTIONS = frozenset({"--strict-markers", "--strict-config"})
# pytest's discovery order: (file, section or TOML table path, format).
_CANDIDATES = (
    ("pytest.ini", "pytest", "ini"),
    (".pytest.ini", "pytest", "ini"),
    ("pytest.toml", "pytest", "toml"),
    (".pytest.toml", "pytest", "toml"),
    ("pyproject.toml", "tool.pytest.ini_options", "toml"),
    ("pyproject.toml", "tool.pytest", "toml"),
    ("tox.ini", "pytest", "ini"),
    ("setup.cfg", "tool:pytest", "ini"),
)
_KEY = re.compile(r"^[A-Za-z0-9_]+$")
_ALWAYS_FOUND = ("pytest.ini", ".pytest.ini", "pytest.toml", ".pytest.toml")


class PytestConfigError(Exception):
    code = "PYTEST_CONFIG_ERROR"

    def __init__(self, detail: str) -> None:
        super().__init__(f"{self.code}: {detail}")


@dataclass(frozen=True)
class PytestSettings:
    source: str = ""
    values: tuple[tuple[str, Any], ...] = ()
    pythonpath: tuple[str, ...] = ()
    import_mode: str | None = None
    dropped: tuple[str, ...] = ()
    strict_args: tuple[str, ...] = ()

    def ini_text(self, checkout: Path, plugins: tuple[str, ...] = ()) -> bytes:
        """The verifier's ini. pythonpath is absolute: pytest resolves it against the
        ini's own directory, which is not the checkout. The checkout root comes first:
        ``-P`` keeps it off sys.path until pytest itself is imported."""
        lines = [f"# base config: {self.source or 'none'}", "[pytest]"]
        for key, value in self.values:
            needs = [p for p in PLUGINS if p[2] and key.startswith(p[2])]
            if needs and needs[0][1] not in plugins:
                raise PytestConfigError(
                    f"the base config sets {key}, but {needs[0][0]} is not installed"
                    " in the verifier's Python"
                )
            lines.append(f"{key} = {_render(value)}")
        paths = [str(checkout), *(str(checkout / entry) for entry in self.pythonpath)]
        lines.append("pythonpath = " + " ".join(shlex.quote(p) for p in dict.fromkeys(paths)))
        if self.dropped:
            lines.insert(1, "# dropped: " + " ".join(sorted(set(self.dropped))))
        return ("\n".join(lines) + "\n").encode()

    def args(self, plugins: tuple[str, ...] = ()) -> list[str]:
        mode = [f"--import-mode={self.import_mode}"] if self.import_mode else []
        loads = (arg for plugin in plugins for arg in ("-p", plugin))
        return [*mode, *self.strict_args, *loads]


def base_settings(repo: Path, base_commit: str | None) -> PytestSettings:
    """Read the root-level pytest configuration of base_commit from repo's objects
    (no base commit: no settings)."""
    if base_commit is None:
        return PytestSettings()
    names = sorted({name for name, _, _ in _CANDIDATES})
    listing = run_git(repo, "ls-tree", "-z", base_commit, "--", *names)
    entries = {}
    for entry in filter(None, listing.split("\0")):
        meta, path = entry.split("\t", 1)
        mode, kind, oid = meta.split()
        entries[path] = (mode, kind, oid)
    for name, section, form in _CANDIDATES:
        if name not in entries:
            continue
        mode, kind, oid = entries[name]
        if kind != "blob" or mode not in ("100644", "100755"):
            raise PytestConfigError(f"{name} at the base is not a regular file")
        found = _section(run_git(repo, "cat-file", "blob", oid), section, form, name)
        if found is not None or name in _ALWAYS_FOUND:
            return _settings(name, found or {})
    return PytestSettings()


def _section(text: str, section: str, form: str, name: str) -> dict[str, Any] | None:
    try:
        if form == "toml":
            table: Any = tomllib.loads(text)
            for part in section.split("."):
                table = table.get(part) if isinstance(table, dict) else None
            return dict(table) if isinstance(table, dict) else None
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(text)
    except (tomllib.TOMLDecodeError, configparser.Error) as exc:
        raise PytestConfigError(f"{name} at the base cannot be parsed") from exc
    return dict(parser.items(section)) if parser.has_section(section) else None


def _settings(source: str, config: dict[str, Any]) -> PytestSettings:
    values, dropped = [], []
    for key, value in config.items():
        if key in BASE_KEYS and _render(value) is not None:
            values.append((key, value))
        elif key not in ("pythonpath", "addopts"):
            dropped.append(key if _KEY.fullmatch(str(key)) else "?")
    paths = tuple(p for p in _words(config.get("pythonpath")) if _inside(p))
    if len(paths) != len(_words(config.get("pythonpath"))):
        dropped.append("pythonpath")
    addopts = _words(config.get("addopts"))
    if "addopts" in config:
        dropped.append("addopts")
    return PytestSettings(
        source,
        tuple(values),
        paths,
        _import_mode(addopts),
        tuple(dropped),
        _strict_args(addopts),
    )


def _words(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        try:
            return shlex.split(value)
        except ValueError:
            return []
    return []


def _inside(entry: str) -> bool:
    """A relative path that stays inside the checkout, judged by its text (an absolute
    path has an empty first part)."""
    parts = entry.split("/")
    return (
        bool(entry)
        and all(part not in ("", "..") for part in parts)
        and not any(ord(ch) < 32 for ch in entry)
    )


def _strict_args(tokens: list[str]) -> tuple[str, ...]:
    """-W filters and strict options from the base addopts, as separate argv items."""
    kept: list[str] = []
    for index, token in enumerate(tokens):
        if token in STRICT_OPTIONS:
            kept.append(token)
        elif token == "-W" and index + 1 < len(tokens):
            kept += ["-W", tokens[index + 1]]
        elif token.startswith("-W") and len(token) > 2:
            kept += ["-W", token[2:]]
    return tuple(kept)


def _import_mode(tokens: list[str]) -> str | None:
    mode = None
    for index, token in enumerate(tokens):
        if token.startswith("--import-mode="):
            value = token.split("=", 1)[1]
        elif token == "--import-mode" and index + 1 < len(tokens):
            value = tokens[index + 1]
        else:
            continue
        mode = value if value in IMPORT_MODES else mode
    return mode


def _render(value: Any) -> str | None:
    """One ini value. Every extra line is indented, so it continues this key and can
    never start a new key or section."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float | str):
        items = [str(value)]
    elif isinstance(value, list) and all(isinstance(i, str | int | float) for i in value):
        items = [str(i) for i in value]
    else:
        return None
    lines = [line.strip() for item in items for line in item.splitlines() if line.strip()]
    if len(lines) == 1 and not isinstance(value, list):
        return lines[0]
    return "".join(f"\n    {line}" for line in lines)


@lru_cache(maxsize=8)
def installed_plugins(python: str) -> tuple[str, ...]:
    """The PLUGINS modules the interpreter can import (found, not imported)."""
    done = subprocess.run(  # noqa: S603 - trusted interpreter path chosen by Cairnwork
        [
            python,
            "-I",
            "-c",
            "import importlib.util as u, json, sys;"
            " print(json.dumps([u.find_spec(n) is not None for n in sys.argv[1:]]))",
            *(name for name, _, _ in PLUGINS),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    found = json.loads(done.stdout)
    return tuple(module for (_, module, _), ok in zip(PLUGINS, found, strict=True) if ok)


@lru_cache(maxsize=8)
def interpreter_roots(python: str) -> tuple[Path, ...]:
    """Directories the interpreter needs to read: its prefixes and its real location."""
    done = subprocess.run(  # noqa: S603 - trusted interpreter path chosen by Cairnwork
        [
            python,
            "-I",
            "-c",
            "import json, sys; print(json.dumps([sys.prefix, sys.base_prefix,"
            " sys.exec_prefix, sys.base_exec_prefix]))",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    roots = {Path(p) for p in json.loads(done.stdout)}
    roots.add(Path(os.path.realpath(shutil.which(python) or python)).parent)
    return tuple(sorted(roots))
