#!/usr/bin/env bash
# PostToolUse hook: after Claude edits code, config or tests that the docs describe, remind it to
# keep docs/*.excalidraw, README.md and GLOSSARY.md in sync with the change. Edits made through
# Bash don't reach this hook; the Stop hook (docs-check.sh) catches what slips through.
set -euo pipefail

file=$(jq -r '.tool_input.file_path // .tool_response.filePath // empty')
root=${CLAUDE_PROJECT_DIR:-$(pwd)}
rel=${file#"$root"/}

case "$rel" in
  src/*|db/*|tests/*|compose.yaml|Dockerfile|pyproject.toml|.env.example) ;;
  *) exit 0 ;;
esac

jq -n --arg f "$rel" '{
  hookSpecificOutput: {
    hookEventName: "PostToolUse",
    additionalContext: ("Docs sync: you changed \($f). Before finishing this task, check docs/*.excalidraw, README.md and GLOSSARY.md against it. If a component, table, command, file, dependency or data flow they show was added, renamed, removed or changed, update the docs to match (keep the existing layout and colours) and add new terms to GLOSSARY.md. If no doc covers this file yet, say so in your reply instead of guessing. If nothing documented changed, do nothing. tests/test_docs.py checks names and references automatically.")
  }
}'
