// maply capture: on-page panel for the capture tab on Google Maps.
// Shows what to aim at, live heading/position feedback from the address bar,
// and handles the single-key shortcuts. Never touches the page otherwise.

(() => {
  const KEYS = { Enter: 'save', n: 'no_imagery', b: 'blocked', x: 'skipped', '[': 'back', h: 'hide' };
  const TOLERANCE = 15; // degrees
  let state = null;
  let panel = null;
  let hidden = false;

  const send = (msg) => chrome.runtime.sendMessage(msg).catch(() => null);

  function parseView(href) {
    const m = decodeURIComponent(href).match(/@(-?\d+\.\d+),(-?\d+\.\d+),3a(?:,([\d.]+)y)?(?:,([\d.]+)h)?(?:,([\d.]+)t)?/);
    if (!m) return null;
    return { lat: +m[1], lng: +m[2], fov: m[3] ? +m[3] : null, heading: m[4] != null ? +m[4] % 360 : null, pitch: m[5] != null ? +m[5] - 90 : null };
  }

  function metres(a, b) {
    const k = Math.PI / 180;
    const x = Math.sin(((b.lat - a.lat) * k) / 2) ** 2 + Math.cos(a.lat * k) * Math.cos(b.lat * k) * Math.sin(((b.lng - a.lng) * k) / 2) ** 2;
    return 2 * 6371008 * Math.asin(Math.sqrt(x));
  }

  function delta(a, b) {
    return ((a - b + 540) % 360) - 180; // observed minus planned, -180..180
  }

  function imageDate() {
    const t = document.body ? document.body.innerText : '';
    const m = t.match(/Image capture:?\s*([A-Z][a-z]{2,8}\.?\s+\d{4})/) || t.match(/Street View\s*[·•-]\s*([A-Z][a-z]{2,8}\.?\s+\d{4})/);
    return m ? m[1].replace(/\s+/g, ' ') : null;
  }

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  function render() {
    if (!state) return;
    if (!panel) {
      panel = el('div', 'maply-hud');
      panel.setAttribute('role', 'region');
      panel.setAttribute('aria-label', 'maply capture');
      document.documentElement.appendChild(panel);
    }
    panel.replaceChildren();
    panel.hidden = hidden;
    const p = state.progress || {};
    const shot = state.shot;

    const top = el('div', 'maply-top');
    top.append(el('span', 'maply-code', shot ? shot.code : 'Done'), el('span', 'maply-progress', `${(p.done || 0).toLocaleString()} of ${(p.total || 0).toLocaleString()} done`));
    panel.append(top);

    if (!shot) {
      panel.append(el('p', 'maply-look', 'Every shot in this job has been handled.'));
      return;
    }
    panel.append(el('p', 'maply-look', shot.look || shot.street));

    const live = el('div', 'maply-live');
    live.id = 'maply-live';
    panel.append(live);
    updateLive();

    if (state.last) {
      const ok = state.last.status === 'captured' || state.last.status === 'no_imagery' || state.last.status === 'skipped' || state.last.status === 'blocked';
      const words = { captured: 'Saved', needs_check: 'Saved, needs a look', no_imagery: 'Marked no imagery', blocked: 'Marked blocked', skipped: 'Skipped', pending: 'Reset' };
      panel.append(el('p', ok ? 'maply-last' : 'maply-last maply-warn', `${words[state.last.status] || state.last.status}: ${state.last.code}`));
    }
    if (state.error) panel.append(el('p', 'maply-last maply-warn', state.error));

    const keys = el('div', 'maply-keys');
    const btn = (label, key, action) => {
      const b = el('button', 'maply-btn', null);
      b.type = 'button';
      b.append(el('kbd', null, key), document.createTextNode(' ' + label));
      b.addEventListener('click', (e) => { e.preventDefault(); act(action); });
      return b;
    };
    keys.append(btn('Save and next', 'Enter', 'save'), btn('No imagery', 'N', 'no_imagery'), btn('Blocked', 'B', 'blocked'), btn('Skip', 'X', 'skipped'));
    if (state.canGoBack) keys.append(btn('Back', '[', 'back'));
    const stop = el('button', 'maply-btn maply-quiet', 'Pause');
    stop.type = 'button';
    stop.addEventListener('click', () => send({ type: 'stop' }));
    keys.append(stop);
    panel.append(keys);
  }

  function updateLive() {
    const live = document.getElementById('maply-live');
    if (!live || !state?.shot) return;
    const v = parseView(location.href);
    live.replaceChildren();
    if (!v) {
      live.append(el('span', 'maply-pill maply-warn', 'Waiting for Street View… press N if Google shows no imagery'));
      return;
    }
    const shot = state.shot;
    if (v.heading != null) {
      const d = delta(v.heading, shot.heading);
      const on = Math.abs(d) <= TOLERANCE;
      const turn = on ? 'On target' : d > 0 ? `Turn left ${Math.round(Math.abs(d))}°` : `Turn right ${Math.round(Math.abs(d))}°`;
      live.append(el('span', on ? 'maply-pill maply-ok' : 'maply-pill maply-warn', `${turn} (facing ${Math.round(v.heading)}°, plan ${Math.round(shot.heading)}°)`));
    }
    const m = metres(v, shot);
    live.append(el('span', m <= 15 ? 'maply-pill maply-ok' : 'maply-pill maply-warn', `Camera ${Math.round(m)} m from the plan`));
  }

  async function act(action) {
    if (!state?.shot && action !== 'hide') return;
    if (action === 'hide') { hidden = !hidden; if (panel) panel.hidden = hidden; return; }
    state.error = null;
    if (action === 'save') await send({ type: 'save' });
    else if (action === 'back') await send({ type: 'back' });
    else await send({ type: 'mark', status: action });
  }

  function typing(e) {
    const t = e.target;
    return t && (t.isContentEditable || /^(input|textarea|select)$/i.test(t.tagName));
  }

  window.addEventListener(
    'keydown',
    (e) => {
      if (!state || e.metaKey || e.ctrlKey || e.altKey || typing(e)) return;
      const action = KEYS[e.key] || KEYS[e.key.toLowerCase()];
      if (!action) return;
      e.preventDefault();
      e.stopPropagation();
      act(action);
    },
    true,
  );

  chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
    if (msg.type === 'prepare') {
      // hide the panel so it isn't in the photo, then report what the page shows
      if (panel) panel.style.visibility = 'hidden';
      requestAnimationFrame(() => requestAnimationFrame(() =>
        reply({ url: location.href, imageDate: imageDate(), dpr: window.devicePixelRatio, viewport: { width: innerWidth, height: innerHeight } }),
      ));
      return true;
    }
    if (msg.type === 'show') { if (panel) panel.style.visibility = ''; reply(true); return; }
    if (msg.type === 'info') { reply({ url: location.href }); return; }
    if (msg.type === 'error') { if (state) { state.error = msg.message; render(); } reply(true); return; }
    if (msg.type === 'progress') { if (state) { state.progress = msg.progress; render(); } reply(true); return; }
    if (msg.type === 'finished') { if (state) { state.shot = null; state.progress = msg.progress; render(); } reply(true); return; }
    if (msg.type === 'stopped') { state = null; panel?.remove(); panel = null; reply(true); return; }
    reply(null);
  });

  (async () => {
    const s = await send({ type: 'hello' });
    if (!s?.active) return;
    state = s;
    render();
    setInterval(() => { if (state) send({ type: 'refresh' }); }, 15000);
    send({ type: 'refresh' });
    let last = location.href;
    setInterval(() => { if (location.href !== last) { last = location.href; updateLive(); } }, 250);
  })();
})();
