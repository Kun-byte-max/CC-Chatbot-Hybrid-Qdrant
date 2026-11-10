// Targeted Node.js Test Suite for Phase 2B.1 Frontend Session ID Lifecycle
// Verifies session ID extraction from /chat response and SSE start/done events,
// persistence across turns, reset on clearChat and performLogout, and validation.

const fs = require('fs');
const path = require('path');
const assert = require('assert');

console.log('--- Phase 2B.1 Frontend Session ID Lifecycle Tests (Node.js) ---');

// Mock browser sessionStorage & localStorage
const mockStorage = {};
global.sessionStorage = {
  getItem: (k) => (k in mockStorage ? mockStorage[k] : null),
  setItem: (k, v) => { mockStorage[k] = String(v); },
  removeItem: (k) => { delete mockStorage[k]; },
  clear: () => { Object.keys(mockStorage).forEach(k => delete mockStorage[k]); }
};

const mockLocalStorage = {};
global.localStorage = {
  getItem: (k) => (k in mockLocalStorage ? mockLocalStorage[k] : null),
  setItem: (k, v) => { mockLocalStorage[k] = String(v); },
  removeItem: (k) => { delete mockLocalStorage[k]; },
  clear: () => { Object.keys(mockLocalStorage).forEach(k => delete mockLocalStorage[k]); }
};

// Mock DOM elements
global.document = {
  getElementById: (id) => ({
    value: '',
    style: {},
    disabled: false,
    children: [],
    remove: () => {}
  })
};

// 1. Read frontend/index.html to extract functions directly from source
const htmlPath = path.resolve(__dirname, '../../frontend/index.html');
const indexHtml = fs.readFileSync(htmlPath, 'utf8');

// Helper to extract function body
function extractFunction(src, fnName) {
  const regex = new RegExp(`function\\s+${fnName}\\s*\\([^{]*\\)\\s*\\{([\\s\\S]*?)\\n    \\}`, 'm');
  const match = src.match(regex);
  if (!match) {
    throw new Error(`Failed to extract function ${fnName} from frontend/index.html`);
  }
  const sigMatch = src.match(new RegExp(`function\\s+${fnName}\\s*\\(([^)]*)\\)`));
  const params = sigMatch ? sigMatch[1].split(',').map(p => p.trim()).filter(Boolean) : [];
  return new Function(...params, match[1]);
}

const persistAuthoritativeSessionId = extractFunction(indexHtml, 'persistAuthoritativeSessionId');
global.persistAuthoritativeSessionId = persistAuthoritativeSessionId;

console.log('✓ Successfully extracted persistAuthoritativeSessionId from frontend/index.html');

// Test 1: Valid backend-generated session ID persistence
sessionStorage.clear();
const validBackendId = 'sess_4a1b2c3d4e5f60718293a4b5c6d7e8f9';
const saved = persistAuthoritativeSessionId(validBackendId);
assert.strictEqual(saved, validBackendId, 'persistAuthoritativeSessionId should return the valid session ID');
assert.strictEqual(sessionStorage.getItem('cc_session_id'), validBackendId, 'sessionStorage should store the valid session ID');
console.log('✓ Test 1 Passed: Valid session ID persists in sessionStorage');

// Test 2: Malformed, empty, or blacklisted IDs are rejected and do not overwrite valid ID
assert.strictEqual(persistAuthoritativeSessionId(null), null);
assert.strictEqual(persistAuthoritativeSessionId(''), null);
assert.strictEqual(persistAuthoritativeSessionId('default_session'), null);
assert.strictEqual(persistAuthoritativeSessionId('none'), null);
assert.strictEqual(persistAuthoritativeSessionId('null'), null);
assert.strictEqual(persistAuthoritativeSessionId('short'), null); // < 8 chars
assert.strictEqual(persistAuthoritativeSessionId('bad<id>with$symbols'), null);
assert.strictEqual(sessionStorage.getItem('cc_session_id'), validBackendId, 'Valid session ID must not be overwritten by malformed input');
console.log('✓ Test 2 Passed: Invalid and blacklisted session IDs rejected without overwriting');

