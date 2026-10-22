"""Tests for cairnwork_attest.keys (keys are generated into tmp_path; none are stored)."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from cairnwork_attest.dsse import _sign_bytes, verify
from cairnwork_attest.keys import (
    KeyFileError,
    _safe_unlink,
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
    assert verify(_sign_bytes(b"{}", private_key), public_key) is True


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


def test_generate_write_loop_one_byte(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real_write = os.write

    def write_one_byte(fd: int, data: bytes | memoryview) -> int:
        return real_write(fd, bytes(data)[:1])

    monkeypatch.setattr(os, "write", write_one_byte)
    public_key = generate_keypair(tmp_path)
    priv = load_private_key(tmp_path)
    assert keyid(priv.public_key()) == keyid(public_key)


def test_generate_failure_leaves_no_stray_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_fsync(fd: int) -> None:
        raise OSError("simulated fsync failure")

    monkeypatch.setattr(os, "fsync", fail_fsync)
    with pytest.raises(KeyFileError) as excinfo:
        generate_keypair(tmp_path)
    assert excinfo.value.code == "KEY_UNWRITABLE"
    assert not private_key_path(tmp_path).exists()


def test_generate_failure_never_deletes_foreign_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fsync_and_swap(fd: int) -> None:
        path = private_key_path(tmp_path)
        path.unlink()
        path.write_text("foreign content")
        raise OSError("simulated failure after swap")

    monkeypatch.setattr(os, "fsync", fsync_and_swap)
    with pytest.raises(KeyFileError) as excinfo:
        generate_keypair(tmp_path)
    assert excinfo.value.code == "KEY_UNWRITABLE"
    assert private_key_path(tmp_path).read_text() == "foreign content"


def test_generate_fstat_failure_leaves_file_for_manual_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:

    def fail_fstat(fd: int) -> os.stat_result:
        raise OSError("simulated fstat failure")

    monkeypatch.setattr(os, "fstat", fail_fstat)
    path = private_key_path(tmp_path)
    with pytest.raises(KeyFileError) as excinfo:
        generate_keypair(tmp_path)
    assert excinfo.value.code == "KEY_UNWRITABLE"
    assert "manual review" in str(excinfo.value)
    assert str(path) in str(excinfo.value)
    assert path.exists()


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
    assert excinfo.value.code == "KEY_PERMISSIONS_LOOSE"


def test_load_rejects_wrong_owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    generate_keypair(tmp_path)
    monkeypatch.setattr(os, "getuid", lambda: 99999)

    with pytest.raises(KeyFileError) as excinfo:
        load_private_key(tmp_path)
    assert excinfo.value.code == "KEY_WRONG_OWNER"


def test_load_rejects_symlinked_key(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    generate_keypair(real)
    private_key_path(tmp_path).symlink_to(private_key_path(real))

    with pytest.raises(KeyFileError) as excinfo:
        load_private_key(tmp_path)
    assert excinfo.value.code == "KEY_NOT_REGULAR"


def test_load_swapped_symlink_fails_closed(tmp_path: Path) -> None:
    generate_keypair(tmp_path)
    real = tmp_path / "real"
    real.mkdir()
    generate_keypair(real)
    path = private_key_path(tmp_path)
    path.unlink()
    path.symlink_to(private_key_path(real))

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


def test_load_rejects_fifo(tmp_path: Path) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("mkfifo not supported on this platform")
    path = private_key_path(tmp_path)
    os.mkfifo(path, 0o600)

    with pytest.raises(KeyFileError) as excinfo:
        load_private_key(tmp_path)
    assert excinfo.value.code == "KEY_NOT_REGULAR"


def test_safe_unlink_preserves_file_when_inode_changed(tmp_path: Path) -> None:
    path = private_key_path(tmp_path)
    path.write_text("original content")
    st_orig = path.stat()

    path.unlink()
    path.write_text("replaced content")
    st_new = path.stat()
    assert st_new.st_ino != st_orig.st_ino

    _safe_unlink(tmp_path, path.name, st_orig.st_dev, st_orig.st_ino)
    assert path.exists()
    assert path.read_text() == "replaced content"


def test_safe_unlink_deletes_when_dev_and_ino_match(tmp_path: Path) -> None:
    path = private_key_path(tmp_path)
    path.write_text("to be unlinked")
    st = path.stat()

    _safe_unlink(tmp_path, path.name, st.st_dev, st.st_ino)
    assert not path.exists()


def test_load_rejects_oversize_key(tmp_path: Path) -> None:
    path = private_key_path(tmp_path)
    path.write_bytes(b"A" * 16385)
    path.chmod(0o600)

    with pytest.raises(KeyFileError) as excinfo:
        load_private_key(tmp_path)
    assert excinfo.value.code == "KEY_TOO_LARGE"
