from __future__ import annotations

import os
from pathlib import Path

import pytest

import py000_nautilus.durability as durability
from py000_nautilus.durability import ParentDirectorySyncError, replace_and_sync_parent


def test_posix_replace_syncs_parent_and_closes_descriptor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, ...]] = []

    monkeypatch.setattr(durability, "_DIRECTORY_FSYNC_SUPPORTED", True)

    def record_open(path: object, flags: int) -> int:
        calls.append(("open", path, flags))
        return 73

    monkeypatch.setattr("py000_nautilus.durability.os.open", record_open)
    monkeypatch.setattr(
        "py000_nautilus.durability.os.replace",
        lambda source, destination: calls.append(("replace", source, destination)),
    )
    monkeypatch.setattr(
        "py000_nautilus.durability.os.fsync",
        lambda fd: calls.append(("fsync", fd)),
    )
    monkeypatch.setattr(
        "py000_nautilus.durability.os.close",
        lambda fd: calls.append(("close", fd)),
    )

    replace_and_sync_parent(Path("state.tmp"), Path("runtime/state.json"))

    assert calls[0][0:2] == ("open", Path("runtime"))
    flags = calls[0][2]
    assert isinstance(flags, int)
    assert flags & os.O_RDONLY == os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        assert flags & os.O_DIRECTORY == os.O_DIRECTORY
    assert calls[1:] == [
        ("replace", Path("state.tmp"), Path("runtime/state.json")),
        ("fsync", 73),
        ("close", 73),
    ]


def test_parent_sync_failure_is_post_replace_and_closes_descriptor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    monkeypatch.setattr(durability, "_DIRECTORY_FSYNC_SUPPORTED", True)
    monkeypatch.setattr("py000_nautilus.durability.os.open", lambda _path, _flags: 91)
    monkeypatch.setattr(
        "py000_nautilus.durability.os.replace",
        lambda _source, _destination: calls.append("replace"),
    )

    def fail_sync(_fd: int) -> None:
        calls.append("fsync")
        raise OSError("directory sync failed")

    monkeypatch.setattr("py000_nautilus.durability.os.fsync", fail_sync)
    monkeypatch.setattr(
        "py000_nautilus.durability.os.close", lambda _fd: calls.append("close")
    )

    with pytest.raises(ParentDirectorySyncError, match="replacement completed") as caught:
        replace_and_sync_parent("state.tmp", "runtime/state.json")

    assert isinstance(caught.value.__cause__, OSError)
    assert calls == ["replace", "fsync", "close"]


def test_replace_failure_never_attempts_parent_sync_and_still_closes_descriptor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    monkeypatch.setattr(durability, "_DIRECTORY_FSYNC_SUPPORTED", True)
    monkeypatch.setattr("py000_nautilus.durability.os.open", lambda _path, _flags: 37)

    def fail_replace(_source: object, _destination: object) -> None:
        calls.append("replace")
        raise OSError("replace failed")

    monkeypatch.setattr("py000_nautilus.durability.os.replace", fail_replace)
    monkeypatch.setattr(
        "py000_nautilus.durability.os.fsync", lambda _fd: calls.append("fsync")
    )
    monkeypatch.setattr(
        "py000_nautilus.durability.os.close", lambda _fd: calls.append("close")
    )

    with pytest.raises(OSError, match="replace failed"):
        replace_and_sync_parent("state.tmp", "runtime/state.json")

    assert calls == ["replace", "close"]


def test_post_replace_close_failure_is_reported_as_uncertain_durability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(durability, "_DIRECTORY_FSYNC_SUPPORTED", True)
    monkeypatch.setattr("py000_nautilus.durability.os.open", lambda _path, _flags: 57)
    monkeypatch.setattr(
        "py000_nautilus.durability.os.replace", lambda _source, _destination: None
    )
    monkeypatch.setattr("py000_nautilus.durability.os.fsync", lambda _fd: None)

    def fail_close(_fd: int) -> None:
        raise OSError("directory close failed")

    monkeypatch.setattr("py000_nautilus.durability.os.close", fail_close)

    with pytest.raises(ParentDirectorySyncError, match="replacement completed") as caught:
        replace_and_sync_parent("state.tmp", "runtime/state.json")

    assert isinstance(caught.value.__cause__, OSError)


def test_non_posix_replace_has_explicitly_weaker_directory_guarantee(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    monkeypatch.setattr(durability, "_DIRECTORY_FSYNC_SUPPORTED", False)
    monkeypatch.setattr(
        "py000_nautilus.durability.os.replace",
        lambda _source, _destination: calls.append("replace"),
    )
    monkeypatch.setattr(
        "py000_nautilus.durability.os.open",
        lambda _path, _flags: pytest.fail("directory open is not portable off POSIX"),
    )

    replace_and_sync_parent("state.tmp", "runtime/state.json")

    assert calls == ["replace"]


def test_create_publishes_complete_bytes_without_replacing_an_existing_target(
    tmp_path: Path,
) -> None:
    source, target = tmp_path / "new.tmp", tmp_path / "state.json"
    source.write_bytes(b"complete candidate")
    durability.create_and_sync_parent(source, target)
    assert target.read_bytes() == source.read_bytes() == b"complete candidate"
    other = tmp_path / "other.tmp"
    other.write_bytes(b"different candidate")
    with pytest.raises(FileExistsError):
        durability.create_and_sync_parent(other, target)
    assert target.read_bytes() == b"complete candidate"
    assert other.read_bytes() == b"different candidate"


@pytest.mark.skipif(os.name != "posix", reason="requires a real POSIX directory descriptor")
def test_create_parent_sync_failure_retains_complete_new_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, target = tmp_path / "new.tmp", tmp_path / "state.json"
    source.write_bytes(b"complete candidate")
    monkeypatch.setattr(durability, "_DIRECTORY_FSYNC_SUPPORTED", True)

    def fail_sync(_fd: int) -> None:
        raise OSError("directory sync failed")

    monkeypatch.setattr("py000_nautilus.durability.os.fsync", fail_sync)
    with pytest.raises(ParentDirectorySyncError, match="creation completed"):
        durability.create_and_sync_parent(source, target)
    assert target.read_bytes() == source.read_bytes() == b"complete candidate"


def test_create_link_failure_leaves_existing_target_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, target = tmp_path / "new.tmp", tmp_path / "state.json"
    source.write_bytes(b"new")
    target.write_bytes(b"old")
    synced: list[int] = []
    monkeypatch.setattr("py000_nautilus.durability.os.fsync", synced.append)
    with pytest.raises(FileExistsError):
        durability.create_and_sync_parent(source, target)
    assert target.read_bytes() == b"old" and source.read_bytes() == b"new"
    assert synced == []
