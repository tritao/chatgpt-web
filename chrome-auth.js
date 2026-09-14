#!/usr/bin/env node

// Capture one authenticated ChatGPT request through Chrome DevTools Protocol.
// The result contains credentials and must only be consumed over a private pipe.

const http = require("http");

const cdpBase = process.env.CHATGPT_WEB_CDP_URL || "http://127.0.0.1:9222";
const timeoutMs = Number(process.env.CHATGPT_WEB_AUTH_TIMEOUT_MS || "15000");

function fail(message) {
  process.stderr.write(`chatgpt-web auth: ${message}\n`);
  process.exit(1);
}

function getJson(url) {
  return new Promise((resolve, reject) => {
    http.get(url, (response) => {
      let body = "";
      response.setEncoding("utf8");
      response.on("data", (chunk) => { body += chunk; });
      response.on("end", () => {
        try {
          resolve(JSON.parse(body));
        } catch (error) {
          reject(new Error("Chrome returned invalid debugger metadata"));
        }
      });
    }).on("error", reject);
  });
}

async function main() {
  const pages = await getJson(`${cdpBase}/json/list`);
  const page = pages.find((item) =>
    item.type === "page" && item.url.startsWith("https://chatgpt.com"));
  if (!page) {
    fail("no ChatGPT tab found in the debugging browser");
  }

  const socket = new WebSocket(page.webSocketDebuggerUrl);
  const requests = new Map();
  const extraHeaders = new Map();
  let sequence = 0;
  let finished = false;

  const timer = setTimeout(() => {
    if (!finished) {
      socket.close();
      fail("timed out waiting for an authenticated conversation request");
    }
  }, timeoutMs);

  function send(method, params = {}) {
    socket.send(JSON.stringify({ id: ++sequence, method, params }));
  }

  function isTarget(request) {
    return request.method === "GET" &&
      request.url.includes("/backend-api/conversations?") &&
      !request.url.includes("hide_snorlax") &&
      request.url.includes("limit=28");
  }

  function finish(requestId) {
    if (finished || !requests.has(requestId) || !extraHeaders.has(requestId)) {
      return;
    }
    finished = true;
    clearTimeout(timer);
    const request = requests.get(requestId);
    const result = {
      url: request.url,
      method: request.method,
      headers: { ...request.headers, ...extraHeaders.get(requestId) },
    };
    process.stdout.write(JSON.stringify(result));
    socket.close();
  }

  socket.addEventListener("open", () => {
    send("Network.enable");
    setTimeout(() => send("Page.navigate", { url: "https://chatgpt.com/" }), 200);
  });
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (message.method === "Network.requestWillBeSent") {
      const { requestId, request } = message.params;
      if (isTarget(request)) {
        requests.set(requestId, request);
        finish(requestId);
      }
    } else if (message.method === "Network.requestWillBeSentExtraInfo") {
      const { requestId, headers } = message.params;
      extraHeaders.set(requestId, headers);
      finish(requestId);
    }
  });
  socket.addEventListener("error", () => fail("lost the Chrome debugger connection"));
}

main().catch((error) => fail(error.message));
