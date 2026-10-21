import stat

import pytest


def test_making_a_tree_writable_never_follows_a_symlink(tmp_path):
    """#40 review: a symlink swapped in (at the top or below) must not let the owner chmod
    a directory outside the attempt."""
    from cairnwork.workspace import _make_tree_writable

    outside = tmp_path / "outside"
    (outside / "inner").mkdir(parents=True)
    outside.chmod(0o550)
    tree = tmp_path / "tree"
    (tree / "locked" / "deep").mkdir(parents=True)
    (tree / "locked").chmod(0)
    (tree / "link").symlink_to(outside)
    top_link = tmp_path / "top-link"
    top_link.symlink_to(outside)
    try:
        _make_tree_writable(tree)
        assert stat.S_IMODE((tree / "locked").stat().st_mode) == 0o700
        with pytest.raises(OSError):
            _make_tree_writable(top_link)
        assert stat.S_IMODE(outside.stat().st_mode) == 0o550
        assert stat.S_IMODE((outside / "inner").stat().st_mode) != 0o700
    finally:
        outside.chmod(0o700)
