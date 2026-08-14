#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
HERMES_ROOT=${HERMES_HOME:-"$HOME/.hermes"}
TARGET="$HERMES_ROOT/skills/human-browser-handoff"
mkdir -p "$TARGET"
install -m 0644 "$ROOT/skills/human-browser-handoff/SKILL.md" "$TARGET/SKILL.md"
printf 'Installed skill at %s. Start a new session or run /reload-skills.\n' "$TARGET/SKILL.md"
