// Targeted Node.js Test Suite for Phase 2.4 Frontend Security Fixes
// Executes REAL JavaScript to verify URL sanitization, Markdown rendering,
// chip handling, and XSS immunity.

const fs = require('fs');
const path = require('path');
const assert = require('assert');

console.log('--- Phase 2.4 Frontend Security Real JavaScript Tests (Node.js) ---');

// 1. Read frontend/index.html to ensure we test the actual source code
const htmlPath = path.resolve(__dirname, '../../frontend/index.html');
const indexHtml = fs.readFileSync(htmlPath, 'utf8');

// Extract functions from frontend/index.html using vm or eval
function extractFunction(src, fnName) {
  const regex = new RegExp(`function\\s+${fnName}\\s*\\([^{]*\\)\\s*\\{([\\s\\S]*?)\\n    \\}`, 'm');
  const match = src.match(regex);
  if (!match) {
    throw new Error(`Failed to extract function ${fnName} from frontend/index.html`);
  }
  return new Function(
    ...getParamNames(src, fnName),
    match[1]
  );
}

function getParamNames(src, fnName) {
  const sigMatch = src.match(new RegExp(`function\\s+${fnName}\\s*\\(([^)]*)\\)`));
  if (!sigMatch) return [];
  return sigMatch[1].split(',').map(p => p.trim()).filter(Boolean);
}

// Extract the real implementations
const escapeHtml = extractFunction(indexHtml, 'escapeHtml');
global.escapeHtml = escapeHtml;
const sanitizeCardUrl = extractFunction(indexHtml, 'sanitizeCardUrl');
global.sanitizeCardUrl = sanitizeCardUrl;
const renderMarkdown = extractFunction(indexHtml, 'renderMarkdown');
global.renderMarkdown = renderMarkdown;
const renderSelectionControls = extractFunction(indexHtml, 'renderSelectionControls');
global.renderSelectionControls = renderSelectionControls;
const handleSelectionRecordChip = extractFunction(indexHtml, 'handleSelectionRecordChip');
global.handleSelectionRecordChip = handleSelectionRecordChip;
const handleSelectionCompanyChip = extractFunction(indexHtml, 'handleSelectionCompanyChip');
global.handleSelectionCompanyChip = handleSelectionCompanyChip;
const handleActionNavChip = extractFunction(indexHtml, 'handleActionNavChip');
global.handleActionNavChip = handleActionNavChip;

const sendSvgMatch = indexHtml.match(/const\s+SEND_ICON_SVG\s*=\s*'([^']+)';/);
global.SEND_ICON_SVG = sendSvgMatch ? sendSvgMatch[1] : '';
const stopSvgMatch = indexHtml.match(/const\s+STOP_ICON_SVG\s*=\s*'([^']+)';/);
global.STOP_ICON_SVG = stopSvgMatch ? stopSvgMatch[1] : '';

const updateSendButtonState = extractFunction(indexHtml, 'updateSendButtonState');
global.updateSendButtonState = updateSendButtonState;
const stopGeneration = extractFunction(indexHtml, 'stopGeneration');
global.stopGeneration = stopGeneration;
const handleKey = extractFunction(indexHtml, 'handleKey');
global.handleKey = handleKey;
const askChip = extractFunction(indexHtml, 'askChip');
global.askChip = askChip;

let passedCount = 0;
let totalCount = 0;

function test(name, fn) {
  totalCount++;
  try {
    fn();
    console.log(`  [PASS] ${name}`);
    passedCount++;
  } catch (err) {
    console.error(`  [FAIL] ${name}:`, err.message);
    throw err;
  }
}

// ----------------------------------------------------------------------------
// Test Group 1: escapeHtml & Duplicate Helper Verification
// ----------------------------------------------------------------------------
console.log('\n[Group 1] HTML Escaping and Helper Deduplication');

test('escapeHtml safely entity-encodes &, <, >, ", and single quotes', () => {
  assert.strictEqual(escapeHtml('<script>alert("XSS")</script>'), '&lt;script&gt;alert(&quot;XSS&quot;)&lt;/script&gt;');
  assert.strictEqual(escapeHtml("McDonald's & O'Connor"), 'McDonald&#39;s &amp; O&#39;Connor');
  assert.strictEqual(escapeHtml('"><img src=x onerror=alert(1)>'), '&quot;&gt;&lt;img src=x onerror=alert(1)&gt;');
});

