#!/usr/bin/env node

// Static compatibility probe for ChatGPT's Sentinel SDK. Network access from
// the evaluated SDK is blocked; this checks whether its proof engine can run in
// Node and records browser objects requested during initialization.

const vm = require("vm");
const cryptoModule = require("crypto");
const fs = require("fs");
const os = require("os");
const path = require("path");

const bootstrapUrl = "https://chatgpt.com/backend-api/sentinel/sdk.js";
const cacheTtlMs = Number(
  process.env.CHATGPT_WEB_SENTINEL_CACHE_TTL_MS || "21600000");

function sha256(value) {
  return cryptoModule.createHash("sha256").update(value).digest("hex");
}

function cacheDirectory() {
  const root = process.env.XDG_CACHE_HOME || path.join(os.homedir(), ".cache");
  return path.join(root, "chatgpt-web", "sentinel");
}

function readCachedSdk() {
  const directory = cacheDirectory();
  const manifestPath = path.join(directory, "current.json");
  const manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
  const sourcePath = path.join(directory, manifest.file);
  const source = fs.readFileSync(sourcePath, "utf8");
  if (sha256(source) !== manifest.sha256) {
    throw new Error("cached Sentinel SDK failed its SHA-256 check");
  }
  return { ...manifest, source, manifestPath };
}

function writeAtomic(file, value) {
  const temporary = `${file}.tmp-${process.pid}`;
  fs.writeFileSync(temporary, value, { mode: 0o600, flag: "wx" });
  fs.renameSync(temporary, file);
  fs.chmodSync(file, 0o600);
}

async function loadSdk() {
  let cached = null;
  try { cached = readCachedSdk(); }
  catch (_error) { cached = null; }
  if (cached && Date.now() - cached.checked_at < cacheTtlMs) {
    return { ...cached, cacheStatus: "hit" };
  }
  try {
    const bootstrapResponse = await fetch(bootstrapUrl);
    if (!bootstrapResponse.ok) throw new Error(`bootstrap HTTP ${bootstrapResponse.status}`);
    const bootstrap = await bootstrapResponse.text();
    const implementation = bootstrap.match(/script\.src = '([^']+)'/)?.[1];
    if (!implementation) throw new Error("Sentinel implementation URL not found");
    const sourceResponse = await fetch(implementation);
    if (!sourceResponse.ok) throw new Error(`SDK HTTP ${sourceResponse.status}`);
    const source = await sourceResponse.text();
    const digest = sha256(source);
    const directory = cacheDirectory();
    fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
    fs.chmodSync(directory, 0o700);
    const file = `sdk-${digest.slice(0, 16)}.js`;
    const sourcePath = path.join(directory, file);
    if (!fs.existsSync(sourcePath)) writeAtomic(sourcePath, source);
    const manifest = { version: 1, implementation, file, sha256: digest,
      checked_at: Date.now() };
    writeAtomic(path.join(directory, "current.json"), `${JSON.stringify(manifest)}\n`);
    return { ...manifest, source, cacheStatus: cached ? "updated" : "miss" };
  } catch (error) {
    if (cached) return { ...cached, cacheStatus: "stale-offline" };
    throw error;
  }
}

function readStdinJson() {
  return new Promise((resolve, reject) => {
    let input = "";
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", (chunk) => { input += chunk; });
    process.stdin.on("end", () => {
      try { resolve(JSON.parse(input)); }
      catch (_error) { reject(new Error("invalid requirements JSON on stdin")); }
    });
  });
}

