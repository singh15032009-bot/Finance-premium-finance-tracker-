/* WealthTrack dashboard.
 *
 * Talks to the FastAPI backend, renders the portfolio, and draws both charts
 * as hand-rolled SVG (no chart library, no CDN — the app works fully offline
 * apart from the Yahoo Finance calls themselves).
 */
'use strict';

const REFRESH_MS = 60_000;
const PALETTE = ['#4c8dff', '#8b5cf6', '#2ecc8f', '#f0b429', '#ff6b6b',
                 '#22d3ee', '#f472b6', '#a3e635', '#fb923c', '#94a3b8'];

const state = {
  portfolio: null,
  period: '1mo',
  historyCache: {},
  currency: 'USD',
  timer: null,
  refreshing: false,
  // premium
  plans: null,
  subscription: null,
  billing: 'annual',
  pendingPlan: null,
};

const $ = (id) => document.getElementById(id);

/* ------------------------------------------------------------ helpers -- */

function money(value, { currency = state.currency, signed = false, maxDigits = 2 } = {}) {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  const formatted = new Intl.NumberFormat(undefined, {
    style: 'currency', currency,
    minimumFractionDigits: 2, maximumFractionDigits: Math.max(2, maxDigits),
  }).format(Math.abs(value));
  if (!signed) return value < 0 ? `-${formatted}` : formatted;
  return `${value < 0 ? '−' : '+'}${formatted}`;
}

/* Per-share prices carry up to 4 decimals (fractional-share fills, penny
 * stocks). Showing them at the same precision the backend calculates with is
 * what lets a user multiply the displayed price by the quantity by hand and
 * land exactly on the displayed value. */
const priceFmt = (value, currency) =>
  money(value, { currency: currency || state.currency, maxDigits: 4 });

function pct(value, { signed = true } = {}) {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  const s = `${Math.abs(value).toFixed(2)}%`;
  if (!signed) return value < 0 ? `-${s}` : s;
  return `${value < 0 ? '−' : '+'}${s}`;
}

function qty(value) {
  if (value === null || value === undefined) return '—';
  return Number(value).toLocaleString(undefined, { maximumFractionDigits: 6 });
}

function toneClass(value) {
  if (value === null || value === undefined || Number.isNaN(value)) return '';
  if (value > 0) return 'gain';
  if (value < 0) return 'loss';
  return '';
}

function relativeTime(iso) {
  if (!iso) return null;
  const then = new Date(iso);
  if (Number.isNaN(then.getTime())) return null;
  const secs = Math.round((Date.now() - then.getTime()) / 1000);
  if (secs < 5) return 'just now';
  if (secs < 60) return `${secs}s ago`;
  const mins = Math.round(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  return then.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

function absoluteTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? '' : d.toLocaleString();
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else if (k.startsWith('on')) node.addEventListener(k.slice(2).toLowerCase(), v);
    else node.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c.nodeType ? c : document.createTextNode(String(c)));
  }
  return node;
}

const SVG_NS = 'http://www.w3.org/2000/svg';
function svgEl(tag, attrs = {}) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined) continue;
    node.setAttribute(k, String(v));
  }
  return node;
}

/* ---------------------------------------------------------------- api -- */

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  let body = null;
  try { body = await res.json(); } catch { /* empty or non-JSON body */ }

  if (!res.ok) {
    const err = new Error(
      body?.error?.message ||
      (Array.isArray(body?.detail) ? body.detail.map(d => d.msg).join('; ') : body?.detail) ||
      `Request failed (${res.status})`
    );
    err.code = body?.error?.code;
    err.status = res.status;
    throw err;
  }
  return body;
}

/* ------------------------------------------------------------ banners -- */

function setBanners(items) {
  const host = $('banners');
  host.replaceChildren();
  for (const { type, message } of items) {
    const icon = type === 'error' ? '⚠' : type === 'warn' ? '!' : 'ℹ';
    host.append(el('div', { class: `banner banner-${type}` },
      el('span', { class: 'banner-icon', text: icon, 'aria-hidden': 'true' }),
      el('span', { text: message })));
  }
}

function setStatus(kind, text) {
  $('status-dot').className = `dot ${kind}`;
  $('status-text').textContent = text;
}

/* ------------------------------------------------------------- render -- */

function detectCurrency(portfolio) {
  const counts = {};
  for (const h of portfolio.holdings) {
    if (h.currency) counts[h.currency] = (counts[h.currency] || 0) + 1;
  }
  const best = Object.entries(counts).sort((a, b) => b[1] - a[1])[0];
  return best ? best[0] : 'USD';
}

function renderTiles(p) {
  const t = p.totals;
  $('t-invested').textContent = money(t.total_invested);
  $('t-invested-sub').textContent =
    t.holdings_count === 0 ? 'No holdings yet'
      : `${t.holdings_count} holding${t.holdings_count === 1 ? '' : 's'}` +
        (t.unpriced_count ? ` · ${t.unpriced_count} unpriced` : '');

  $('t-value').textContent = t.priced_count ? money(t.total_value) : '—';
  const dayEl = $('t-value-sub');
  if (t.day_change !== null && t.day_change !== undefined) {
    dayEl.textContent = `${money(t.day_change, { signed: true })} (${pct(t.day_change_pct)}) today`;
    dayEl.className = `tile-sub ${toneClass(t.day_change)}`;
  } else {
    dayEl.textContent = t.priced_count ? 'Live market value' : 'Waiting for prices';
    dayEl.className = 'tile-sub';
  }

  const plEl = $('t-pl');
  plEl.textContent = t.priced_count ? money(t.total_profit_loss, { signed: true }) : '—';
  plEl.className = `tile-value ${toneClass(t.total_profit_loss)}`;
  $('t-pl-sub').textContent = t.priced_count
    ? `on ${money(t.priced_invested)} invested` : '—';

  const rEl = $('t-return');
  rEl.textContent = t.total_return_pct === null || t.total_return_pct === undefined
    ? '—' : pct(t.total_return_pct);
  rEl.className = `tile-value ${toneClass(t.total_return_pct)}`;
  $('t-return-sub').textContent = t.priced_count ? 'since purchase' : '—';
}

