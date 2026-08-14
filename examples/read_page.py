#!/usr/bin/env python3
"""Read non-sensitive state from a managed Human Handoff browser."""

import json
import os

from playwright.sync_api import sync_playwright

with sync_playwright() as playwright:
    browser = playwright.chromium.connect_over_cdp(os.environ["HANDOFF_CDP_URL"])
    context = browser.contexts[0]
    page = context.pages[0]
    print(json.dumps({"title": page.title(), "url": page.url}))
    # Exiting disconnects this client. Do not call browser.close(); the handoff
    # worker owns the browser and keeps it alive between automation steps.
