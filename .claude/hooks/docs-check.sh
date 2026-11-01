#!/usr/bin/env bash
# Stop hook: before Claude ends a turn, run tests/test_docs.py (every file, table and command the docs
# name exists; every table, command and service is drawn). If it fails, block once and hand Claude the
# failures so it fixes the docs first; if it still fails on the retry, warn instead of looping.
set -uo pipefail

input=$(cat)
retry=$(jq -r '.stop_hook_active // false' <<<"$input")
root=${CLAUDE_PROJECT_DIR:-$(pwd)}
cd "$root" || exit 0
command -v uv >/dev/null || exit 0

if out=$(uv run --quiet pytest -q -p no:cacheprovider tests/test_docs.py 2>&1); then
  exit 0
fi
failures=$(grep -E '^(E  |FAILED)' <<<"$out" | head -n 30)

if [ "$retry" = "true" ]; then
  jq -n --arg f "$failures" '{systemMessage: ("Docs still out of sync with the code (tests/test_docs.py):\n" + $f)}'
else
  jq -n --arg f "$failures" '{decision: "block", reason: ("Docs are out of sync with the code: tests/test_docs.py failed. Update docs/*.excalidraw, README.md or GLOSSARY.md (or the code) before finishing.\n" + $f)}'
fi
