// Shared state, formatting helpers and small widgets for every view.

const DEMO = new URLSearchParams(location.search).has('demo');
const LIVE_MAX = 1200;

const S = {
  identity: {}, host: null, containers: [], apps: { groups: [], links: [], counts: {} },
  history: { fields: [], live: [], day: [] }, events: [], tailnet: null, docker: {}, build: {},
  owner: '', connected: false, loaded: false,
  session: { authenticated: false, checked: false },
  reports: { unseen: 0, recent: [] }, reportCache: {}, control: null, account: null,
  ui: { appFilter: 'all', activity: 'all', procs: 'cpu', range: '1h', netRange: '1h',
    logSource: 'all', logLevel: 'all', logQ: '', follow: true },
  detail: { key: null, data: null, loading: false, error: null },
  logs: { key: null, lines: [], tail: 200, loading: false, error: null },
};

// ------------------------------------------------------------------ helpers
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const $ = (sel, root = document) => root.querySelector(sel);
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const isNum = (v) => typeof v === 'number' && Number.isFinite(v);

function gib(n, digits = 1) { return isNum(n) ? (n / 1024 ** 3).toFixed(digits) : '–'; }
function bytes(n) {
  if (!isNum(n)) return '–';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (Math.abs(n) >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n >= 100 || i === 0 ? n.toFixed(0) : n.toFixed(1)} ${units[i]}`;
}
// Drive sizes use decimal units, like the label on the drive.
function driveSize(n) {
  if (!isNum(n)) return '–';
  if (n >= 1e12) return `${+(n / 1e12).toFixed(1)} TB`;
  if (n < 1e9) return `${Math.max(1, Math.round(n / 1e6))} MB`;
  return `${Math.round(n / 1e9)} GB`;
}
function mbit(bps) { return isNum(bps) ? (bps * 8 / 1e6) : null; }
function fixed(v, d = 1) { return isNum(v) ? v.toFixed(d) : '–'; }
function pct(v) { return isNum(v) ? `${Math.round(v)}%` : '–'; }
function rate(bps) {
  if (!isNum(bps)) return '–';
  if (bps >= 1e6) return `${(bps / 1e6).toFixed(1)} MB/s`;
  if (bps >= 1e3) return `${(bps / 1e3).toFixed(0)} KB/s`;
  return `${Math.round(bps)} B/s`;
}
function duration(sec) {
  if (!isNum(sec)) return '–';
  sec = Math.max(0, Math.round(sec));
  const d = Math.floor(sec / 86400), h = Math.floor(sec % 86400 / 3600), m = Math.floor(sec % 3600 / 60);
  const plural = (n, w) => `${n} ${w}${n === 1 ? '' : 's'}`;
  if (d) return h ? `${plural(d, 'day')}, ${plural(h, 'hour')}` : plural(d, 'day');
  if (h) return m ? `${plural(h, 'hour')}, ${m} min` : plural(h, 'hour');
  if (m) return `${m} min`;
  return `${sec} s`;
}
function toEpoch(t) {
  if (t == null) return null;
  if (isNum(t)) return t;
  const ms = Date.parse(t);
  return Number.isNaN(ms) || ms < 0 ? null : ms / 1000;
}
function ago(t) {
  const e = toEpoch(t);
  if (e == null) return '';
  const s = Date.now() / 1000 - e;
  if (s < 45) return 'just now';
  return `${duration(s).split(',')[0]} ago`;
}
function clock(t, withDay = false) {
  const d = new Date(toEpoch(t) * 1000);
  const time = d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  const today = new Date().toDateString() === d.toDateString();
  return withDay && !today ? `${d.toLocaleDateString([], { month: 'short', day: 'numeric' })}, ${time}` : time;
}
function plural(n, one, many) { return `${n} ${n === 1 ? one : (many || one + 's')}`; }
function hostName() { return S.identity.hostname || location.hostname || 'stx-server'; }
function appUrl(app) {
  if (app.url) return app.url;
  if (!app.port) return null;
  const scheme = app.port === 443 ? 'https' : 'http';
  return `${scheme}://${location.hostname || hostName()}${app.port === 443 || app.port === 80 ? '' : ':' + app.port}`;
}

