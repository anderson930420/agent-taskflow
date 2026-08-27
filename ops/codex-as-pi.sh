#!/bin/bash
# Shim: taskflow calls this as --pi-bin with: --no-session -p <prompt>
# We extract the prompt and feed it to `codex exec` (subscription-backed),
# so the Pi executor slot is driven by Codex without changing the dispatcher.
#
# Override the Codex binary with CODEX_BIN; the default is the VPS layout.
CODEX_BIN="${CODEX_BIN:-/home/ubuntu/tools/pi-agent/bin/codex}"

prompt=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-session|-p|--print) shift ;;
    *) prompt="$1"; shift ;;
  esac
done
[[ -z "$prompt" ]] && { echo "codex-as-pi: empty prompt" >&2; exit 2; }
printf '%s' "$prompt" | exec "$CODEX_BIN" exec --sandbox danger-full-access
