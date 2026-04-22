#!/usr/bin/env bash
set -euo pipefail

# Placeholder utility for byte-level comparison of two GBX artifacts.
# Example:
#   tools/reverse_engineering/diff_gbx_hex.sh a.Replay.Gbx b.Replay.Gbx | less

if [[ $# -ne 2 ]]; then
  echo "usage: $0 <file_a> <file_b>" >&2
  exit 2
fi

file_a="$1"
file_b="$2"

tmp_a="$(mktemp)"
tmp_b="$(mktemp)"
trap 'rm -f "$tmp_a" "$tmp_b"' EXIT

xxd -g 1 "$file_a" > "$tmp_a"
xxd -g 1 "$file_b" > "$tmp_b"

diff -u "$tmp_a" "$tmp_b" || true