async function main() {
  const suppliedRequirements = process.argv.includes("--requirements-stdin")
    ? await readStdinJson()
    : null;
  const loadedSdk = await loadSdk();
  const implementation = loadedSdk.implementation;
  let source = loadedSdk.source;
  const proofExport = /var P=new _;/;
  if (!proofExport.test(source)) {
    throw new Error("Sentinel proof engine signature changed");
  }
  source = source.replace(proofExport, "var P=new _;globalThis.__proofEngine=P;");
  const turnstileExport = /function _n\(t,n\)\{/;
  if (!turnstileExport.test(source)) {
    throw new Error("Sentinel turnstile VM signature changed");
  }
  source = source.replace(
    turnstileExport,
    "globalThis.__turnstileVM=(t,n)=>_n(t,n);function _n(t,n){",
  );

  const requestedElements = [];
  const appendedElements = [];
  const listeners = new Map();
  const noop = () => {};
  let sandbox;
  const bridgeWindow = {
    postMessage: (message) => {
      if (!message?.requestId || !listeners.has("message")) return;
      const requirements = suppliedRequirements || {
        token: "probe-requirements-token",
        proofofwork: {
          required: true,
          seed: "chatgpt-web-node-probe",
          difficulty: "0fffff",
        },
        turnstile: { required: true, dx: "probe-turnstile-config" },
      };
      setTimeout(() => listeners.get("message")({
        source: bridgeWindow,
        origin: "https://chatgpt.com",
        data: {
          type: "response",
          requestId: message.requestId,
          result: {
            cachedChatReq: requirements,
            cachedProof: message.p,
          },
        },
      }), 0);
    },
  };
  const currentScript = { src: implementation };
  const document = {
    cookie: "",
    currentScript,
    scripts: [currentScript],
    location: { href: implementation, origin: "https://chatgpt.com" },
    documentElement: { getAttribute: () => null },
    body: { appendChild: (element) => { appendedElements.push(element.tagName); } },
    createElement: (tag) => {
      requestedElements.push(tag);
      return {
        tagName: tag.toUpperCase(),
        style: {},
        setAttribute: noop,
        addEventListener: (name, callback) => {
          if (name === "load") setTimeout(callback, 0);
        },
        removeEventListener: noop,
        contentWindow: bridgeWindow,
      };
    },
    addEventListener: noop,
    removeEventListener: noop,
    querySelector: () => null,
  };
  sandbox = {
    console,
    crypto: globalThis.crypto,
    TextEncoder,
    TextDecoder,
    URL,
    URLSearchParams,
    setTimeout,
    clearTimeout,
    setInterval,
    clearInterval,
    performance,
    Response,
    Request,
    Headers,
    AbortController,
    atob,
    btoa,
    screen: { width: 1920, height: 1080, colorDepth: 24, pixelDepth: 24 },
    navigator: {
      userAgent: `Node.js ${process.version}`,
      language: "en-US",
      languages: ["en-US"],
      platform: process.platform,
      hardwareConcurrency: 8,
      deviceMemory: 8,
    },
    document,
    location: { href: "https://chatgpt.com/", origin: "https://chatgpt.com" },
    requestIdleCallback: (callback) => setTimeout(
      () => callback({ timeRemaining: () => 10, didTimeout: false }), 0),
    addEventListener: (name, callback) => listeners.set(name, callback),
    removeEventListener: (name) => listeners.delete(name),
    dispatchEvent: noop,
    fetch: async () => { throw new Error("SDK network access disabled by probe"); },
  };
  sandbox.window = sandbox;
  sandbox.self = sandbox;
  sandbox.top = sandbox;
  sandbox.parent = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox, { filename: "sentinel-sdk.js", timeout: 5000 });

  if (process.argv.includes("--emit-initial")) {
    const initial = await sandbox.__proofEngine.getRequirementsToken();
    process.stdout.write(`${JSON.stringify({ initial })}\n`);
    return;
  }

  const requirements = suppliedRequirements || {
    proofofwork: {
      required: true,
      seed: "chatgpt-web-node-probe",
      difficulty: "0fffff",
    },
  };
  const proof = await sandbox.__proofEngine.getEnforcementToken(requirements);
  if (process.argv.includes("--emit-enforcement-json")) {
    const initial = await sandbox.__proofEngine.getRequirementsToken();
    const turnstile = requirements?.turnstile?.dx
      ? await sandbox.__turnstileVM(requirements, requirements.turnstile.dx)
      : null;
    process.stdout.write(`${JSON.stringify({ initial, proof, turnstile })}\n`);
    return;
  }
  let tokenResult;
  let tokenError;
  try {
    tokenResult = await Promise.race([
      sandbox.SentinelSDK.token("chatgpt-web-node-probe"),
      new Promise((_, reject) => setTimeout(
        () => reject(new Error("token generation timed out")), 3000)),
    ]);
  } catch (error) {
    tokenError = error.message;
  }
  let tokenKeys = [];
  let tokenShape = {};
  if (typeof tokenResult === "string") {
    try {
      const parsedToken = JSON.parse(tokenResult);
      tokenKeys = Object.keys(parsedToken).sort();
      tokenShape = Object.fromEntries(tokenKeys.map((key) => {
        const value = parsedToken[key];
        return [key, {
          type: value === null ? "null" : typeof value,
          length: typeof value === "string" ? value.length : null,
        }];
      }));
    }
    catch (_error) { tokenKeys = []; }
  }
  const result = {
    sdk_url: implementation,
    sdk_sha256: loadedSdk.sha256,
    cache_status: loadedSdk.cacheStatus,
    public_api: Object.keys(sandbox.SentinelSDK || {}).sort(),
    proof_generated: typeof proof === "string" && proof.startsWith("gAAAAAB"),
    proof_length: typeof proof === "string" ? proof.length : 0,
    requested_elements: [...new Set(requestedElements)],
    appended_elements: [...new Set(appendedElements)],
    message_bridge_installed: listeners.has("message"),
    full_token_generated: typeof tokenResult === "string",
    full_token_keys: tokenKeys,
    full_token_shape: tokenShape,
    full_token_error: tokenError || null,
  };
  process.stdout.write(`${JSON.stringify(result)}\n`);
}

main().catch((error) => {
  process.stderr.write(`sentinel-node-probe: ${error.message}\n`);
  process.exit(1);
});
