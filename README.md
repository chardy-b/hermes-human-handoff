# Hermes Human Handoff

Human-in-the-loop browser takeover for agents running on headless Linux.

An agent can prepare a signup, checkout, consent, authentication, or identity workflow, send the user an expiring Tailnet link to the exact browser session, pause while the user completes the human-only step, then resume and verify the result.

It is **default-installed infrastructure, trigger-invoked**: client agents should have it available, while ordinary browser work continues through normal automation.

## Use cases

- CAPTCHA, Cloudflare Turnstile, and human-presence checks
- email/SMS/TOTP codes and device approval
- OAuth/SSO consent and account recovery
- card entry, 3-D Secure, checkout approval, and subscription confirmation
- Terms, legal attestations, permissions, and irreversible account creation
- KYC, identity fields, document upload, signature, or user-requested takeover

Human Handoff does not solve CAPTCHAs, accept terms, make purchases, or provide identity assertions. It preserves the session and gives the authorized human the controls.

## How it works

```text
managed Chromium on selected profile mode
              │
         Xvfb display
              │
  x11vnc (loopback + password)
              │
websockify/noVNC (loopback only)
              │
Tailscale Serve temporary HTTPS port
              │
       user's web browser
```

Every session gets a fresh display, random VNC capability, loopback ports, Tailnet HTTPS port, and hard expiry. The default and template-clone modes also get a fresh disposable browser profile. The capability travels in the URL fragment and is used for VNC authentication; it is absent from HTTP request lines and ordinary access logs. Stop/expiry removes the Serve mapping, processes, capability, and disposable profile. An explicitly selected persistent profile remains on disk by design.

## Requirements

- Linux
- Python 3.10+
- `Xvfb` and `x11vnc`
- Chromium or Google Chrome; Playwright-managed Chromium is detected
- Tailscale connected with MagicDNS and permission to use Tailscale Serve
- Git during installation, used to fetch pinned noVNC `v1.7.0`

System Chrome/Chromium keeps its normal browser sandbox. Playwright-downloaded Chromium is launched with `--no-sandbox` on Linux because that build lacks a usable system/AppArmor sandbox; use a system browser when browser-process isolation is part of your threat model.

## Install

On Ubuntu:

```bash
git clone https://github.com/humanitylabs-org/hermes-human-handoff.git
cd hermes-human-handoff
./scripts/install-ubuntu-deps.sh
# Install/connect Tailscale if it is not already running.
./scripts/install.sh
```

The installer creates a dedicated user-local virtual environment, installs a Playwright-managed Chromium when no system browser exists, fetches and verifies the pinned noVNC commit, installs `hermes-human-handoff` under `~/.local/bin`, installs the `human-browser-handoff` skill under `$HERMES_HOME/skills`, and enables a five-minute user-systemd cleanup timer when user systemd is available. Every new `start` also recovers stale sessions before publishing another route.

Start a new Hermes session or run `/reload-skills` after installation.

## CLI

Check readiness:

```bash
hermes-human-handoff doctor
```

Start an isolated browser:

```bash
hermes-human-handoff start \
  --purpose captcha \
  --ttl 900 \
  --url 'https://service.example/signup'
```

### Browser profile modes

- **Disposable (default):** creates a new empty profile inside the session and removes it at stop or expiry.
- **Template clone:** `--profile-template /private/template` copies an owner-only Chromium profile into a new disposable session profile. Chromium lock files are excluded; the clone is removed at stop or expiry, while the template is unchanged.
- **Persistent:** `--persistent-profile /private/profile` uses an owner-only profile in place and keeps its cookies, address data, and other browser state after stop or expiry. The runtime disables browser password management and card storage, but the persistent profile is durable sensitive data and must be protected accordingly. One session at a time may use it.

Use `--purpose address-setup --persistent-profile /private/profile` when the intended result is durable browser address autofill. `address-setup` refuses disposable and template-clone profiles so the user is not told that an address was saved when the profile will be destroyed.

The result is JSON:

```json
{
  "session_id": "hh-YYYYMMDD-HHMMSS-xxxxxx",
  "status": "ready",
  "cdp_url": "http://127.0.0.1:PORT",
  "handoff_url": "https://TAILNET_HOST:PORT/handoff.html#CAPABILITY",
  "expires_at": "ISO-8601"
}
```

The agent automates the browser through the loopback Chrome DevTools endpoint. When a human gate appears, it sends `handoff_url`, pauses browser inspection, and waits for the user.

After provider readback:

```bash
hermes-human-handoff stop <session_id>
```

Other commands:

```bash
hermes-human-handoff status
hermes-human-handoff status <session_id>
hermes-human-handoff url <session_id>
hermes-human-handoff run <session_id> automation.py
hermes-human-handoff cleanup
```

## Playwright connection

The managed browser exposes Chrome DevTools only on loopback. The package includes the Playwright client and a `run` command that injects the selected session's loopback endpoint without putting it in script arguments:

```bash
hermes-human-handoff run <session_id> examples/read_page.py
```

Automation scripts connect with:

```python
import os
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp(os.environ["HANDOFF_CDP_URL"])
    page = browser.contexts[0].pages[0]
    print(page.url)  # Print only sanitized state.
```

Disconnect the Playwright client after each task script and let the handoff worker own browser shutdown. Calling `browser.close()` would terminate the shared session.

For workflows likely to hit CAPTCHA, payment, identity, consent, or authentication gates, start inside Human Handoff from the beginning. An arbitrary browser-tool session cannot generally be transferred after the fact without losing cookies and page state.

## Agent behavior

The packaged skill tells the agent to:

1. run `doctor`;
2. begin likely human-gated flows in the managed browser;
3. automate authorized non-sensitive steps through CDP;
4. send one current Tailnet link and one concrete human action;
5. pause screenshots, DOM reads, accessibility reads, OCR, console access, tracing, and recording while sensitive values are present;
6. resume only after the user submits and raw values are gone;
7. verify authoritative provider state before retrying or declaring success;
8. stop the session and verify teardown.

Payment and legal actions remain human-approved. A submission with an uncertain outcome is never automatically retried.

## Security boundary

This package keeps sensitive human input out of normal chat/model context when the agent follows the skill. It assumes the endpoint host, operating-system account, model provider, VPS provider, Tailscale account, browser, and destination provider are trusted.

It is not a cryptographic boundary against a malicious host administrator or compromised browser. Camera, NFC, USB security keys, and some device-bound passkeys may require a local-browser handoff instead.

See [SECURITY.md](SECURITY.md).

## Development and QA

Unit tests:

```bash
python3 -m unittest discover -s tests -v
```

Leak lint:

```bash
python3 qa/leak_lint.py .
```

Live local smoke test:

```bash
./qa/smoke.sh
```

The smoke test launches the real Xvfb → x11vnc → websockify → Chromium path on loopback, checks the Chrome DevTools endpoint, proves the correct VNC capability renders, proves a wrong capability is refused, and verifies teardown. The local smoke does not prove Tailscale Serve transport; release operators should run a normal `start`, connect from a second Tailnet peer, and verify from that peer that the temporary route disappears after stop or expiry.

## Uninstall

```bash
./scripts/uninstall.sh
```

Uninstall refuses while an active handoff exists.

## License

MIT. noVNC and websockify remain under their respective upstream licenses and are installed as dependencies rather than copied into this repository.
