#!/usr/bin/env node

// Submit a prompt through the authenticated ChatGPT frontend. This deliberately
// leaves challenge-token generation to the web application.

const http = require("http");

const cdpBase = process.env.CHATGPT_WEB_CDP_URL || "http://127.0.0.1:9222";
const timeoutMs = Number(process.env.CHATGPT_WEB_SEND_TIMEOUT_MS || "300000");

function fail(message) {
  process.stderr.write(`chatgpt-web send: ${message}\n`);
  process.exit(1);
}

function readInput() {
  return new Promise((resolve, reject) => {
    let body = "";
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", (chunk) => { body += chunk; });
    process.stdin.on("end", () => {
      try {
        resolve(JSON.parse(body));
      } catch (_error) {
        reject(new Error("invalid prompt request"));
      }
    });
  });
}

function getJson(url) {
  return new Promise((resolve, reject) => {
    http.get(url, (response) => {
      let body = "";
      response.setEncoding("utf8");
      response.on("data", (chunk) => { body += chunk; });
      response.on("end", () => {
        try { resolve(JSON.parse(body)); }
        catch (_error) { reject(new Error("invalid Chrome debugger response")); }
      });
    }).on("error", reject);
  });
}

async function main() {
  const input = await readInput();
  if (typeof input.prompt !== "string" || !input.prompt.trim()) {
    fail("prompt must not be empty");
  }
  const pages = await getJson(`${cdpBase}/json/list`);
  const page = pages.find((item) =>
    item.type === "page" && item.url.startsWith("https://chatgpt.com"));
  if (!page) fail("no ChatGPT tab found in the debugging browser");

  const socket = new WebSocket(page.webSocketDebuggerUrl);
  const pending = new Map();
  let sequence = 0;
  let conversationRequest = null;
  let conversationStatus = null;
  let finished = false;
  let baselineAssistantCount = 0;
  let renderedText = "";
  let polling = false;

  function emit(value) {
    process.stdout.write(`${JSON.stringify(value)}\n`);
  }

  function call(method, params = {}) {
    return new Promise((resolve, reject) => {
      const id = ++sequence;
      pending.set(id, { resolve, reject });
      socket.send(JSON.stringify({ id, method, params }));
    });
  }

  async function finish() {
    if (finished) return;
    finished = true;
    clearTimeout(timer);
    await new Promise((resolve) => setTimeout(resolve, 250));
    const location = await call("Runtime.evaluate", {
      expression: "location.href",
      returnByValue: true,
    });
    const url = location.result.value;
    const match = typeof url === "string" ? url.match(/\/c\/([^/?#]+)/) : null;
    emit({
      type: "completed",
      completed: true,
      conversation_id: match ? match[1] : input.conversation_id || null,
    });
    socket.close();
  }

  async function pollAssistant() {
    if (polling || finished || socket.readyState !== WebSocket.OPEN) return;
    polling = true;
    try {
      const result = await call("Runtime.evaluate", {
        expression: `(()=>{const a=[...document.querySelectorAll("[data-message-author-role=assistant]")];const e=a[a.length-1];return {count:a.length,text:e?(e.innerText||e.textContent||""):""}})()`,
        returnByValue: true,
      });
      const state = result.result.value || {};
      const text = state.count > baselineAssistantCount ? state.text || "" : "";
      if (text && text !== renderedText) {
        if (text.startsWith(renderedText)) {
          emit({ type: "delta", text: text.slice(renderedText.length) });
        } else {
          emit({ type: "replace", text });
        }
        renderedText = text;
      }
    } catch (_error) {
      // Navigation can briefly invalidate the execution context.
    } finally {
      polling = false;
    }
  }

  const timer = setTimeout(() => {
    if (!finished) {
      socket.close();
      fail("timed out waiting for ChatGPT to finish responding");
    }
  }, timeoutMs);

  socket.addEventListener("message", async (event) => {
    const message = JSON.parse(event.data);
    if (message.id && pending.has(message.id)) {
      const request = pending.get(message.id);
      pending.delete(message.id);
      if (message.error) request.reject(new Error(message.error.message));
      else request.resolve(message.result);
      return;
    }
    if (message.method === "Network.requestWillBeSent") {
      const request = message.params.request;
      const url = new URL(request.url);
      if (request.method === "POST" &&
          url.pathname === "/backend-api/f/conversation") {
        conversationRequest = message.params.requestId;
      }
    } else if (conversationRequest &&
               message.method === "Network.responseReceived" &&
               message.params.requestId === conversationRequest) {
      conversationStatus = message.params.response.status;
    } else if (conversationRequest &&
               message.method === "Network.loadingFinished" &&
               message.params.requestId === conversationRequest) {
      if (conversationStatus === 429) {
        fail("ChatGPT web rate limit reached; wait before sending another prompt");
      }
      if (conversationStatus && conversationStatus >= 400) {
        fail(`ChatGPT submission returned HTTP ${conversationStatus}`);
      }
      await finish();
    } else if (conversationRequest &&
               message.method === "Network.loadingFailed" &&
               message.params.requestId === conversationRequest) {
      fail(`ChatGPT request failed: ${message.params.errorText}`);
    }
  });

  socket.addEventListener("open", async () => {
    try {
      await call("Network.enable");
      const target = input.conversation_id
        ? `https://chatgpt.com/c/${encodeURIComponent(input.conversation_id)}`
        : "https://chatgpt.com/";
      const current = await call("Runtime.evaluate", {
        expression: "location.href",
        returnByValue: true,
      });
      if (current.result.value !== target) {
        await call("Page.navigate", { url: target });
      }
      const ready = await call("Runtime.evaluate", {
        expression: `(async()=>{for(let i=0;i<300;i++){const e=document.querySelector("#prompt-textarea");if(e)return true;await new Promise(r=>setTimeout(r,100));}return false})()`,
        awaitPromise: true,
        returnByValue: true,
      });
      if (!ready.result.value) fail("ChatGPT composer did not become ready");
      const baseline = await call("Runtime.evaluate", {
        expression: `document.querySelectorAll("[data-message-author-role=assistant]").length`,
        returnByValue: true,
      });
      baselineAssistantCount = baseline.result.value || 0;
      const focused = await call("Runtime.evaluate", {
        expression: `(()=>{const e=document.querySelector("#prompt-textarea");if(!e)return false;e.focus();e.replaceChildren();e.dispatchEvent(new InputEvent("input",{bubbles:true,inputType:"deleteContentBackward"}));return true})()`,
        returnByValue: true,
      });
      if (!focused.result.value) fail("could not focus the ChatGPT composer");
      await call("Input.insertText", { text: input.prompt });
      const submitted = await call("Runtime.evaluate", {
        expression: `(async()=>{for(let i=0;i<100;i++){const b=document.querySelector("button[data-testid=send-button]");if(b&&!b.disabled){b.click();return true;}await new Promise(r=>setTimeout(r,100));}return false})()`,
        awaitPromise: true,
        returnByValue: true,
      });
      if (!submitted.result.value) fail("ChatGPT send button was unavailable");
      const pollTimer = setInterval(pollAssistant, 100);
      const heartbeat = setInterval(() => {
        if (!finished) emit({ type: "heartbeat" });
      }, 1000);
      socket.addEventListener("close", () => {
        clearInterval(pollTimer);
        clearInterval(heartbeat);
      });
    } catch (error) {
      fail(error.message);
    }
  });
  socket.addEventListener("error", () => fail("lost the Chrome debugger connection"));

  process.on("SIGTERM", async () => {
    try {
      if (socket.readyState === WebSocket.OPEN) {
        await call("Runtime.evaluate", {
          expression: `(()=>{const b=document.querySelector("button[data-testid=stop-button]")||[...document.querySelectorAll("button")].find(x=>/stop generating/i.test(x.getAttribute("aria-label")||""));if(!b)return false;b.click();return true})()`,
          returnByValue: true,
        });
      }
    } catch (_error) {
      // The page may already have completed or navigated.
    }
    setTimeout(() => process.exit(130), 200);
  });
}

main().catch((error) => fail(error.message));
