/* Run with node --test tests/editor_requests.test.js. No server or browser needed. */
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function harness(fetch) {
  const timers = new Map();
  let next = 0;
  const node = () => ({style: {}, dataset: {}, setAttribute() {}, append() {}, addEventListener() {}, querySelectorAll: () => []});
  const context = vm.createContext({fetch, AbortController, TypeError, setTimeout(fn) {
    const id = ++next; timers.set(id, fn); return id;
  }, clearTimeout(id) { timers.delete(id); },
    document: {createElement: node, head: node(), body: node(), addEventListener() {}},
    window: {addEventListener() {}}});
  const source = fs.readFileSync(path.join(__dirname, '../web/editor.js'), 'utf8');
  // Expose only the real transport functions in this isolated test context.
  vm.runInContext(source.replace(/\}\)\(\);\s*$/, 'globalThis.transport = {request, stopPolling, pendingRequests};})();'), context);
  return {...context.transport, timers};
}

test('closing a view aborts its request and releases timers', async () => {
  let signal;
  const h = harness((url, options) => new Promise((resolve, reject) => {
    signal = options.signal;
    signal.addEventListener('abort', () => reject(new Error('aborted')));
  }));
  const result = h.request('/projects');
  assert.equal(h.pendingRequests.size, 1);
  h.stopPolling();
  await assert.rejects(result, error => error.constructor.name === 'StaleView');
  assert.equal(signal.aborted, true);
  assert.equal(h.pendingRequests.size, 0);
  assert.equal(h.timers.size, 0);
});

test('a late reply cannot enter a new view even when transport ignores abort', async () => {
  let reply;
  const h = harness(() => new Promise(resolve => { reply = resolve; }));
  const result = h.request('/projects/A/commands', {base_revision: 2});
  h.stopPolling();
  reply({ok: true, json: async () => ({revision: 3})});
  await assert.rejects(result, error => error.constructor.name === 'StaleView');
  assert.equal(h.timers.size, 0);
});

test('uncertain writes explain recovery instead of claiming they failed to save', async () => {
  const h = harness(async () => { throw new TypeError('Network error'); });
  await assert.rejects(h.request('/projects/A/commands', {}), /server may still finish.*Reload/);
  assert.equal(h.pendingRequests.size, 0);
});

test('timeouts release the UI request and give recovery instructions', async () => {
  const h = harness((url, options) => new Promise((resolve, reject) => {
    options.signal.addEventListener('abort', () => reject(new Error('aborted')));
  }));
  const result = h.request('/projects/A/commands', {});
  [...h.timers.values()][0]();
  await assert.rejects(result, /server may still finish/);
  assert.equal(h.pendingRequests.size, 0);
  assert.equal(h.timers.size, 0);
});

test('malformed responses and API conflicts retain actionable errors', async () => {
  const malformed = harness(async () => ({ok: true, json: async () => { throw new Error('bad JSON'); }}));
  await assert.rejects(malformed.request('/projects'), /unreadable response/);
  const conflict = harness(async () => ({ok: false, json: async () => ({detail: 'Reload revision 3.'})}));
  await assert.rejects(conflict.request('/projects/A/commands', {}), /Reload revision 3/);
});

test('successful writes preserve request payload and clear request state', async () => {
  let sent;
  const h = harness(async (url, options) => {
    sent = {url, options};
    return {ok: true, json: async () => ({revision: 4})};
  });
  const body = {op_id: 'unique-save', base_revision: 3, ops: []};
  assert.equal((await h.request('/projects/A/commands', body)).revision, 4);
  assert.deepEqual(JSON.parse(sent.options.body), body);
  assert.equal(sent.options.method, 'POST');
  assert.equal(sent.url, '/api/editor/projects/A/commands');
  assert.equal(h.pendingRequests.size, 0);
  assert.equal(h.timers.size, 0);
});
