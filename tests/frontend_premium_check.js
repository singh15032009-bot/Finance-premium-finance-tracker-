// Headless render check for the premium area, in BOTH states:
// free (locked teasers + pricing) and Pro (real analytics).
//
// Requires the server running and `npm install jsdom`. Run from tests/:
//     node frontend_premium_check.js
const fs = require('fs');
const { JSDOM, VirtualConsole } = require('jsdom');

const ROOT = 'C:/Users/prabh/Finance Tracker/static';
const BASE = 'http://127.0.0.1:8000';

let failures = 0;
const check = (ok, name, detail = '') => {
  console.log(`${ok ? '[PASS]' : '[FAIL]'} ${name}${detail ? '\n         ' + detail : ''}`);
  if (!ok) failures++;
};

async function setPlan(pro) {
  const path = pro ? '/api/subscription/trial' : '/api/subscription/cancel';
  await fetch(BASE + path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: pro ? JSON.stringify({ plan_id: 'pro_annual' }) : undefined,
  });
}

async function boot() {
  const pageErrors = [];
  const virtualConsole = new VirtualConsole();
  virtualConsole.on('jsdomError', e => pageErrors.push(String(e.message || e)));
  virtualConsole.on('error', (...a) => pageErrors.push(a.join(' ')));

  const html = fs.readFileSync(`${ROOT}/index.html`, 'utf8');
  const dom = new JSDOM(html, { runScripts: 'outside-only', url: BASE, virtualConsole });
  const { window } = dom;

  for (const id of ['edit-dialog', 'upgrade-dialog']) {
    const d = window.document.getElementById(id);
    d.showModal = () => d.setAttribute('open', '');
    d.close = () => d.removeAttribute('open');
  }
  window.confirm = () => true;
  window.fetch = async (path, opts = {}) => {
    const res = await fetch(path.startsWith('http') ? path : BASE + path, opts);
    const text = await res.text();
    return { ok: res.ok, status: res.status, json: async () => JSON.parse(text) };
  };
  window.Element.prototype.getBoundingClientRect = () =>
    ({ left: 0, top: 0, width: 640, height: 240, right: 640, bottom: 240 });

  window.eval(fs.readFileSync(`${ROOT}/app.js`, 'utf8'));
  window.document.dispatchEvent(new window.Event('DOMContentLoaded'));
  await new Promise(r => setTimeout(r, 8000));
  return { d: window.document, w: window, pageErrors };
}

