#!/usr/bin/env node
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assets = path.join(__dirname, '../src/hermes_human_handoff/assets');
const bridge = import('data:text/javascript;base64,' + Buffer.from(
  fs.readFileSync(path.join(assets, 'mobile-keyboard.js'), 'utf8')).toString('base64'));

class Element {
  constructor() {
    this.listeners = {};
    this.value = '';
    this.disabled = false;
    this.style = {};
    const classes = new Set();
    this.classList = { add: x => classes.add(x), remove: x => classes.delete(x), contains: x => classes.has(x) };
  }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  emit(type, data = {}) {
    const event = { type, prevented: false, stopped: false,
      preventDefault() { this.prevented = true; },
      stopPropagation() { this.stopped = true; }, ...data };
    event.results = (this.listeners[type] || []).map(fn => fn(event));
    return event;
  }
  focus() { this.focused = true; }
  blur() { this.focused = false; this.emit('blur'); }
  setSelectionRange(start, end) { this.selection = [start, end]; }
}
async function setup() {
  const input = new Element();
  const keys = [];
  const api = (await bridge).createMobileKeyboard(input, (...args) => keys.push(args));
  api.enable();
  return { input, keys, api };
}
function insert(input, data, inputType = 'insertText', extra = {}) {
  input.value = '\u200b' + (data || '');
  input.emit('input', { data, inputType, ...extra });
}

test('Unicode code points, Latin-1, emoji and multiline input use sendKey without physical codes', async () => {
  const { input, keys } = await setup();
  insert(input, 'Abé中😀\r\nZ');
  assert.deepEqual(keys, [65, 98, 233, 0x01004e2d, 0x0101f600, 0xff0d, 90].map(k => [k]));
  assert.equal(input.value, '\u200b');
  assert.deepEqual(input.selection, [1, 1]);
});

test('keydown/beforeinput/input/keyup sends insertion, Backspace and Enter only once', async () => {
  const { input, keys } = await setup();
  for (const [key, type, data] of [['a', 'insertText', 'a'], ['Backspace', 'deleteContentBackward', null], ['Enter', 'insertLineBreak', null]]) {
    const down = input.emit('keydown', { key });
    assert.equal(down.stopped, true);
    assert.equal(down.prevented, false); // Native editing still runs.
    input.emit('beforeinput', { inputType: type, data });
    insert(input, data, type);
    input.emit('keyup', { key });
  }
  insert(input, null, 'deleteContentBackward');
  assert.deepEqual(keys, [[97], [0xff08], [0xff0d], [0xff08]]);
});

test('input without data uses transient textarea content', async () => {
  const { input, keys } = await setup();
  input.value = '\u200btest';
  input.emit('input', { inputType: 'insertText', data: null });
  assert.deepEqual(keys, [...'test'].map(c => [c.codePointAt(0)]));
});

for (const order of ['before-end', 'after-end']) {
  for (const finalType of ['insertText', 'insertCompositionText', 'insertFromComposition']) {
    test(`IME ${finalType} final input ${order} commits once`, async () => {
      const { input, keys } = await setup();
      input.emit('compositionstart');
      input.emit('keydown', { keyCode: 229 });
      insert(input, 'n', 'insertCompositionText', { isComposing: true });
      insert(input, 'ni', 'insertCompositionText', { isComposing: true });
      assert.deepEqual(keys, []);
      if (order === 'before-end') insert(input, '你', finalType);
      input.emit('compositionend', { data: '你' });
      if (order === 'after-end') insert(input, '你', finalType);
      assert.deepEqual(keys, [[0x01004f60]]);
      assert.equal(input.value, '\u200b');
      await new Promise(resolve => setTimeout(resolve, 5));
      insert(input, '你'); // A later identical character must not be swallowed.
      assert.deepEqual(keys, [[0x01004f60], [0x01004f60]]);
    });
  }
}

test('cancelled IME, blur and disable erase uncommitted input', async () => {
  const { input, keys, api } = await setup();
  input.emit('compositionstart');
  insert(input, 'draft', 'insertCompositionText', { isComposing: true });
  input.emit('compositionend', { data: '' });
  assert.deepEqual(keys, []);
  input.emit('compositionstart');
  input.value = 'draft';
  input.blur();
  input.emit('compositionend', { data: 'draft' });
  assert.deepEqual(keys, []);
  input.emit('compositionstart');
  input.value = 'draft';
  api.disable();
  input.emit('compositionend', { data: 'draft' });
  insert(input, 'late');
  api.focus();
  assert.equal(input.value, '');
  assert.equal(input.disabled, true);
  assert.equal(input.focused, false);
  assert.deepEqual(keys, []);
});