function renderHoldings(p) {
  const body = $('holdings-body');
  body.replaceChildren();

  const count = p.holdings.length;
  $('holdings-count').textContent = count
    ? `${count} position${count === 1 ? '' : 's'}` : '';
  $('empty-state').hidden = count > 0;

  for (const h of p.holdings) {
    const row = el('tr');

    row.append(el('td', { 'data-label': 'Symbol' },
      el('div', { class: 'sym-cell' },
        el('span', { class: 'sym-ticker', text: h.symbol }),
        h.name ? el('span', { class: 'sym-name', text: h.name, title: h.name }) : null)));

    row.append(el('td', { class: 'num', 'data-label': 'Qty', text: qty(h.quantity) }));
    row.append(el('td', { class: 'num', 'data-label': 'Buy price', text: priceFmt(h.purchase_price, h.currency) }));
    row.append(el('td', { class: 'num', 'data-label': 'Invested', text: money(h.invested) }));

    // Price now (+ when Yahoo last updated it)
    const priceCell = el('td', { class: 'num', 'data-label': 'Price now' });
    if (h.price_available) {
      const stamp = h.market_time || h.fetched_at;
      priceCell.append(el('div', { class: 'price-cell' },
        el('span', {}, priceFmt(h.current_price, h.currency)),
        el('span', {
          class: 'price-time',
          title: h.market_time
            ? `Yahoo Finance market time: ${absoluteTime(h.market_time)}`
            : `Fetched from Yahoo Finance: ${absoluteTime(h.fetched_at)}`,
        }, relativeTime(stamp) || ''),
        h.stale ? el('span', { class: 'badge badge-stale', text: 'stale' }) : null));
    } else {
      priceCell.append(el('span', { class: 'badge badge-error', text: 'no price' }));
    }
    row.append(priceCell);

    if (h.price_available) {
      row.append(el('td', { class: 'num', 'data-label': 'Value now', text: money(h.current_value, { currency: h.currency || state.currency }) }));
      row.append(el('td', { class: `num ${toneClass(h.profit_loss)}`, 'data-label': 'P / L' },
        el('div', { class: 'pl-cell' },
          el('span', {}, money(h.profit_loss, { signed: true, currency: h.currency || state.currency })),
          h.day_change !== null && h.day_change !== undefined
            ? el('span', { class: `price-time ${toneClass(h.day_change)}` },
                `${money(h.day_change, { signed: true, currency: h.currency || state.currency })} today`)
            : null)));
      row.append(el('td', { class: `num ${toneClass(h.return_pct)}`, 'data-label': 'Return', text: pct(h.return_pct) }));
    } else {
      const reason = h.price_error || 'Price unavailable from Yahoo Finance';
      row.append(el('td', { class: 'num', 'data-label': 'Value now' },
        el('span', { class: 'no-price', title: reason, text: 'unavailable' })));
      row.append(el('td', { class: 'num', 'data-label': 'P / L' }, el('span', { class: 'no-price', text: '—' })));
      row.append(el('td', { class: 'num', 'data-label': 'Return' }, el('span', { class: 'no-price', text: '—' })));
    }

    row.append(el('td', { class: 'col-actions', 'data-label': 'Actions' },
      el('div', { class: 'row-actions' },
        el('button', {
          class: 'icon-btn', type: 'button', title: `Edit ${h.symbol}`,
          'aria-label': `Edit ${h.symbol}`, onclick: () => openEdit(h),
        }, '✎'),
        el('button', {
          class: 'icon-btn danger', type: 'button', title: `Remove ${h.symbol}`,
          'aria-label': `Remove ${h.symbol}`, onclick: () => removeHolding(h),
        }, '✕'))));

    body.append(row);
  }
}

/* ------------------------------------------------------- donut chart -- */

function polarPoint(cx, cy, r, angleDeg) {
  const a = ((angleDeg - 90) * Math.PI) / 180;
  return [cx + r * Math.cos(a), cy + r * Math.sin(a)];
}

function arcPath(cx, cy, rOuter, rInner, startAngle, endAngle) {
  const sweep = endAngle - startAngle;
  // A single arc command cannot draw a full circle; nudge it just shy of 360.
  const end = sweep >= 360 ? startAngle + 359.99 : endAngle;
  const largeArc = end - startAngle > 180 ? 1 : 0;
  const [x1, y1] = polarPoint(cx, cy, rOuter, startAngle);
  const [x2, y2] = polarPoint(cx, cy, rOuter, end);
  const [x3, y3] = polarPoint(cx, cy, rInner, end);
  const [x4, y4] = polarPoint(cx, cy, rInner, startAngle);
  return [
    `M ${x1} ${y1}`,
    `A ${rOuter} ${rOuter} 0 ${largeArc} 1 ${x2} ${y2}`,
    `L ${x3} ${y3}`,
    `A ${rInner} ${rInner} 0 ${largeArc} 0 ${x4} ${y4}`,
    'Z',
  ].join(' ');
}

