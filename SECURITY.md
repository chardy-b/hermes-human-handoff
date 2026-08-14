# Security model

Human Handoff preserves an isolated browser session and lets a human operate it through an expiring noVNC link available only on the host's Tailscale network.

## Intended boundary

- VNC, WebSocket, browser debugging, and helper listeners bind to loopback.
- Tailscale Serve publishes one temporary HTTPS port. The CLI refuses candidate ports already marked for Funnel, verifies again before activation, and rolls back if the resulting route reports Funnel access.
- An eight-character random VNC capability is carried in the URL fragment, so it is absent from HTTP request lines and ordinary access logs.
- Each handoff gets a new display, capability, and ports. The default mode creates a new empty browser profile; template-clone mode copies an owner-only source profile into a new disposable profile.
- The worker removes its Serve mapping, processes, capability file, startup URL/config, and disposable browser profile at stop or expiry. An explicitly selected `--persistent-profile` is used in place and intentionally remains on disk with its cookies, address data, and other browser state. Treat that directory as durable sensitive data; the package disables browser password management and card storage but does not erase the persistent profile. Clipboard transfer is disabled rather than bridged. New sessions recover stale state first, and the installer enables a five-minute cleanup timer when user systemd is available. Operators who require recovery after logout must also enable user lingering for the account.
- The agent should pause model-visible browser inspection during sensitive entry and resume after the human has submitted the form.

The runtime prefers system Chrome/Chromium. Playwright-downloaded Chromium is launched with `--no-sandbox` because that build commonly lacks a usable Linux system/AppArmor sandbox; this is acceptable only when the endpoint host and visited service are inside the accepted threat model. The loopback Chrome DevTools endpoint has no independent authentication and is protected by the trusted operating-system-account boundary.

## Accepted threat model

This protects card details, passwords, OTPs, identity fields, and similar values from normal chat/model context when the agent follows the supplied skill. It assumes the endpoint host, operating-system account, model provider, VPS provider, Tailscale account, browser binary, and target service are trusted.

It does not make data cryptographically inaccessible to a malicious host administrator or compromised browser. Use a local-browser or separately trusted payment/identity broker when that stronger boundary is required.

## Reporting vulnerabilities

Open a private security advisory at https://github.com/humanitylabs-org/hermes-human-handoff/security/advisories/new. Do not include live credentials, Tailnet hostnames, handoff URLs, browser profiles, or payment information in an issue.
