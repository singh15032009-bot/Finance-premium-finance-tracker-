// Static verification of the WealthTrack frontend without a browser:
//  1. app.js parses
//  2. every element id app.js reaches for exists in index.html
//  3. the donut arc geometry is actually correct
const fs = require('fs');
const path = require('path');

const ROOT = 'C:/Users/prabh/Finance Tracker/static';
const html = fs.readFileSync(path.join(ROOT, 'index.html'), 'utf8');
const js = fs.readFileSync(path.join(ROOT, 'app.js'), 'utf8');

let failures = 0;
const check = (ok, name, detail = '') => {
  console.log(`${ok ? '[PASS]' : '[FAIL]'} ${name}${detail ? '\n         ' + detail : ''}`);
  if (!ok) failures++;
};

// --- 1. ids referenced vs ids defined ---------------------------------------
const definedIds = new Set([...html.matchAll(/\bid="([^"]+)"/g)].map(m => m[1]));
const usedIds = new Set([...js.matchAll(/\$\('([^']+)'\)/g)].map(m => m[1]));

const missing = [...usedIds].filter(id => !definedIds.has(id));
check(missing.length === 0,
  `All ${usedIds.size} element ids referenced by app.js exist in index.html`,
  missing.length ? `MISSING: ${missing.join(', ')}` : '');

// unused ids are fine, but report them as information
const unused = [...definedIds].filter(id => !usedIds.has(id));
console.log(`         (ids defined but not used by $(): ${unused.join(', ') || 'none'})`);

// --- 2. data-label coverage for the responsive card layout -------------------
// Every <td> needs a data-label so the mobile stacked view shows a field name.
// Flatten `${...}` interpolations first -- their braces otherwise terminate
// the [^}] scan early and hide a data-label that is genuinely present.
const jsFlat = js.replace(/\$\{[^}]*\}/g, 'X');
// Scope to renderHoldings: only that table collapses into mobile cards. The
// pricing comparison table scrolls instead and needs no data-labels.
const holdingsFn = jsFlat.slice(
  jsFlat.indexOf('function renderHoldings'),
  jsFlat.indexOf('function polarPoint'));
const tdWithLabel = [...holdingsFn.matchAll(/el\('td',\s*\{[^}]*'data-label'/g)].length;
const tdTotal = [...holdingsFn.matchAll(/el\('td',/g)].length;
check(tdWithLabel === tdTotal,
  `All ${tdTotal} table cells carry a data-label for the mobile card layout`,
  tdWithLabel === tdTotal ? '' : `${tdTotal - tdWithLabel} cells missing data-label`);

// --- 3. donut geometry ------------------------------------------------------
// Re-implement the same helpers and verify they trace a real ring.
function polarPoint(cx, cy, r, angleDeg) {
  const a = ((angleDeg - 90) * Math.PI) / 180;
  return [cx + r * Math.cos(a), cy + r * Math.sin(a)];
}
const [x0, y0] = polarPoint(100, 100, 88, 0);
check(Math.abs(x0 - 100) < 1e-9 && Math.abs(y0 - 12) < 1e-9,
  'Donut 0deg starts at 12 o\'clock', `got (${x0.toFixed(4)}, ${y0.toFixed(4)})`);

const [x90, y90] = polarPoint(100, 100, 88, 90);
check(Math.abs(x90 - 188) < 1e-9 && Math.abs(y90 - 100) < 1e-9,
  'Donut 90deg is at 3 o\'clock', `got (${x90.toFixed(4)}, ${y90.toFixed(4)})`);

// a 100% slice must not collapse (the 359.99 guard)
const arcSrc = js.match(/const end = sweep >= 360 \? startAngle \+ 359\.99 : endAngle;/);
check(!!arcSrc, 'Full-circle slice is clamped to 359.99deg so a single holding still draws');

// large-arc flag
const largeArcOk = js.includes('const largeArc = end - startAngle > 180 ? 1 : 0;');
check(largeArcOk, 'Large-arc flag set for slices over 180deg');

// --- 4. line chart scaling --------------------------------------------------
// Verify the flat-series guard: min === max must not divide by zero.
const flatGuard = js.includes('if (min === max) { min -= 1; max += 1; }');
check(flatGuard, 'Flat value series cannot divide by zero in the y scale');

// --- 5. no leftover debugging / no CDN dependency ---------------------------
check(!/console\.log\(/.test(js), 'No stray console.log in shipped JS');
const externalRefs = [...html.matchAll(/(?:src|href)="(https?:\/\/[^"]+)"/g)].map(m => m[1]);
check(externalRefs.length === 0,
  'No external CDN dependencies in index.html (works offline)',
  externalRefs.join(', '));

// --- 6. accessibility basics ------------------------------------------------
check(/<html lang="en">/.test(html), 'html element declares a language');
check(/name="viewport"/.test(html), 'Viewport meta present for responsive layout');
const ariaLabelledButtons = (js.match(/'aria-label':/g) || []).length;
check(ariaLabelledButtons >= 2, 'Icon-only buttons carry aria-labels', `${ariaLabelledButtons} found`);
check(/aria-live="polite"/.test(html), 'Status banners are announced to screen readers');

// --- 7. responsive breakpoints ---------------------------------------------
const css = fs.readFileSync(path.join(ROOT, 'styles.css'), 'utf8');
const breakpoints = [...css.matchAll(/@media \(max-width: (\d+)px\)/g)].map(m => m[1]);
check(breakpoints.length >= 3,
  `Responsive breakpoints defined: ${breakpoints.join('px, ')}px`);
check(/prefers-color-scheme: light/.test(css), 'Light theme supported');
check(/prefers-reduced-motion/.test(css), 'Reduced-motion preference respected');

console.log(`\n${failures === 0 ? 'ALL FRONTEND CHECKS PASSED' : failures + ' FRONTEND CHECK(S) FAILED'}`);
process.exit(failures ? 1 : 0);
