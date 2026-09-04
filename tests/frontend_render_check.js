// Headless render test: load the real index.html + app.js into jsdom, point
// fetch at the live WealthTrack server, and assert the dashboard actually
// paints real numbers. Catches runtime errors the static checks cannot.
//
// Requires the server running with the 5-stock demo portfolio (AAPL, MSFT,
// TSLA, NVDA, AMZN) and a one-off `npm install jsdom`. Run from this folder:
//     npm install jsdom && node frontend_render_check.js
//
// This is what caught the Node.append() bug: append() returns undefined, so
// `path.append(svgEl('title')).textContent = ...` threw and took the whole
// allocation chart down with it.
const fs = require('fs');
const { JSDOM, VirtualConsole } = require('jsdom');

const ROOT = 'C:/Users/prabh/Finance Tracker/static';
const BASE = 'http://127.0.0.1:8000';

let failures = 0;
const check = (ok, name, detail = '') => {
  console.log(`${ok ? '[PASS]' : '[FAIL]'} ${name}${detail ? '\n         ' + detail : ''}`);
  if (!ok) failures++;
};

const pageErrors = [];
const virtualConsole = new VirtualConsole();
virtualConsole.on('jsdomError', (e) => pageErrors.push(String(e.message || e)));
virtualConsole.on('error', (...args) => pageErrors.push(args.join(' ')));

(async () => {
  const html = fs.readFileSync(`${ROOT}/index.html`, 'utf8');
  const dom = new JSDOM(html, { runScripts: 'outside-only', url: BASE, virtualConsole });
  const { window } = dom;

  // jsdom has no <dialog> support and no fetch wired to our server; supply both.
  window.HTMLDialogElement = window.HTMLDialogElement || function () {};
  const dlg = window.document.getElementById('edit-dialog');
  dlg.showModal = () => { dlg.setAttribute('open', ''); };
  dlg.close = () => { dlg.removeAttribute('open'); };
  window.confirm = () => true;

  window.fetch = async (path, opts = {}) => {
    const url = path.startsWith('http') ? path : BASE + path;
    const res = await fetch(url, opts);
    const text = await res.text();
    return { ok: res.ok, status: res.status, json: async () => JSON.parse(text) };
  };
  // getBoundingClientRect returns zeros in jsdom; the hover handler needs a box.
  window.Element.prototype.getBoundingClientRect = function () {
    return { left: 0, top: 0, width: 640, height: 240, right: 640, bottom: 240 };
  };

  const js = fs.readFileSync(`${ROOT}/app.js`, 'utf8');
  window.eval(js);
  window.document.dispatchEvent(new window.Event('DOMContentLoaded'));

  // let the fetches settle
  await new Promise(r => setTimeout(r, 6000));

  const d = window.document;
  const txt = (id) => (d.getElementById(id).textContent || '').trim();

  check(pageErrors.length === 0, 'No JavaScript runtime errors during render',
        pageErrors.slice(0, 3).join(' | '));

  // --- summary tiles ---
  const invested = txt('t-invested'), value = txt('t-value');
  const pl = txt('t-pl'), ret = txt('t-return');
  check(/^\$[\d,]+\.\d{2}$/.test(invested), 'Total invested tile shows currency', invested);
  check(/^\$[\d,]+\.\d{2}$/.test(value), 'Current value tile shows currency', value);
  check(/^[+\u2212]\$[\d,]+\.\d{2}$/.test(pl), 'Profit/loss tile shows a signed amount', pl);
  check(/^[+\u2212][\d.]+%$/.test(ret), 'Return tile shows a signed percentage', ret);

  // --- holdings table ---
  const rows = d.querySelectorAll('#holdings-body tr');
  check(rows.length === 5, 'All 5 holdings rendered as table rows', `${rows.length} rows`);

  const firstRow = rows[0];
  const cells = firstRow.querySelectorAll('td');
  check(cells.length === 9, 'Each row has all 9 columns', `${cells.length} cells`);
  const ticker = firstRow.querySelector('.sym-ticker')?.textContent;
  const name = firstRow.querySelector('.sym-name')?.textContent;
  check(/^[A-Z]{1,5}$/.test(ticker || ''), 'Row shows a real ticker', ticker);
  check(!!name && name.length > 3, 'Row shows the real company name from Yahoo', name);

  const priceTime = firstRow.querySelector('.price-time')?.textContent;
  check(!!priceTime && /ago|just now|\w/.test(priceTime),
        'Row shows when the price was last updated', priceTime);

  const allText = d.getElementById('holdings-body').textContent;
  check(!/NaN|undefined|null/.test(allText),
        'No NaN/undefined leaked into the table', allText.match(/NaN|undefined|null/)?.[0] || '');

  // --- charts ---
  const donut = d.querySelectorAll('#allocation-chart svg path');
  check(donut.length === 5, 'Allocation donut drew one arc per holding', `${donut.length} arcs`);
  const legendRows = d.querySelectorAll('#allocation-chart .legend-row');
  check(legendRows.length === 5, 'Allocation legend lists every holding', `${legendRows.length} rows`);
  const donutCentre = d.querySelector('.donut-center-value')?.textContent;
  check(/^\$[\d,]+\.\d{2}$/.test(donutCentre || ''), 'Donut centre shows total value', donutCentre);

  const line = d.querySelector('#history-chart svg polyline');
  const pts = (line?.getAttribute('points') || '').trim().split(/\s+/).filter(Boolean);
  check(pts.length > 5, 'History line chart plotted a real series', `${pts.length} points`);
  const allFinite = pts.every(p => p.split(',').every(n => Number.isFinite(parseFloat(n))));
  check(allFinite, 'Every plotted coordinate is a finite number');

  const gridLabels = d.querySelectorAll('#history-chart svg text');
  check(gridLabels.length >= 6, 'Chart has axis labels', `${gridLabels.length} labels`);

  // --- status + footer ---
  const status = txt('status-text');
  check(/updated|Connected/i.test(status), 'Status line reports live prices', status);
  const footer = txt('footer-meta');
  check(/Yahoo Finance/.test(footer) && /self-check: passed/.test(footer),
        'Footer confirms source and passing self-check', footer);

  check(d.getElementById('empty-state').hidden === true,
        'Empty-state message hidden when holdings exist');
  check(d.getElementById('banners').children.length === 0,
        'No warning banners for a fully-priced portfolio',
        d.getElementById('banners').textContent);

  console.log(`\n${failures === 0 ? 'ALL RENDER CHECKS PASSED' : failures + ' RENDER CHECK(S) FAILED'}`);
  process.exit(failures ? 1 : 0);
})().catch(e => { console.error('render test crashed:', e); process.exit(1); });
