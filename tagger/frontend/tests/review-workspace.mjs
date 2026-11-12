// Isolated UI contract fixtures: no upstream API, database, or model calls.
import { chromium } from 'playwright';
import { createServer } from 'node:http';
import { readFile, mkdir } from 'node:fs/promises';
import { resolve, extname } from 'node:path';
import assert from 'node:assert/strict';

const root = resolve(import.meta.dirname, '../dist');
const output = '/tmp/maply-review-test';
await mkdir(output, { recursive: true });
const tag = (value) => ({ value, confidence: 0.89, evidence: 'The intended verge is visible beside the road.', visibility: 'clear' });
const labels = (value) => ({ schema_version: 'maply-accessibility/1', footpath: tag(value), features: [], notes: [], review_reasons: [] });
const shot = { id: 'photo-1', code: 'FP-001', street: 'Example Road', status: 'needs_check', flags: ['manual_review'], note: '', result: { manual_review_reason: 'footpath: absence requires review before route exclusion' }, labels: labels('absent'), image_url: '/fixture.svg', thumb_url: '/fixture.svg', review_image_url: null, url: 'https://maps.google.com/', footpath: 'left', side: 'left', lat: -33, lng: 151, heading: 90, review: null };
const proposal = { id: 'review-1', batch_id: 'batch-1', status: 'ready', proposed_labels: labels('unknown'), error: null, model: 'fixture/model', usage: {}, created_at: '2026-11-04T00:00:00Z', decided_at: null };
const batch = { id: 'batch-1', status: 'running', model: 'fixture/model', limit: 20, concurrency: 2, auto_confirm: true, counts: { queued: 20, running: 0, ready: 0, failed: 0, confirmed: 0, rejected: 0, stale: 0, cancelled: 0 }, created_at: '2026-11-04T00:00:00Z' };
let batches = [];
let recovery = false;
let secondOpinion = proposal;
const writes = [];
const server = createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost');
  const json = (body) => res.writeHead(200, { 'Content-Type': 'application/json' }).end(JSON.stringify(body));
  try {
    if (req.method === 'POST') {
      let body = ''; for await (const chunk of req) body += chunk;
      writes.push({ path: url.pathname, body: JSON.parse(body) });
      if (url.pathname.endsWith('/recover')) { recovery = true; return json({ ok: true, status: 'retake' }); }
      if (url.pathname.endsWith('/decision')) { return json({ ok: true, status: 'confirmed' }); }
      if (url.pathname.endsWith('/pause')) batch.status = 'paused';
      else if (url.pathname.endsWith('/cancel')) batch.status = 'cancelled';
      else batch.status = 'running';
      batches = [batch]; return json(batch);
    }
    if (req.method !== 'GET') { res.writeHead(405).end(); return; }
    if (url.pathname === '/api/status') return json({ version: 'test', jobs_folder: 'fixture', browser: 'local', basemap: { url: '', attribution: '' }, scope: null, local_streets: true });
    if (url.pathname === '/api/jobs') return json([{ id: 'job-1', name: 'Local review dataset', counts: { total: 60000, done: 52483 } }]);
    if (url.pathname.endsWith('/review/summary')) return json({ counts: { absence: 4229, absence_only: 3262, needs_check: 10805, skipped: 2616, no_imagery: 2404, blocked: 811, recovery: recovery ? 1 : 0, untagged: 1, ready: 1, confirmed: 0 }, batches, provider: { model: 'fixture/model', configured: true } });
    if (url.pathname.endsWith('/review/items')) {
      const queue = url.searchParams.get('queue');
      const item = queue === 'ready' ? { ...shot, review: secondOpinion } : queue === 'untagged' ? { ...shot, labels: null } : queue === 'skipped' || queue === 'recovery' ? { ...shot, image_url: null, thumb_url: null, labels: null, status: recovery ? 'retake' : 'skipped' } : shot;
      return json({ items: [item], total: 1, offset: 0, limit: 24 });
    }
    if (url.pathname.startsWith('/api/')) { res.writeHead(404).end('Unexpected fixture API'); return; }
    if (url.pathname === '/fixture.svg') { res.writeHead(200, { 'Content-Type': 'image/svg+xml' }).end('<svg xmlns="http://www.w3.org/2000/svg" width="1500" height="900"><rect width="1500" height="900" fill="#c8d4c2"/><path d="M0 900L600 300H900L1500 900" fill="#86918a"/><text x="600" y="150" font-size="42">Fixture street view</text></svg>'); return; }
    const file = resolve(root, `.${url.pathname === '/' ? '/index.html' : url.pathname}`);
    if (!file.startsWith(`${root}/`)) { res.writeHead(403).end(); return; }
    const mime = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.woff': 'font/woff', '.woff2': 'font/woff2' };
    res.writeHead(200, { 'Content-Type': mime[extname(file)] || 'application/octet-stream' }).end(await readFile(file));
  } catch (error) { res.writeHead(500).end(String(error)); }
});
await new Promise((ready) => server.listen(0, '127.0.0.1', ready));
const browser = await chromium.launch({ headless: true, ...(process.env.PLAYWRIGHT_CHANNEL ? { channel: process.env.PLAYWRIGHT_CHANNEL } : {}) });
const page = await browser.newPage({ viewport: { width: 1512, height: 1100 } });
const errors = [];
page.on('pageerror', (error) => errors.push(error.message));
try {
  await page.goto(`http://127.0.0.1:${server.address().port}`);
  await page.getByRole('heading', { name: 'No footpath', exact: true }).waitFor();
  await page.getByRole('button', { name: 'Review next 20 with AI' }).waitFor();
  assert.equal(writes.length, 0, 'Opening the workspace never starts AI');
  assert.equal(await page.getByRole('button', { name: /New job/ }).count(), 0);
  await page.screenshot({ path: `${output}/workspace.png`, fullPage: true });
  await page.getByLabel('Parallel AI reviews').selectOption('20');
  await page.getByRole('button', { name: 'Review next 20 with AI' }).click();
  await page.getByRole('button', { name: 'Pause', exact: true }).waitFor();
  assert.deepEqual(writes[0], { path: '/api/jobs/job-1/review/batches', body: { limit: 20, concurrency: 20, auto_confirm: true } });
  await page.getByRole('button', { name: 'Pause', exact: true }).click();
  await page.getByRole('button', { name: 'Resume AI review' }).waitFor();
  await page.getByRole('button', { name: 'Cancel remaining' }).click();
  await page.getByRole('button', { name: /Ready to confirm/ }).click();
  await page.getByRole('button', { name: 'Confirm reviewed annotation' }).waitFor();
  assert.equal(await page.getByRole('button', { name: 'Confirm reviewed annotation' }).isDisabled(), true, 'Unknown cannot be confirmed');
  secondOpinion = { ...proposal, proposed_labels: labels('absent') };
  await page.getByRole('button', { name: 'Refresh', exact: true }).click();
  await page.getByRole('button', { name: 'Confirm no footpath' }).click();
  await page.getByRole('status').filter({ hasText: 'Review confirmed.' }).waitFor();
  assert.deepEqual(writes.at(-1), { path: '/api/review/items/review-1/decision', body: { decision: 'confirm' } });
  await page.getByRole('button', { name: /Skipped/ }).click();
  await page.getByRole('button', { name: 'Prepare for capture' }).click();
  await page.getByRole('button', { name: 'Copy photo code' }).waitFor();
  assert.deepEqual(writes.at(-1), { path: '/api/review/shots/photo-1/recover', body: {} });
  assert.ok(await page.getByText(/paste this code into “Start at photo code”/).isVisible());
  await page.getByRole('button', { name: /Photos to tag/ }).click();
  await page.getByRole('button', { name: 'Request AI review · 1 photo' }).click();
  await page.getByRole('button', { name: 'Pause', exact: true }).waitFor();
  assert.deepEqual(writes.at(-1), { path: '/api/review/shots/photo-1/tag', body: {} });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: `${output}/mobile.png`, fullPage: true });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true, 'Mobile layout has no horizontal overflow');
  assert.deepEqual(errors, []);
  console.log('PASS: review workspace opens without paid calls; explicit batch/pause/cancel; unknown blocked; human confirm; targeted recovery; explicit recovered-photo tagging; mobile layout.');
} finally { await browser.close(); await new Promise((done) => server.close(done)); }
