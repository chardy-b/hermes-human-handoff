#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
PREFIX=${HUMAN_HANDOFF_PREFIX:-"${XDG_DATA_HOME:-$HOME/.local/share}/hermes-human-handoff"}
BIN_DIR=${HUMAN_HANDOFF_BIN_DIR:-"$HOME/.local/bin"}
HERMES_ROOT=${HERMES_HOME:-"$HOME/.hermes"}
SYSTEMD_USER_DIR=${XDG_CONFIG_HOME:-"$HOME/.config"}/systemd/user
NOVNC_TAG=v1.7.0
NOVNC_COMMIT=63107bd06d9e1f6136ff21aeda8cd62cbf0d433e

for command in python3 git Xvfb x11vnc tailscale; do
  if ! command -v "$command" >/dev/null 2>&1; then
    printf 'Missing required command: %s\n' "$command" >&2
    printf 'On Ubuntu, run: sudo apt-get update && sudo apt-get install -y git python3-venv xvfb x11vnc\n' >&2
    printf 'Install and connect Tailscale separately when tailscale is missing.\n' >&2
    exit 2
  fi
done

mkdir -p "$PREFIX" "$BIN_DIR" "$HERMES_ROOT/skills/human-browser-handoff"
chmod 700 "$PREFIX"

if [[ ! -d "$PREFIX/noVNC/.git" ]] || [[ $(git -C "$PREFIX/noVNC" rev-parse HEAD 2>/dev/null || true) != "$NOVNC_COMMIT" ]]; then
  temp=$(mktemp -d)
  trap 'rm -rf "$temp"' EXIT
  git -c advice.detachedHead=false clone --quiet --depth 1 --branch "$NOVNC_TAG" https://github.com/novnc/noVNC.git "$temp/noVNC"
  actual=$(git -C "$temp/noVNC" rev-parse HEAD)
  if [[ "$actual" != "$NOVNC_COMMIT" ]]; then
    printf 'Pinned noVNC commit mismatch: %s\n' "$actual" >&2
    exit 3
  fi
  rm -rf "$PREFIX/noVNC"
  mv "$temp/noVNC" "$PREFIX/noVNC"
fi

python3 -m venv "$PREFIX/venv"
"$PREFIX/venv/bin/python" -m pip install --quiet --disable-pip-version-check --constraint "$ROOT/requirements.txt" "$ROOT"
if ! command -v google-chrome >/dev/null 2>&1 \
  && ! command -v chromium >/dev/null 2>&1 \
  && ! command -v chromium-browser >/dev/null 2>&1; then
  "$PREFIX/venv/bin/python" -m playwright install chromium
fi
ln -sfn "$PREFIX/venv/bin/hermes-human-handoff" "$BIN_DIR/hermes-human-handoff"
install -m 0644 "$ROOT/skills/human-browser-handoff/SKILL.md" "$HERMES_ROOT/skills/human-browser-handoff/SKILL.md"

if [[ ${HUMAN_HANDOFF_SKIP_SYSTEMD:-0} != 1 ]] && command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
  mkdir -p "$SYSTEMD_USER_DIR"
  SYSTEMD_EXEC=${PREFIX//%/%%}/venv/bin/hermes-human-handoff
  cat > "$SYSTEMD_USER_DIR/hermes-human-handoff-cleanup.service" <<EOF
[Unit]
Description=Recover expired or orphaned Hermes Human Handoff sessions
After=network-online.target tailscaled.service

[Service]
Type=oneshot
Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart="$SYSTEMD_EXEC" cleanup
EOF
  cat > "$SYSTEMD_USER_DIR/hermes-human-handoff-cleanup.timer" <<'EOF'
[Unit]
Description=Periodically recover Hermes Human Handoff sessions

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
AccuracySec=30s
Persistent=true

[Install]
WantedBy=timers.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable --now hermes-human-handoff-cleanup.timer >/dev/null
fi

doctor_output=$(mktemp)
trap 'rm -f "$doctor_output"; [[ -n ${temp:-} ]] && rm -rf "$temp"' EXIT
if ! "$BIN_DIR/hermes-human-handoff" doctor >"$doctor_output"; then
  cat "$doctor_output" >&2
  printf 'Human Handoff was installed, but readiness checks failed. Connect Tailscale with MagicDNS/Serve access, verify Xvfb and x11vnc, then rerun: hermes-human-handoff doctor\n' >&2
  exit 2
fi
cat "$doctor_output"
cat <<EOF
Installed Human Handoff.
CLI: $BIN_DIR/hermes-human-handoff
Skill: $HERMES_ROOT/skills/human-browser-handoff/SKILL.md
Start a new Hermes session or run /reload-skills before first use.
EOF
