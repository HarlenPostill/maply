const { test } = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');

function worker({ reviewing = false, next = 'next', backlogOnly = false } = {}) {
  let session = { running: true, jobId: 'job', tabId: 7, shot: { id: 'old', url: 'old-url' }, reviewing };
  const messages = [], navigations = [], requests = [];
  let captures = 0;
  const context = vm.createContext({
    chrome: {
      storage: { local: {
        get: async (key) => key === 'session' ? { session: structuredClone(session) } : { backlogOnly },
        set: async (data) => { session = structuredClone(data.session); },
      } },
      tabs: {
        sendMessage: async (id, message) => { messages.push(message); return null; },
        update: async (id, data) => navigations.push(data.url),
        captureVisibleTab: async () => { captures++; },
      },
      runtime: { onMessage: { addListener() {} } },
      commands: { onCommand: { addListener() {} } },
    },
    fetch: async (url) => { requests.push(url); return { ok: true, json: async () => ({ shot: next ? { id: next, url: `${next}-url` } : null, progress: { done: 12, total: 20 } }) }; },
  });
  vm.runInContext(fs.readFileSync(require.resolve('../background.js'), 'utf8'), context);
  return { context, messages, navigations, requests, session: () => session, captures: () => captures };
}

test('sync advances completed current photo and stores shared count', async () => {
  const w = worker();
  assert.equal(await vm.runInContext('refresh()', w.context), true);
  assert.equal(w.session().shot.id, 'next');
  assert.equal(w.session().progress.done, 12);
  assert.deepEqual(w.navigations, ['next-url']);
});

test('sync updates counts without moving an unfinished photo or explicit back review', async () => {
  for (const options of [{ next: 'old' }, { reviewing: true }]) {
    const w = worker(options);
    assert.equal(await vm.runInContext('refresh()', w.context), false);
    assert.equal(w.session().shot.id, 'old');
    assert.equal(w.session().progress.done, 12);
    assert.equal(w.messages[0].type, 'progress');
    assert.equal(w.navigations.length, 0);
  }
});

test('save refreshes first and does not overwrite a photo completed elsewhere', async () => {
  const w = worker();
  await vm.runInContext('save()', w.context);
  assert.equal(w.captures(), 0);
  assert.equal(w.session().shot.id, 'next');
});

test('last completed photo updates the panel and clears the session shot', async () => {
  const w = worker({ next: null });
  await vm.runInContext('refresh()', w.context);
  assert.equal(w.session().shot, null);
  assert.equal(w.messages[0].type, 'finished');
});


test('manual backlog mode is preserved when refreshing the capture queue', async () => {
  const w = worker({ backlogOnly: true });
  await vm.runInContext('refresh()', w.context);
  assert.match(w.requests[0], /backlog_only=true/);
  assert.equal(w.session().shot.id, 'next');
});
