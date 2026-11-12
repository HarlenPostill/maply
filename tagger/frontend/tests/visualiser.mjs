// Uses a real, read-only snapshot and GET-only app proxy; never starts the app or database.
// npm run build && MAPLY_SNAPSHOT=/path/to/job.json npm run test:visualiser
import { chromium } from 'playwright';
import { createServer } from 'node:http';
import { readFile, mkdir } from 'node:fs/promises';
import { resolve, extname } from 'node:path';
import assert from 'node:assert/strict';

if (!process.env.MAPLY_SNAPSHOT) throw new Error('Set MAPLY_SNAPSHOT to a read-only exported job snapshot.');
const snapshot = JSON.parse(await readFile(process.env.MAPLY_SNAPSHOT, 'utf8'));
assert.equal(snapshot.version, 2, 'Snapshot must be version 2');
const upstream = process.env.MAPLY_READ_API || 'http://127.0.0.1:8000';
const output = process.env.MAPLY_TEST_OUTPUT || '/tmp/maply-visualiser-test';
await mkdir(output, { recursive: true });
let failSnapshots = false;
let full = 0, metas = 0;
const meta = () => ({ version: 2, job_id: snapshot.job_id, digest: snapshot.digest, generated_at: snapshot.generated_at, checked_at: new Date().toISOString() });
const server = createServer(async (req, res) => {
  try {
    if (req.method !== 'GET') { res.writeHead(405).end(); return; }
    const path = new URL(req.url, 'http://localhost').pathname;
    if (path.startsWith('/assets/accessibility/')) {
      if (failSnapshots) { res.writeHead(503).end('Unavailable'); return; }
      if (path.endsWith('.meta.json')) { metas++; res.writeHead(200, { 'Content-Type': 'application/json' }).end(JSON.stringify(meta())); return; }
      full++;
      res.writeHead(200, { 'Content-Type': 'application/json' }).end(JSON.stringify(snapshot)); return;
    }
    if (path.startsWith('/api/') || path.startsWith('/files/')) {
      const response = await fetch(`${upstream}${req.url}`);
      res.writeHead(response.status, { 'Content-Type': response.headers.get('Content-Type') || 'application/json' });
      res.end(Buffer.from(await response.arrayBuffer())); return;
    }
    const root = resolve('dist');
    const file = resolve(root, `.${path === '/' ? '/index.html' : path}`);
    if (!file.startsWith(`${root}/`)) { res.writeHead(403).end(); return; }
    const mime = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.woff': 'font/woff', '.woff2': 'font/woff2' };
    res.writeHead(200, { 'Content-Type': mime[extname(file)] || 'application/octet-stream' }).end(await readFile(file));
  } catch (e) { res.writeHead(500).end(String(e)); }
});
await new Promise((ready) => server.listen(0, '127.0.0.1', ready));
// PLAYWRIGHT_CHANNEL=chrome uses an installed Chrome instead of Playwright's downloaded browser.
const browser = await chromium.launch({ headless: true, ...(process.env.PLAYWRIGHT_CHANNEL ? { channel: process.env.PLAYWRIGHT_CHANNEL } : {}) });
const page = await browser.newPage({ viewport: { width: 1512, height: 1000 } });
const errors = [];
page.on('pageerror', (error) => errors.push(error.stack || error.message));
const n = snapshot.photos.id.length;
const footpathAbsent = snapshot.photos.footpath.filter((v, i) => v === 1 && snapshot.photos.annotation[i] === 1).length;
const result = () => page.locator('.viz-result b').first().textContent();
const tab = (name) => page.getByRole('navigation', { name: 'Atlas views' }).getByRole('button', { name, exact: true });
const step = (name) => console.log(`✓ ${name}`);
try {
  await page.addInitScript(() => {
    window.__maplyDrawCounts = [];
    const draw = WebGL2RenderingContext.prototype.drawElementsInstanced;
    WebGL2RenderingContext.prototype.drawElementsInstanced = function (...args) { window.__maplyDrawCounts.push(args[4]); return draw.apply(this, args); };
  });
  await page.clock.install();
  await page.goto(`http://127.0.0.1:${server.address().port}`);
  await page.getByRole('button', { name: 'Accessibility atlas ↗' }).click();
  await page.locator('.viz-hero-tile strong').first().getByText(n.toLocaleString('en-AU'), { exact: true }).waitFor({ timeout: 60000 });
  await page.screenshot({ path: `${output}/overview.png`, fullPage: true });
  step('overview shows every planned photo');

  // Linked filtering from a chart bar.
  await page.locator('.viz-card').filter({ hasText: 'Footpath presence' }).locator('button.viz-bar-row', { hasText: 'Absent' }).first().click();
  await page.locator('.viz-chip', { hasText: 'Footpath presence: Absent' }).waitFor();
  assert.equal(await result(), footpathAbsent.toLocaleString('en-AU'));
  await page.getByRole('button', { name: 'Clear all filters' }).click();
  assert.equal(await result(), n.toLocaleString('en-AU'));
  step('chart bars filter every view and clear');

  // Map with categorical legend filter.
  await tab('Map').click();
  await page.locator('.leaflet-tile-loaded').first().waitFor({ timeout: 30000 }).catch(() => console.log('  (basemap tiles unavailable)'));
  await page.locator('.viz-legend-rows button', { hasText: 'Asphalt' }).first().click();
  await page.locator('.viz-chip', { hasText: 'surface: Asphalt' }).waitFor();
  assert.ok(Number((await result()).replaceAll(',', '')) > 100);
  await page.screenshot({ path: `${output}/map-asphalt.png` });
  await page.getByRole('button', { name: 'Clear all filters' }).click();
  await page.getByRole('group', { name: 'Map layer' }).getByRole('button', { name: 'Segments' }).click();
  await page.getByLabel('Highlight value').selectOption({ label: 'Share: Brick pavers' });
  await page.screenshot({ path: `${output}/map-segments-share.png` });
  await page.getByRole('group', { name: 'Map layer' }).getByRole('button', { name: 'Grid' }).click();
  await page.getByLabel('Colour by').selectOption('imagery_year');
  await page.screenshot({ path: `${output}/map-grid-year.png` });
  step('map photos, segment shares and grid by imagery year');

  // 3D renders every photo.
  await page.getByLabel('Colour by').selectOption('tag:footpath_section:surface');
  await tab('3D').click();
  await page.locator('.viz-three canvas').waitFor();
  await page.waitForFunction((count) => window.__maplyDrawCounts.includes(count), n);
  await page.getByLabel('Height represents').selectOption('age');
  await page.screenshot({ path: `${output}/3d-age.png` });
  await page.getByRole('group', { name: '3D data points' }).getByRole('button', { name: 'Segments' }).click();
  await page.screenshot({ path: `${output}/3d-segments.png` });
  step('3D renders all photos and segment columns');

  // Cross analysis.
  await tab('Compare').click();
  await page.getByRole('button', { name: 'Hazard type × persistence' }).click();
  await page.locator('.viz-matrix tbody td button').first().waitFor();
  await page.screenshot({ path: `${output}/compare.png`, fullPage: true });
  await page.locator('.viz-matrix tbody td button', { hasText: /\d/ }).first().click();
  assert.equal(await page.locator('.viz-chip').count() >= 3, true, 'matrix cell sets both filters');
  await page.getByRole('button', { name: 'Clear all filters' }).click();
  step('compare matrix filters to a cell');

  // Dates.
  await tab('Time').click();
  const chart = page.getByRole('img', { name: 'Imagery month histogram' });
  await chart.waitFor();
  const box = await chart.boundingBox();
  await page.mouse.move(box.x + box.width * .6, box.y + box.height * .5);
  await page.mouse.down(); await page.mouse.move(box.x + box.width * .9, box.y + box.height * .5, { steps: 4 }); await page.mouse.up();
  await page.locator('.viz-chip', { hasText: 'Imagery' }).waitFor();
  await page.screenshot({ path: `${output}/time.png`, fullPage: true });
  await page.getByRole('button', { name: 'Clear all filters' }).click();
  step('imagery month brushing');

  await tab('Coverage').click();
  await page.locator('.viz-coverage-table tbody tr').first().waitFor();
  await page.screenshot({ path: `${output}/coverage.png`, fullPage: true });
  await tab('Streets').click();
  assert.ok(await page.locator('.viz-street-table tbody tr').count() > 10);
  step('coverage and streets');

  // Source photo drilldown.
  await tab('Photos').click();
  await page.locator('.viz-thumb img').first().waitFor({ timeout: 30000 });
  await page.locator('.viz-thumb').first().click();
  await page.locator('.viz-detail img').first().waitFor({ timeout: 30000 });
  assert.equal(await page.locator('.viz-detail img').first().evaluate((image) => image.complete && image.naturalWidth > 0), true);
  await page.screenshot({ path: `${output}/evidence.png` });
  await page.getByRole('button', { name: 'Close evidence' }).click();
  step('gallery thumbnails and evidence');

  const downloadPromise = page.waitForEvent('download');
  await page.locator('.viz-export summary').click();
  await page.getByRole('button', { name: 'Photos (CSV)' }).click();
  const download = await downloadPromise;
  assert.ok(download.suggestedFilename().endsWith('-photos.csv'));
  await page.locator('.viz-export summary').click();
  step('filtered export');

  // Refresh: meta polling, full download only on digest change, pause, failure and recovery.
  const before = full;
  await page.clock.fastForward(30001);
  await page.waitForFunction(() => true);
  assert.equal(full, before, 'unchanged digest does not re-download the snapshot');
  snapshot.digest = 'changed-digest';
  await page.clock.fastForward(30001);
  await page.waitForFunction(() => document.querySelector('.viz-live')?.textContent.includes('Live'));
  for (let i = 0; i < 50 && full === before; i++) await page.waitForTimeout(100);
  assert.ok(full > before, 'changed digest downloads the new snapshot');
  await page.getByRole('button', { name: 'Pause', exact: true }).click();
  const pausedMeta = metas;
  await page.clock.fastForward(60001);
  assert.equal(metas, pausedMeta, 'paused atlas makes no periodic requests');
  failSnapshots = true;
  await page.getByRole('button', { name: 'Refresh', exact: true }).click();
  await page.getByRole('alert').waitFor();
  assert.equal(await page.locator('.viz-gallery-grid').count(), 1, 'last successful view survives refresh failure');
  failSnapshots = false;
  await page.getByRole('button', { name: 'Refresh', exact: true }).click();
  await page.getByRole('alert').waitFor({ state: 'hidden' });
  step('refresh, pause and recovery');

  await page.setViewportSize({ width: 390, height: 844 });
  await tab('Overview').click();
  await page.screenshot({ path: `${output}/mobile.png`, fullPage: true });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true, 'Mobile has no horizontal page overflow');
  await tab('Map').click();
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true, 'Mobile map has no page overflow');
  await page.screenshot({ path: `${output}/mobile-map.png`, fullPage: true });
  step('mobile layout');
  await page.setViewportSize({ width: 1512, height: 1000 });
  await page.getByRole('button', { name: '← Capture workspace' }).click();
  await page.getByRole('button', { name: 'Accessibility atlas ↗' }).waitFor();
  assert.deepEqual(errors, []);
  console.log('PASS: overview, linked filters, map modes, 3D, compare, dates, coverage, streets, gallery evidence, export, refresh/recovery, mobile and navigation.');
} catch (error) {
  await page.screenshot({ path: `${output}/failure.png`, fullPage: true });
  console.error('Page errors:', errors);
  throw error;
} finally {
  await browser.close();
  server.closeAllConnections();
  await new Promise((done) => server.close(done));
}
