#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
TEMP=$(mktemp -d)
SERVER_PID=''
SESSION_ID=''
cleanup() {
  if [[ -n "$SESSION_ID" ]] && [[ -x "$TEMP/venv/bin/hermes-human-handoff" ]]; then
    "$TEMP/venv/bin/hermes-human-handoff" stop "$SESSION_ID" >/dev/null 2>&1 || true
  fi
  if [[ -n "$SERVER_PID" ]]; then
    kill "$SERVER_PID" >/dev/null 2>&1 || true
    wait "$SERVER_PID" >/dev/null 2>&1 || true
  fi
  rm -rf "$TEMP"
}
trap cleanup EXIT

python3 -m venv "$TEMP/venv"
"$TEMP/venv/bin/python" -m pip install --quiet --disable-pip-version-check "$ROOT"

export HUMAN_HANDOFF_HOME="$TEMP/state"
if [[ -z ${HUMAN_HANDOFF_NOVNC_DIR:-} ]]; then
  INSTALLED_NOVNC="${XDG_DATA_HOME:-$HOME/.local/share}/hermes-human-handoff/noVNC"
  if [[ -f "$INSTALLED_NOVNC/core/rfb.js" ]]; then
    export HUMAN_HANDOFF_NOVNC_DIR="$INSTALLED_NOVNC"
  else
    git clone --quiet --depth 1 --branch v1.7.0 https://github.com/novnc/noVNC.git "$TEMP/noVNC"
    [[ $(git -C "$TEMP/noVNC" rev-parse HEAD) == 63107bd06d9e1f6136ff21aeda8cd62cbf0d433e ]]
    export HUMAN_HANDOFF_NOVNC_DIR="$TEMP/noVNC"
  fi
fi

mkdir -p "$TEMP/site"
printf '<!doctype html><title>Handoff Target</title><h1 id="sentinel">HANDOFF_TARGET_OK</h1>\n' > "$TEMP/site/index.html"
TARGET_PORT=$("$TEMP/venv/bin/python" - <<'PY'
import socket
with socket.socket() as s:
    s.bind(('127.0.0.1', 0))
    print(s.getsockname()[1])
PY
)
"$TEMP/venv/bin/python" -m http.server "$TARGET_PORT" --bind 127.0.0.1 --directory "$TEMP/site" >"$TEMP/http.log" 2>&1 &
SERVER_PID=$!

for _ in $(seq 1 50); do
  if curl -fsS "http://127.0.0.1:$TARGET_PORT/" >/dev/null 2>&1; then break; fi
  sleep 0.1
done

RESULT=$("$TEMP/venv/bin/hermes-human-handoff" start \
  --local-only --purpose captcha --ttl 120 --url "http://127.0.0.1:$TARGET_PORT/")
printf '%s' "$RESULT" | "$TEMP/venv/bin/python" -c 'import json,sys; d=json.load(sys.stdin); d.pop("handoff_url",None); print(json.dumps(d,sort_keys=True))'
SESSION_ID=$(printf '%s' "$RESULT" | "$TEMP/venv/bin/python" -c 'import json,sys; print(json.load(sys.stdin)["session_id"])')
HANDOFF_URL=$(printf '%s' "$RESULT" | "$TEMP/venv/bin/python" -c 'import json,sys; print(json.load(sys.stdin)["handoff_url"])')
CDP_URL=$(printf '%s' "$RESULT" | "$TEMP/venv/bin/python" -c 'import json,sys; print(json.load(sys.stdin)["cdp_url"])')
HANDOFF_PAGE=${HANDOFF_URL%%#*}

curl -fsS "$HANDOFF_PAGE" | grep -q 'Human Handoff'
curl -fsS "$CDP_URL/json/list" | "$TEMP/venv/bin/python" -c 'import json,sys; pages=json.load(sys.stdin); assert any(p.get("title")=="Handoff Target" and p.get("type")=="page" for p in pages)'
RUN_OUTPUT=$("$TEMP/venv/bin/hermes-human-handoff" run "$SESSION_ID" "$ROOT/examples/read_page.py")
printf '%s' "$RUN_OUTPUT" | "$TEMP/venv/bin/python" -c 'import json,sys; d=json.load(sys.stdin); assert d["title"]=="Handoff Target" and d["url"].startswith("http://127.0.0.1:")'
"$TEMP/venv/bin/python" "$ROOT/qa/ws_probe.py" "$HANDOFF_PAGE"
PLAYWRIGHT_MODULE=$("$TEMP/venv/bin/python" - <<'PY'
from pathlib import Path
import playwright
print(Path(playwright.__file__).resolve().parent / "driver" / "package")
PY
)
HUMAN_HANDOFF_PLAYWRIGHT_MODULE="$PLAYWRIGHT_MODULE" node "$ROOT/qa/browser_probe.cjs" "$HANDOFF_URL"

STOP=$("$TEMP/venv/bin/hermes-human-handoff" stop "$SESSION_ID")
printf '%s\n' "$STOP" | "$TEMP/venv/bin/python" -c 'import json,sys; d=json.load(sys.stdin); assert d["status"]=="stopped"; assert not d.get("cleanup_error")'
[[ ! -e "$HUMAN_HANDOFF_HOME/sessions/$SESSION_ID/capability.json" ]]
[[ ! -e "$HUMAN_HANDOFF_HOME/sessions/$SESSION_ID/config.json" ]]
[[ ! -e "$HUMAN_HANDOFF_HOME/sessions/$SESSION_ID/browser-profile" ]]
if curl -fsS --max-time 1 "$HANDOFF_PAGE" >/dev/null 2>&1; then
  printf 'handoff listener remained reachable after stop\n' >&2
  exit 1
fi
SESSION_ID=''
printf 'HANDOFF_SMOKE_OK\n'
