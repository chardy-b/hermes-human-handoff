#!/usr/bin/env node
'use strict';

const modulePath = process.env.HUMAN_HANDOFF_PLAYWRIGHT_MODULE || 'playwright';
const { chromium } = require(modulePath);

(async () => {
  const url = process.argv[2];
  if (!url) throw new Error('usage: browser_probe.cjs HANDOFF_URL');
  const launchArgs = [];
  const resolveIp = process.env.HUMAN_HANDOFF_RESOLVE_IP;
  if (resolveIp) {
    const hostname = new URL(url).hostname;
    launchArgs.push(`--host-resolver-rules=MAP ${hostname} ${resolveIp}`);
  }
  const browser = await chromium.launch({ headless: true, args: launchArgs });
  try {
    const page = await browser.newPage({ viewport: { width: 1000, height: 800 } });
    await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 20000 });
    await page.waitForFunction(
      () => document.querySelector('#status')?.textContent === 'You have control',
      null,
      { timeout: 20000 }
    );
    const canvas = await page.locator('#screen canvas').count();
    if (canvas < 1) throw new Error('noVNC connected without rendering a canvas');
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
    console.log('noVNC browser connected; wrong capability refused');
  } finally {
    await browser.close();
  }
})().catch(error => {
  console.error(error.stack || String(error));
  process.exit(1);
});
