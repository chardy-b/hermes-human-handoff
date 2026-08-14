#!/usr/bin/env bash
set -euo pipefail

if [[ $(uname -s) != Linux ]]; then
  printf 'This dependency installer supports Linux only.\n' >&2
  exit 2
fi

sudo apt-get update
sudo apt-get install -y git python3-venv xvfb x11vnc
temp=$(mktemp -d)
trap 'rm -rf "$temp"' EXIT
python3 -m venv "$temp/venv"
"$temp/venv/bin/python" -m pip install --quiet --disable-pip-version-check playwright==1.62.0
sudo "$temp/venv/bin/python" -m playwright install-deps chromium
printf 'System dependencies installed. scripts/install.sh will install Chromium when no system browser exists. Install and connect Tailscale before running it.\n'