// ------------------------------------------------------------------- icons
const ICON = {
  overview: '<circle cx="11" cy="11" r="8"/><circle cx="11" cy="11" r="3"/>',
  apps: '<rect x="3" y="3" width="6.5" height="6.5"/><rect x="12.5" y="3" width="6.5" height="6.5"/><rect x="3" y="12.5" width="6.5" height="6.5"/><rect x="12.5" y="12.5" width="6.5" height="6.5"/>',
  system: '<polyline points="2,12 6,12 9,5 13,18 16,11 20,11" stroke-linejoin="round"/>',
  storage: '<ellipse cx="11" cy="5" rx="7" ry="2.5"/><path d="M4 5v12c0 1.4 3.1 2.5 7 2.5s7-1.1 7-2.5V5"/><path d="M4 11c0 1.4 3.1 2.5 7 2.5s7-1.1 7-2.5"/>',
  activity: '<path d="M8 5h11M8 11h11M8 17h11" stroke-linecap="round"/><circle cx="4" cy="5" r="1"/><circle cx="4" cy="11" r="1"/><circle cx="4" cy="17" r="1"/>',
  close: '<path d="M5 5 L17 17 M17 5 L5 17" stroke-linecap="round"/>',
  logs: '<rect x="2" y="3" width="18" height="16"/><path d="M6 9l3 2.5L6 14M11 14h5" stroke-linecap="round" stroke-linejoin="round"/>',
  control: '<path d="M4 6h14M4 11h14M4 16h14" stroke-linecap="round"/><circle cx="8" cy="6" r="2" fill="var(--bg)"/><circle cx="14" cy="11" r="2" fill="var(--bg)"/><circle cx="7" cy="16" r="2" fill="var(--bg)"/>',
  key: '<circle cx="8" cy="7" r="3.6"/><path d="M2 19c0-3 2.8-5.4 6-5.4 1.2 0 2.3.3 3.2.8"/><circle cx="16.5" cy="13.5" r="2.3"/><path d="M16.5 15.8V20M16.5 18.3h1.8" stroke-linecap="round"/>',
  face: '<path d="M4 7V5a1 1 0 0 1 1-1h2M15 4h2a1 1 0 0 1 1 1v2M18 15v2a1 1 0 0 1-1 1h-2M7 18H5a1 1 0 0 1-1-1v-2" stroke-linecap="round"/><path d="M8.5 8.5v1.3M13.5 8.5v1.3M11 8.5v3.5h-.8M8.5 14.5c1.4 1.1 3.6 1.1 5 0" stroke-linecap="round"/>',
  warn: '<path d="M11 3 L20 18 H2 Z" stroke-linejoin="round"/><path d="M11 9v4M11 15.5v.5" stroke-linecap="round"/>',
};
const icon = (name, size = 20) => `<svg width="${size}" height="${size}" viewBox="0 0 22 22" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.5">${ICON[name]}</svg>`;

// phone: shown in the bottom tab bar. Storage is reached from System on a phone.
const SECTIONS = [
  { id: 'overview', label: 'Overview', phone: true },
  { id: 'apps', label: 'Apps', phone: true },
  { id: 'logs', label: 'Logs', phone: true },
  { id: 'control', label: 'Control', phone: true },
  { id: 'system', label: 'System', phone: true },
  { id: 'storage', label: 'Storage', phone: false },
];

