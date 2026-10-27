'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const { chromium } = require(process.env.HUMAN_HANDOFF_PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..');
const assets = path.join(root, 'src/hermes_human_handoff/assets');

// Exercise production URL generation without starting services or publishing routes.
function links(base) {
  return JSON.parse(execFileSync(process.env.PYTHON || 'python3', ['-c', `
import json, sys
sys.path.insert(0, 'src')
from hermes_human_handoff.cli import build_handoff_url
print(json.dumps([build_handoff_url(sys.argv[1], 'Test1234') for _ in range(2)]))
`, base], { cwd: root, encoding: 'utf8' }));
}

test('fresh session bypasses cached old HTML; capability stays out of HTTP and referrers', async () => {
  const requests = [];
  let latest = false;
  // Real HTTP caching is essential here: Playwright routing disables the cache.
  const server = http.createServer((req, res) => {
    requests.push({ url: req.url, headers: req.rawHeaders.join('\n') });
    const pathname = new URL(req.url, 'http://localhost').pathname;
    res.setHeader('Cache-Control', 'public, max-age=86400');
    if (pathname === '/handoff.html') {
      res.setHeader('Content-Type', 'text/html');
      res.end(latest ? fs.readFileSync(path.join(assets, 'handoff.html')) :
        '<!doctype html><title>Old handoff</title><p id="old">Cached page without Keyboard</p>');
    } else if (pathname === '/mobile-keyboard.js') {
      res.setHeader('Content-Type', 'text/javascript');
      res.end(fs.readFileSync(path.join(assets, 'mobile-keyboard.js')));
    } else if (pathname === '/core/rfb.js') {
      // Only the VNC transport is stubbed; the shipped HTML/keyboard code runs.
      res.setHeader('Content-Type', 'text/javascript');
      res.end(`export default class RFB extends EventTarget {
        constructor(screen, url, options) {
          super();
          window.testTransport = { url, password: options.credentials.password };
          setTimeout(() => this.dispatchEvent(new Event('connect')), 0);
        }
        sendKey() {} disconnect() {} sendCredentials() {}
      }`);
    } else if (pathname === '/core/util/logging.js') {
      res.setHeader('Content-Type', 'text/javascript');
      res.end('export function initLogging() {}');
    } else {
      res.end('local referrer target');
    }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  let browser;
  try {
    const base = `http://127.0.0.1:${server.address().port}`;
    const [oldLink, freshLink] = links(base);
    assert.notEqual(oldLink.split('#')[0], freshLink.split('#')[0]);
    browser = await chromium.launch({ headless: true });
    const context = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
    const page = await context.newPage();
    await page.goto(oldLink);
    assert.equal(await page.locator('#old').count(), 1);
    latest = true;
    await page.goto('about:blank');
    await page.goto(oldLink.replace('#Test1234', '#Other123'));
    assert.equal(await page.locator('#old').count(), 1, 'fragment-only change still uses old cached HTML');
    assert.equal(requests.filter(r => r.url.startsWith('/handoff.html')).length, 1);
    await page.goto('about:blank');
    await page.goto(freshLink);
    await page.waitForFunction(() => document.querySelector('#status')?.textContent === 'You have control');
    assert.equal(await page.locator('#old').count(), 0);
    assert.equal(await page.locator('#keyboard').isVisible(), true);
    assert.equal(await page.locator('#keyboard').isEnabled(), true);
    assert.equal(await page.locator('#done').isVisible(), true);
    await page.locator('#keyboard').click();
    assert.equal(await page.evaluate(() => document.activeElement.id), 'mobile-input');
    assert.equal(await page.evaluate(() => location.hash), '');
    assert.equal(new URL(page.url()).search, new URL(freshLink).search);
    assert.equal(await page.evaluate(() => window.testTransport.password === 'Test1234'), true);
    assert.equal(await page.evaluate(() => window.testTransport.url.endsWith('/websockify')), true);
    await page.evaluate(() => fetch('/referrer-check'));
    const referrer = requests.find(r => r.url === '/referrer-check');
    assert.ok(referrer.headers.includes(new URL(freshLink).search), 'exercise a real same-origin referrer');
    assert.equal(requests.filter(r => r.url.startsWith('/handoff.html')).length, 2);
    for (const request of requests) {
      assert.ok(!/Test1234|Other123/.test(request.url + request.headers), 'HTTP request must not contain a capability');
      assert.ok(!request.url.includes('#'), 'fragment must not reach HTTP server');
    }
  } finally {
    if (browser) await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
});
