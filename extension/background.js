// maply capture: background service worker.
// Owns the capture session (stored in chrome.storage so it survives restarts),
// talks to the local maply app, captures the tab and moves to the next shot.

const DEFAULTS = { server: 'http://127.0.0.1:8000', quality: 92, includeSkipped: false };
let busy = false;

async function settings() {
  return { ...DEFAULTS, ...(await chrome.storage.local.get(Object.keys(DEFAULTS))) };
}
async function getSession() {
  return (await chrome.storage.local.get('session')).session || null;
}
async function setSession(s) {
  await chrome.storage.local.set({ session: s });
}

async function api(path, body) {
  const { server } = await settings();
  const res = await fetch(server.replace(/\/$/, '') + path, {
    method: body ? 'POST' : 'GET',
    headers: { 'X-Maply': 'extension', ...(body ? { 'Content-Type': 'application/json' } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `maply replied ${res.status}`);
  return data;
}

async function tell(tabId, msg) {
  try {
    return await chrome.tabs.sendMessage(tabId, msg);
  } catch {
    return null; // page still loading or not a Maps page
  }
}

async function open(data, { remember = true } = {}) {
  const s = await getSession();
  if (!s) return;
  if (remember && s.shot) s.history = [...(s.history || []).slice(-99), s.shot.id];
  s.shot = data.shot;
  s.progress = data.progress;
  s.last = data.saved || null;
  await setSession(s);
  if (data.shot) {
    await chrome.tabs.update(s.tabId, { url: data.shot.url });
  } else {
    await tell(s.tabId, { type: 'finished', progress: data.progress });
  }
}

async function start(jobId) {
  const { includeSkipped } = await settings();
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  const data = await api(`/api/ext/next?job_id=${jobId}&after=0&include_skipped=${includeSkipped}`);
  const target = tab && !tab.url?.startsWith('chrome') ? tab : await chrome.tabs.create({ url: 'about:blank' });
  await setSession({ jobId, job: data.job, tabId: target.id, windowId: target.windowId, shot: null, history: [], running: true });
  await open(data, { remember: false });
  return data;
}

async function stop() {
  const s = await getSession();
  if (s) {
    s.running = false;
    await setSession(s);
    await tell(s.tabId, { type: 'stopped' });
  }
}

async function withLock(fn) {
  if (busy) return;
  busy = true;
  try {
    await fn();
  } catch (e) {
    const s = await getSession();
    if (s) await tell(s.tabId, { type: 'error', message: e.message || String(e) });
  } finally {
    busy = false;
  }
}

async function save() {
  await withLock(async () => {
    const s = await getSession();
    if (!s?.running || !s.shot) return;
    const [visible] = await chrome.tabs.query({ active: true, windowId: s.windowId });
    if (visible?.id !== s.tabId) throw new Error('Switch back to the capture tab first. Only the visible tab can be saved.');
    const page = await tell(s.tabId, { type: 'prepare' }); // hides the panel, returns URL + on-screen date
    if (!page) throw new Error('This tab is not showing the Street View page yet.');
    const { quality, includeSkipped } = await settings();
    let image;
    try {
      image = await chrome.tabs.captureVisibleTab(s.windowId, { format: 'jpeg', quality });
    } finally {
      await tell(s.tabId, { type: 'show' });
    }
    const data = await api(`/api/ext/capture?include_skipped=${includeSkipped}`, {
      shot_id: s.shot.id,
      image,
      page_url: page.url,
      image_date_text: page.imageDate,
      device_pixel_ratio: page.dpr,
      viewport: page.viewport,
    });
    await open(data);
  });
}

async function mark(status) {
  await withLock(async () => {
    const s = await getSession();
    if (!s?.running || !s.shot) return;
    const page = await tell(s.tabId, { type: 'info' });
    const { includeSkipped } = await settings();
    const data = await api(`/api/ext/mark?include_skipped=${includeSkipped}`, { shot_id: s.shot.id, status, page_url: page?.url || null });
    await open(data);
  });
}

async function back() {
  await withLock(async () => {
    const s = await getSession();
    if (!s?.running || !s.history?.length) return;
    const prev = s.history.pop();
    await setSession(s);
    await open(await api(`/api/ext/shot/${prev}`), { remember: false });
  });
}

chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  (async () => {
    const s = await getSession();
    switch (msg.type) {
      case 'hello': // a Maps page loaded: show the panel only in the capture tab
        reply(s && s.running && sender.tab?.id === s.tabId ? { active: true, shot: s.shot, progress: s.progress, job: s.job, last: s.last, canGoBack: !!s.history?.length } : { active: false });
        return;
      case 'save': return reply(await save());
      case 'mark': return reply(await mark(msg.status));
      case 'back': return reply(await back());
      case 'stop': return reply(await stop());
      case 'start':
        try { reply({ ok: true, data: await start(msg.jobId) }); } catch (e) { reply({ ok: false, error: e.message }); }
        return;
      case 'session': return reply(s);
      case 'jobs':
        try { reply({ ok: true, jobs: await api('/api/jobs') }); } catch (e) { reply({ ok: false, error: e.message }); }
        return;
      default:
        reply(null);
    }
  })();
  return true; // async reply
});

chrome.commands.onCommand.addListener((command) => {
  if (command === 'save-and-next') save();
  else if (command === 'no-imagery') mark('no_imagery');
  else if (command === 'skip') mark('skipped');
  else if (command === 'back') back();
});