// ------------------------------------------------------------------- routes
function route() {
  const [path, query = ''] = location.hash.replace(/^#\/?/, '').split('?');
  const parts = path.split('/').filter(Boolean);
  if (parts[0] === 'activity') { parts.splice(0, 1, 'logs', 'events'); } // the old Activity page
  const section = SECTIONS.some((s) => s.id === parts[0]) ? parts[0] : 'overview';
  let sub = parts.slice(1).join('/');
  try { sub = decodeURIComponent(sub); } catch { /* keep raw */ }
  if (section === 'system' && !['performance', 'laptop', 'network'].includes(sub)) sub = 'performance';
  return { section, sub, params: new URLSearchParams(query) };
}

// ------------------------------------------------------- region rendering
// Each view draws a static shell once, then updates small regions by id.
// A region is only rewritten when its HTML changes, so scroll position,
// focus and the spinning ring survive the 3-second updates.
const regionCache = new WeakMap();
function region(id, html) {
  const el = document.getElementById(id);
  if (!el) return;
  if (regionCache.get(el) !== html) {
    el.innerHTML = html;
    regionCache.set(el, html);
  }
}

// -------------------------------------------------------------- derived data
function allApps() { return S.apps.groups.flatMap((g) => g.apps); }
function findApp(id) { return allApps().find((a) => a.id === id); }
const DOWNISH = new Set(['down', 'unhealthy', 'restarting']);

function hist(range = '1h') {
  const f = S.history.fields || [];
  const rows = range === '24h' ? S.history.day : S.history.live;
  const col = (name) => f.indexOf(name);
  return { rows: rows || [], col };
}
function series(name, range = '1h', limit = 0) {
  const { rows, col } = hist(range);
  const i = col(name);
  if (i < 0) return [];
  const out = rows.map((r) => [r[0], r[i]]);
  return limit ? out.slice(-limit) : out;
}
function physicalNics() { return (S.host?.network || []).filter((n) => n.physical); }
function netTotals() {
  const nics = physicalNics();
  const sum = (k) => nics.some((n) => isNum(n[k])) ? nics.reduce((a, n) => a + (n[k] || 0), 0) : null;
  return { rx: sum('rx_rate'), tx: sum('tx_rate') };
}
function maxFan() {
  const fans = S.host?.fans || [];
  return fans.length ? Math.max(...fans.map((f) => f.rpm)) : null;
}
function battery() { return S.host?.power_supply?.battery || null; }
function onBattery() { return S.host?.power_supply?.ac_online === false; }

function exitReason(code, oom) {
  if (oom) return 'It went over its memory limit, so Linux shut it down.';
  code = Number(code);
  if (code === 0) return 'It exited normally.';
  if (code === 137) return 'Exit code 137: it was killed, usually for running out of memory or a forced stop.';
  if (code === 143) return 'Exit code 143: it was asked to stop.';
  if (Number.isFinite(code)) return `Exit code ${code}: it crashed or quit with an error.`;
  return '';
}
function stoppedSentence(c) {
  const m = /Exited \((\d+)\)\s*(.*)/.exec(c?.status || '');
  if (m) return `Stopped ${m[2] || 'recently'}. ${exitReason(m[1])}`;
  if (c?.state === 'restarting') return 'It keeps crashing and Docker keeps restarting it.';
  if (c?.health === 'unhealthy') return 'Running, but its health check is failing.';
  return c?.status || 'Not running.';
}

function alerts() {
  const out = [];
  for (const a of allApps()) {
    if (!DOWNISH.has(a.status)) continue;
    const title = a.status === 'unhealthy' ? `${a.name} is unhealthy` : a.status === 'restarting' ? `${a.name} keeps restarting` : `${a.name} is down`;
    const report = S.reports.recent.find((r) => r.kind === 'container' && r.target === a.container?.name);
    out.push({ level: 'red', title, detail: stoppedSentence(a.container),
      href: report ? `#/logs/failures/${report.id}` : `#/apps/${encodeURIComponent(a.id)}`,
      action: report ? 'Read the failure report' : 'See what happened' });
  }
  if (S.docker?.enabled && S.docker.ok === false) {
    out.push({ level: 'red', title: "Can't reach Docker", detail: 'The socket proxy is not answering, so app status is unknown.', href: '#/activity', action: 'Open activity' });
  }
  if (onBattery()) {
    const b = battery();
    out.push({ level: 'amber', title: 'Running on battery', detail: `Wall power may be out. Battery at ${b?.capacity ?? '?'}%.`, href: '#/system/laptop', action: 'Check power' });
  }
  if (isNum(S.host?.cpu_temp) && S.host.cpu_temp >= 90) {
    out.push({ level: 'amber', title: `CPU is hot: ${Math.round(S.host.cpu_temp)} °C`, detail: 'It will slow itself down to cool off. Check the fans and vents.', href: '#/system/laptop', action: 'See sensors' });
  }
  for (const m of S.host?.mounts || []) {
    if (m.percent >= 90) out.push({ level: 'amber', title: `${m.mountpoint} is ${Math.round(m.percent)}% full`, detail: `${bytes(m.free)} left.`, href: '#/storage', action: 'Open storage' });
  }
  return out;
}

// ------------------------------------------------------------ small widgets
function ring({ value, max = 100, r = 54, size = 140, color = 'var(--cyan)', width = 9, outer = true, label = '' }) {
  const c = 2 * Math.PI * r;
  const frac = isNum(value) ? clamp(value / max, 0, 1) : 0;
  const mid = size / 2;
  return `<svg viewBox="0 0 ${size} ${size}" role="img" aria-label="${esc(label)}">
    ${outer ? `<circle cx="${mid}" cy="${mid}" r="${r + 10}" fill="none" stroke="var(--edge)" stroke-width="1" stroke-dasharray="2 4"/>` : ''}
    <circle cx="${mid}" cy="${mid}" r="${r}" fill="none" stroke="var(--track)" stroke-width="${width}"/>
    ${frac > 0 ? `<circle cx="${mid}" cy="${mid}" r="${r}" fill="none" stroke="${color}" stroke-width="${width}" stroke-linecap="round" stroke-dasharray="${(frac * c).toFixed(1)} ${c.toFixed(1)}" transform="rotate(-90 ${mid} ${mid})"/>` : ''}
  </svg>`;
}

function downsample(points, n) {
  if (points.length <= n) return points;
  const step = points.length / n;
  const out = [];
  for (let i = 0; i < n; i++) {
    const slice = points.slice(Math.floor(i * step), Math.floor((i + 1) * step));
    const vals = slice.map((p) => p[1]).filter(isNum);
    out.push([slice[0][0], vals.length ? vals.reduce((a, b) => a + b, 0) / vals.length : null]);
  }
  return out;
}

function sparkline(points, { min = 0, max = null } = {}) {
  const pts = downsample(points.filter((p) => isNum(p[1])), 60);
  if (pts.length < 2) return '<svg class="spark" viewBox="0 0 200 36" aria-hidden="true"></svg>';
  const vals = pts.map((p) => p[1]);
  const hi = max ?? (Math.max(...vals) * 1.15 || 1);
  const lo = min;
  const xy = pts.map((p, i) => `${(i * 200 / (pts.length - 1)).toFixed(1)},${(35 - clamp((p[1] - lo) / (hi - lo || 1), 0, 1) * 32).toFixed(1)}`).join(' ');
  return `<svg class="spark" viewBox="0 0 200 36" preserveAspectRatio="none" aria-hidden="true">
    <polygon points="0,36 ${xy} 200,36" fill="rgba(63,216,255,0.12)"/>
    <polyline points="${xy}" fill="none" stroke="var(--cyan)" stroke-width="1.5" vector-effect="non-scaling-stroke"/></svg>`;
}

function chart(lines, { max = null, height = 160 } = {}) {
  const W = 600, H = height;
  const all = lines.flatMap((l) => l.points.filter((p) => isNum(p[1])));
  if (all.length < 2) return `<div class="empty">Collecting data. The chart fills in over the next few minutes.</div>`;
  const t0 = Math.min(...all.map((p) => p[0])), t1 = Math.max(...all.map((p) => p[0]));
  const hi = max ?? (Math.max(...all.map((p) => p[1])) * 1.15 || 1);
  const x = (t) => ((t - t0) / (t1 - t0 || 1)) * W;
  const y = (v) => H - 2 - clamp(v / hi, 0, 1) * (H - 6);
  const grid = [0.25, 0.5, 0.75].map((f) => `<line x1="0" x2="${W}" y1="${(H - f * H).toFixed(1)}" y2="${(H - f * H).toFixed(1)}" stroke="var(--line)" stroke-width="1" vector-effect="non-scaling-stroke"/>`).join('');
  const paths = lines.map((l) => {
    const pts = downsample(l.points, 300);
    let d = '', pen = false;
    for (const [t, v] of pts) {
      if (!isNum(v)) { pen = false; continue; }
      d += `${pen ? 'L' : 'M'}${x(t).toFixed(1)},${y(v).toFixed(1)}`;
      pen = true;
    }
    return `<path d="${d}" fill="none" stroke="${l.color}" stroke-width="1.6" vector-effect="non-scaling-stroke"/>`;
  }).join('');
  return `<svg class="chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="${esc(lines.map((l) => l.label).join(' and '))} over time">${grid}${paths}</svg>`;
}
function chartLegend(lines, range) {
  return `<div class="chart-legend">${lines.map((l) => `<span><i style="background:${l.color}"></i>${esc(l.label)}</span>`).join('')}<span style="margin-left:auto">${range === '24h' ? 'Last 24 hours' : 'Last hour'}</span></div>`;
}
function rangeSeg(key) {
  return `<div class="seg" role="group" aria-label="Time range">${['1h', '24h'].map((r) => `<button type="button" data-action="range" data-key="${key}" data-value="${r}" aria-pressed="${S.ui[key] === r}">${r === '1h' ? '1 hour' : '24 hours'}</button>`).join('')}</div>`;
}
function panel(inner, cls = '') { return `<div class="panel ${cls}"><div class="in">${inner}</div></div>`; }


// ===================================================================== views
// Each section registers itself here: { shell(route) -> html, update(route) }.
const views = {};
// Set by app.js; lets any module ask for a redraw or a sign-in.
const hooks = { render: () => {}, signedOut: () => {}, signedIn: () => {} };

// ================================================================ requests
const net = { api: null }; // the demo backend in demo mode

class HttpError extends Error {
  constructor(status, message, data = {}) {
    super(message);
    this.status = status;
    this.data = data;
  }
}

async function detailOf(res) {
  try {
    const data = await res.json();
    return [data.detail || res.statusText, data];
  } catch {
    return [res.statusText, {}];
  }
}

async function getJSON(path) {
  if (net.api) return net.api.get(path);
  const res = await fetch(path, { cache: 'no-store', credentials: 'same-origin' });
  if (res.status === 401) { hooks.signedOut(); throw new HttpError(401, 'Sign in first'); }
  if (!res.ok) { const [msg, data] = await detailOf(res); throw new HttpError(res.status, msg, data); }
  return res.json();
}

// Every POST carries the session's CSRF token; the browser adds the Origin header.
async function postJSON(path, body) {
  if (net.api) return net.api.post(path, body ?? {});
  const res = await fetch(path, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': S.session.csrf || '' },
    body: JSON.stringify(body ?? {}),
  });
  if (res.status === 401 && !path.startsWith('api/auth/')) hooks.signedOut();
  if (!res.ok) { const [msg, data] = await detailOf(res); throw new HttpError(res.status, msg, data); }
  return res.json();
}

