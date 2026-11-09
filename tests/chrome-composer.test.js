"use strict";

// Offline, deterministic tests for the recipe composer. Run: node --test tests/

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { cdpPage, normalize, pageComposer, submitPrompt } = require("../chrome-composer.js");

const recipe = fs.readFileSync(path.join(__dirname, "..", "chrome-recipe.js"), "utf8");
const PROMPT = "first line\n\n  indented secret";

// A simulated page with a replaceable editor, a virtual clock and an action log.
function fakePage({ text = "", sendReady = true, typing = true } = {}) {
  let clock = 0;
  let seq = 0;
  const s = { actions: [], sendReady, hooks: {} };
  s.replace = (value = "") => { s.editor = { id: ++seq, text: value, focused: false }; };
  s.replace(text);
  const snap = () => (s.editor
    ? { present: true, id: s.editor.id, text: s.editor.text, focused: s.editor.focused, sendReady: s.sendReady }
    : { present: false });
  const page = {
    async inspect() { s.hooks.inspect?.(); return snap(); },
    async focus() { s.actions.push("focus"); s.hooks.focus?.(); if (s.editor) s.editor.focused = true; return snap(); },
    async insertText(value) {
      s.actions.push("insert");
      if (typing && s.editor?.focused) s.editor.text += value;
      s.hooks.insert?.();
    },
    // Mirrors the page: compare and click happen atomically, after hooks.click.
    async clickSend(expected, id) {
      s.hooks.click?.();
      const ok = !!s.editor && s.editor.id === id && normalize(s.editor.text) === expected && s.sendReady;
      if (ok) s.actions.push("click");
      return { ...snap(), clicked: ok, refused: !ok };
    },
  };
  return { page, s, now: () => clock, sleep: async (ms) => { clock += ms; } };
}
const run = (f, options = {}) => submitPrompt(f.page, PROMPT, { now: f.now, sleep: f.sleep, ...options });

test("inserts once, verifies, and clicks exactly once", async () => {
  const f = fakePage();
  const events = [];
  await run(f, { onBeforeClick: () => events.push("before-click") });
  assert.deepEqual(f.s.actions, ["focus", "insert", "click"]);
  assert.deepEqual(events, ["before-click"]);
});

test("waits until the same editor stays visible through hydration", async () => {
  const f = fakePage();
  let n = 0;
  f.s.hooks.inspect = () => { n++; if (n === 2 || n === 4) f.s.replace(); if (n === 3) f.s.editor = null; };
  await run(f);
  assert.ok(f.now() >= 300 + 400);
  assert.deepEqual(f.s.actions, ["focus", "insert", "click"]);
});

test("readiness is bounded when the editor keeps being replaced", async () => {
  const f = fakePage();
  f.s.hooks.inspect = () => f.s.replace();
  await assert.rejects(run(f, { readyMs: 2000 }), /did not become ready; nothing was sent/);
  assert.ok(f.now() <= 2000);
  assert.deepEqual(f.s.actions, []);
});

test("an existing draft, even one restored late, is never touched", async () => {
  for (const f of [fakePage({ text: "my draft" }), fakePage()]) {
    let n = 0;
    f.s.hooks.inspect = () => { if (++n === 2 && !f.s.editor.text) f.s.replace("restored draft"); };
    await assert.rejects(run(f), /existing draft; it was left untouched/);
    assert.deepEqual(f.s.actions, []);
    assert.match(f.s.editor.text, /draft/);
  }
});

test("a dropped insertion is retyped only into an empty replacement, bounded", async () => {
  const once = fakePage();
  once.s.hooks.insert = () => { once.s.hooks.insert = null; once.s.replace(); };
  await run(once);
  assert.deepEqual(once.s.actions, ["focus", "insert", "focus", "insert", "click"]);

  const always = fakePage();
  always.s.hooks.insert = () => always.s.replace();
  await assert.rejects(run(always), /kept being replaced; nothing was sent/);
  assert.equal(always.s.actions.filter((a) => a === "insert").length, 2);
  assert.ok(!always.s.actions.includes("click"));
});

test("unverified or changed input is never sent", async () => {
  await assert.rejects(run(fakePage({ typing: false })), /did not contain the inserted prompt/);
  const partial = fakePage();
  partial.s.hooks.insert = () => { partial.s.editor.text = "first line"; };
  await assert.rejects(run(partial), /did not contain the inserted prompt/);
  const edited = fakePage({ sendReady: false });
  let n = 0;
  edited.s.hooks.inspect = () => { if (edited.s.actions.includes("insert") && ++n === 2) edited.s.editor.text += "!"; };
  await assert.rejects(run(edited), /changed before sending; nothing was sent/);
  for (const f of [partial, edited]) assert.ok(!f.s.actions.includes("click"));
});

