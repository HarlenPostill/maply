// Uses a real, read-only snapshot and GET-only app proxy; never starts the app or database.
// MAPLY_SNAPSHOT=/path/to/job.json npm run test:visualiser
import { chromium } from 'playwright';
import { createServer } from 'node:http';
import { readFile, mkdir } from 'node:fs/promises';
import { resolve, extname } from 'node:path';
import assert from 'node:assert/strict';

if (!process.env.MAPLY_SNAPSHOT) throw new Error('Set MAPLY_SNAPSHOT to a read-only exported job snapshot.');
const snapshot = JSON.parse(await readFile(process.env.MAPLY_SNAPSHOT, 'utf8'));
const upstream = process.env.MAPLY_READ_API || 'http://127.0.0.1:8000';
const output = process.env.MAPLY_TEST_OUTPUT || '/tmp/maply-visualiser-test';
await mkdir(output, { recursive: true });
let failSnapshots = false;
let requests = 0;
const server = createServer(async (req, res) => {
  try {
    if (req.method !== 'GET') { res.writeHead(405).end(); return; }
    const path = new URL(req.url, 'http://localhost').pathname;
    if (path.startsWith('/assets/accessibility/')) {
      requests++;
      if (failSnapshots) { res.writeHead(503).end('Unavailable'); return; }
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
const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 1512, height: 1100 } });
const errors = [];
page.on('pageerror', (error) => errors.push(error.stack || error.message));
try {
  await page.addInitScript(() => {
    window.__maplyDrawCounts = [];
    const draw = WebGL2RenderingContext.prototype.drawElementsInstanced;
    WebGL2RenderingContext.prototype.drawElementsInstanced = function (...args) {
      window.__maplyDrawCounts.push(args[4]);
      return draw.apply(this, args);
    };
  });
  await page.clock.install();
  await page.goto(`http://127.0.0.1:${server.address().port}`);
  console.log('Loaded app');
  await page.getByRole('button', { name: 'Accessibility atlas ↗' }).click();
  await page.getByRole('heading', { name: 'Tagging coverage' }).waitFor();
  console.log('Opened atlas');
  const tagged = snapshot.segments.reduce((sum, s) => sum + s.tagged, 0);
  await page.getByText(tagged.toLocaleString(), { exact: true }).waitFor();
  await page.locator('.leaflet-tile-loaded').first().waitFor();
  await page.screenshot({ path: `${output}/map.png` });
  await page.getByRole('button', { name: 'Street comparison', exact: true }).click();
  await page.locator('.viz-table tbody tr').first().waitFor();
  assert.ok(await page.locator('.viz-table tbody tr').count() > 10);
  await page.getByRole('button', { name: /Narrowed or blocked passage/ }).click();
  await page.getByRole('heading', { name: 'Narrowed or blocked passage' }).waitFor();
  await page.screenshot({ path: `${output}/streets.png` });
  const example = snapshot.segments.find((s) => s.metrics.passage.flagged && s.examples.length);
  assert.ok(example, 'Live snapshot has a passage concern to inspect');
  await page.getByLabel('Street', { exact: true }).selectOption(example.street);
  await page.getByRole('button', { name: 'Map', exact: true }).click();
  await page.getByLabel('Inspect street segment').selectOption(example.key);
  await page.locator('.viz-evidence-photo').waitFor();
  assert.equal(await page.locator('.viz-evidence-photo').evaluate((image) => image.complete && image.naturalWidth > 0), true);
  await page.screenshot({ path: `${output}/evidence.png` });
  await page.getByRole('button', { name: 'Close segment evidence' }).click();
  await page.getByRole('button', { name: 'Clear filters', exact: true }).click();
  await page.getByRole('button', { name: '3D landscape', exact: true }).click();
  await page.locator('.viz-three canvas').waitFor();
  await page.waitForFunction(() => document.querySelector('.viz-three canvas')?.width > 500);
  await page.screenshot({ path: `${output}/3d.png` });
  await page.getByLabel('Column height', { exact: true }).fill('0.3');
  await page.screenshot({ path: `${output}/3d-low.png` });
  assert.notDeepEqual(await readFile(`${output}/3d.png`), await readFile(`${output}/3d-low.png`), 'Height slider changes the rendered columns');
  assert.equal(await page.getByLabel('3D data points').inputValue(), 'photos');
  await page.waitForFunction((count) => window.__maplyDrawCounts.includes(count), snapshot.points.length);
  await page.getByLabel('3D data points').selectOption('segments');
  await page.getByLabel('Height represents').selectOption('count');
  await page.getByLabel('3D minimum observations').selectOption('10');
  await page.getByRole('button', { name: 'Focus view', exact: true }).click();
  await page.getByRole('button', { name: 'Hide sidebar', exact: true }).click();
  assert.equal(await page.locator('.viz-sidebar').isVisible(), false);
  assert.equal(await page.locator('.viz-stats').isVisible(), false);
  await page.waitForFunction(() => document.querySelector('.viz-three canvas').getBoundingClientRect().height > window.innerHeight - 190);
  await page.screenshot({ path: `${output}/3d-focused.png` });
  await page.getByRole('button', { name: 'Show sidebar', exact: true }).click();
  assert.equal(await page.locator('.viz-sidebar').isVisible(), true);
  await page.keyboard.press('Escape');
  assert.equal(await page.locator('.viz-stats').isVisible(), true);
  await page.getByLabel('Height represents').selectOption('rate');
  await page.getByLabel('3D minimum observations').selectOption('1');
  await page.getByLabel('Column height', { exact: true }).fill('1');
  await page.screenshot({ path: `${output}/3d.png` });
  const canvas = page.locator('.viz-three canvas');
  await canvas.focus(); await canvas.press('ArrowLeft'); await canvas.press('+');
  await page.screenshot({ path: `${output}/3d-orbit.png` });
  assert.notDeepEqual(await readFile(`${output}/3d.png`), await readFile(`${output}/3d-orbit.png`), 'Camera changes rendered scene');
  const beforeAuto = requests;
  snapshot.generated_at = new Date().toISOString();
  snapshot.segments[0].planned += 1;
  await page.clock.fastForward(30001);
  await page.waitForFunction((planned) => document.querySelector('.viz-stat small')?.textContent.includes(planned.toLocaleString()), snapshot.segments.reduce((sum, s) => sum + s.planned, 0));
  assert.ok(requests > beforeAuto, 'Automatic refresh fetches new observations');
  const before = requests;
  await page.getByRole('button', { name: 'Pause', exact: true }).click();
  snapshot.generated_at = new Date().toISOString();
  snapshot.segments[0].planned += 1;
  await page.getByRole('button', { name: 'Refresh', exact: true }).click();
  await page.waitForFunction((planned) => document.querySelector('.viz-stat small')?.textContent.includes(planned.toLocaleString()), snapshot.segments.reduce((sum, s) => sum + s.planned, 0));
  assert.ok(requests > before, 'Manual refresh fetches new observations');
  assert.equal(await page.getByRole('button', { name: '3D landscape', exact: true }).getAttribute('aria-pressed'), 'true');
  failSnapshots = true;
  await page.getByRole('button', { name: 'Refresh', exact: true }).click();
  await page.getByRole('alert').waitFor();
  assert.equal(await page.locator('.viz-three canvas').count(), 1, 'Last successful scene survives refresh failure');
  failSnapshots = false;
  await page.getByRole('button', { name: 'Refresh', exact: true }).click();
  await page.getByRole('alert').waitFor({ state: 'hidden' });
  const pausedRequests = requests;
  await page.clock.fastForward(60001);
  assert.equal(requests, pausedRequests, 'Paused visualiser makes no periodic snapshot requests');
  await page.getByRole('button', { name: 'Insights', exact: true }).click();
  await page.getByRole('heading', { name: 'Street profiles across all measures' }).waitFor();
  assert.equal(await page.locator('.viz-heatmap thead th').count(), 9);
  assert.ok(await page.locator('.viz-insight-card circle').count() > 0);
  await page.screenshot({ path: `${output}/insights.png` });
  const originalRows = await page.locator('.viz-heatmap tbody tr').count();
  await page.getByLabel('Insights minimum observations').selectOption('50');
  assert.ok(await page.locator('.viz-heatmap tbody tr').count() <= originalRows);
  await page.getByLabel('Insights minimum observations').selectOption('5');
  await page.locator('.viz-heatmap tbody button:not(:disabled)').first().click();
  assert.equal(await page.getByRole('button', { name: 'Map', exact: true }).getAttribute('aria-pressed'), 'true');
  await page.getByRole('button', { name: 'Clear filters', exact: true }).click();
  await page.getByRole('button', { name: /^Footpath quality/ }).click();
  await page.getByRole('heading', { name: 'Footpath quality', exact: true }).waitFor();
  await page.getByLabel('Individual footpath photo observations').waitFor();
  await page.screenshot({ path: `${output}/quality-photos.png` });
  await page.getByRole('button', { name: 'Heatmap', exact: true }).click();
  await page.getByLabel('Photo observation grid heatmap').waitFor();
  await page.getByLabel('Heatmap grid size').selectOption('50');
  await page.getByLabel('Heatmap minimum observations').selectOption('5');
  await page.getByLabel('Heatmap intensity').selectOption('count');
  await page.screenshot({ path: `${output}/quality-heatmap.png` });
  await page.getByRole('button', { name: /^Missing footpath/ }).click();
  await page.getByLabel('Heatmap intensity').selectOption('rate');
  await page.screenshot({ path: `${output}/missing-heatmap.png` });
  const byStreet = new Map();
  for (const point of snapshot.points) { const list = byStreet.get(point.street) ?? []; list.push(point); byStreet.set(point.street, list); }
  const cellPhotos = [...byStreet.values()].find((photos) => new Set(photos.map((p) => `${Math.floor((p.coordinates[0] - 151.21) * 92300 / 200)}:${Math.floor((p.coordinates[1] + 33.88) * 111320 / 200)}`)).size === 1);
  assert.ok(cellPhotos, 'A street fits in one heatmap cell for source inspection');
  const single = cellPhotos[0];
  await page.getByLabel('Heatmap grid size').selectOption('200');
  await page.getByLabel('Street', { exact: true }).selectOption(single.street);
  await page.getByLabel('Heatmap minimum observations').selectOption('1');
  const heatBounds = await page.getByLabel('Photo observation grid heatmap').boundingBox();
  await page.mouse.click(heatBounds.x + heatBounds.width / 2, heatBounds.y + heatBounds.height / 2);
  await page.getByLabel('Inspect heatmap photo').selectOption(single.id);
  await page.locator('.viz-photo-meta strong').getByText(single.code, { exact: true }).waitFor();
  await page.getByRole('button', { name: 'Close segment evidence' }).click();
  await page.getByRole('button', { name: 'Clear filters', exact: true }).click();
  await page.getByRole('button', { name: '3D landscape', exact: true }).click();
  assert.equal(await page.getByLabel('3D data points').inputValue(), 'photos');
  await page.waitForFunction((count) => window.__maplyDrawCounts.includes(count), snapshot.points.length);
  await page.screenshot({ path: `${output}/all-photo-columns.png` });
  const downloadPromise = page.waitForEvent('download');
  await page.getByRole('button', { name: 'Export CSV' }).click();
  const download = await downloadPromise;
  assert.ok(download.suggestedFilename().endsWith('.csv'));
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole('button', { name: 'Map', exact: true }).click();
  await page.screenshot({ path: `${output}/mobile.png`, fullPage: true });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true, 'Mobile has no horizontal page overflow');
  await page.getByRole('button', { name: '3D landscape', exact: true }).click();
  await page.getByRole('button', { name: 'Focus view', exact: true }).click();
  await page.getByRole('button', { name: 'Hide sidebar', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('.viz-three canvas')?.getBoundingClientRect().height > 400);
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true, 'Mobile focus has no page overflow');
  await page.screenshot({ path: `${output}/mobile-focus.png` });
  await page.getByRole('button', { name: 'Exit focus', exact: true }).click();
  await page.getByRole('button', { name: '← Capture workspace' }).click();
  await page.getByRole('heading', { name: 'Jobs', exact: true }).waitFor();
  assert.deepEqual(errors, []);
  console.log('PASS: real-data map, street comparison, evidence, 3D height/count controls, full-height focus, sidebar collapse, insights, refresh/recovery, CSV, mobile and capture navigation.');
} catch (error) {
  await page.screenshot({ path: `${output}/failure.png`, fullPage: true });
  console.error('Page errors:', errors);
  throw error;
} finally {
  await browser.close();
  server.closeAllConnections();
  await new Promise((done) => server.close(done));
}
