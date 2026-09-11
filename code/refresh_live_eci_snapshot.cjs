#!/usr/bin/env node
/* Pull the currently displayed METR frontier-ECI series before an elicitation.

   Usage (on a browser-capable runner):
     PLAYWRIGHT_MODULE=/path/to/playwright node code/refresh_live_eci_snapshot.cjs

   It writes a small, auditable input for make_eci_self_conditions.py.  The
   Streamlit app is ahead of its public repository at times, so do not replace
   this with a GitHub CSV fetch. */
const fs = require('fs');
const path = require('path');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

const root = path.resolve(__dirname, '..');
const out = path.join(root, 'data', 'live_metr_eci_frontier.json');
const app = 'https://metrgraph.streamlit.app/~/+/?tab=eci';

(async () => {
  const browser = await chromium.launch({ headless: true, executablePath: process.env.CHROME_BIN });
  try {
    const page = await browser.newPage();
    await page.goto(app, { waitUntil: 'networkidle', timeout: 120000 });
    await page.waitForFunction(() => Array.from(document.querySelectorAll('.js-plotly-plot'))
      .some(el => (el.data || []).some(t => String(t.name || '').startsWith('Projection ('))), null,
      { timeout: 120000 });
    const traces = await page.locator('.js-plotly-plot').evaluateAll(els => els.flatMap(el => el.data || []));
    const points = traces.filter(t => (t.x || []).length === 1 && (t.y || []).length === 1 && (t.text || []).length === 1)
      .map(t => ({ date: String(t.x[0]).slice(0, 10), score: Number(t.y[0]), model: String(t.text[0]) }))
      .filter(p => Number.isFinite(p.score) && p.date.length === 10)
      .sort((a, b) => a.date.localeCompare(b.date) || a.score - b.score);
    if (points.length < 10) throw new Error(`METR chart yielded only ${points.length} frontier points`);
    const rows = [];
    let high = -Infinity;
    for (const p of points) if (p.score > high) { rows.push(p); high = p.score; }
    const payload = { source: app, retrieved_at: new Date().toISOString(), frontier: rows };
    fs.writeFileSync(out, JSON.stringify(payload, null, 2) + '\n');
    console.log(`${out}: ${rows.length} frontier highs; latest ${rows.at(-1).model} ${rows.at(-1).score}`);
  } finally { await browser.close(); }
})().catch(err => { console.error(err.stack || err); process.exit(1); });