test("a disabled send button fails within the deadline without clicking", async () => {
  const f = fakePage({ sendReady: false });
  let signalled = false;
  await assert.rejects(run(f, { deadline: 2000, onBeforeClick: () => { signalled = true; } }),
    /send button was unavailable; nothing was sent/);
  assert.ok(f.now() <= 2000);
  assert.equal(signalled, false);
});

test("an ambiguous click is flagged first and never retried", async () => {
  const f = fakePage();
  let clicks = 0;
  const events = [];
  f.page.clickSend = async () => { clicks++; events.push("click"); throw new Error("Runtime.evaluate timed out"); };
  await assert.rejects(run(f, { onBeforeClick: () => events.push("before") }),
    (error) => error.clicked === true && /ambiguous; not retried/.test(error.message));
  assert.equal(clicks, 1);
  assert.deepEqual(events, ["before", "click"]);
});

test("a change after the last host inspection is refused at click time", async () => {
  for (const mutate of [(f) => { f.s.editor.text += " (user edit)"; }, (f) => f.s.replace(PROMPT)]) {
    const f = fakePage();
    let signalled = false;
    f.s.hooks.click = () => mutate(f);
    await assert.rejects(run(f, { onBeforeClick: () => { signalled = true; } }),
      (error) => error.clicked === false && /changed at the moment of sending; nothing was sent/.test(error.message));
    assert.ok(!f.s.actions.includes("click"));
    assert.equal(signalled, true); // conservative: flagged before the attempt
  }
});

test("transient lifecycle errors are retried; other errors propagate", async () => {
  const f = fakePage();
  const inspect = f.page.inspect;
  let n = 0;
  f.page.inspect = async () => { if (++n <= 2) throw new Error("Execution context was destroyed."); return inspect(); };
  await run(f);
  f.page.inspect = async () => { throw new Error("WebSocket is not open"); };
  await assert.rejects(run(f), /WebSocket is not open/);
});

test("in-page snapshot reads ProseMirror lines, skips hidden editors, acts only when asked", () => {
  let clicks = 0;
  const text = (v) => ({ nodeType: 3, textContent: v });
  const p = (...childNodes) => ({ nodeName: "P", childNodes });
  const br = { nodeName: "BR", childNodes: [] };
  const document = { activeElement: null };
  const node = (rects, childNodes) => ({
    isConnected: true, isContentEditable: true, childNodes, textContent: "x",
    getClientRects: () => rects, contains: () => false, focus() { document.activeElement = this; },
  });
  const editor = node([1], [p(text("first line")), p(br), p(text("  indented secret"))]);
  const button = { isConnected: true, disabled: false, click() { clicks++; } };
  Object.assign(document, { querySelectorAll: () => [node([], []), editor], querySelector: () => button });
  const ctx = vm.createContext({ document, window: {} });
  const evalIn = (action, expected = "", id = 0) => vm.runInContext(
    `(${pageComposer})(${[action, expected, id].map((v) => JSON.stringify(v))},${normalize})`, ctx);
  const first = evalIn("inspect");
  assert.equal(first.text, PROMPT);
  assert.equal(first.focused, false);
  assert.equal(clicks, 0);
  assert.equal(evalIn("inspect").id, first.id);
  assert.equal(evalIn("focus").focused, true);
  assert.equal(evalIn("click", "first line", first.id).refused, true);
  assert.equal(evalIn("click", PROMPT, first.id + 1).refused, true);
  assert.equal(clicks, 0);
  assert.equal(evalIn("click", PROMPT, first.id).clicked, true);
  assert.equal(clicks, 1);
  // Text mutated after the host's inspection: the same evaluation refuses.
  editor.childNodes[2] = p(text("  edited secret"));
  const refused = evalIn("click", PROMPT, first.id);
  assert.deepEqual([refused.clicked, refused.refused, clicks], [false, true, 1]);
  button.disabled = true;
  editor.childNodes[2] = p(text("  indented secret"));
  assert.equal(evalIn("click", PROMPT, first.id).refused, true);
  assert.equal(clicks, 1);
});

test("cdpPage treats page exceptions as transient", async () => {
  const page = cdpPage(async () => ({ exceptionDetails: { text: "boom" } }));
  await assert.rejects(page.inspect(), /Execution context was destroyed/);
});

test("recipe is bounded, never clobbers the editor, and never logs the prompt", () => {
  assert.match(recipe, /submitPrompt\(cdpPage\(call\), input\.prompt/);
  assert.match(recipe, /onBeforeClick: \(\) => \{ clicked = true; \}/);
  assert.match(recipe, /CHATGPT_WEB_CDP_CALL_TIMEOUT_MS/);
  assert.match(recipe, /not retried/);
  assert.doesNotMatch(recipe, /awaitPromise|replaceChildren|new InputEvent|execCommand/);
  assert.doesNotMatch(recipe, /fail\([^)]*input\.prompt|stderr\.write\([^)]*input\.prompt/);
});
