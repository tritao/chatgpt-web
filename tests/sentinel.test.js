"use strict";
const assert = require("node:assert/strict");
const test = require("node:test");
const vm = require("node:vm");
const { instrumentProofEngines, parseEvidence } = require("../sentinel-node-probe.js");

test("proof discovery survives renamed and formatted singleton bindings", () => {
  for (const declaration of ["var P=new _;", "var E=new O;", "const $proof = new Engine();", "let proof_2 = new Engine ;"]) {
    const source = `class Engine {
      getRequirementsToken() { return 'fixture-initial'; }
      getEnforcementToken() { return 'fixture-proof'; }
    }
    const _ = Engine, O = Engine;
    ${declaration}`;
    const sandbox = {};
    vm.runInNewContext(instrumentProofEngines(source), sandbox);
    const engines = sandbox.__sentinelProofCandidates;
    assert.equal(engines.length, 1);
    assert.equal(engines[0].getRequirementsToken(), "fixture-initial");
    assert.equal(engines[0].getEnforcementToken(), "fixture-proof");
  }
});

test("instrumentation preserves unrelated singletons and rejects absent interfaces", () => {
  const source = "class Other {} var unrelated=new Other;";
  const sandbox = {};
  vm.runInNewContext(instrumentProofEngines(source), sandbox);
  const engines = sandbox.__sentinelProofCandidates.filter((value) =>
    typeof value.getRequirementsToken === "function" &&
    typeof value.getEnforcementToken === "function");
  assert.equal(engines.length, 0);
});


test("SDK evidence accepts optional absent work and required completed VM evidence", () => {
  assert.deepEqual(parseEvidence(JSON.stringify({p:null,t:null}), {}), {proof:null,turnstile:null});
  const evidence = {p:"proof-fixture", t:btoa("completed-fixture")};
  assert.deepEqual(parseEvidence(JSON.stringify(evidence), {
    proofofwork:{required:true},turnstile:{required:true},
  }), {proof:evidence.p, turnstile:evidence.t});
});

test("SDK VM diagnostics, timeouts and missing evidence are rejected without leaking tokens", () => {
  for (const token of ["invalid JSON", "null", JSON.stringify({e:"private-error"}),
    JSON.stringify({p:"fixture",t:btoa("0: TypeError: private-error")}),
    JSON.stringify({p:"fixture",t:"124"}), JSON.stringify({p:null,t:null})]) {
    assert.throws(() => parseEvidence(token, {turnstile:{required:true}}),
      (error) => !error.message.includes("private-error"));
  }
  assert.throws(() => parseEvidence('{}', {proofofwork:{required:true}}), /proof of work/);
});

test("Node storage keeps values only in the current helper process", () => {
  const { createMemoryStorage } = require('../sentinel-node-probe.js');
  const storage = createMemoryStorage();
  assert.equal(storage.getItem('missing'), null);
  storage.setItem('fixture', 12);
  assert.equal(storage.getItem('fixture'), '12');
  assert.equal(storage.length, 1);
  assert.equal(storage.key(0), 'fixture');
  assert.equal(createMemoryStorage().getItem('fixture'), null);
  storage.removeItem('fixture');
  assert.equal(storage.length, 0);
  storage.setItem('other', 'value');
  storage.clear();
  assert.equal(storage.key(0), null);
});

test("helper uses the public SDK API with the original preparation proof", () => {
  const fs = require('node:fs');
  const os = require('node:os');
  const path = require('node:path');
  const crypto = require('node:crypto');
  const { spawnSync } = require('node:child_process');
  const cache = fs.mkdtempSync(path.join(os.tmpdir(), 'sentinel-fixture-'));
  const directory = path.join(cache, 'chatgpt-web', 'sentinel');
  const source = `class Engine {
    getRequirementsToken() { return 'fresh-initial'; }
    getEnforcementToken() { throw Error('must use public SDK for enforcement'); }
  }
  var renamed = new Engine;
  globalThis.SentinelSDK = {token: async () => new Promise((resolve) => {
    const canvas = document.createElement('canvas');
    if (canvas.getContext('webgl') !== null) throw Error('unexpected graphics context');
    const div = document.createElement('div');
    if (div.getBoundingClientRect().width !== 0) throw Error('unexpected layout');
    document.body.removeChild(document.body.appendChild(div));
    localStorage.setItem('fixture', 'value');
    if (localStorage.getItem('fixture') !== 'value') throw Error('storage unavailable');
    addEventListener('message', event => {
      const result = event.data.result;
      const valid = result.cachedProof === 'original-fixture' &&
        result.cachedChatReq.prepare_token === 'prepared-fixture';
      resolve(JSON.stringify(valid ? {p:'gAAAAABfixture',t:btoa('completed-fixture')} : {e:'wrong-proof'}));
    });
    document.createElement('iframe').contentWindow.postMessage({requestId:'fixture',p:'fresh-initial'});
  })};`;
  try {
    fs.mkdirSync(directory, {recursive:true});
    fs.writeFileSync(path.join(directory, 'fixture.js'), source);
    fs.writeFileSync(path.join(directory, 'current.json'), JSON.stringify({
      file:'fixture.js', sha256:crypto.createHash('sha256').update(source).digest('hex'),
      checked_at:Date.now(), implementation:'https://chatgpt.com/sentinel/fixture/sdk.js',
    }));
    const result = spawnSync(process.execPath,
      [path.join(__dirname, '..', 'sentinel-node-probe.js'), '--requirements-stdin', '--emit-enforcement-json'], {
        input:JSON.stringify({initial:'original-fixture',requirements:{
          prepare_token:'prepared-fixture',proofofwork:{required:true},turnstile:{required:true,dx:'fixture'},
        }}), encoding:'utf8', timeout:5000,
        env:{...process.env,XDG_CACHE_HOME:cache,CHATGPT_WEB_SENTINEL_CACHE_TTL_MS:'21600000'},
      });
    assert.equal(result.status, 0, result.stderr);
    assert.deepEqual(JSON.parse(result.stdout), {proof:'gAAAAABfixture',turnstile:btoa('completed-fixture')});
  } finally { fs.rmSync(cache, {recursive:true,force:true}); }
});