function renderAllocation(p) {
  const host = $('allocation-chart');
  host.replaceChildren();

  const slices = p.allocation || [];
  if (!slices.length) {
    host.append(el('p', { class: 'chart-empty' },
      p.holdings.length
        ? 'No live prices available, so allocation cannot be shown.'
        : 'Add a holding to see your allocation.'));
    return;
  }

  const size = 200, cx = size / 2, cy = size / 2, rOuter = 88, rInner = 56;
  const svg = svgEl('svg', {
    viewBox: `0 0 ${size} ${size}`, role: 'img',
    'aria-label': 'Portfolio allocation by current value',
    style: 'max-width:230px;margin:0 auto',
  });

  let angle = 0;
  slices.forEach((slice, i) => {
    const sweep = (slice.pct / 100) * 360;
    const path = svgEl('path', {
      d: arcPath(cx, cy, rOuter, rInner, angle, angle + sweep),
      fill: PALETTE[i % PALETTE.length],
      stroke: 'var(--bg-elev)', 'stroke-width': 2,
    });
    // Note: Node.append() returns undefined, so build the title separately.
    const title = svgEl('title');
    title.textContent = `${slice.symbol}: ${money(slice.value)} (${slice.pct.toFixed(2)}%)`;
    path.append(title);
    svg.append(path);
    angle += sweep;
  });

  svg.append(Object.assign(
    svgEl('text', { x: cx, y: cy - 2, 'text-anchor': 'middle', class: 'donut-center-value' }),
    { textContent: money(p.totals.total_value) }));
  svg.append(Object.assign(
    svgEl('text', { x: cx, y: cy + 14, 'text-anchor': 'middle', class: 'donut-center-label' }),
    { textContent: 'Total value' }));

  const legend = el('div', { class: 'legend' });
  slices.forEach((slice, i) => {
    legend.append(el('div', { class: 'legend-row' },
      el('span', { class: 'legend-swatch', style: `background:${PALETTE[i % PALETTE.length]}` }),
      el('span', { class: 'legend-sym', text: slice.symbol }),
      el('span', { class: 'legend-pct', text: `${slice.pct.toFixed(2)}%` })));
  });

  host.append(el('div', { style: 'width:100%' }, svg, legend));
}

/* -------------------------------------------------------- line chart -- */

function renderHistory(data) {
  const host = $('history-chart');
  host.replaceChildren();

  const points = (data && data.points) || [];
  if (points.length < 2) {
    host.append(el('p', { class: 'chart-empty' },
      points.length === 1
        ? 'Not enough history yet for this period.'
        : 'Add a holding to see your portfolio history.'));
    return;
  }

  const W = 640, H = 240, padL = 58, padR = 14, padT = 14, padB = 26;
  const innerW = W - padL - padR, innerH = H - padT - padB;

  const values = points.map(p => p.value);
  let min = Math.min(...values), max = Math.max(...values);
  if (min === max) { min -= 1; max += 1; }
  const span = max - min;
  min -= span * 0.08;
  max += span * 0.08;

  const x = i => padL + (i / (points.length - 1)) * innerW;
  const y = v => padT + innerH - ((v - min) / (max - min)) * innerH;

  const svg = svgEl('svg', {
    viewBox: `0 0 ${W} ${H}`, role: 'img', preserveAspectRatio: 'none',
    'aria-label': `Portfolio value over the selected period, ${points.length} data points`,
    style: 'width:100%;height:240px',
  });

  const rising = values[values.length - 1] >= values[0];
  const stroke = rising ? 'var(--gain)' : 'var(--loss)';
  const gradId = 'wt-area-grad';

  const defs = svgEl('defs');
  const grad = svgEl('linearGradient', { id: gradId, x1: 0, y1: 0, x2: 0, y2: 1 });
  grad.append(svgEl('stop', { offset: '0%', 'stop-color': stroke, 'stop-opacity': 0.28 }));
  grad.append(svgEl('stop', { offset: '100%', 'stop-color': stroke, 'stop-opacity': 0 }));
  defs.append(grad);
  svg.append(defs);

  // horizontal gridlines + y labels
  const TICKS = 4;
  for (let i = 0; i <= TICKS; i++) {
    const v = min + ((max - min) * i) / TICKS;
    const yy = y(v);
    svg.append(svgEl('line', {
      x1: padL, y1: yy, x2: W - padR, y2: yy,
      stroke: 'var(--border-soft)', 'stroke-width': 1,
    }));
    svg.append(Object.assign(
      svgEl('text', {
        x: padL - 9, y: yy + 3.5, 'text-anchor': 'end',
        'font-size': 10, fill: 'var(--text-faint)',
      }),
      { textContent: new Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 1 }).format(v) }));
  }

  const linePts = points.map((p, i) => `${x(i)},${y(p.value)}`).join(' ');
  svg.append(svgEl('polygon', {
    points: `${padL},${padT + innerH} ${linePts} ${W - padR},${padT + innerH}`,
    fill: `url(#${gradId})`,
  }));
  svg.append(svgEl('polyline', {
    points: linePts, fill: 'none', stroke,
    'stroke-width': 2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round',
  }));

  // x labels: first, middle, last
  [0, Math.floor((points.length - 1) / 2), points.length - 1].forEach((i, n) => {
    const d = new Date(points[i].date + 'T00:00:00');
    svg.append(Object.assign(
      svgEl('text', {
        x: x(i), y: H - 8,
        'text-anchor': n === 0 ? 'start' : n === 2 ? 'end' : 'middle',
        'font-size': 10, fill: 'var(--text-faint)',
      }),
      { textContent: d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) }));
  });

  // hover guide
  const guide = svgEl('line', {
    y1: padT, y2: padT + innerH, stroke: 'var(--text-dim)',
    'stroke-width': 1, 'stroke-dasharray': '3 3', opacity: 0,
  });
  const marker = svgEl('circle', {
    r: 4, fill: stroke, stroke: 'var(--bg-elev)', 'stroke-width': 2, opacity: 0,
  });
  svg.append(guide, marker);

  const readout = el('div', {
    class: 'muted',
    style: 'text-align:right;font-variant-numeric:tabular-nums;min-height:1.2em;margin-top:4px',
  });
  const last = points[points.length - 1];
  const first = points[0];
  const delta = last.value - first.value;
  const setDefaultReadout = () => {
    readout.replaceChildren(
      el('span', { text: `${points.length} trading days · ` }),
      el('span', {
        class: toneClass(delta),
        text: `${money(delta, { signed: true })} (${pct(first.value ? (delta / first.value) * 100 : null)})`,
      }));
  };
  setDefaultReadout();

  const overlay = svgEl('rect', {
    x: padL, y: padT, width: innerW, height: innerH, fill: 'transparent',
    style: 'cursor:crosshair',
  });
  const onMove = (evt) => {
    const box = svg.getBoundingClientRect();
    const clientX = evt.touches ? evt.touches[0].clientX : evt.clientX;
    const svgX = ((clientX - box.left) / box.width) * W;
    let i = Math.round(((svgX - padL) / innerW) * (points.length - 1));
    i = Math.max(0, Math.min(points.length - 1, i));
    const p = points[i];
    guide.setAttribute('x1', x(i));
    guide.setAttribute('x2', x(i));
    guide.setAttribute('opacity', 1);
    marker.setAttribute('cx', x(i));
    marker.setAttribute('cy', y(p.value));
    marker.setAttribute('opacity', 1);
    const d = new Date(p.date + 'T00:00:00');
    readout.replaceChildren(
      el('span', { text: `${d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })} · ` }),
      el('strong', { text: money(p.value) }));
  };
  const onLeave = () => {
    guide.setAttribute('opacity', 0);
    marker.setAttribute('opacity', 0);
    setDefaultReadout();
  };
  overlay.addEventListener('mousemove', onMove);
  overlay.addEventListener('mouseleave', onLeave);
  overlay.addEventListener('touchmove', onMove, { passive: true });
  overlay.addEventListener('touchend', onLeave);
  svg.append(overlay);

  const wrap = el('div', { style: 'width:100%' }, svg, readout);
  if (data.missing && data.missing.length) {
    wrap.append(el('p', {
      class: 'muted', style: 'margin:6px 0 0;font-size:.75rem',
      text: `Excluded (no history): ${data.missing.map(m => m.symbol).join(', ')}`,
    }));
  }
  host.append(wrap);
}

