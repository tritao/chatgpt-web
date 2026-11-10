const test = require('node:test');
const assert = require('node:assert/strict');
const { isTarget } = require('../chrome-auth.js');

test('auth capture accepts current conversation queries regardless of pagination', () => {
  for (const suffix of ['', '?limit=28', '?hide_snorlax=true&limit=50&expand=true']) {
    assert.equal(isTarget({method:'GET',url:'https://chatgpt.com/backend-api/conversations'+suffix}), true);
  }
});

test('auth capture rejects other origins, endpoints, methods and malformed URLs', () => {
  for (const url of ['https://chatgpt.com.evil.example/backend-api/conversations',
    'https://example.com/backend-api/conversations', 'https://chatgpt.com/backend-api/me',
    'invalid']) {
    assert.equal(isTarget({method:'GET',url}), false);
  }
  assert.equal(isTarget({method:'POST',url:'https://chatgpt.com/backend-api/conversations'}), false);
});
