"use strict";

// Composer handling for chrome-recipe.js. ChatGPT may replace its editor
// while React hydrates, so every wait runs on the host with a deadline, the
// same editor node must stay visible before it is touched, an existing draft
// is never cleared, and the prompt is verified before a single send click.
// Errors carry fixed messages only (never page text or the prompt).

class ComposerError extends Error {
  constructor(message, clicked = false) {
    super(message);
    this.clicked = clicked;
  }
}

// Evaluated in the page; must stay self-contained. For "click", the editor
// identity and text are re-checked in the same evaluation as the click, so a
// change after the host's last inspection can never be sent.
function pageComposer(action, expected, expectedId, normalizeText) {
  const editor = [...document.querySelectorAll("#prompt-textarea")].find((node) =>
    node.isConnected && node.getClientRects().length > 0 &&
    (node.isContentEditable || node.tagName === "TEXTAREA"));
  if (!editor) return { present: false, refused: action === "click" };
  if (!editor.__chatgptWebId) {
    window.__chatgptWebSeq = (window.__chatgptWebSeq || 0) + 1;
    editor.__chatgptWebId = window.__chatgptWebSeq;
  }
  // ProseMirror renders one <p> per line; an empty line holds a trailing <br>.
  const inline = (node) => node.nodeType === 3 ? node.textContent
    : node.nodeName === "BR" ? "" : [...node.childNodes].map(inline).join("");
  const children = [...(editor.childNodes || [])];
  const text = editor.tagName === "TEXTAREA" ? editor.value
    : children.length && children.every((node) => node.nodeName === "P")
      ? children.map(inline).join("\n") : editor.textContent;
  if (action === "focus") editor.focus();
  const button = document.querySelector('button[data-testid="send-button"]');
  const sendReady = !!button && button.isConnected && !button.disabled;
  const matches = action === "click" && editor.__chatgptWebId === expectedId &&
    normalizeText(text) === expected;
  if (matches && sendReady) button.click();
  const active = document.activeElement;
  return {
    present: true, id: editor.__chatgptWebId, text: text || "", sendReady,
    focused: active === editor || (!!active && editor.contains(active)),
    clicked: matches && sendReady,
    refused: action === "click" && !(matches && sendReady),
  };
}

function cdpPage(call) {
  const run = async (action, expected = "", id = 0) => {
    const args = [action, expected, id].map((value) => JSON.stringify(value)).join(",");
    const response = await call("Runtime.evaluate", {
      expression: `(${pageComposer})(${args},${normalize})`, returnByValue: true,
    });
    if (response.exceptionDetails) throw new Error("Execution context was destroyed");
    return response.result?.value || { present: false };
  };
  return {
    inspect: () => run("inspect"),
    focus: () => run("focus"),
    clickSend: (expected, id) => run("click", expected, id),
    insertText: (text) => call("Input.insertText", { text }),
  };
}

// Only line endings and trailing whitespace may differ; all else is exact.
const normalize = (text) => String(text || "").replace(/\r\n?/g, "\n")
  .replace(/ /g, " ").replace(/[ \t]+$/gm, "").trim();

const transient = /context was destroyed|Cannot find (default )?(execution )?context|target navigated|Promise was collected|timed out/i;

async function inspect(page, action = "inspect") {
  try {
    return await page[action]();
  } catch (error) {
    if (transient.test(error?.message || "")) return { present: false };
    throw error;
  }
}

const LIMITS = { readyMs: 15000, stableMs: 400, pollMs: 125, verifyMs: 3000, sendMs: 10000, attempts: 2 };

async function submitPrompt(page, prompt, options = {}) {
  const o = { ...LIMITS, ...options };
  const now = o.now || Date.now;
  const sleep = o.sleep || ((ms) => new Promise((resolve) => setTimeout(resolve, ms)));
  const until = (ms) => Math.min(now() + ms, o.deadline ?? Infinity);
  const pause = (end) => sleep(Math.max(1, Math.min(o.pollMs, end - now())));
  const expected = normalize(prompt);
  if (!expected) throw new ComposerError("prompt must not be empty");

  // Wait until one editor node stays visible for stableMs, then require it empty.
  async function stableEmptyEditor() {
    const end = until(o.readyMs);
    let id = null;
    let since = 0;
    while (now() < end) {
      const snap = await inspect(page);
      if (!snap.present) id = null;
      else if (snap.id !== id) [id, since] = [snap.id, now()];
      else if (now() - since >= o.stableMs) {
        if (normalize(snap.text)) {
          throw new ComposerError("ChatGPT composer has an existing draft; it was left untouched");
        }
        return snap;
      }
      await pause(end);
    }
    throw new ComposerError("ChatGPT composer did not become ready; nothing was sent");
  }

  // Bounded hydration retry: re-type only into an empty replacement editor
  // and only before any click has happened.
  for (let attempt = 1; ; attempt++) {
    const editor = await stableEmptyEditor();
    const focused = await inspect(page, "focus");
    if (focused.present && focused.id === editor.id && focused.focused && !normalize(focused.text)) {
      await page.insertText(prompt);
      const end = until(o.verifyMs);
      let snap;
      do {
        snap = await inspect(page);
        if (snap.present && normalize(snap.text) === expected) break;
        await pause(end);
      } while (now() < end);
      if (snap.present && normalize(snap.text) === expected) break;
      const replacedEmpty = !snap.present || (snap.id !== editor.id && !normalize(snap.text));
      if (!replacedEmpty) {
        throw new ComposerError("ChatGPT composer did not contain the inserted prompt; nothing was sent");
      }
    } else if (focused.present && focused.id === editor.id) {
      throw new ComposerError("ChatGPT composer focus was unavailable; nothing was sent");
    }
    if (attempt >= o.attempts) {
      throw new ComposerError("ChatGPT composer kept being replaced; nothing was sent");
    }
  }

  // Click exactly once, and only while the verified prompt is still present.
  const end = until(o.sendMs);
  for (;;) {
    const snap = await inspect(page);
    if (!snap.present || normalize(snap.text) !== expected) {
      throw new ComposerError("ChatGPT composer changed before sending; nothing was sent");
    }
    if (snap.sendReady) {
      o.onBeforeClick?.();
      let result;
      try {
        result = await page.clickSend(expected, snap.id);
      } catch (_error) {
        throw new ComposerError("ChatGPT send outcome is ambiguous; not retried", true);
      }
      if (result?.clicked) return;
      // The page refused in the same evaluation, so no click happened.
      if (result?.refused) {
        throw new ComposerError("ChatGPT composer changed at the moment of sending; nothing was sent");
      }
      throw new ComposerError("ChatGPT send outcome is ambiguous; not retried", true);
    }
    if (now() >= end) throw new ComposerError("ChatGPT send button was unavailable; nothing was sent");
    await pause(end);
  }
}

module.exports = { ComposerError, cdpPage, normalize, pageComposer, submitPrompt };