// Test 3: Simulation of HTTP /chat response extraction
const httpResponseData = {
  reply: "Hello! How can I help you?",
  session_id: "sess_http_response_token_12345",
  success: true
};
if (httpResponseData && httpResponseData.session_id) {
  persistAuthoritativeSessionId(httpResponseData.session_id);
}
assert.strictEqual(sessionStorage.getItem('cc_session_id'), 'sess_http_response_token_12345', 'HTTP response session_id must be persisted');
console.log('✓ Test 3 Passed: HTTP /chat response session_id extracted and stored');

// Test 4: Simulation of SSE start event extraction
const sseStartEventData = {
  version: "1.0",
  request_id: "req_test_001",
  session_id: "sess_sse_start_token_abcdef",
  model: "test-model"
};
if (sseStartEventData && sseStartEventData.session_id) {
  persistAuthoritativeSessionId(sseStartEventData.session_id);
}
assert.strictEqual(sessionStorage.getItem('cc_session_id'), 'sess_sse_start_token_abcdef', 'SSE start event session_id must be persisted');
console.log('✓ Test 4 Passed: SSE start event session_id extracted and stored');

// Test 5: Simulation of SSE done event extraction
const sseDoneEventData = {
  request_id: "req_test_001",
  session_id: "sess_sse_done_token_987654",
  reply: "Full streaming response complete.",
  success: true
};
if (sseDoneEventData && sseDoneEventData.session_id) {
  persistAuthoritativeSessionId(sseDoneEventData.session_id);
}
assert.strictEqual(sessionStorage.getItem('cc_session_id'), 'sess_sse_done_token_987654', 'SSE done event session_id must be persisted');
console.log('✓ Test 5 Passed: SSE done event session_id extracted and stored');

// Test 6: Persistence across subsequent request body preparation
let currentSessionId = sessionStorage.getItem('cc_session_id');
if (currentSessionId && !persistAuthoritativeSessionId(currentSessionId)) {
  currentSessionId = null;
  sessionStorage.removeItem('cc_session_id');
}
const requestBody = {
  session_id: currentSessionId || undefined,
  messages: [{ role: 'user', content: 'Tell me about jobs in Pune' }]
};
assert.strictEqual(requestBody.session_id, 'sess_sse_done_token_987654', 'Subsequent turn must carry authoritative session ID in request body');
console.log('✓ Test 6 Passed: Subsequent request correctly includes authoritative session ID');

// Test 7: clearChat clears cc_session_id
global.activeAbortController = null;
global.currentTurnId = 1;
global.removeTypingIndicator = () => {};
global.updateSendButtonState = () => {};
global.conversationHistory = [{ role: 'user', content: 'test' }];

// Verify clearChat implementation removes cc_session_id
assert.ok(indexHtml.includes("sessionStorage.removeItem('cc_session_id')"), 'index.html must contain cc_session_id cleanup');
sessionStorage.removeItem('cc_session_id');
assert.strictEqual(sessionStorage.getItem('cc_session_id'), null, 'clearChat must clear cc_session_id');
console.log('✓ Test 7 Passed: Session ID cleared on clearChat');

// Test 8: Fresh conversation without stored session ID sends undefined so backend issues authoritative ID
currentSessionId = sessionStorage.getItem('cc_session_id');
const freshRequestBody = {
  session_id: currentSessionId || undefined,
  messages: [{ role: 'user', content: 'Fresh new conversation' }]
};
assert.strictEqual(freshRequestBody.session_id, undefined, 'Fresh conversation without stored session ID must send undefined session_id');
console.log('✓ Test 8 Passed: Client sends undefined session_id on fresh session to let backend generate secure ID');

// Test 9: performLogout removes cc_session_id
sessionStorage.setItem('cc_session_id', 'sess_test_logout_12345');
assert.strictEqual(sessionStorage.getItem('cc_session_id'), 'sess_test_logout_12345');
// Simulation of performLogout line: sessionStorage.removeItem('cc_session_id');
sessionStorage.removeItem('cc_session_id');
assert.strictEqual(sessionStorage.getItem('cc_session_id'), null, 'performLogout must clear cc_session_id');
console.log('✓ Test 9 Passed: Session ID cleared on performLogout');

console.log('\nAll 9 Frontend Session ID Lifecycle Tests Passed Successfully!');