// ============================================================ confirm dialog
// Every action asks first. Risky ones can require typing a word (the hostname).
function confirmAction({ title, body, warning = '', typed = '', confirmLabel = 'Confirm', danger = false, kicker = 'Confirm' }) {
  return new Promise((resolve) => {
    const prev = document.activeElement;
    const root = document.createElement('div');
    root.className = 'dialog-wrap';
    const device = S.session.device ? `, from your ${esc(S.session.device.split(',')[0])}` : '';
    root.innerHTML = `
      <div class="dialog-backdrop" data-close></div>
      <div class="dialog ${danger ? 'danger' : ''}" role="dialog" aria-modal="true" aria-labelledby="dlg-title">
        <div class="in">
          <span class="grip" aria-hidden="true"></span>
          <div><span class="kicker ${danger ? 'red' : ''}">${esc(kicker)}</span><h2 id="dlg-title" class="dlg-title">${esc(title)}</h2></div>
          <p class="soft" style="margin:0">${body}</p>
          ${warning ? `<div class="dlg-warn">${icon('warn', 20)}<span>${warning}</span></div>` : ''}
          ${typed ? `<label class="field"><span>Type <b>${esc(typed)}</b> to confirm</span><input id="dlg-typed" type="text" autocomplete="off" autocapitalize="off" spellcheck="false"></label>` : ''}
          <span class="muted" style="font-size:13px">This goes in History as you${device}.</span>
          <div class="dlg-buttons">
            <button type="button" class="btn solid ${danger ? 'danger' : ''}" id="dlg-ok" ${typed ? 'disabled' : ''}>${esc(confirmLabel)}</button>
            <button type="button" class="btn quiet" data-close>Cancel</button>
          </div>
        </div>
      </div>`;
    document.body.appendChild(root);
    document.body.classList.add('modal-open');
    const ok = root.querySelector('#dlg-ok');
    const input = root.querySelector('#dlg-typed');
    const done = (value) => {
      root.remove();
      document.body.classList.remove('modal-open');
      document.removeEventListener('keydown', onKey, true);
      prev?.focus?.();
      resolve(value);
    };
    const onKey = (e) => {
      if (e.key === 'Escape') { e.stopPropagation(); done(false); }
      if (e.key === 'Tab') { // keep focus inside the dialog
        const items = [...root.querySelectorAll('button:not([disabled]), input')];
        const i = items.indexOf(document.activeElement);
        if (e.shiftKey && i <= 0) { e.preventDefault(); items[items.length - 1].focus(); }
        else if (!e.shiftKey && i === items.length - 1) { e.preventDefault(); items[0].focus(); }
      }
    };
    document.addEventListener('keydown', onKey, true);
    root.addEventListener('click', (e) => { if (e.target.closest('[data-close]')) done(false); });
    input?.addEventListener('input', () => { ok.disabled = input.value.trim() !== typed; });
    input?.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !ok.disabled) done(input.value.trim()); });
    ok.addEventListener('click', () => done(typed ? input.value.trim() : true));
    (input || ok).focus();
  });
}

// ===================================================================== toasts
let toastTimer = null;
function toast(text, tone = '') {
  let el = document.getElementById('toast');
  if (!el) {
    el = document.createElement('div');
    el.id = 'toast';
    el.setAttribute('role', 'status');
    el.setAttribute('aria-live', 'polite');
    document.body.appendChild(el);
  }
  el.className = `toast show ${tone}`;
  el.textContent = text;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = 'toast'; }, tone === 'busy' ? 60000 : 6000);
}

export {
  $, DEMO, DOWNISH, ICON, LIVE_MAX, S, SECTIONS, ago, alerts, allApps, appUrl, battery, bytes, chart, chartLegend,
  clamp, clock, downsample, driveSize, duration, esc, exitReason, findApp, fixed, gib, hist, hostName, icon, isNum,
  maxFan, mbit, netTotals, onBattery, panel, pct, physicalNics, plural, rangeSeg, rate, region, regionCache, ring,
  route, series, sparkline, stoppedSentence, toEpoch,
  views, hooks, net, HttpError, getJSON, postJSON, confirmAction, toast,
};
