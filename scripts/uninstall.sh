#!/usr/bin/env bash
set -euo pipefail

PREFIX=${HUMAN_HANDOFF_PREFIX:-"${XDG_DATA_HOME:-$HOME/.local/share}/hermes-human-handoff"}
BIN_DIR=${HUMAN_HANDOFF_BIN_DIR:-"$HOME/.local/bin"}
HERMES_ROOT=${HERMES_HOME:-"$HOME/.hermes"}
SYSTEMD_USER_DIR=${XDG_CONFIG_HOME:-"$HOME/.config"}/systemd/user

if [[ -x "$BIN_DIR/hermes-human-handoff" ]]; then
  "$BIN_DIR/hermes-human-handoff" cleanup >/dev/null || {
    printf 'A stale handoff could not be cleaned. Repair it before uninstalling.\n' >&2
    exit 2
  }
  "$BIN_DIR/hermes-human-handoff" status | python3 -c 'import json,sys; active=[x for x in json.load(sys.stdin) if x.get("status") not in {"stopped","expired"}]; raise SystemExit(1 if active else 0)' || {
    printf 'An active or failed handoff remains. Stop or repair it before uninstalling.\n' >&2
    exit 2
  }
fi

if command -v systemctl >/dev/null 2>&1; then
  systemctl --user disable --now hermes-human-handoff-cleanup.timer >/dev/null 2>&1 || true
fi
rm -f "$SYSTEMD_USER_DIR/hermes-human-handoff-cleanup.timer"
rm -f "$SYSTEMD_USER_DIR/hermes-human-handoff-cleanup.service"
if command -v systemctl >/dev/null 2>&1; then
  systemctl --user daemon-reload >/dev/null 2>&1 || true
fi

rm -f "$BIN_DIR/hermes-human-handoff"
rm -rf "$PREFIX"
rm -rf "$HERMES_ROOT/skills/human-browser-handoff"
printf 'Human Handoff removed.\n'
