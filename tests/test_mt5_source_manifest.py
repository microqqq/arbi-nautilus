"""Lock the EA source manifest and the recipe that derives it.

The manifest is the only thing binding a running EA to a reviewed source set,
through `InpDeclaredSourceSha256` and the execution client's
`expected_source_sha256`. A recipe that cannot be recomputed is not a binding,
so both the per-file hashes and the derivation are pinned here.
"""

import hashlib
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "tools" / "mt5_source_manifest.sh"

# Ordered, repository-root-relative. Order and spelling are inside the hash.
SOURCES: tuple[tuple[str, str], ...] = (
    (
        "mt5_ea/PY000_Nautilus_MT5.mq5",
        "d4712049f4a7f866988e786d6dfc42d6a480aafacb49fdd1b2cba2020fcc7f6f",
    ),
    (
        "mt5_ea/include/Py000Execution.mqh",
        "d32b6b95d28aa664c761363fbd71cc38cad725f63da801a4bfdabba3a89808c7",
    ),
    (
        "mt5_ea/include/Py000Journal.mqh",
        "9baa2226ff1cb8c6a65ec431c74260cea41215a5fdf81f53878cca184e7343c3",
    ),
    (
        "mt5_ea/include/Py000Json.mqh",
        "30681335eb2f8d1c5f8266746164a7afa46aa4eed68cc138760a4ea8ec760d3f",
    ),
    (
        "mt5_ea/include/Py000Protocol.mqh",
        "6ae23cc49b782a99e1f8fcab3e8a94b126ce0af9a363549b7e1a461953f1a0c0",
    ),
    (
        "mt5_ea/include/Py000Zmq.mqh",
        "e9936e5e063cd85146937182f6c9c9f6a6f375d9f27697d41ae1f4b09a70bf40",
    ),
)

MANIFEST = "1ff6ce551b2f11c0876e5e0d4780d348da6a582d1eda9bb609371478a7f5daff"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(SCRIPT), *args],
        cwd=REPO_ROOT / "tests",  # deliberately not the repo root
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(("path", "digest"), SOURCES, ids=lambda value: Path(value).name)
def test_each_reviewed_source_keeps_its_digest(path: str, digest: str) -> None:
    content = (REPO_ROOT / path).read_bytes()
    assert hashlib.sha256(content).hexdigest() == digest


def test_recipe_is_reproducible_in_pure_python() -> None:
    """The shell script is a convenience; the recipe itself must be portable."""
    block = "".join(
        f"{hashlib.sha256((REPO_ROOT / path).read_bytes()).hexdigest()}  {path}\n"
        for path, _ in SOURCES
    )
    assert hashlib.sha256(block.encode("utf-8")).hexdigest() == MANIFEST


def test_script_prints_the_pinned_manifest_from_any_directory() -> None:
    result = _run()
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == MANIFEST


def test_lines_mode_emits_repository_root_relative_paths() -> None:
    result = _run("--lines")
    assert result.returncode == 0, result.stderr
    block, _, manifest = result.stdout.partition("--\n")
    assert manifest.strip() == MANIFEST
    emitted = [line for line in block.splitlines() if line]
    assert len(emitted) == len(SOURCES)
    for line, (path, digest) in zip(emitted, SOURCES, strict=True):
        # Two spaces, repository-root-relative path: both are inside the hash.
        assert line == f"{digest}  {path}"


def test_check_mode_accepts_the_pinned_manifest() -> None:
    assert _run("--check", MANIFEST).returncode == 0


def test_check_mode_rejects_a_different_manifest() -> None:
    result = _run("--check", "0" * 64)
    assert result.returncode == 1
    assert "manifest mismatch" in result.stderr


def test_check_mode_rejects_a_malformed_expectation() -> None:
    result = _run("--check", "not-a-digest")
    assert result.returncode == 2


def test_declared_placeholder_is_never_the_live_manifest() -> None:
    """Writing the manifest into a hashed file would make it self-referential."""
    source = (REPO_ROOT / "mt5_ea" / "PY000_Nautilus_MT5.mq5").read_text(encoding="utf-8")
    assert MANIFEST not in source
    assert '"0000000000000000000000000000000000000000000000000000000000000000"' in source
