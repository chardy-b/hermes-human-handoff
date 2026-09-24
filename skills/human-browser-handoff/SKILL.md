---
name: human-browser-handoff
description: Use when browser automation reaches a CAPTCHA, OTP, payment, consent, identity, or other human-only step. Preserve the exact browser through an expiring Tailnet takeover, pause inspection, verify completion, and retire it.
version: 0.1.0
author: Humanity Labs
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [browser, human-in-the-loop, tailscale, captcha, payments, privacy]
    related_skills: []
---

# Human Browser Handoff

This skill is packaged for Hermes Agent. Install it under `$HERMES_HOME/skills` and reload skills before use.

## Overview

Use `hermes-human-handoff` to run one isolated headful Chromium session on Linux and let the user temporarily control that exact session from a Tailnet-only noVNC page. The CLI owns the virtual display, browser-profile mode, loopback VNC/WebSocket listeners, temporary Tailscale Serve port, expiry, and teardown.

This is a human-presence bridge. It never solves CAPTCHAs or supplies legal, financial, identity, or authentication decisions for the user.

## When to use

Start a managed browser when a workflow is likely to encounter:

- CAPTCHA, Turnstile, or other human-presence checks;
- email/SMS/TOTP codes, device approval, SSO, OAuth consent, passkeys, or account recovery;
- card entry, 3-D Secure, final purchase review, or subscription confirmation;
- Terms acceptance, permissions, legal attestations, age checks, or irreversible account creation;
- KYC, identity details, document upload, signature, or similar personally sensitive forms;
- any page where the user asks to take the wheel before the agent continues.

Use a separate secure, one-time secret-intake path instead when the agent needs an API key, password, token, or private URL after the interaction. Some camera, USB, smart-card, and device-bound passkey flows require the user's local browser; switch to a local merchant/provider handoff when the remote browser cannot access the factor.

## Prerequisite gate

Run:

```bash
hermes-human-handoff doctor
```

Proceed only when it returns `"ready": true`. The expected host has Tailscale connected, no Funnel-enabled handoff ports, noVNC installed by this package, and loopback-capable `Xvfb`, `x11vnc`, `websockify`, and Chromium binaries.

## Start the browser before the risky flow

The exact session can only be preserved if the signup, checkout, or identity workflow begins in the managed browser. Start it before entering credentials or building state that may hit a human gate:

```bash
hermes-human-handoff start \
  --purpose captcha \
  --ttl 900 \
  --url 'https://service.example/signup'
```

The JSON response contains:

- `session_id`: safe identifier used for status and stop;
- `cdp_url`: loopback Chrome DevTools endpoint for local automation;
- `handoff_url`: expiring Tailnet link to send only to the intended human;
- `expires_at`: hard session deadline.

Profile modes are explicit:

- default: a fresh empty disposable profile, removed at stop or expiry;
- `--profile-template /private/template`: a fresh disposable clone, removed at stop or expiry while the template remains unchanged;
- `--persistent-profile /private/profile`: an owner-only profile used in place and retained after stop or expiry with its cookies, address data, and other browser state. Password management and card storage are disabled, but this directory remains durable sensitive data.

`--purpose address-setup` requires `--persistent-profile`; use it only when durable address autofill is the intended outcome. Never tell the user that a persistent profile was destroyed during teardown.

If an unrelated browser tool unexpectedly reaches a human gate, preserve it if that tool already offers native user takeover. Otherwise restart the still-reversible flow in Human Handoff. Never replay a purchase or account submission whose outcome is uncertain.

## Automate through the managed session

Use the packaged Playwright client through `run`; it injects the selected session's loopback endpoint as `HANDOFF_CDP_URL`. Keep browser data out of model-visible logs. A task script:

```python
import os
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp(os.environ["HANDOFF_CDP_URL"])
    page = browser.contexts[0].pages[0]
    # Navigate and fill only fields authorized by the user.
```

Run it with `hermes-human-handoff run <session_id> task.py`. Let the context manager disconnect the automation client; do not call `browser.close()`, because the handoff worker owns the shared browser.

Use exact selectors and sanitized exceptions around passwords or other secrets. Do not print form values, cookies, signed URLs, or page dumps containing personal information.

## Human gate

