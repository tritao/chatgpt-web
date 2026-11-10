"use strict";
const assert = require("node:assert/strict");
const test = require("node:test");
const vm = require("node:vm");
const { instrumentProofEngines } = require("../sentinel-node-probe.js");

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
