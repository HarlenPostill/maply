const $ = (id) => document.getElementById(id);
const DEFAULTS = { server: 'http://127.0.0.1:8000', quality: 92, includeSkipped: false, backlogOnly: false, jobId: null };

async function load() {
  const cfg = { ...DEFAULTS, ...(await chrome.storage.local.get(Object.keys(DEFAULTS))) };
  $('server').value = cfg.server;
  $('quality').value = cfg.quality;
  $('skipped').checked = cfg.includeSkipped;
  $('backlog').checked = cfg.backlogOnly;

  const session = await chrome.runtime.sendMessage({ type: 'session' });
  const running = !!session?.running;
  $('stop').hidden = !running;
  $('start').textContent = running ? 'Restart from the next shot' : 'Start capturing';

  const res = await chrome.runtime.sendMessage({ type: 'jobs' });
  if (!res?.ok) {
    $('status').textContent = `Can’t reach maply at ${cfg.server}. Start it, then reopen this.`;
    $('start').disabled = true;
    return;
  }
  const jobs = res.jobs;
  if (!jobs.length) {
    $('status').textContent = 'No jobs yet. Create one in maply first.';
    $('start').disabled = true;
    return;
  }
  $('status').textContent = running ? `Capturing ${session.job?.name ?? ''}.` : 'Choose a job, open Chrome where you want to work, then start.';
  $('job').replaceChildren(...jobs.map((j) => new Option(`${j.name} (${j.counts.done.toLocaleString()} of ${j.counts.total.toLocaleString()})`, j.id)));
  $('job').value = (running && session.jobId) || cfg.jobId || jobs[0].id;
  const show = () => {
    const j = jobs.find((x) => x.id === $('job').value);
    const left = cfg.backlogOnly ? (j.counts.manual_review || 0) : j.counts.total - j.counts.done;
    $('progress').textContent = `${left.toLocaleString()} ${cfg.backlogOnly ? "manual review photos" : "photos"} left${j.counts.skipped ? `, ${j.counts.skipped.toLocaleString()} skipped` : ''}.`;
  };
  show();
  $('job').onchange = () => { chrome.storage.local.set({ jobId: $('job').value }); show(); };
}

$('start').onclick = async () => {
  $('start').disabled = true;
  await chrome.storage.local.set({ jobId: $('job').value });
  const r = await chrome.runtime.sendMessage({ type: 'start', jobId: $('job').value });
  if (r?.ok) window.close();
  else { $('status').textContent = r?.error || 'Couldn’t start.'; $('start').disabled = false; }
};
$('stop').onclick = async () => { await chrome.runtime.sendMessage({ type: 'stop' }); window.close(); };
$('server').onchange = () => chrome.storage.local.set({ server: $('server').value.trim() || DEFAULTS.server }).then(load);
$('quality').onchange = () => chrome.storage.local.set({ quality: Math.max(60, Math.min(100, Number($('quality').value) || 92)) });
$('backlog').onchange = () => chrome.storage.local.set({ backlogOnly: $('backlog').checked }).then(load);
$('skipped').onchange = () => chrome.storage.local.set({ includeSkipped: $('skipped').checked });
$('shortcuts').onclick = (e) => { e.preventDefault(); chrome.tabs.create({ url: 'chrome://extensions/shortcuts' }); };

load();
