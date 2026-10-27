#!/usr/bin/env node
'use strict';

const modulePath = process.env.HUMAN_HANDOFF_PLAYWRIGHT_MODULE || 'playwright';
const { chromium } = require(modulePath);

(async () => {
  const url = process.argv[2];
  if (!url) throw new Error('usage: browser_probe.cjs HANDOFF_URL');
  if (!/^[a-f0-9]{32}$/.test(new URL(url).searchParams.get('v') || '')) {
    throw new Error('handoff URL is missing its cache nonce');
  }
  const launchArgs = [];
  const resolveIp = process.env.HUMAN_HANDOFF_RESOLVE_IP;
  if (resolveIp) {
    const hostname = new URL(url).hostname;
    launchArgs.push(`--host-resolver-rules=MAP ${hostname} ${resolveIp}`);
  }
  const browser = await chromium.launch({ headless: true, args: launchArgs });
  try {
    const page = await browser.newPage({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
    let capabilityLeaked = false;
    const capability = new URL(url).hash.slice(1);
    page.on('request', request => {
      if ((request.url() + JSON.stringify(request.headers())).includes(capability)) capabilityLeaked = true;
    });
    page.on('websocket', socket => {
      if (socket.url().includes(capability)) capabilityLeaked = true;
    });
    await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 20000 });
    await page.waitForFunction(
      () => document.querySelector('#status')?.textContent === 'You have control',
      null,
      { timeout: 20000 }
    );
    const canvas = await page.locator('#screen canvas').count();
    if (canvas < 1) throw new Error('noVNC connected without rendering a canvas');
    if (!await page.locator('#keyboard').isVisible() || !await page.locator('#keyboard').isEnabled()) {
      throw new Error('mobile Keyboard control unavailable');
    }
    await page.locator('#keyboard').click();
    if (!await page.evaluate(() => document.activeElement.id === 'mobile-input')) {
      throw new Error('Keyboard did not focus mobile input');
    }
    if (capabilityLeaked) throw new Error('capability leaked in request metadata');
    const wrongUrl = new URL(url);
    wrongUrl.hash = 'Wrong123';
    const wrong = await browser.newPage({ viewport: { width: 1000, height: 800 } });
    await wrong.goto(wrongUrl.toString(), { waitUntil: 'domcontentloaded', timeout: 20000 });
    await wrong.waitForFunction(
      () => ['Handoff authorization failed', 'Connection lost'].includes(document.querySelector('#status')?.textContent),
      null,
      { timeout: 20000 }
    );
    if ((await wrong.locator('#status').textContent()) === 'You have control') {
      throw new Error('wrong capability unexpectedly authenticated');
    }
    console.log('noVNC browser connected; mobile Keyboard focused; request metadata clean; wrong capability refused');
  } finally {
    await browser.close();
  }
})().catch(error => {
  // Browser errors can embed the full URL, including its secret fragment.
  console.error('noVNC browser probe failed (' + error.name + '); URL details suppressed');
  process.exit(1);
});
