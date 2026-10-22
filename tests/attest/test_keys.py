"""Tests for cairnwork_attest.keys (keys are generated into tmp_path; none are stored)."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from cairnwork_attest.dsse import sign, verify
from cairnwork_attest.keys import (
    KeyFileError,
    generate_keypair,
    keyid,
    load_private_key,
    load_public_key_pem,
    main,
    private_key_path,
    public_key_pem,
)


def test_generate_writes_private_key_0600(tmp_path: Path) -> None:
    public_key = generate_keypair(tmp_path)

    path = private_key_path(tmp_path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    private_key = load_private_key(tmp_path)
    assert keyid(private_key.public_key()) == keyid(public_key)
    assert verify(sign(b"{}", private_key), public_key) is True


def test_generate_refuses_to_overwrite(tmp_path: Path) -> None:
    generate_keypair(tmp_path)
    before = private_key_path(tmp_path).read_bytes()

    with pytest.raises(KeyFileError) as excinfo:
        generate_keypair(tmp_path)
    assert excinfo.value.code == "KEY_EXISTS"
    assert private_key_path(tmp_path).read_bytes() == before


def test_generate_refuses_symlink_target(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    private_key_path(tmp_path).symlink_to(target)

    with pytest.raises(KeyFileError) as excinfo:
        generate_keypair(tmp_path)
    assert excinfo.value.code == "KEY_EXISTS"
    assert not target.exists()


def test_generate_needs_state_dir(tmp_path: Path) -> None:
    with pytest.raises(KeyFileError) as excinfo:
        generate_keypair(tmp_path / "missing")
    assert excinfo.value.code == "STATE_DIR_NOT_FOUND"


def test_load_missing_key(tmp_path: Path) -> None:
    with pytest.raises(KeyFileError) as excinfo:
        load_private_key(tmp_path)
    assert excinfo.value.code == "KEY_NOT_FOUND"


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o644, 0o660])
def test_load_rejects_loose_permissions(tmp_path: Path, mode: int) -> None:
    generate_keypair(tmp_path)
    os.chmod(private_key_path(tmp_path), mode)

    with pytest.raises(KeyFileError) as excinfo:
        load_private_key(tmp_path)
    assert excinfo.value.code == "KEY_PERMISSIONS"


def test_load_rejects_symlinked_key(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    generate_keypair(real)
    private_key_path(tmp_path).symlink_to(private_key_path(real))

    with pytest.raises(KeyFileError) as excinfo:
        load_private_key(tmp_path)
    assert excinfo.value.code == "KEY_NOT_REGULAR"


def test_load_rejects_malformed_key(tmp_path: Path) -> None:
    path = private_key_path(tmp_path)
    path.write_bytes(b"not a pem")
    path.chmod(0o600)

    with pytest.raises(KeyFileError) as excinfo:
        load_private_key(tmp_path)
    assert excinfo.value.code == "KEY_MALFORMED"


def test_public_key_pem_roundtrip(tmp_path: Path) -> None:
    public_key = generate_keypair(tmp_path)
    pem = public_key_pem(public_key)

    assert pem.startswith("-----BEGIN PUBLIC KEY-----")
    assert "PRIVATE" not in pem
    assert keyid(load_public_key_pem(pem)) == keyid(public_key)


def test_load_public_key_pem_rejects_garbage() -> None:
    with pytest.raises(KeyFileError) as excinfo:
        load_public_key_pem("garbage")
    assert excinfo.value.code == "KEY_MALFORMED"


def test_cli_generate_then_print_public(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["generate", "--state", str(tmp_path)]) == 0
    generated = capsys.readouterr().out
    assert main(["public", "--state", str(tmp_path)]) == 0
    printed = capsys.readouterr().out

    assert printed == generated
    assert "BEGIN PUBLIC KEY" in printed and "PRIVATE" not in printed
    assert printed.startswith(f"keyid: {keyid(load_private_key(tmp_path).public_key())}\n")


def test_cli_public_without_key_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["public", "--state", str(tmp_path)]) == 1
    assert "KEY_NOT_FOUND" in capsys.readouterr().err
