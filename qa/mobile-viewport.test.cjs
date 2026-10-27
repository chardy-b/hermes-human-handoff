'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(process.env.HUMAN_HANDOFF_PLAYWRIGHT_MODULE || 'playwright');
const assets = path.resolve(__dirname, '../src/hermes_human_handoff/assets');
const novnc = process.env.HUMAN_HANDOFF_NOVNC_DIR;
assert.ok(novnc, 'Set HUMAN_HANDOFF_NOVNC_DIR to local noVNC v1.7.0');

// Real noVNC renderer, coordinate conversion and input handlers. Only the
// transport/handshake is replaced; no VNC server, credentials or network peers.
// Private _resize/_display/_canvas fields below are intentionally tied to pinned
// noVNC v1.7.0. Production uses public APIs; this fixture exercises its renderer
// and wire encoder rather than reimplementing either.
const fixture = `import RFB from '/novnc/core/rfb.js';
export default class LocalRFB extends RFB {
  constructor(screen) {
    const packets = [];
    const channel = { readyState: 'open', protocol: '', binaryType: 'arraybuffer',
      onerror: null, onmessage: null, onopen: null,
      send(bytes) { packets.push(Array.from(bytes)); }, close() {} };
    super(screen, channel);
    window.fixture = { rfb: this, packets };
    setTimeout(() => {
      this._resize(1280, 800);
      const ctx = this._display._drawCtx;
      ctx.fillStyle = '#e9edf5'; ctx.fillRect(0, 0, 1280, 800);
      ctx.fillStyle = '#182238'; ctx.font = '24px sans-serif';
      for (let y = 40; y < 800; y += 80) ctx.fillText('Local dummy remote browser — readable native text', 30, y);
      this._updateConnectionState('connected');
    }, 0);
  }
}`;