/* Same guard as the allocation chart: a drawing failure must not take the
 * dashboard down with it. */
function drawHistory(data) {
  try {
    renderHistory(data);
  } catch (err) {
    $('history-chart').replaceChildren(
      el('p', { class: 'chart-error', text: `Could not draw the history chart: ${err.message}` }));
  }
}

async function loadHistory(force = false) {
  const period = state.period;
  const host = $('history-chart');

  if (!state.portfolio || state.portfolio.holdings.length === 0) {
    host.replaceChildren(el('p', { class: 'chart-empty', text: 'Add a holding to see your portfolio history.' }));
    return;
  }
  if (!force && state.historyCache[period]) {
    drawHistory(state.historyCache[period]);
    return;
  }

  host.replaceChildren(el('p', { class: 'chart-empty', text: 'Loading history from Yahoo Finance…' }));
  try {
    const data = await api(`/api/portfolio/history?period=${encodeURIComponent(period)}`);
    state.historyCache[period] = data;
    if (state.period === period) drawHistory(data);
  } catch (err) {
    if (err.code === 'premium_required') {
      host.replaceChildren(el('div', { style: 'text-align:center;padding:22px 12px' },
        el('p', { class: 'chart-empty', style: 'padding:0 0 12px', text: err.message }),
        el('button', {
          class: 'btn btn-primary', type: 'button',
          onclick: () => openUpgrade(state.billing === 'monthly' ? 'pro_monthly' : 'pro_annual'),
        }, 'Unlock longer history')));
      return;
    }
    host.replaceChildren(el('p', { class: 'chart-error', text: `Could not load history: ${err.message}` }));
  }
}

/* --------------------------------------------------------- portfolio -- */

function render(p) {
  state.currency = detectCurrency(p);
  renderTiles(p);
  renderHoldings(p);

  // A chart is secondary to the numbers: if drawing one fails, say so in the
  // chart slot rather than letting the exception blank the whole dashboard.
  try {
    renderAllocation(p);
  } catch (err) {
    $('allocation-chart').replaceChildren(
      el('p', { class: 'chart-error', text: `Could not draw the allocation chart: ${err.message}` }));
  }

  const banners = [];
  for (const w of p.warnings || []) {
    banners.push({ type: w.startsWith('Internal check') ? 'error' : 'warn', message: w });
  }
  setBanners(banners);

  const stamps = p.holdings.filter(h => h.fetched_at).map(h => new Date(h.fetched_at).getTime());
  const newest = stamps.length ? new Date(Math.max(...stamps)).toISOString() : p.as_of;

  if (p.totals.unpriced_count > 0 && p.totals.priced_count === 0 && p.holdings.length) {
    setStatus('error', 'No live prices');
  } else if (p.totals.unpriced_count > 0 || (p.warnings || []).length) {
    setStatus('warn', `Partial data · ${relativeTime(newest) || 'now'}`);
  } else if (p.holdings.length === 0) {
    setStatus('live', 'Connected to Yahoo Finance');
  } else {
    setStatus('live', `Prices updated ${relativeTime(newest) || 'just now'}`);
  }

  $('footer-meta').textContent =
    `Source: ${p.source} · Totals self-check: ${p.totals_verified ? 'passed' : 'FAILED'} · ` +
    `Last refresh ${absoluteTime(p.as_of)}`;
}