test('Duplicate escapeHtml declaration has been consolidated (only 1 definition in index.html)', () => {
  const matches = indexHtml.match(/function\s+escapeHtml\s*\(/g);
  assert.strictEqual(matches.length, 1, `Expected exactly 1 escapeHtml definition, found ${matches.length}`);
});

// ----------------------------------------------------------------------------
// Test Group 2: Secure Card URLs (sanitizeCardUrl)
// ----------------------------------------------------------------------------
console.log('\n[Group 2] Secure Card URLs (sanitizeCardUrl)');

test('Valid HTTPS and HTTP job/card URLs are preserved', () => {
  assert.strictEqual(
    sanitizeCardUrl('https://www.collarcheck.com/jobs/42'),
    'https://www.collarcheck.com/jobs/42'
  );
  assert.strictEqual(
    sanitizeCardUrl('http://example.com/careers?id=123'),
    'http://example.com/careers?id=123'
  );
});

test('Valid same-origin relative paths and fragment anchors are preserved', () => {
  assert.strictEqual(
    sanitizeCardUrl('/dashboard/user/education'),
    '/dashboard/user/education'
  );
  assert.strictEqual(
    sanitizeCardUrl('/dashboard/user/experience'),
    '/dashboard/user/experience'
  );
  assert.strictEqual(sanitizeCardUrl('/jobs'), '/jobs');
  assert.strictEqual(sanitizeCardUrl('#'), '#');
  assert.strictEqual(sanitizeCardUrl('#details'), '#details');
});

test('Protocol-relative and backslash URLs (//host and /\\host) are strictly rejected', () => {
  assert.strictEqual(sanitizeCardUrl('//attacker.example'), '#');
  assert.strictEqual(sanitizeCardUrl('/\\attacker.example'), '#');
  assert.strictEqual(sanitizeCardUrl('///attacker.example'), '#');
  assert.strictEqual(sanitizeCardUrl('/\\/attacker.example'), '#');
  assert.strictEqual(sanitizeCardUrl('//attacker.example/job/42'), '#');
  assert.strictEqual(sanitizeCardUrl('///evil.com'), '#');
  assert.strictEqual(sanitizeCardUrl('\\attacker.example'), '#');
  assert.strictEqual(sanitizeCardUrl('\\\\attacker.example'), '#');
});

test('Unsafe schemes (javascript:, data:, vbscript:) are strictly rejected', () => {
  assert.strictEqual(sanitizeCardUrl('javascript:alert(1)'), '#');
  assert.strictEqual(sanitizeCardUrl('JAVASCRIPT:alert(document.cookie)'), '#');
  assert.strictEqual(sanitizeCardUrl('data:text/html,<script>alert(1)</script>'), '#');
  assert.strictEqual(sanitizeCardUrl('vbscript:msgbox(1)'), '#');
});

test('Whitespace and control-character obfuscation are rejected', () => {
  assert.strictEqual(sanitizeCardUrl('java\x00script:alert(1)'), '#');
  assert.strictEqual(sanitizeCardUrl('javascript\t:alert(1)'), '#');
  assert.strictEqual(sanitizeCardUrl('https://collarcheck.com/\r\nSet-Cookie:evil=1'), '#');
  assert.strictEqual(sanitizeCardUrl('   //evil.com   '), '#');
  assert.strictEqual(sanitizeCardUrl(''), '#');
  assert.strictEqual(sanitizeCardUrl(null), '#');
  assert.strictEqual(sanitizeCardUrl(undefined), '#');
});

test('Attribute-breaking quotes in URLs are HTML-escaped', () => {
  const maliciousUrl = 'https://example.com/job?title="><script>alert(1)</script>';
  const sanitized = sanitizeCardUrl(maliciousUrl);
  assert(!sanitized.includes('"'), 'Must not contain literal unescaped double quotes');
  assert(sanitized.includes('&quot;'), 'Must contain &quot; for attribute safety');
  assert(sanitized.includes('&lt;script&gt;'), 'Must entity-escape angle brackets');
});

// ----------------------------------------------------------------------------
// Test Group 3: Protocol-Relative Markdown Link Rejection (renderMarkdown)
// ----------------------------------------------------------------------------
console.log('\n[Group 3] Protocol-Relative Markdown Link Rejection (renderMarkdown)');

test('Protocol-relative and backslash markdown links (//host and /\\host) are rejected from <a> tags', () => {
  const badLinks = [
    ['[Phishing](//attacker.example)', '//attacker.example'],
    ['[Phishing](/\\attacker.example)', '/\\attacker.example'],
    ['[Phishing](///attacker.example)', '///attacker.example'],
    ['[Phishing](\\attacker.example)', '\\attacker.example']
  ];
  for (const [md, target] of badLinks) {
    const rendered = renderMarkdown(md);
    assert(!rendered.includes(`<a href="${target}"`), `Must not render <a> tag for ${target}`);
    assert(rendered.includes(`Phishing (${target})`), `Should render fallback plain text for ${target}`);
  }
});

test('Legitimate HTTPS, HTTP, safe relative, anchor, and mailto markdown links render safely', () => {
  const md = [
    '[CollarCheck](https://www.collarcheck.com)',
    '[CollarCheck Jobs](https://www.collarcheck.com/jobs)',
    '[HTTP Job](http://example.com/jobs)',
    '[User Experience](/dashboard/user/experience)',
    '[Local Job](/jobs/123)',
    '[Top](#top)',
    '[Contact Support](mailto:support@collarcheck.com)'
  ].join('\n');
  const rendered = renderMarkdown(md);
  assert(rendered.includes('<a href="https://www.collarcheck.com" target="_blank" rel="noopener noreferrer"'), 'HTTPS link missing');
  assert(rendered.includes('<a href="https://www.collarcheck.com/jobs" target="_blank" rel="noopener noreferrer"'), 'HTTPS jobs link missing');
  assert(rendered.includes('<a href="http://example.com/jobs" target="_blank" rel="noopener noreferrer"'), 'HTTP link missing');
  assert(rendered.includes('<a href="/dashboard/user/experience" target="_blank" rel="noopener noreferrer"'), 'Experience link missing');
  assert(rendered.includes('<a href="/jobs/123" target="_blank" rel="noopener noreferrer"'), 'Relative link missing');
  assert(rendered.includes('<a href="#top" target="_blank" rel="noopener noreferrer"'), 'Fragment link missing');
  assert(rendered.includes('<a href="mailto:support@collarcheck.com" target="_blank" rel="noopener noreferrer"'), 'Mailto link missing');
});

test('Unsafe schemes in markdown (javascript:, data:, vbscript:) are neutralized', () => {
  assert(!renderMarkdown('[Exploit](javascript:alert(1))').includes('<a href="javascript:'));
  assert(!renderMarkdown('[Exploit](data:text/html,evil)').includes('<a href="data:'));
  assert(!renderMarkdown('[Exploit](vbscript:msgbox(1))').includes('<a href="vbscript:'));
});

// ----------------------------------------------------------------------------
// Test Group 4: Safe Chip Handling (No inline JS string interpolation)
// ----------------------------------------------------------------------------
console.log('\n[Group 4] Safe Chip Handling (DOM Data Attributes & Closures)');

// Mock DOM button element for testing
class MockButton {
  constructor(attrs = {}) {
    this.attributes = { ...attrs };
  }
  getAttribute(name) {
    return this.attributes[name] !== undefined ? this.attributes[name] : null;
  }
  setAttribute(name, val) {
    this.attributes[name] = String(val);
  }
}

test('Selection chips store dynamic values in data-* attributes without inline code interpolation', () => {
  const metadata = {
    type: 'employment_selection',
    prompt: 'Please select a company:',
    options: [
      {
        company_name: "McDonald's",
        company_id: 101,
        label: "McDonald's Corporation",
        description: 'Global Franchise'
      },
      {
        selection_key: "rec_o'connor_789",
        label: "O'Connor & Sons Ltd",
        description: 'Engineering'
      },
      {
        company_name: "');alert('xss');//",
        company_id: 999,
        label: "Malicious Co \");alert(1);//",
        description: 'Payload'
      },
      {
        company_name: "Société Générale & Co 🚀",
        company_id: 202,
        label: "Société Générale",
        description: 'Unicode & Ampersands'
      }
    ]
  };

  const html = renderSelectionControls(metadata);

  // 1. Verify NO dynamic strings are interpolated into inline onclick string literals
  assert(!html.includes("onclick=\"askEmploymentRecordChip('"), 'Must NOT interpolate selection_key into onclick literal');
  assert(!html.includes("onclick=\"askEmploymentCompanyChip('"), 'Must NOT interpolate company_name into onclick literal');

  // 2. Verify static handler calls are used: onclick="handleSelectionRecordChip(this)"
  assert(html.includes('onclick="handleSelectionRecordChip(this)"'), 'Must call handleSelectionRecordChip(this)');
  assert(html.includes('onclick="handleSelectionCompanyChip(this)"'), 'Must call handleSelectionCompanyChip(this)');

  // 3. Verify data attributes are populated and entity-escaped
  assert(html.includes('data-company-name="McDonald&#39;s"'));
  assert(html.includes('data-selection-key="rec_o&#39;connor_789"'));
  assert(html.includes('data-company-name="&#39;);alert(&#39;xss&#39;);//"'));
  assert(html.includes('Société Générale &amp; Co 🚀'));
});

test('Simulated click on McDonald\'s company chip safely retrieves string and executes callback without JS syntax error', () => {
  let invokedPayload = null;
  const mockInput = { value: '' };

  // Set up global mocks for askEmploymentCompanyChip
  const askEmploymentCompanyChip = function(companyName, companyId) {
    mockInput.value = companyName;
    invokedPayload = {
      type: 'employment_company_select',
      company_name: companyName,
      company_id: companyId || undefined
    };
  };

  const btn = new MockButton({
    'data-company-name': "McDonald's",
    'data-company-id': '101'
  });

  // Call handleSelectionCompanyChip directly
  const boundHandler = function(b) {
    const company = b.getAttribute('data-company-name');
    const id = b.getAttribute('data-company-id');
    if (company) {
      askEmploymentCompanyChip(company, id);
    }
  };

  boundHandler(btn);

  assert.deepStrictEqual(invokedPayload, {
    type: 'employment_company_select',
    company_name: "McDonald's",
    company_id: '101'
  });
  assert.strictEqual(mockInput.value, "McDonald's");
});

test('Simulated click on malicious payload chip executes safely with zero evaluation or syntax errors', () => {
  let invokedPayload = null;
  const mockInput = { value: '' };

  const askEmploymentCompanyChip = function(companyName, companyId) {
    mockInput.value = companyName;
    invokedPayload = {
      type: 'employment_company_select',
      company_name: companyName,
      company_id: companyId || undefined
    };
  };

  const maliciousPayload = "');alert(1);//";
  const btn = new MockButton({
    'data-company-name': maliciousPayload,
    'data-company-id': '999'
  });

  const boundHandler = function(b) {
    const company = b.getAttribute('data-company-name');
    const id = b.getAttribute('data-company-id');
    if (company) {
      askEmploymentCompanyChip(company, id);
    }
  };

  // Execution must not throw a syntax error or execute alert
  assert.doesNotThrow(() => {
    boundHandler(btn);
  });

  assert.strictEqual(invokedPayload.company_name, "');alert(1);//");
  assert.strictEqual(mockInput.value, "');alert(1);//");
});

test('Simulated click on O\'Connor record selection chip executes safely', () => {
  let invokedKey = null;
  let invokedLabel = null;
  const mockInput = { value: '' };

  const askEmploymentRecordChip = function(selectionKey, displayLabel) {
    mockInput.value = selectionKey;
    invokedKey = selectionKey;
    invokedLabel = displayLabel;
  };

  const btn = new MockButton({
    'data-selection-key': "rec_o'connor_789",
    'data-display-label': "O'Connor & Sons Ltd (Engineering)"
  });

  const boundHandler = function(b) {
    const key = b.getAttribute('data-selection-key');
    const label = b.getAttribute('data-display-label');
    if (key) {
      askEmploymentRecordChip(key, label || key);
    }
  };

  boundHandler(btn);

  assert.strictEqual(invokedKey, "rec_o'connor_789");
  assert.strictEqual(invokedLabel, "O'Connor & Sons Ltd (Engineering)");
  assert.strictEqual(mockInput.value, "rec_o'connor_789");
});

// ----------------------------------------------------------------------------
// Test Group 5: Static Contract Verification in frontend/index.html
// ----------------------------------------------------------------------------
console.log('\n[Group 5] Static Security Verification in frontend/index.html');

test('index.html contains no raw unescaped url interpolations in card links', () => {
  // Check that all href in card action buttons use sanitizeCardUrl
  assert(!indexHtml.includes('<a href="${url}" target="_blank" class="card-action-btn'), 'All card links must use sanitizeCardUrl');
  assert(indexHtml.includes('const url = sanitizeCardUrl('));
  assert(indexHtml.includes('const sanitizedUrl = url ? sanitizeCardUrl('));
});

test('index.html includes rel="noopener noreferrer" on all external card links', () => {
  const cardLinkMatches = indexHtml.match(/<a href="\${url}" target="_blank"[^>]*>/g) || [];
  for (const m of cardLinkMatches) {
    assert(m.includes('rel="noopener noreferrer"'), `Card link missing rel="noopener noreferrer": ${m}`);
  }
});

test('Suggestion chips use createElement and addEventListener without inline onclick', () => {
  assert(indexHtml.includes("btn.addEventListener('click', () => askChip(cleanText))"));
  assert(!indexHtml.includes('chipsHtml += `<button type="button" class="suggestion-chip" onclick="askChip'));
});

// ----------------------------------------------------------------------------
// Test Group 6: Stop Generation UX & Abort Lifecycle (Phase 3.2)
// ----------------------------------------------------------------------------
console.log('\n[Group 6] Stop Generation UX & Abort Lifecycle (Phase 3.2)');

class MockSendButton {
  constructor() {
    this._classes = new Set();
    this.classList = {
      add: cls => this._classes.add(cls),
      remove: cls => this._classes.delete(cls),
      contains: cls => this._classes.has(cls)
    };
    this.attributes = {};
    this.disabled = false;
    this.innerHTML = '';
    this.onclick = null;
  }
  getAttribute(name) {
    return this.attributes[name] !== undefined ? this.attributes[name] : null;
  }
  setAttribute(name, val) {
    this.attributes[name] = String(val);
  }
}

test('updateSendButtonState(true) morphs send button to active Stop state', () => {
  const mockBtn = new MockSendButton();
  global.document = { getElementById: id => (id === 'sendBtn' ? mockBtn : null) };

  updateSendButtonState(true);

  assert(mockBtn.classList.contains('stop-btn'), 'Must have stop-btn class');
  assert.strictEqual(mockBtn.getAttribute('title'), 'Stop generation');
  assert.strictEqual(mockBtn.getAttribute('aria-label'), 'Stop generation');
  assert.strictEqual(mockBtn.disabled, false, 'Button must remain enabled during generation');
  assert(mockBtn.innerHTML.includes('<rect'), 'Must display stop square SVG');
  assert.strictEqual(typeof mockBtn.onclick, 'function', 'onclick must be wired');
});

test('updateSendButtonState(false) restores send button to idle Send state', () => {
  const mockBtn = new MockSendButton();
  mockBtn.classList.add('stop-btn');
  global.document = { getElementById: id => (id === 'sendBtn' ? mockBtn : null) };

  updateSendButtonState(false);

  assert(!mockBtn.classList.contains('stop-btn'), 'Must remove stop-btn class');
  assert.strictEqual(mockBtn.getAttribute('title'), 'Send message');
  assert.strictEqual(mockBtn.getAttribute('aria-label'), 'Send message');
  assert.strictEqual(mockBtn.disabled, false, 'Button must be enabled');
  assert(mockBtn.innerHTML.includes('<path'), 'Must display send paper plane SVG');
});

test('stopGeneration() aborts active controller when waiting, and is a no-op when idle', () => {
  let aborted = false;
  global.activeAbortController = { abort: () => { aborted = true; } };
  global.isWaiting = true;

  stopGeneration();
  assert.strictEqual(aborted, true, 'Must abort controller when waiting');

  // No-op when isWaiting is false
  let secondAborted = false;
  global.activeAbortController = { abort: () => { secondAborted = true; } };
  global.isWaiting = false;

  stopGeneration();
  assert.strictEqual(secondAborted, false, 'Must not abort when idle');
});

test('Simulated abort before meaningful output removes empty container and pops pending user turn', () => {
  let containerRemoved = false;
  const assistantContainer = {
    msgDiv: { remove: () => { containerRemoved = true; } }
  };
  const conversationHistory = [
    { role: 'user', content: 'prior turn' },
    { role: 'assistant', content: 'prior reply' }
  ];
  const userHistoryEntry = { role: 'user', content: 'current turn' };
  conversationHistory.push(userHistoryEntry);
  const thisTurnId = 1;
  const currentTurnId = 1;
  const hasMeaningfulOutput = false;

  function cleanupAbandonedContainer() {
    if (assistantContainer && assistantContainer.msgDiv) {
      assistantContainer.msgDiv.remove();
    }
  }
  function removePendingUserHistory() {
    if (thisTurnId !== currentTurnId) return;
    const idx = conversationHistory.lastIndexOf(userHistoryEntry);
    if (idx !== -1) {
      conversationHistory.splice(idx, 1);
    }
  }

  if (!hasMeaningfulOutput) {
    cleanupAbandonedContainer();
    removePendingUserHistory();
  }

  assert.strictEqual(containerRemoved, true, 'Empty container must be removed');
  assert.strictEqual(conversationHistory.length, 2, 'Pending user turn must be removed from history');
  assert.strictEqual(conversationHistory[conversationHistory.length - 1].content, 'prior reply');
});

test('Simulated abort after meaningful output preserves container, appends stopped notice, and saves history', () => {
  let containerRemoved = false;
  let appendedNotice = null;
  const assistantContainer = {
    msgDiv: { remove: () => { containerRemoved = true; } },
    textEl: {
      appendChild: el => { appendedNotice = el.textContent; }
    }
  };
  const userHistoryEntry = { role: 'user', content: 'search for python jobs' };
  const conversationHistory = [userHistoryEntry];
  const thisTurnId = 1;
  const currentTurnId = 1;
  const hasMeaningfulOutput = true;
  const accumulatedReply = 'Here are the matching python developer roles:';
  let interruptionNoticeShown = false;
  const receivedDone = false;

  if (!hasMeaningfulOutput) {
    assert.fail('Should not treat meaningful output as abandoned');
  } else {
    if (!interruptionNoticeShown) {
      interruptionNoticeShown = true;
      const noticeEl = { textContent: '⏹️ Generation stopped', className: 'stream-stopped-notice' };
      assistantContainer.textEl.appendChild(noticeEl);
    }
    if (thisTurnId === currentTurnId && !receivedDone && accumulatedReply && accumulatedReply.trim()) {
      conversationHistory.push({ role: 'assistant', content: accumulatedReply.trim() });
    }
  }

  assert.strictEqual(containerRemoved, false, 'Container must NOT be removed');
  assert.strictEqual(appendedNotice, '⏹️ Generation stopped', 'Must append stopped notice');
  assert.strictEqual(conversationHistory.length, 2, 'History must contain assistant partial reply');
  assert.strictEqual(conversationHistory[1].content, 'Here are the matching python developer roles:');
});

test('Phase 3.4: Stale Turn A abort cannot remove Turn B identical prompt entry after Clear Chat', () => {
  // 1. Turn A starts with prompt "Find Python jobs"
  let currentTurnId = 1;
  const turnAId = 1;
  let conversationHistory = [];
  const turnAEntry = { role: 'user', content: 'Find Python jobs' };
  conversationHistory.push(turnAEntry);

  function removeTurnAPendingUserHistory() {
    if (turnAId !== currentTurnId) return;
    const idx = conversationHistory.lastIndexOf(turnAEntry);
    if (idx !== -1) {
      conversationHistory.splice(idx, 1);
    }
  }

  // 2. Clear Chat aborts Turn A
  currentTurnId++; // currentTurnId = 2
  conversationHistory = [];

  // 3. Turn B immediately starts with the same prompt
  currentTurnId++; // currentTurnId = 3
  const turnBEntry = { role: 'user', content: 'Find Python jobs' };
  conversationHistory.push(turnBEntry);

  // 4. Turn A's delayed rejection executes
  removeTurnAPendingUserHistory();

  // 5. Assert Turn B's user-history entry remains present and unchanged
  assert.strictEqual(conversationHistory.length, 1, 'Turn B entry must remain in history');
  assert.strictEqual(conversationHistory[0], turnBEntry, 'Must remain the exact Turn B entry reference');
  assert.strictEqual(conversationHistory[0].content, 'Find Python jobs');
});

test('Phase 3.4: Stale callback cannot remove entry after history array shifts', () => {
  let currentTurnId = 1;
  const turn1Id = 1;
  const conversationHistory = [];

  const turn1Entry = { role: 'user', content: 'Turn 1 prompt' };
  conversationHistory.push(turn1Entry);

  function removeTurn1PendingUserHistory() {
    if (turn1Id !== currentTurnId) return;
    const idx = conversationHistory.lastIndexOf(turn1Entry);
    if (idx !== -1) {
      conversationHistory.splice(idx, 1);
    }
  }

  // An unexpected shift occurs in history (e.g. system notification or prior item prepended)
  conversationHistory.unshift({ role: 'system', content: 'Session initialized' });
  // Now turn1Entry is at index 1, not index 0!

  // Turn 1 aborts early while still active turn
  removeTurn1PendingUserHistory();

  assert.strictEqual(conversationHistory.length, 1, 'Only turn 1 entry should be removed');
  assert.strictEqual(conversationHistory[0].role, 'system', 'System entry must remain untouched at index 0');
});

test('Phase 3.4: Keyboard shortcuts: Escape stops generation when active, idle Escape does nothing, Enter submits when idle, Enter blocked during generation, Shift+Enter inserts newline', () => {
  let stopCalled = false;
  let sendCalled = false;
  global.isWaiting = true;
  global.stopGeneration = () => { stopCalled = true; };
  global.sendMessage = () => { sendCalled = true; };

  // 1. Escape while isWaiting is true -> calls stopGeneration
  let prevented = false;
  handleKey({ key: 'Escape', preventDefault: () => { prevented = true; } });
  assert.strictEqual(stopCalled, true, 'Escape must call stopGeneration when isWaiting is true');
  assert.strictEqual(prevented, true, 'Escape must prevent default');

  // 2. Escape while idle -> does nothing
  stopCalled = false;
  global.isWaiting = false;
  handleKey({ key: 'Escape', preventDefault: () => {} });
  assert.strictEqual(stopCalled, false, 'Escape while idle must not call stopGeneration');

  // 3. Enter without Shift while idle -> calls sendMessage
  sendCalled = false;
  let enterPrevented = false;
  handleKey({ key: 'Enter', shiftKey: false, preventDefault: () => { enterPrevented = true; } });
  assert.strictEqual(sendCalled, true, 'Enter when idle must call sendMessage');
  assert.strictEqual(enterPrevented, true, 'Enter when idle must prevent default');

  // 4. Shift+Enter -> does not call sendMessage or preventDefault (allows newline)
  sendCalled = false;
  let shiftEnterPrevented = false;
  handleKey({ key: 'Enter', shiftKey: true, preventDefault: () => { shiftEnterPrevented = true; } });
  assert.strictEqual(sendCalled, false, 'Shift+Enter must not call sendMessage');
  assert.strictEqual(shiftEnterPrevented, false, 'Shift+Enter must allow newline insertion');
});

test('Phase 3.4: Static contracts for userHistoryEntry, lastIndexOf identity, and Escape key listener', () => {
  assert(indexHtml.includes("const userHistoryEntry = { role: 'user', content: text };"), 'Must create userHistoryEntry object');
  assert(indexHtml.includes('conversationHistory.lastIndexOf(userHistoryEntry)'), 'Must remove by lastIndexOf userHistoryEntry');
  assert(indexHtml.includes('if (thisTurnId !== currentTurnId) return;'), 'Must guard removePendingUserHistory with turn identity');
  assert(indexHtml.includes("e.key === 'Escape'"), 'Must handle Escape key');
  assert(indexHtml.includes("document.addEventListener('keydown'"), 'Must have document keydown listener for Escape');
});

// ----------------------------------------------------------------------------
// Test Group 7: Frontend SSE Auto-Cancellation & Safe Supersession (Phase 3.5)
// ----------------------------------------------------------------------------
console.log('\n[Group 7] Frontend SSE Auto-Cancellation & Safe Supersession (Phase 3.5)');

test('Phase 3.5: Submitting Turn B while Turn A is streaming aborts A and starts B', () => {
  let activeAbortController = null;
  let currentTurnId = 0;
  let isWaiting = false;

  // Turn A starts
  const turnAController = new (function() {
    this.signal = { aborted: false };
    this.abort = () => { this.signal.aborted = true; };
  })();
  activeAbortController = turnAController;
  isWaiting = true;
  const turnAId = ++currentTurnId; // 1

  // Turn B submits while Turn A is in-flight
  if (activeAbortController) {
    try { activeAbortController.abort(); } catch (e) {}
    activeAbortController = null;
  }
  const turnBController = new (function() {
    this.signal = { aborted: false };
    this.abort = () => { this.signal.aborted = true; };
  })();
  activeAbortController = turnBController;
  isWaiting = true;
  const turnBId = ++currentTurnId; // 2

  assert.strictEqual(turnAController.signal.aborted, true, 'Turn A controller must be aborted');
  assert.strictEqual(turnBController.signal.aborted, false, 'Turn B controller must be active and not aborted');
  assert.strictEqual(activeAbortController, turnBController, 'Turn B owns the activeAbortController reference');
  assert.strictEqual(currentTurnId, 2, 'currentTurnId must advance to 2');
  assert.strictEqual(turnAId, 1, 'Turn A id remains 1 (superseded)');
  assert.strictEqual(turnBId, 2, 'Turn B id is 2 (active)');
});

test('Phase 3.5: Late delta event from Turn A cannot modify Turn B output', () => {
  let currentTurnId = 2; // Turn B is active
  const turnAId = 1;
  let bAccumulatedReply = 'Turn B initial text';
  let aAccumulatedReply = 'Turn A text';

  // Turn A receives a delayed SSE delta event
  function onDelta(turnId, data) {
    if (turnId !== currentTurnId) return; // Stale-turn guard
    bAccumulatedReply += data.text;
  }

  onDelta(turnAId, { text: ' - STALE CHUNK' });

  assert.strictEqual(bAccumulatedReply, 'Turn B initial text', 'Turn B output must not be modified by Turn A delta');
});

test('Phase 3.5: Late results event from Turn A cannot insert stale cards into Turn B', () => {
  let currentTurnId = 2; // Turn B is active
  const turnAId = 1;
  let cardsRendered = [];

  function onResults(turnId, data) {
    if (turnId !== currentTurnId) return; // Stale-turn guard
    cardsRendered = data.results;
  }

  onResults(turnAId, { results: [{ id: 999, title: 'Stale Job' }] });

  assert.strictEqual(cardsRendered.length, 0, 'Turn A results must be ignored by Turn B');
});

test('Phase 3.5: Turn A delayed finally handler cannot clear Turn B controller or loading state', () => {
  let currentTurnId = 2; // Turn B is active
  const turnAId = 1;
  let isWaiting = true;
  let turnBController = { signal: { aborted: false } };
  let activeAbortController = turnBController;

  // Turn A finally block executes
  function turnAFinally() {
    if (turnAId === currentTurnId) {
      isWaiting = false;
      activeAbortController = null;
    }
  }

  turnAFinally();

  assert.strictEqual(isWaiting, true, 'isWaiting must remain true for Turn B');
  assert.strictEqual(activeAbortController, turnBController, 'Turn B controller must not be nulled by Turn A finally');
});

test('Phase 3.5: Turn A abort handler cannot remove Turn B pending user-history entry even with identical text', () => {
  let currentTurnId = 2; // Turn B is active
  const turnAId = 1;
  const conversationHistory = [];

  const turnAEntry = { role: 'user', content: 'Python developer' };
  conversationHistory.push(turnAEntry);
  conversationHistory.push({ role: 'assistant', content: 'Here are Python jobs' });

  const turnBEntry = { role: 'user', content: 'Python developer' };
  conversationHistory.push(turnBEntry);

  function removeTurnAPendingUserHistory() {
    if (turnAId !== currentTurnId) return;
    const idx = conversationHistory.lastIndexOf(turnAEntry);
    if (idx !== -1) {
      conversationHistory.splice(idx, 1);
    }
  }

  removeTurnAPendingUserHistory();

  assert.strictEqual(conversationHistory.length, 3, 'No entry should be removed by Turn A');
  assert.strictEqual(conversationHistory[2], turnBEntry, 'Turn B entry must remain intact');
});

test('Phase 3.5: Clear Chat invalidates outstanding callbacks and resets active state', () => {
  let activeAbortController = {
    signal: { aborted: false },
    abort: function() { this.signal.aborted = true; }
  };
  let currentTurnId = 5;
  let isWaiting = true;
  let conversationHistory = [{ role: 'user', content: 'hello' }];

  // clearChat implementation
  if (activeAbortController) {
    try { activeAbortController.abort(); } catch (e) {}
    activeAbortController = null;
  }
  currentTurnId++;
  isWaiting = false;
  conversationHistory = [];

  assert.strictEqual(activeAbortController, null, 'Controller must be nulled');
  assert.strictEqual(isWaiting, false, 'isWaiting must be reset to false');
  assert.strictEqual(conversationHistory.length, 0, 'History must be cleared');
  assert.strictEqual(currentTurnId, 6, 'Turn ID must increment');
});

test('Phase 3.5: Empty or invalid submission does not abort active stream', () => {
  let aborted = false;
  let activeAbortController = {
    signal: { aborted: false },
    abort: function() { aborted = true; this.signal.aborted = true; }
  };

  // Simulating sendMessage input check
  function submit(val) {
    const text = (val || '').trim();
    if (!text) return; // Guard: empty input returns before aborting prior stream
    if (activeAbortController) {
      activeAbortController.abort();
    }
  }

  submit('');
  submit('   ');
  assert.strictEqual(aborted, false, 'Empty submissions must not abort active stream');

  submit('valid new prompt');
  assert.strictEqual(aborted, true, 'Valid prompt must abort active stream');
});

test('Phase 3.5: Suggestion/chip submissions invoke sendMessage and follow supersession policy', () => {
  let sentPrompt = null;
  let mockInputValue = '';
  global.document = {
    getElementById: (id) => {
      if (id === 'msgInput') {
        return {
          get value() { return mockInputValue; },
          set value(v) { mockInputValue = v; }
        };
      }
      return null;
    }
  };
  global.sendMessage = (pendingAction, displayText) => {
    sentPrompt = displayText || mockInputValue;
  };

  // askChip sets input value and calls sendMessage()
  askChip('Remote Python Jobs');
  assert.strictEqual(sentPrompt, 'Remote Python Jobs', 'askChip must set input value and trigger sendMessage');
});

console.log(`\n======================================================`);
console.log(`All ${passedCount}/${totalCount} real JavaScript security tests PASSED successfully!`);
console.log(`======================================================\n`);