test('local mobile layouts and real RFB interactions', async t => {
  let doneOK = false;
  const requests = [];
  const capability = 'Test1234';
  const server = http.createServer((req, res) => {
    requests.push({ url: req.url, headers: { ...req.headers } });
    const url = new URL(req.url, 'http://localhost');
    if (url.pathname === '/handoff-done') {
      res.statusCode = req.method !== 'POST' || req.headers['x-handoff-capability'] !== capability
        ? 403 : doneOK ? 200 : 503;
      res.end(); return;
    }
    if (url.pathname === '/referrer-check') { res.end('local target'); return; }
    res.setHeader('Content-Type', url.pathname.endsWith('.html') ? 'text/html' : 'text/javascript');
    if (url.pathname === '/core/rfb.js') { res.end(fixture); return; }
    const relative = url.pathname.startsWith('/novnc/') ? url.pathname.slice(7) : url.pathname.slice(1);
    const root = url.pathname.startsWith('/novnc/') || url.pathname.startsWith('/core/') ? novnc : assets;
    const file = path.resolve(root, relative);
    if (!file.startsWith(path.resolve(root) + '/') || !fs.existsSync(file)) { res.statusCode = 404; res.end(); return; }
    res.end(fs.readFileSync(file));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({ headless: true });
  try {
    for (const [width, height, mobile] of [[320,568,true], [390,844,true], [430,932,true], [844,390,true], [568,320,true], [1440,900,false]]) {
      await t.test(`${width}x${height} ${mobile ? 'touch' : 'desktop'}`, async () => {
        const context = await browser.newContext({ viewport: { width, height }, isMobile: mobile, hasTouch: mobile });
        const page = await context.newPage();
        const errors = [];
        page.on('pageerror', e => errors.push(e.message));
        await page.goto(`http://127.0.0.1:${server.address().port}/handoff.html?v=local#Test1234`);
        await page.waitForFunction(() => document.querySelector('#status').textContent === 'You have control');
        assert.equal(await page.evaluate(() => fixture.rfb.scaleViewport), !mobile);
        const layout = async () => page.evaluate(() => {
          const box = id => { const b = document.getElementById(id).getBoundingClientRect(); return { x:b.x, y:b.y, width:b.width, height:b.height, bottom:b.bottom, right:b.right }; };
          return { screen:box('screen'), dock:box('controls'), buttons:['fit','native','pointer','keyboard','done'].map(box), width:innerWidth, height:innerHeight };
        });
        // Wait for noVNC's ResizeObserver to resize the actual canvas, then
        // check CSS/backing dimensions and independently predict wire coordinates.
        const checkCanvas = async native => {
          await page.waitForFunction(native => {
            const c = document.querySelector('#screen canvas');
            const host = document.querySelector('#screen').getBoundingClientRect();
            const b = c.getBoundingClientRect();
            const scale = Math.min(host.width / 1280, host.height / 800);
            return Math.abs(b.width - (native ? Math.min(1280, host.width) : 1280 * scale)) < 2 &&
              Math.abs(b.height - (native ? Math.min(800, host.height) : 800 * scale)) < 2;
          }, native);
          const g = await page.evaluate(() => {
            const r = fixture.rfb, c = r._canvas, b = c.getBoundingClientRect();
            return { x:b.x, y:b.y, width:b.width, height:b.height,
              pixelsX:c.width, pixelsY:c.height, panX:r._display._viewportLoc.x,
              panY:r._display._viewportLoc.y };
          });
          if (native) {
            assert.ok(Math.abs(g.width - g.pixelsX) < 1);
            assert.ok(Math.abs(g.height - g.pixelsY) < 1);
          } else {
            assert.equal(g.pixelsX, 1280);
            assert.equal(g.pixelsY, 800);
          }
          const x = Math.floor(g.x + g.width * .4), y = Math.floor(g.y + g.height * .4);
          const expected = [Math.floor((x-g.x) * g.pixelsX/g.width + g.panX),
            Math.floor((y-g.y) * g.pixelsY/g.height + g.panY)];
          await page.evaluate(() => fixture.packets.length = 0);
          await page.mouse.click(x, y);
          const actual = await page.evaluate(() => {
            const p = fixture.packets.find(p => p[0] === 5 && p[1] === 1);
            return p && [(p[2]<<8)|p[3], (p[4]<<8)|p[5]];
          });
          assert.ok(actual && actual.every((v,i) => Math.abs(v-expected[i]) <= 1),
            `canvas click coordinates: ${actual} vs ${expected}`);
        };
        await checkCanvas(mobile);
        assert.equal(await page.locator(mobile ? '#native' : '#fit').getAttribute('aria-pressed'), 'true');
        assert.equal(await page.locator('#native').textContent(), '1:1 view');
        assert.equal(await page.locator('#fit').textContent(), 'Fit view');
        assert.equal(await page.locator('#pointer').textContent(), mobile ? 'Pan' : 'Click');
        assert.equal(await page.locator('#pointer').getAttribute('aria-label'),
          `Current interaction: ${mobile ? 'Pan' : 'Click'}`);
        assert.ok(await page.locator('#mode-feedback').textContent().then(s => s.includes(mobile ? 'Pan: drag' : 'Click: tap')));
        if (mobile) {
          assert.equal(await page.locator('#controls').evaluate(el => getComputedStyle(el).paddingTop), '8px',
            'mobile dock overrides desktop top safe-area padding');
        }
        assert.ok(await page.locator('#view-controls button').evaluateAll(buttons =>
          buttons.every(b => b.scrollWidth <= b.clientWidth)), 'labels fit without horizontal clipping');
        let boxes = await layout();
        assert.ok(boxes.screen.height > height * .45, JSON.stringify(boxes));
        for (const b of boxes.buttons) {
          assert.ok(b.height >= (mobile ? 48 : 44));
          assert.ok(b.width >= 48);
          assert.ok(b.x >= 0 && b.right <= width && b.bottom <= height);
        }
        assert.ok(mobile ? boxes.dock.y >= boxes.screen.bottom - 1 : boxes.dock.bottom <= boxes.screen.y + 1);
        await page.locator('#native').click();
        await checkCanvas(true);
        assert.deepEqual(await page.evaluate(() => { const r = fixture.rfb; return [r.scaleViewport,r.clipViewport,r.resizeSession,r.dragViewport,r._display.scale]; }), [false,true,false,true,1]);
        const canvas = page.locator('#screen canvas');
        const b = await canvas.boundingBox();
        const x = b.x + Math.min(b.width - 20, 200), y = b.y + Math.min(b.height - 20, 140);
        await page.evaluate(() => fixture.packets.length = 0);
        // Real browser mouse events go through noVNC's installed handlers.
        await page.mouse.move(x,y); await page.mouse.down(); await page.mouse.move(x-80,y-60,{steps:5}); await page.mouse.up();
        if (mobile) assert.ok(await page.evaluate(() => fixture.rfb._display._viewportLoc.x > 0));
        assert.equal(await page.evaluate(() => fixture.packets.some(p => p[0] === 5 && p[1] !== 0)), false, 'pan drag never presses remote button');
        if (mobile) {
          // Touch recognizer must also pan, while a tap retains noVNC click semantics.
          const cdp = await context.newCDPSession(page);
          const before = await page.evaluate(() => fixture.rfb._display._viewportLoc.x);
          await cdp.send('Input.dispatchTouchEvent', { type:'touchStart', touchPoints:[{x,y}] });
          for (let d=10; d<=60; d+=10) await cdp.send('Input.dispatchTouchEvent', { type:'touchMove', touchPoints:[{x:x-d,y}] });
          await cdp.send('Input.dispatchTouchEvent', { type:'touchEnd', touchPoints:[] });
          assert.ok(await page.evaluate(() => fixture.rfb._display._viewportLoc.x) > before);
          await page.touchscreen.tap(x,y);
          assert.ok(await page.evaluate(() => fixture.packets.some(p => p[0] === 5 && p[1] === 1)));
        }
        await page.locator('#pointer').click();
        assert.equal(await page.evaluate(() => fixture.rfb.dragViewport), false);
        assert.equal(await page.locator('#pointer').textContent(), 'Click');
        assert.equal(await page.locator('#native').textContent(), '1:1 view');
        await page.evaluate(() => fixture.packets.length = 0);
        const expected = await page.evaluate(({x,y}) => {
          const r = fixture.rfb, b = r._canvas.getBoundingClientRect();
          return [Math.floor(x-b.x+r._display._viewportLoc.x), Math.floor(y-b.y+r._display._viewportLoc.y)];
        }, {x,y});
        await page.mouse.click(x,y);
        const actual = await page.evaluate(() => {
          const p = fixture.packets.find(p => p[0] === 5 && p[1] === 1);
          return [(p[2]<<8)|p[3], (p[4]<<8)|p[5]];
        });
        // Touch emulation can round fractional CSS coordinates by one pixel.
        assert.ok(actual.every((v,i) => Math.abs(v-expected[i]) <= 1),
          `remote click coordinates include pan offset: ${actual} vs ${expected}`);
        assert.ok(await page.evaluate(() => fixture.packets.some(p => p[0] === 5 && p[1] === 1)));
        await page.locator('#fit').click();
        assert.deepEqual(await page.evaluate(() => { const r = fixture.rfb; return [r.scaleViewport,r.clipViewport,r.resizeSession,r.dragViewport]; }), [true,false,true,false]);
        await checkCanvas(false);
        assert.equal(await page.locator('#pointer').textContent(), 'Click');
        if (width === 390) {
          await page.locator('#native').click();
          await page.setViewportSize({ width:844, height:390 });
          await page.waitForFunction(() => document.querySelector('#controls').getBoundingClientRect().bottom <= 390);
          assert.equal(await page.evaluate(() => fixture.rfb.scaleViewport), false, 'rotation preserves selected mode');
          await checkCanvas(true);
          await page.locator('#fit').click();
          await checkCanvas(false);
          await page.setViewportSize({ width:390, height:844 });
          await checkCanvas(false);
          await page.locator('#native').click();
          await checkCanvas(true);
        }
        await page.locator('#keyboard').click();
        await page.keyboard.insertText('dummy');
        assert.equal(await page.locator('#mobile-input').inputValue(), '\u200b');
        assert.ok(await page.evaluate(() => fixture.packets.some(p => p[0] === 4)), 'keyboard reaches real RFB key encoder');
        assert.equal(await page.evaluate(() => localStorage.length + sessionStorage.length), 0);
        if (width === 390) {
          // Headless browsers have no native keyboard. Simulate its VisualViewport
          // resize/scroll contract explicitly, including an iOS-style top offset.
          await page.evaluate(() => {
            const vv = new EventTarget();
            Object.assign(vv, { width:390, height:330, offsetLeft:0, offsetTop:45 });
            const original = window.visualViewport;
            Object.defineProperty(window, 'visualViewport', { value:vv });
            original.dispatchEvent(new Event('resize'));
          });
          boxes = await layout();
          assert.ok(boxes.dock.bottom <= 375 && boxes.screen.y === 45);
          assert.ok(boxes.screen.height > 100);
          assert.equal(await page.evaluate(() => fixture.rfb.resizeSession), false);
          await checkCanvas(true);
          await page.locator('#fit').click();
          await checkCanvas(false);
        }
        assert.equal(new URL(page.url()).hash, '');
        await page.evaluate(() => fetch('/referrer-check'));
        doneOK = true;
        for (const headers of [{}, { 'X-Handoff-Capability':'Wrong123' }]) {
          assert.equal(await page.evaluate(async headers =>
            (await fetch('/handoff-done', { method:'POST', headers })).status, headers), 403);
        }
        doneOK = false;
        await page.locator('#done').click();
        await page.waitForFunction(() => document.querySelector('#status').textContent.includes('try again'));
        assert.equal(await page.locator('#native').isEnabled(), true);
        assert.equal(requests.at(-1).headers['x-handoff-capability'], capability);
        doneOK = true;
        await page.locator('#done').click();
        await page.waitForFunction(() => document.querySelector('#status').textContent === 'Control returned to agent');
        assert.equal(await page.locator('#mobile-input').inputValue(), '');
        assert.equal(await page.locator('#native').isEnabled(), false);
        assert.equal(requests.at(-1).headers['x-handoff-capability'], capability);
        const referrer = requests.find(r => r.url === '/referrer-check');
        assert.ok(referrer.headers.referer.includes('/handoff.html?v=local'));
        for (const request of requests) {
          assert.ok(!/Test1234|Wrong123|#/.test(request.url));
          for (const [name, value] of Object.entries(request.headers)) {
            if (name === 'x-handoff-capability' && request.url === '/handoff-done') continue;
            assert.ok(!/Test1234|Wrong123/.test(value), `capability leaked in ${name}`);
          }
        }
        assert.deepEqual(errors, []);
        await context.close();
      });
    }
  } finally { await browser.close(); await new Promise(resolve => server.close(resolve)); }
});