async function loadPortfolio({ force = false, silent = false } = {}) {
  if (state.refreshing) return;
  state.refreshing = true;
  const btn = $('refresh-btn');
  btn.disabled = true;
  btn.classList.add('spinning');
  if (!silent) setStatus('loading', 'Fetching live prices…');

  try {
    const p = force
      ? await api('/api/portfolio/refresh', { method: 'POST' })
      : await api('/api/portfolio');
    state.portfolio = p;
    render(p);
  } catch (err) {
    setStatus('error', 'Connection problem');
    setBanners([{
      type: 'error',
      message: `Could not load the portfolio: ${err.message}`,
    }]);
  } finally {
    state.refreshing = false;
    btn.disabled = false;
    btn.classList.remove('spinning');
  }
}

async function refreshAll({ force = false } = {}) {
  await loadPortfolio({ force });
  await loadHistory(force);
  await loadPremiumInsights();
}

/* ------------------------------------------------------------ actions -- */

async function removeHolding(h) {
  if (!confirm(`Remove ${qty(h.quantity)} × ${h.symbol} from your portfolio?`)) return;
  try {
    await api(`/api/holdings/${h.id}`, { method: 'DELETE' });
    state.historyCache = {};
    await refreshAll();
  } catch (err) {
    setBanners([{ type: 'error', message: `Could not remove ${h.symbol}: ${err.message}` }]);
  }
}

function openEdit(h) {
  $('e-id').value = h.id;
  $('e-quantity').value = h.quantity;
  $('e-price').value = h.purchase_price;
  $('e-date').value = h.purchase_date || '';
  $('edit-title').textContent = `Edit ${h.symbol}`;
  $('edit-error').hidden = true;
  $('edit-dialog').showModal();
}

async function saveEdit() {
  const id = $('e-id').value;
  const errBox = $('edit-error');
  const payload = {
    quantity: parseFloat($('e-quantity').value),
    purchase_price: parseFloat($('e-price').value),
    purchase_date: $('e-date').value || null,
  };
  if (!(payload.quantity > 0) || !(payload.purchase_price > 0)) {
    errBox.textContent = 'Quantity and purchase price must both be greater than zero.';
    errBox.hidden = false;
    return;
  }
  try {
    await api(`/api/holdings/${id}`, { method: 'PUT', body: JSON.stringify(payload) });
    $('edit-dialog').close();
    state.historyCache = {};
    await refreshAll();
  } catch (err) {
    errBox.textContent = err.message;
    errBox.hidden = false;
  }
}

/* symbol validation on the add form (debounced, hits Yahoo through our API) */
let validateTimer = null;
function scheduleValidate() {
  clearTimeout(validateTimer);
  const hint = $('symbol-hint');
  const raw = $('f-symbol').value.trim().toUpperCase();
  if (!raw) { hint.textContent = ' '; hint.className = 'field-hint'; return; }
  hint.textContent = 'Checking with Yahoo Finance…';
  hint.className = 'field-hint checking';
  validateTimer = setTimeout(async () => {
    try {
      const res = await api(`/api/validate/${encodeURIComponent(raw)}`);
      if ($('f-symbol').value.trim().toUpperCase() !== raw) return;
      if (res.valid) {
        const q = res.quote;
        hint.textContent = `${q.short_name || q.symbol} · ${priceFmt(q.price, q.currency || 'USD')}`;
        hint.className = 'field-hint ok';
      } else {
        hint.textContent = res.error.message;
        hint.className = 'field-hint bad';
      }
    } catch (err) {
      hint.textContent = err.message;
      hint.className = 'field-hint bad';
    }
  }, 450);
}

async function submitAdd(evt) {
  evt.preventDefault();
  const btn = $('add-btn');
  const errBox = $('form-error');
  errBox.hidden = true;

  const payload = {
    symbol: $('f-symbol').value.trim().toUpperCase(),
    quantity: parseFloat($('f-quantity').value),
    purchase_price: parseFloat($('f-price').value),
    purchase_date: $('f-date').value || null,
  };

  if (!payload.symbol) { errBox.textContent = 'Enter a ticker symbol.'; errBox.hidden = false; return; }
  if (!(payload.quantity > 0)) { errBox.textContent = 'Quantity must be greater than zero.'; errBox.hidden = false; return; }
  if (!(payload.purchase_price > 0)) { errBox.textContent = 'Purchase price must be greater than zero.'; errBox.hidden = false; return; }

  btn.disabled = true;
  btn.textContent = 'Adding…';
  try {
    await api('/api/holdings', { method: 'POST', body: JSON.stringify(payload) });
    $('add-form').reset();
    $('symbol-hint').textContent = ' ';
    $('symbol-hint').className = 'field-hint';
    state.historyCache = {};
    await refreshAll();
    $('f-symbol').focus();
  } catch (err) {
    if (err.code === 'premium_required') {
      // Hitting the free limit is the single best moment to show the offer.
      errBox.replaceChildren(
        el('span', { text: err.message + ' ' }),
        el('button', {
          class: 'btn btn-primary', type: 'button',
          style: 'margin-left:8px;padding:4px 12px;font-size:.8rem',
          onclick: () => openUpgrade(state.billing === 'monthly' ? 'pro_monthly' : 'pro_annual'),
        }, 'See Pro'));
      errBox.hidden = false;
    } else {
      errBox.textContent = err.message;
      errBox.hidden = false;
    }
  } finally {
    btn.disabled = false;
    btn.textContent = 'Add holding';
  }
}

