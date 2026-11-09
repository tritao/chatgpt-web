#!/usr/bin/env node

// Build a ChatGPT send request in the authenticated frontend, pause it before
// transmission, and pass the request recipe to a local caller over stdout.

const http = require("http");
const { ComposerError, cdpPage, submitPrompt } = require("./chrome-composer.js");

const cdpBase = process.env.CHATGPT_WEB_CDP_URL || "http://127.0.0.1:9222";
const timeoutMs = Number(process.env.CHATGPT_WEB_RECIPE_TIMEOUT_MS || "30000");
const callTimeoutMs = Number(process.env.CHATGPT_WEB_CDP_CALL_TIMEOUT_MS || "10000");

function fail(message) {
  process.stderr.write(`chatgpt-web recipe: ${message}\n`);
  process.exit(1);
}

function readInput() {
  return new Promise((resolve, reject) => {
    let body = "";
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", (chunk) => { body += chunk; });
    process.stdin.on("end", () => {
      try { resolve(JSON.parse(body)); }
      catch (_error) { reject(new Error("invalid recipe request")); }
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
  const sentinelRequests = new Map();
  const sentinelResponses = [];
  let sequence = 0;
  let finished = false;
  let clicked = false;
  const startedAt = Date.now();

  function call(method, params = {}) {
    return new Promise((resolve, reject) => {
      const id = ++sequence;
      const timer = setTimeout(() => {
        pending.delete(id);
        reject(new Error(`${method} timed out`));
      }, callTimeoutMs);
      const settle = (fn) => (value) => { clearTimeout(timer); fn(value); };
      pending.set(id, { resolve: settle(resolve), reject: settle(reject) });
      socket.send(JSON.stringify({ id, method, params }));
    });
  }

  const timer = setTimeout(() => {
    if (!finished) {
      socket.close();
      fail(clicked
        ? "send click did not produce a request in time; not retried"
        : "timed out waiting for the ChatGPT composer; nothing was sent");
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
          (url.pathname === "/backend-api/sentinel/chat-requirements/prepare" ||
           url.pathname === "/backend-api/sentinel/chat-requirements/finalize")) {
        sentinelRequests.set(message.params.requestId, {
          path: url.pathname,
          requestBody: request.postData || "",
        });
      }
      return;
    }
    if (message.method === "Network.loadingFinished" &&
        sentinelRequests.has(message.params.requestId)) {
      const capturedRequest = sentinelRequests.get(message.params.requestId);
      try {
        const response = await call("Network.getResponseBody", {
          requestId: message.params.requestId,
        });
        sentinelResponses.push({
          ...capturedRequest,
          responseBody: response.body,
          base64Encoded: response.base64Encoded,
        });
      } catch (_error) {
        // The request recipe is still useful if Chrome evicted a response body.
      }
      return;
    }
    if (finished || message.method !== "Fetch.requestPaused") return;
    const request = message.params.request;
    const url = new URL(request.url);
    if (request.method !== "POST" ||
        url.pathname !== "/backend-api/f/conversation") {
      await call("Fetch.continueRequest", { requestId: message.params.requestId });
      return;
    }
    finished = true;
    clearTimeout(timer);
    const cookies = await call("Network.getCookies", { urls: [request.url] });
    await call("Fetch.failRequest", {
      requestId: message.params.requestId,
      errorReason: "Aborted",
    });
    const headers = { ...request.headers };
    headers.cookie = cookies.cookies
      .map((cookie) => `${cookie.name}=${cookie.value}`)
      .join("; ");
    // Network.loadingFinished and Fetch.requestPaused can arrive in adjacent
    // tasks. Give the response-body callback a brief chance to complete.
    await new Promise((resolve) => setTimeout(resolve, 300));
    process.stdout.write(JSON.stringify({
      url: request.url,
      method: request.method,
      headers,
      postData: request.postData || "",
      sentinel: sentinelResponses,
    }));
    socket.close();
  });

  socket.addEventListener("open", async () => {
    try {
      await call("Network.enable");
      await call("Fetch.enable", {
        patterns: [{
          urlPattern: "https://chatgpt.com/backend-api/f/conversation*",
          requestStage: "Request",
        }],
      });
      const target = input.conversation_id
        ? `https://chatgpt.com/c/${encodeURIComponent(input.conversation_id)}`
        : "https://chatgpt.com/";
      // The intercepted request is deliberately aborted in the page. Always
      // navigate so a prior optimistic send cannot leave the composer disabled.
      await call("Page.navigate", { url: target });
      // Leave headroom so a pre-click failure keeps its specific message, and
      // mark the click before it can be in flight.
      await submitPrompt(cdpPage(call), input.prompt, {
        deadline: startedAt + Math.max(0, timeoutMs - callTimeoutMs - 1000),
        onBeforeClick: () => { clicked = true; },
      });
    } catch (error) {
      // After an ambiguous click, wait for the paused request; never retry.
      if (finished || error.clicked) return;
      fail(error instanceof ComposerError ? error.message : "ChatGPT debugger command failed");
    }
  });
  socket.addEventListener("error", () => fail("lost the Chrome debugger connection"));
}

main().catch((error) => fail(error.message));