1. Stop automation on the sensitive page. Keep the browser and worker alive.
2. Send the single current `handoff_url` and one concrete instruction, such as “Complete the CAPTCHA, submit the form, then press Done.”
3. During takeover, make no screenshot, DOM, accessibility-tree, OCR, console, trace, recording, or browser-history calls against the session.
4. The user performs the CAPTCHA, OTP, card entry, consent, identity action, or final approval and submits it.
5. Tell the user to press the visible **Done** button. The button appears only after the authenticated VNC connection is established; it sends a session-scoped completion signal in a header, never in the URL.
6. Wait without inspecting the browser:

```bash
hermes-human-handoff wait <session_id> --timeout <ttl-seconds>
```

Resume only when this returns `completion: done`. Do not resume based only on a chat reply.

For external legal or financial effects, the user's takeover and click are the approval gate. A staged order, snapshot, or previous credential is never approval.

## Resume and verify

After the user reports completion:

1. Inspect only the resulting page/state after raw sensitive fields are gone.
2. Verify authoritative provider state: authenticated product UI, account settings, subscription/receipt, order history, email verification, or an official API read.
3. If submission may have happened but confirmation is unclear, record the run as `submitted_unconfirmed`, meaning “the side effect may have occurred and must not be replayed yet.” Read account/order/payment history before any retry.
4. Continue the authorized non-sensitive workflow and promote durable credentials through a user-approved password or secrets manager.
5. Stop the handoff:

```bash
hermes-human-handoff stop <session_id>
```

6. Read the returned status. Completion requires `stopped` or `expired`, no `cleanup_error`, the capability file removed, the temporary Serve port absent, and any disposable profile removed. For an explicitly selected persistent profile, completion requires the profile to remain owner-only and unlocked rather than removed.

## Payment privacy contract

For card entry or similarly sensitive forms:

- the user types and submits the value;
- the agent pauses all model-visible browser inspection while the value is present;
- the agent resumes after the page advances and never navigates back to recover, inspect, or re-expose the value;
- no screenshot, trace, recording, clipboard bridge, DOM dump, or console output may contain the value;
- the session is retired immediately after receipt/provider verification.

This keeps the value out of normal model/chat context under a threat model that trusts the agent host, model provider, VPS provider, Tailscale account, browser binary, and target merchant/provider. It is not a cryptographic boundary against a malicious host administrator.

## Direct commands

```bash
hermes-human-handoff status                 # list sanitized sessions
hermes-human-handoff status <session_id>    # one sanitized session
hermes-human-handoff url <session_id>       # explicitly reprint active capability URL
hermes-human-handoff run <session_id> task.py # run Playwright against that browser
hermes-human-handoff stop <session_id>      # tear down now
hermes-human-handoff cleanup                # request cleanup of elapsed sessions
```

Avoid repeatedly printing the capability URL. Send the newest active link once; retire older or failed sessions before creating another.

## Common pitfalls

1. **Starting in an ordinary headless browser.** The CAPTCHA session cannot be magically transferred. Begin likely signup/payment flows inside the managed browser.
2. **Treating Tailnet as the only secret.** The package also uses an expiring random VNC capability in the URL fragment and VNC authentication.
3. **Inspecting during card or OTP entry.** Pause every model-visible browser read until the user has submitted and the page has advanced.
4. **Retrying uncertain submissions.** Read provider history first; duplicate charges and duplicate accounts are worse than a delayed status check.
5. **Leaving the bridge running.** Stop after provider readback. TTL is a backstop, not the normal completion path.
6. **Using it for durable API secrets.** Route those through one-time secret intake and a secret manager.
7. **Claiming local-device factors will work remotely.** Camera, NFC, USB keys, and some passkeys need a local-browser handoff.

## Verification checklist

- [ ] `doctor` returned ready before the flow.
- [ ] The managed browser was used from the start of the stateful workflow.
- [ ] The handoff link was Tailnet-only, current, and sent once with one action.
- [ ] Automation and model-visible inspection paused during sensitive entry.
- [ ] Human completion was followed by authoritative provider readback.
- [ ] Uncertain submissions were checked before any retry.
- [ ] Durable credentials landed in the correct vault surface.
- [ ] Teardown returned cleanly; the temporary route and capability were gone; disposable profiles were removed; any explicitly persistent profile remained private and unlocked.