/* ============================== PREMIUM ================================ */

/* Sample figures used only to shape the blurred teaser behind the lock.
 * They are never shown legibly and never presented as the user's own data --
 * the real numbers are fetched from the Pro endpoints once unlocked. */
const TEASER = {
  dividends: { value: '$1,284', unit: '/ year', caption: 'across 7 dividend payers',
               rows: [['MSFT', 62], ['AAPL', 41], ['NVDA', 28]] },
  sectors:   { value: '61.8%', unit: 'Technology', caption: 'beta 1.61 · 2 sectors held',
               rows: [['Technology', 62], ['Consumer Cyc.', 38], ['Healthcare', 12]] },
  benchmark: { value: '+3.4%', unit: 'vs S&P 500', caption: 'over the last 12 months',
               rows: [['Portfolio', 78], ['S&P 500', 64]] },
};

function insightShell(id, icon, title, bodyNodes, { locked = false, feature = null } = {}) {
  const card = $(`insight-${id}`);
  card.className = `card insight-card${locked ? ' locked' : ''}`;
  card.replaceChildren(
    el('div', { class: 'insight-head' },
      el('span', { class: 'insight-icon', text: icon, 'aria-hidden': 'true' }),
      el('span', { class: 'insight-title', text: title })),
    el('div', { class: 'insight-body' }, bodyNodes));

  if (locked) {
    card.append(el('div', { class: 'lock-overlay' },
      el('span', { class: 'lock-icon', text: '🔒', 'aria-hidden': 'true' }),
      el('span', { class: 'lock-title', text: title }),
      el('p', { class: 'lock-blurb', text: feature?.blurb || '' }),
      el('button', {
        class: 'btn btn-primary', type: 'button',
        onclick: () => openUpgrade(state.billing === 'monthly' ? 'pro_monthly' : 'pro_annual'),
      }, 'Unlock with Pro')));
  }
  return card;
}

function bars(rows, { suffix = '%' } = {}) {
  return el('div', { class: 'insight-rows' },
    rows.map(([label, value]) => el('div', { class: 'insight-row' },
      el('span', { class: 'row-label', text: label, title: label }),
      el('span', { class: 'bar-track' },
        el('span', { class: 'bar-fill', style: `width:${Math.max(2, Math.min(100, value))}%` })),
      el('span', { class: 'row-val', text: `${value}${suffix}` }))));
}

function metric(value, unit, caption) {
  return [
    el('div', { class: 'insight-metric' },
      el('span', { class: 'insight-value', text: value }),
      unit ? el('span', { class: 'insight-unit', text: unit }) : null),
    caption ? el('span', { class: 'insight-caption', text: caption }) : null,
  ];
}

function renderLockedInsights() {
  const byId = Object.fromEntries((state.plans?.premium_features || []).map(f => [f.id, f]));
  for (const [id, icon, title] of [
    ['dividends', '◈', 'Dividend income'],
    ['sectors', '◐', 'Sector & risk'],
    ['benchmark', '◭', 'vs S&P 500'],
  ]) {
    const t = TEASER[id];
    insightShell(id, icon, title,
      [...metric(t.value, t.unit, t.caption), bars(t.rows)],
      { locked: true, feature: byId[id] });
  }
}

async function loadPremiumInsights() {
  if (!state.subscription?.is_premium) { renderLockedInsights(); return; }

  // Dividends
  insightShell('dividends', '◈', 'Dividend income',
    [el('span', { class: 'insight-caption', text: 'Loading…' })]);
  try {
    const d = await api('/api/premium/dividends');
    const t = d.totals;
    const top = d.holdings.filter(h => h.pays_dividend).slice(0, 3);
    const maxIncome = Math.max(1, ...top.map(h => h.annual_income));
    const next = d.upcoming_ex_dates[0];
    insightShell('dividends', '◈', 'Dividend income', [
      ...metric(money(t.annual_income), '/ year',
        t.paying_holdings
          ? `${money(t.monthly_income)}/mo · ${t.paying_holdings} of ${t.total_holdings} holdings pay`
          : 'None of your holdings pay a dividend'),
      top.length ? bars(top.map(h => [h.symbol, Math.round(h.annual_income / maxIncome * 100)]),
                        { suffix: '' }) : null,
      t.yield_on_cost !== null
        ? el('span', { class: 'insight-caption', text: `Yield on cost ${t.yield_on_cost}%` }) : null,
      next
        ? el('span', { class: 'insight-caption', text: `Next ex-date: ${next.symbol} on ${next.ex_dividend_date}` })
        : null,
    ]);
  } catch (err) {
    insightShell('dividends', '◈', 'Dividend income',
      [el('span', { class: 'chart-error', text: err.message })]);
  }

  // Sectors
  try {
    const s = await api('/api/premium/sectors');
    insightShell('sectors', '◐', 'Sector & risk', [
      ...metric(
        s.sectors.length ? `${s.sectors[0].pct}%` : '—',
        s.sectors.length ? s.sectors[0].label : '',
        s.portfolio_beta !== null
          ? `Beta ${s.portfolio_beta} · diversification ${s.diversification_score}/100`
          : `Diversification ${s.diversification_score ?? '—'}/100`),
      bars(s.sectors.slice(0, 4).map(x => [x.label, x.pct])),
      s.effective_sectors
        ? el('span', { class: 'insight-caption',
                       text: `Effectively ${s.effective_sectors} of ${s.well_diversified_sectors} sectors` })
        : null,
      s.concentration_warning
        ? el('p', { class: 'insight-warn', text: s.concentration_warning }) : null,
    ]);
  } catch (err) {
    insightShell('sectors', '◐', 'Sector & risk',
      [el('span', { class: 'chart-error', text: err.message })]);
  }

  // Benchmark
  try {
    const b = await api('/api/premium/benchmark?period=1y');
    const has = b.outperformance_pct !== null;
    insightShell('benchmark', '◭', 'vs S&P 500', has ? [
      ...metric(pct(b.outperformance_pct), `vs ${b.benchmark_name}`,
        `You ${pct(b.portfolio_return_pct)} · index ${pct(b.benchmark_return_pct)} over 1 year`),
      bars([
        ['Portfolio', Math.round(Math.max(0, b.portfolio_return_pct))],
        [b.benchmark_name, Math.round(Math.max(0, b.benchmark_return_pct))],
      ]),
      el('p', {
        class: b.beating_benchmark ? 'insight-caption gain' : 'insight-warn',
        text: b.beating_benchmark
          ? 'You are ahead of the index over this period.'
          : 'The index is ahead of you over this period.',
      }),
    ] : [el('span', { class: 'insight-caption', text: 'Not enough history yet to compare.' })]);
  } catch (err) {
    insightShell('benchmark', '◭', 'vs S&P 500',
      [el('span', { class: 'chart-error', text: err.message })]);
  }
}