(async () => {
  // ---------------------------------------------------------- FREE state --
  console.log('=== FREE PLAN ===');
  await setPlan(false);
  let { d, w, pageErrors } = await boot();

  check(pageErrors.length === 0, 'No JS errors on the free plan',
        pageErrors.slice(0, 2).join(' | '));

  check(d.getElementById('plan-badge').textContent.includes('Free'),
        'Topbar badge shows the free plan', d.getElementById('plan-badge').textContent);

  const locked = d.querySelectorAll('.insight-card.locked');
  check(locked.length === 3, 'All 3 insight cards are locked', `${locked.length} locked`);
  check(d.querySelectorAll('.lock-overlay').length === 3, 'Each locked card has a lock overlay');
  const unlockBtns = [...d.querySelectorAll('.lock-overlay button')];
  check(unlockBtns.length === 3 && unlockBtns.every(b => /Unlock/.test(b.textContent)),
        'Each locked card has an unlock CTA');
  const blurb = d.querySelector('.lock-blurb')?.textContent || '';
  check(blurb.length > 30, 'Locked card explains what you would get', blurb.slice(0, 60) + '…');

  // pricing section
  const planCards = d.querySelectorAll('#plan-grid .plan');
  check(planCards.length === 3, 'Three plans rendered', `${planCards.length}`);
  const names = [...planCards].map(p => p.querySelector('.plan-name').textContent);
  check(JSON.stringify(names) === JSON.stringify(['Free', 'Pro', 'Lifetime']),
        'Plans are Free / Pro / Lifetime', names.join(', '));
  const amounts = [...planCards].map(p => p.querySelector('.plan-amount').textContent);
  check(amounts[0] === '$0' && amounts[1] === '$5.99' && amounts[2] === '$149',
        'Annual pricing shown by default', amounts.join(' / '));
  check(!!d.querySelector('.plan-ribbon'), 'A plan carries a ribbon (current / most popular)');
  check(d.querySelector('.plan.current') !== null, 'Free is marked as the current plan');

  const featureItems = d.querySelectorAll('#premium-feature-grid .feature-item');
  check(featureItems.length >= 5, 'Premium feature grid populated', `${featureItems.length} items`);

  const compareRows = d.querySelectorAll('#compare-table tbody tr');
  check(compareRows.length > 10, 'Comparison table populated', `${compareRows.length} rows`);
  check(d.querySelectorAll('#compare-table .tick').length > 5, 'Comparison table shows ticks');

  const gated = d.querySelectorAll('#period-picker button.gated');
  check(gated.length === 2, 'Long history periods show as locked', `${gated.length} gated`);

  check(/not connected/i.test(d.getElementById('pricing-note').textContent),
        'Pricing note is honest that checkout is not wired up',
        d.getElementById('pricing-note').textContent);

  // upgrade dialog
  unlockBtns[0].dispatchEvent(new w.Event('click'));
  await new Promise(r => setTimeout(r, 300));
  const dlg = d.getElementById('upgrade-dialog');
  check(dlg.hasAttribute('open'), 'Unlock CTA opens the upgrade dialog');
  check(d.getElementById('upgrade-list').children.length >= 5,
        'Upgrade dialog lists the Pro features');
  const dialogText = dlg.textContent;
  check(!/card number|cvc|expiry/i.test(dialogText),
        'Upgrade dialog collects NO card details');
  check(d.querySelectorAll('#upgrade-dialog input').length === 0,
        'Upgrade dialog has no input fields at all');

  // ----------------------------------------------------------- PRO state --
  console.log('\n=== PRO PLAN ===');
  await setPlan(true);
  ({ d, w, pageErrors } = await boot());

  check(pageErrors.length === 0, 'No JS errors on Pro',
        pageErrors.slice(0, 2).join(' | '));
  check(/Pro/.test(d.getElementById('plan-badge').textContent),
        'Topbar badge shows Pro', d.getElementById('plan-badge').textContent);
  check(d.querySelectorAll('.insight-card.locked').length === 0,
        'No cards are locked on Pro');
  check(d.querySelectorAll('.lock-overlay').length === 0, 'Lock overlays removed');

  const values = [...d.querySelectorAll('.insight-value')].map(e => e.textContent);
  check(values.length === 3, 'All 3 insight cards show a headline metric', values.join(' | '));
  check(values.every(v => v && !/NaN|undefined/.test(v)),
        'Insight metrics are real values', values.join(' | '));
  check(/^\$[\d,]+\.\d{2}$/.test(values[0]), 'Dividend card shows currency income', values[0]);

  const insightText = d.getElementById('premium-insights').textContent;
  check(!/NaN|undefined|null/.test(insightText), 'No NaN/undefined in the insight cards');
  check(/Yield on cost/.test(insightText), 'Dividend card shows yield on cost');
  check(/Beta/i.test(insightText), 'Sector card shows portfolio beta');
  check(/S&P 500/.test(insightText), 'Benchmark card names the index');

  const bars = d.querySelectorAll('.insight-rows .bar-fill');
  check(bars.length >= 5, 'Insight cards drew comparison bars', `${bars.length} bars`);

  const proCurrent = [...d.querySelectorAll('#plan-grid .plan.current .plan-name')]
    .map(e => e.textContent);
  check(proCurrent.includes('Pro'), 'Pro is marked as the current plan', proCurrent.join(','));
  check(d.querySelectorAll('#period-picker button.gated').length === 0,
        'History periods unlocked on Pro');

  // leave the app on the free plan so the demo starts where a new user would
  await setPlan(false);

  console.log(`\n${failures === 0 ? 'ALL PREMIUM RENDER CHECKS PASSED' : failures + ' CHECK(S) FAILED'}`);
  process.exit(failures ? 1 : 0);
})().catch(e => { console.error('crashed:', e); process.exit(1); });
