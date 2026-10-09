// A transient input sink. Only committed text reaches RFB; nothing is retained.
const SENTINEL = '\u200b'; // Lets a native keyboard delete even after we clear text.

export function createMobileKeyboard(input, sendKey) {
  let enabled = false;
  let composing = false;
  let compositionTail = null;
  let tailTimer;

  function clearTail() {
    clearTimeout(tailTimer);
    compositionTail = null;
  }
  function reset() {
    input.value = enabled ? SENTINEL : '';
    if (enabled) input.setSelectionRange(1, 1);
  }
  function clear() {
    composing = false;
    clearTail();
    reset();
  }
  function text(value) {
    for (const char of value.replace(/\r\n?/g, '\n')) {
      const cp = char.codePointAt(0);
      if (cp === 10) sendKey(0xff0d);
      else if (cp >= 32 && cp !== 127 && !(cp >= 0xd800 && cp <= 0xdfff)) {
        sendKey(cp <= 255 ? cp : 0x01000000 + cp);
      }
    }
  }
  function value() {
    return input.value.startsWith(SENTINEL) ? input.value.slice(1) : input.value;
  }

  // Do not forward keydown as well as input (including keyCode 229/IME).
  // These handlers are local: the noVNC canvas keeps its desktop key handling.
  for (const type of ['keydown', 'keypress', 'keyup']) {
    input.addEventListener(type, event => event.stopPropagation());
  }
  for (const type of ['paste', 'copy', 'cut', 'drop']) {
    input.addEventListener(type, event => event.preventDefault());
  }
  input.addEventListener('beforeinput', event => {
    if (!enabled || /Paste|Drop|Cut/.test(event.inputType || '')) {
      event.preventDefault();
      reset();
    }
  });
  input.addEventListener('compositionstart', () => {
    clearTail();
    composing = enabled;
  });
  input.addEventListener('compositionend', event => {
    if (!enabled || !composing) { clear(); return; }
    composing = false;
    const committed = event.data || '';
    text(committed);
    reset();
    // Engines can emit a final input after compositionend, or before it.
    // Suppress only the matching trailing commit, never a later keystroke.
    compositionTail = committed;
    tailTimer = setTimeout(clearTail, 0);
  });
  input.addEventListener('input', event => {
    if (!enabled) { clear(); return; }
    if (composing || event.isComposing) return;
    const type = event.inputType || '';
    const inserted = event.data ?? value();
    if (compositionTail !== null &&
        /^(insertText|insertCompositionText|insertFromComposition)$/.test(type) &&
        inserted === compositionTail) {
      clearTail();
      reset();
      return;
    }
    clearTail();
    if (type === 'deleteContentBackward') sendKey(0xff08);
    else if (type === 'insertLineBreak' || type === 'insertParagraph') sendKey(0xff0d);
    else if (/^(insertText|insertReplacementText|insertFromComposition|insertCompositionText)$/.test(type) || !type) {
      text(inserted);
    }
    reset();
  });
  input.addEventListener('blur', clear);

  input.disabled = true;
  input.value = '';
  return {
    enable() { enabled = true; input.disabled = false; reset(); },
    disable() {
      enabled = false;
      input.disabled = true;
      input.blur();
      clear();
    },
    focus() {
      if (!enabled) return;
      clear();
      // Must run synchronously inside the Keyboard button's user gesture.
      input.focus({ preventScroll: true });
    },
  };
}

export function fitVisualViewport(viewport, frame) {
  frame.style.width = `${viewport.width}px`;
  frame.style.height = `${viewport.height}px`;
  frame.style.left = `${viewport.offsetLeft}px`;
  frame.style.top = `${viewport.offsetTop}px`;
}