function renderPlanBadge() {
  const sub = state.subscription;
  const badge = $('plan-badge');
  if (!sub) return;
  if (sub.is_trial) {
    badge.className = 'plan-badge trial';
    badge.textContent = `Pro trial · ${sub.trial_days_left}d left`;
  } else if (sub.is_premium) {
    badge.className = 'plan-badge pro';
    badge.textContent = sub.plan === 'lifetime' ? 'Lifetime' : 'Pro';
  } else {
    badge.className = 'plan-badge';
    badge.textContent = 'Free · Upgrade';
  }
}

function planFeatureLines(planId) {
  const m = state.plans?.feature_matrix || [];
  const key = planId === 'free' ? 'free' : 'pro';
  const lines = [];
  for (const group of m) {
    for (const row of group.rows) {
      const v = row[key];
      if (v === false) continue;
      lines.push({ text: v === true ? row.name : `${row.name}: ${v}`, on: true });
    }
  }
  return lines.slice(0, 8);
}

function renderPlans() {
  const data = state.plans;
  if (!data) return;
  const sub = state.subscription;
  const grid = $('plan-grid');
  grid.replaceChildren();

  const proId = state.billing === 'monthly' ? 'pro_monthly' : 'pro_annual';
  const order = ['free', proId, 'lifetime'];

  for (const id of order) {
    const plan = data.plans[id];
    if (!plan) continue;
    const isCurrent = sub.plan === id || (id === 'free' && !sub.is_premium);
    const featured = plan.highlight && !isCurrent;

    const card = el('div', {
      class: `plan${featured ? ' featured' : ''}${isCurrent ? ' current' : ''}`,
    });

    if (isCurrent) card.append(el('span', { class: 'plan-ribbon current-ribbon', text: 'Your plan' }));
    else if (featured) card.append(el('span', { class: 'plan-ribbon', text: 'Most popular' }));

    card.append(
      el('span', { class: 'plan-name', text: plan.name }),
      el('div', { class: 'plan-price' },
        el('span', { class: 'plan-amount', text: plan.price_label }),
        el('span', { class: 'plan-period', text: plan.period === 'forever' ? '' : `/ ${plan.period === 'one-off' ? 'once' : 'month'}` }),
        plan.price_sublabel ? el('p', { class: 'plan-sublabel', text: plan.price_sublabel }) : null),
      el('p', { class: 'plan-tagline', text: plan.tagline }),
      el('ul', { class: 'plan-features' },
        planFeatureLines(id).map(f => el('li', {},
          el('span', { class: 'tick', text: '✓', 'aria-hidden': 'true' }),
          el('span', { text: f.text })))));

    if (isCurrent) {
      card.append(el('button', { class: 'btn btn-ghost btn-block', type: 'button', disabled: true },
        'Current plan'));
    } else if (id === 'free') {
      card.append(el('button', {
        class: 'btn btn-ghost btn-block', type: 'button',
        onclick: downgrade,
      }, 'Switch to Free'));
    } else {
      card.append(el('button', {
        class: `btn ${featured ? 'btn-primary' : 'btn-ghost'} btn-block`, type: 'button',
        onclick: () => openUpgrade(id),
      }, plan.cta));
    }

    grid.append(card);
  }

  // comparison table
  const table = $('compare-table');
  const proLabel = 'Pro';
  table.replaceChildren(
    el('thead', {}, el('tr', {},
      el('th', { scope: 'col', text: 'Feature' }),
      el('th', { scope: 'col', class: 'plan-col', text: 'Free' }),
      el('th', { scope: 'col', class: 'plan-col pro-col', text: proLabel }))),
    el('tbody', {}, (data.feature_matrix || []).flatMap(group => [
      el('tr', { class: 'group-row' }, el('td', { colspan: '3', text: group.group })),
      ...group.rows.map(row => el('tr', {},
        el('td', { text: row.name }),
        el('td', { class: 'plan-col' }, cellValue(row.free)),
        el('td', { class: 'plan-col pro-col' }, cellValue(row.pro)))),
    ])));

  $('pricing-note').textContent = data.checkout_note;
}

function cellValue(v) {
  if (v === true) return el('span', { class: 'tick', text: '✓', title: 'Included' });
  if (v === false) return el('span', { class: 'cross', text: '—', title: 'Not included' });
  return el('span', { text: String(v) });
}

