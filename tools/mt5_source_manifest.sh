#!/usr/bin/env bash
# Recompute the PY000 MT5 EA declared source manifest.
#
# The manifest is what an operator supplies through the EA input
# `InpDeclaredSourceSha256`, and what the Nautilus execution client checks
# against `expected_source_sha256` during `hello`. It therefore has to be
# recomputable from a clean checkout, byte for byte, by anyone.
#
# Recipe (this is the contract; do not "improve" it):
#
#   1. Hash each source file with SHA-256, in the fixed order below.
#   2. Render one line per file as `<hex><two spaces><path>` followed by LF,
#      where <path> is REPOSITORY-ROOT-RELATIVE. The paths are inside the
#      hash, so the spelling matters: `mt5_ea/include/Py000Zmq.mqh`, not
#      `Py000Zmq.mqh` and not an absolute path.
#   3. SHA-256 that whole block, including the trailing LF of the last line.
#
# That is exactly the output of `shasum -a 256 <paths...>` piped into
# `shasum -a 256`, run from the repository root. GNU `sha256sum` in its
# default (text) mode produces the identical two-space format; do not pass
# `-b`, which switches the separator to " *" and changes the manifest.
#
# The declared value must never be written into a hashed file: doing so makes
# the manifest self-referential and unverifiable. The all-zero placeholder in
# `PY000_Nautilus_MT5.mq5` is deliberate.
#
# Usage:
#   tools/mt5_source_manifest.sh                 print the manifest
#   tools/mt5_source_manifest.sh --lines         print the per-file block too
#   tools/mt5_source_manifest.sh --check <hex>   exit 1 unless it matches

set -euo pipefail

# Ordered, repository-root-relative. Order and spelling are part of the hash.
SOURCES=(
  "mt5_ea/PY000_Nautilus_MT5.mq5"
  "mt5_ea/include/Py000Execution.mqh"
  "mt5_ea/include/Py000Journal.mqh"
  "mt5_ea/include/Py000Json.mqh"
  "mt5_ea/include/Py000Protocol.mqh"
  "mt5_ea/include/Py000Zmq.mqh"
)

usage() {
  sed -n '/^# Usage:/,/--check <hex>/p' "$0" | sed 's/^# \{0,1\}//'
  exit 2
}

show_lines=0
expected=""
while [ $# -gt 0 ]; do
  case "$1" in
    --lines) show_lines=1; shift ;;
    --check)
      [ $# -ge 2 ] || { echo "error: --check needs a value" >&2; exit 2; }
      expected="$2"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "error: unknown argument '$1'" >&2; usage ;;
  esac
done

if [ -n "$expected" ] && ! printf '%s' "$expected" | grep -Eq '^[0-9a-f]{64}$'; then
  echo "error: --check expects 64 lowercase hex characters" >&2
  exit 2
fi

if command -v shasum >/dev/null 2>&1; then
  sha256() { shasum -a 256 "$@"; }
elif command -v sha256sum >/dev/null 2>&1; then
  sha256() { sha256sum "$@"; }   # default text mode: two-space separator
else
  echo "error: neither shasum nor sha256sum is available" >&2
  exit 3
fi

# Resolve the repository root from this script's own location, so the paths
# fed to the hasher are always the repository-root-relative ones above,
# whatever the caller's working directory is.
script_dir="$(cd -- "$(dirname -- "$0")" && pwd)"
repo_root="$(cd -- "$script_dir/.." && pwd)"
cd -- "$repo_root"

missing=0
for path in "${SOURCES[@]}"; do
  if [ ! -f "$path" ]; then
    echo "error: missing source file: $path" >&2
    missing=1
  fi
done
[ "$missing" -eq 0 ] || exit 3

lines="$(sha256 "${SOURCES[@]}")"
manifest="$(printf '%s\n' "$lines" | sha256 | awk '{print $1}')"

if [ "$show_lines" -eq 1 ]; then
  printf '%s\n' "$lines"
  printf -- '--\n'
fi

printf '%s\n' "$manifest"

if [ -n "$expected" ] && [ "$manifest" != "$expected" ]; then
  echo "error: manifest mismatch" >&2
  echo "  expected $expected" >&2
  echo "  actual   $manifest" >&2
  exit 1
fi