test('clipboard and drop are blocked, unknown input does not forward text', async () => {
  const { input, keys } = await setup();
  for (const type of ['paste', 'copy', 'cut', 'drop']) assert.equal(input.emit(type).prevented, true);
  for (const inputType of ['insertFromPaste', 'insertFromDrop', 'deleteByCut']) {
    assert.equal(input.emit('beforeinput', { inputType }).prevented, true);
    insert(input, 'dummy', inputType);
  }
  assert.deepEqual(keys, []);
  assert.equal(input.value, '\u200b');
});

async function page(fetch = async () => ({ ok: true }), hash = '#Ab12Cd34') {
  const elements = Object.fromEntries(['status', 'done', 'screen', 'keyboard', 'viewport', 'mobile-input'].map(id => [id, new Element()]));
  // Reflect initial HTML disabled attributes in this small DOM test double.
  elements.keyboard.disabled = elements['mobile-input'].disabled = true;
  const window = new Element();
  window.visualViewport = Object.assign(new Element(), { width: 390, height: 700, offsetLeft: 0, offsetTop: 0 });
  let rfb;
  let logging;
  const source = fs.readFileSync(path.join(assets, 'handoff.html'), 'utf8').match(/<script type="module">([\s\S]*?)<\/script>/)[1].replace(/^\s*import .*;$/gm, '');
  const context = {
    ...await bridge, document: { getElementById: id => elements[id] }, window,
    location: { hash, pathname: '/handoff.html', search: '', protocol: 'https:', host: 'example.invalid' },
    history: { replaceState() {} }, fetch,
    initLogging(level) { logging = level; },
    RFB: class extends Element {
      constructor() { super(); rfb = this; this.keys = []; }
      sendKey(...args) { this.keys.push(args); }
      disconnect() { this.emit('disconnect', { detail: { clean: true } }); }
    },
  };
  vm.runInNewContext(source, context);
  assert.equal(logging, 'none');
  return { elements, window, rfb };
}

test('page enables Keyboard only on connect, focuses synchronously and leaves canvas handlers alone', async () => {
  const { elements: e, rfb } = await page();
  e.keyboard.emit('click');
  assert.equal(e['mobile-input'].focused, undefined);
  rfb.emit('connect');
  assert.equal(e.keyboard.disabled, false);
  assert.equal(e.keyboard.classList.contains('visible'), true);
  e.keyboard.emit('click');
  assert.equal(e['mobile-input'].focused, true);
  assert.deepEqual(e.screen.listeners, {});
  assert.equal(rfb.viewOnly, false);
  insert(e['mobile-input'], 'dummy');
  assert.equal(rfb.keys.length, 5);
});

for (const ending of ['disconnect', 'securityfailure', 'pagehide', 'done']) {
  test(`page clears and disables input on ${ending}`, async () => {
    const { elements: e, window, rfb } = await page();
    rfb.emit('connect');
    e.keyboard.emit('click');
    e['mobile-input'].emit('compositionstart');
    e['mobile-input'].value = 'draft';
    if (ending === 'done') await Promise.all(e.done.emit('click').results);
    else if (ending === 'pagehide') window.emit('pagehide');
    else rfb.emit(ending, { detail: { clean: false } });
    assert.equal(e['mobile-input'].disabled, true);
    assert.equal(e['mobile-input'].value, '');
    assert.equal(e.keyboard.disabled, true);
    assert.equal(e.done.classList.contains('visible'), false);
    e['mobile-input'].emit('compositionend', { data: 'draft' });
    insert(e['mobile-input'], 'late');
    assert.deepEqual(rfb.keys, []);
  });
}

test('failed Done permits retry only while still connected', async () => {
  let reject;
  const { elements: e, rfb } = await page(() => new Promise((_, no) => { reject = no; }));
  rfb.emit('connect');
  let pending = e.done.emit('click').results;
  assert.equal(e['mobile-input'].disabled, true);
  assert.equal(rfb.viewOnly, true);
  reject(new Error('dummy failure'));
  await Promise.all(pending);
  assert.equal(e.keyboard.disabled, false);
  assert.equal(rfb.viewOnly, false);
  pending = e.done.emit('click').results;
  rfb.emit('securityfailure');
  reject(new Error('dummy failure'));
  await Promise.all(pending);
  assert.equal(e.keyboard.disabled, true);
  assert.equal(e['mobile-input'].value, '');
});

test('visualViewport resize and scroll keep frame within visible area', async () => {
  const { elements: e, window } = await page();
  Object.assign(window.visualViewport, { width: 320, height: 260, offsetLeft: 4, offsetTop: 120 });
  window.visualViewport.emit('resize');
  assert.deepEqual(e.viewport.style, { width: '320px', height: '260px', left: '4px', top: '120px' });
  window.visualViewport.offsetTop = 80;
  window.visualViewport.emit('scroll');
  assert.equal(e.viewport.style.top, '80px');
});

test('invalid capability never creates an enabled bridge', async () => {
  await assert.rejects(page(undefined, '#invalid'), /invalid handoff capability/);
});