function openUpgrade(planId) {
  const plan = state.plans?.plans?.[planId];
  if (!plan) return;
  state.pendingPlan = planId;

  $('upgrade-title').textContent =
    planId === 'lifetime' ? 'Get lifetime access' : 'Upgrade to Pro';
  $('upgrade-price').textContent =
    plan.period === 'one-off'
      ? `${plan.price_label} once — yours forever`
      : `${plan.price_label} / month${plan.period === 'year' ? ', billed annually' : ''}`;

  $('upgrade-list').replaceChildren(
    ...(state.plans.premium_features || []).map(f => el('li', {},
      el('span', { class: 'tick', text: '✓', 'aria-hidden': 'true' }),
      el('span', { text: f.name }))));

  const trialBtn = $('upgrade-trial');
  trialBtn.textContent = planId === 'lifetime'
    ? `Try Pro free for ${state.plans.trial_days} days`
    : plan.cta;
  $('upgrade-note').textContent = state.plans.checkout_note;
  $('upgrade-dialog').showModal();
}

async function startTrial() {
  const btn = $('upgrade-trial');
  btn.disabled = true;
  const original = btn.textContent;
  btn.textContent = 'Unlocking…';
  try {
    const planId = state.pendingPlan === 'lifetime' ? 'pro_annual' : state.pendingPlan;
    await api('/api/subscription/trial', {
      method: 'POST', body: JSON.stringify({ plan_id: planId }),
    });
    $('upgrade-dialog').close();
    state.historyCache = {};
    await loadSubscription();
    await refreshAll();
    setBanners([{ type: 'info', message: `Pro unlocked. Enjoy your ${state.plans.trial_days}-day trial.` }]);
  } catch (err) {
    $('upgrade-note').textContent = err.message;
  } finally {
    btn.disabled = false;
    btn.textContent = original;
  }
}

async function goToCheckout() {
  const note = $('upgrade-note');
  note.textContent = 'Contacting checkout…';
  try {
    await api('/api/checkout', {
      method: 'POST', body: JSON.stringify({ plan_id: state.pendingPlan }),
    });
    note.textContent = 'Checkout complete.';
  } catch (err) {
    // Expected: no payment provider is connected in this build.
    note.textContent = err.message;
  }
}

async function downgrade() {
  if (!confirm('Switch back to the Free plan? Pro insights will be locked again.')) return;
  await api('/api/subscription/cancel', { method: 'POST' });
  state.historyCache = {};
  await loadSubscription();
  await refreshAll();
}

async function loadSubscription() {
  try {
    state.plans = await api('/api/plans');
    state.subscription = state.plans.subscription;
    renderPlanBadge();
    renderPlans();
    renderFeatureGrid();
    syncPeriodPicker();
  } catch (err) {
    setBanners([{ type: 'error', message: `Could not load plan information: ${err.message}` }]);
  }
}

function renderFeatureGrid() {
  const host = $('premium-feature-grid');
  host.replaceChildren(
    ...(state.plans?.premium_features || []).map(f => el('div', { class: 'feature-item' },
      el('span', { class: 'feature-icon', text: f.icon, 'aria-hidden': 'true' }),
      el('div', {},
        el('div', { class: 'feature-name', text: f.name }),
        el('p', { class: 'feature-blurb', text: f.blurb })))));
}

/* Free-plan history periods beyond 1y are gated; mark them so the picker can
 * show a lock instead of silently failing. */
function syncPeriodPicker() {
  const premium = state.subscription?.is_premium;
  for (const btn of $('period-picker').children) {
    const gated = !premium && btn.dataset.premium === 'true';
    btn.classList.toggle('gated', gated);
    btn.title = gated ? 'Pro feature' : '';
  }
}

/* ---------------------------------------------------------------- init -- */

async function init() {
  $('add-form').addEventListener('submit', submitAdd);
  $('f-symbol').addEventListener('input', scheduleValidate);
  $('refresh-btn').addEventListener('click', () => { state.historyCache = {}; refreshAll({ force: true }); });
  $('edit-save').addEventListener('click', saveEdit);
  $('edit-cancel').addEventListener('click', () => $('edit-dialog').close());

  $('upgrade-trial').addEventListener('click', startTrial);
  $('upgrade-checkout').addEventListener('click', goToCheckout);
  $('upgrade-cancel').addEventListener('click', () => $('upgrade-dialog').close());

  $('billing-toggle').addEventListener('click', (evt) => {
    const btn = evt.target.closest('button[data-billing]');
    if (!btn) return;
    state.billing = btn.dataset.billing;
    for (const b of $('billing-toggle').children) b.classList.toggle('active', b === btn);
    renderPlans();
  });

  $('period-picker').addEventListener('click', (evt) => {
    const btn = evt.target.closest('button[data-period]');
    if (!btn) return;
    state.period = btn.dataset.period;
    for (const b of $('period-picker').children) b.classList.toggle('active', b === btn);
    loadHistory();
  });

  // Plan first: the insight cards need to know whether to lock themselves.
  await loadSubscription();
  await refreshAll();

  // Auto-refresh live prices, but not while the tab is hidden.
  state.timer = setInterval(() => {
    if (!document.hidden) loadPortfolio({ force: true, silent: true });
  }, (state.subscription?.refresh_seconds ?? REFRESH_MS / 1000) * 1000);

  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) loadPortfolio({ force: true, silent: true });
  });

  // Keep the "3m ago" stamps honest between refreshes.
  setInterval(() => { if (state.portfolio && !document.hidden) renderHoldings(state.portfolio); }, 30_000);
}

document.addEventListener('DOMContentLoaded', init);
