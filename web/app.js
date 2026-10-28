// stx-monitor web UI. Plain ES modules, no build step.
// State comes from /api/overview once, then live "tick" and "event" messages
// over Server-Sent Events (/api/stream). Add ?demo to the URL for fake data.

const DEMO = new URLSearchParams(location.search).has('demo');
const LIVE_MAX = 1200;

const S = {
  identity: {}, host: null, containers: [], apps: { groups: [], links: [], counts: {} },
  history: { fields: [], live: [], day: [] }, events: [], tailnet: null, docker: {}, build: {},
  owner: '', connected: false, loaded: false,
  ui: { appFilter: 'all', activity: 'all', procs: 'cpu', range: '1h', netRange: '1h' },
  detail: { key: null, data: null, loading: false, error: null },
  logs: { key: null, lines: [], tail: 200, loading: false, error: null },
};
let api = null; // set to the demo backend in demo mode

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
};
const icon = (name, size = 20) => `<svg width="${size}" height="${size}" viewBox="0 0 22 22" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.5">${ICON[name]}</svg>`;

const SECTIONS = [
  { id: 'overview', label: 'Overview' },
  { id: 'apps', label: 'Apps' },
  { id: 'system', label: 'System' },
  { id: 'storage', label: 'Storage' },
  { id: 'activity', label: 'Activity' },
];

// ------------------------------------------------------------------- routes
function route() {
  const parts = location.hash.replace(/^#\/?/, '').split('/').filter(Boolean);
  const section = SECTIONS.some((s) => s.id === parts[0]) ? parts[0] : 'overview';
  let sub = parts.slice(1).join('/');
  try { sub = decodeURIComponent(sub); } catch { /* keep raw */ }
  if (section === 'system' && !['performance', 'laptop', 'network'].includes(sub)) sub = 'performance';
  return { section, sub };
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
    out.push({ level: 'red', title, detail: stoppedSentence(a.container), href: `#/apps/${encodeURIComponent(a.id)}`, action: 'See what happened' });
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
const views = {};

// ------------------------------------------------------------------ overview
views.overview = {
  shell() {
    const arc = (id, r, color) => `<circle cx="180" cy="180" r="${r}" fill="none" stroke="var(--track)" stroke-width="8"/>
      <circle id="${id}" cx="180" cy="180" r="${r}" fill="none" stroke="${color}" stroke-width="8" stroke-linecap="round" stroke-dasharray="0 ${(2 * Math.PI * r).toFixed(1)}" transform="rotate(-90 180 180)"/>`;
    return `
    <div class="page-head"><div><span class="kicker">System overview</span><h1 id="r-greet"></h1></div><span class="clock num" id="r-clock"></span></div>
    <div class="ov-top">
      <div class="panel lg ov-core"><div class="in">
        <div class="core">
          <svg viewBox="0 0 360 360" id="core-svg" role="img" aria-label="Server core">
            <defs><radialGradient id="core-glow"><stop offset="0" stop-color="#3FD8FF" stop-opacity="0.35"/><stop offset="1" stop-color="#3FD8FF" stop-opacity="0"/></radialGradient></defs>
            <circle cx="180" cy="180" r="174" fill="none" stroke="var(--edge)" stroke-width="1"/>
            <g class="spin"><circle cx="180" cy="180" r="162" fill="none" stroke="#3FD8FF" stroke-opacity="0.55" stroke-width="10" stroke-dasharray="1.5 8.5"/></g>
            <circle cx="180" cy="180" r="144" fill="none" stroke="var(--dash)" stroke-width="1" stroke-dasharray="70 10 16 10"/>
            ${arc('arc-cpu', 124, 'var(--cyan)')}${arc('arc-mem', 106, 'var(--text)')}${arc('arc-heat', 88, 'var(--amber)')}
            <circle cx="180" cy="180" r="70" fill="url(#core-glow)"/>
            <circle cx="180" cy="180" r="70" fill="none" stroke="#3FD8FF" stroke-opacity="0.4" stroke-width="1"/>
          </svg>
          <div class="core-center" id="r-core-center"></div>
        </div>
        <div class="legend" id="r-legend"></div>
      </div></div>
      <div class="ov-side">
        <div id="r-alert"></div>
        <div class="panel"><div class="in" id="r-appsum"></div></div>
        <div class="cells" id="r-quick"></div>
      </div>
    </div>
    <div class="readouts" id="r-readouts"></div>`;
  },
  update() {
    const h = S.host || {};
    const hour = new Date().getHours();
    const greet = hour < 5 ? 'Good evening' : hour < 12 ? 'Good morning' : hour < 18 ? 'Good afternoon' : 'Good evening';
    region('r-greet', esc(S.owner ? `${greet}, ${S.owner}` : greet));
    region('r-clock', esc(new Date().toLocaleString([], { weekday: 'short', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })));

    const cpu = h.cpu?.percent, mem = h.memory?.percent, temp = h.cpu_temp;
    const setArc = (id, frac, r) => {
      const el = document.getElementById(id);
      if (!el) return;
      const c = 2 * Math.PI * r;
      el.setAttribute('stroke-dasharray', `${(clamp(frac || 0, 0, 1) * c).toFixed(1)} ${c.toFixed(1)}`);
    };
    setArc('arc-cpu', isNum(cpu) ? cpu / 100 : 0, 124);
    setArc('arc-mem', isNum(mem) ? mem / 100 : 0, 106);
    setArc('arc-heat', isNum(temp) ? temp / 100 : 0, 88);
    $('#core-svg')?.setAttribute('aria-label', `Server core: processor at ${pct(cpu)}, memory at ${pct(mem)}, heat at ${isNum(temp) ? Math.round(temp) + ' degrees' : 'unknown'}`);

    const counts = S.apps.counts || {};
    region('r-core-center', S.docker?.enabled === false
      ? `<span class="core-big">${pct(cpu)}</span><span class="muted" style="font-size:13px">processor</span>`
      : `<span class="core-big num">${counts.running ?? 0}/${counts.deployed ?? 0}</span><span class="muted" style="font-size:13px">apps running</span>`);
    region('r-legend', `
      <span><i style="background:var(--cyan)"></i>Processor ${pct(cpu)}</span>
      <span><i style="background:var(--text)"></i>Memory ${pct(mem)}</span>
      <span><i style="background:var(--amber)"></i>Heat ${isNum(temp) ? Math.round(temp) + ' °C' : '–'}</span>`);

    const list = alerts();
    region('r-alert', list.length ? panel(`
      <span class="kicker ${list[0].level === 'red' ? 'red' : 'amber'}">Needs attention${list.length > 1 ? ` · ${list.length} things` : ''}</span>
      <span class="alert-title">${esc(list[0].title)}</span>
      <span class="soft">${esc(list[0].detail)}</span>
      <a class="btn ${list[0].level === 'red' ? 'red' : ''}" style="align-self:flex-start" href="${list[0].href}">${esc(list[0].action)}</a>
      ${list.slice(1, 4).map((a) => `<a href="${a.href}" style="font-size:14px">${esc(a.title)}</a>`).join('')}`, list[0].level) : '');

    const bar = allApps().filter((a) => !a.unlisted).map((a) => `<i class="${a.status === 'running' || a.status === 'starting' ? 'run' : a.status === 'planned' ? 'plan' : 'down'}"></i>`).join('');
    region('r-appsum', `
      <div class="row-head"><span class="kicker">Apps</span><a href="#/apps" style="font-size:14px">Open Apps</a></div>
      <div class="counts">
        <div class="count"><b style="color:var(--cyan)">${counts.running ?? 0}</b><span>Running</span></div>
        <div class="count"><b style="color:${counts.down ? 'var(--red-soft)' : 'var(--muted)'}">${counts.down ?? 0}</b><span>Down</span></div>
        <div class="count"><b style="color:var(--muted)">${counts.planned ?? 0}</b><span>Planned</span></div>
      </div>
      <div class="segbar" aria-hidden="true">${bar}</div>`);

    const b = battery();
    const ps = h.power_supply;
    const power = !ps ? 'Unknown' : onBattery() ? `On battery, ${b?.capacity ?? '?'}%` : b ? `Wall, battery ${b.capacity ?? '?'}%` : 'Wall power';
    const tn = S.tailnet;
    const remote = tn ? `Tailscale, ${plural(tn.devices.filter((d) => !d.self && d.online).length, 'device')} on` : 'Tailscale not found';
    region('r-quick', `
      <div class="cell"><span class="note">Power</span><span class="plain">${esc(power)}</span></div>
      <div class="cell"><span class="note">Remote access</span><span class="plain">${esc(remote)}</span></div>`);

    const p = h.power || {};
    const fan = maxFan();
    const readout = (label, value, unit, note, pts, opts) => panel(`
      <span class="kicker">${label}</span>
      <span class="big num">${value}<small> ${unit}</small></span>
      ${sparkline(pts, opts)}
      <span class="muted" style="font-size:13px">${note}</span>`, 'sm readout');
    region('r-readouts', [
      readout('Processor', isNum(cpu) ? Math.round(cpu) : '–', '%', `${S.identity.cpu_threads || '?'} threads at ${isNum(h.cpu?.freq_mhz) ? (h.cpu.freq_mhz / 1000).toFixed(1) + ' GHz' : '–'}`, series('cpu', '1h', 400), { max: 100 }),
      readout('Memory', gib(h.memory?.used), 'GB', `of ${gib(h.memory?.total)} GB`, series('mem', '1h', 400), { max: 100 }),
      readout('Heat', isNum(temp) ? Math.round(temp) : '–', '°C', fan == null ? 'No fan sensor' : fan === 0 ? 'Fans resting' : `Fans at ${fan.toLocaleString()} rpm`, series('temp', '1h', 400), { min: 25, max: 100 }),
      readout('Power', isNum(p.watts) ? Math.round(p.watts) : '–', 'W', isNum(p.monthly_cost) ? `${p.source === 'battery' ? 'Whole system' : 'CPU only'}, about $${p.monthly_cost.toFixed(2)} a month` : esc(p.label || 'No sensor'), series('watts', '1h', 400), {}),
    ].join(''));
  },
};

// ---------------------------------------------------------------------- apps
const STATUS_WORD = { running: 'Running', down: 'Down', unhealthy: 'Unhealthy', restarting: 'Restarting', paused: 'Paused', starting: 'Starting', planned: 'Planned' };

views.apps = {
  shell() {
    return `
    <div class="page-head"><div><span class="kicker">Apps</span><h1 id="r-apps-title"></h1></div><div class="chips" role="group" aria-label="Show apps" id="r-filter"></div></div>
    <div id="r-apps-note"></div>
    <div class="apps-layout">
      <div class="apps-groups" id="r-groups"></div>
      <aside id="r-drawer-box" aria-labelledby="d-title"></aside>
    </div>`;
  },
  update(r) {
    const apps = allApps();
    const planned = apps.filter((a) => !a.unlisted).length;
    const c = S.apps.counts || {};
    region('r-apps-title', `${plural(planned, 'app')} on the plan`);
    const f = S.ui.appFilter;
    const chip = (id, label) => `<button type="button" class="chip" data-action="app-filter" data-value="${id}" aria-pressed="${f === id}">${label}</button>`;
    region('r-filter', chip('all', `All ${c.total ?? 0}`) + chip('running', `Running ${c.running ?? 0}`) + chip('down', `Down ${c.down ?? 0}`) + chip('planned', `Planned ${c.planned ?? 0}`));
    const notes = [];
    if (S.apps.error) notes.push(`apps.yaml problem: ${esc(S.apps.error)}`);
    if (S.docker?.enabled === false) notes.push('Docker is turned off (DOCKER_HOST=none), so every app shows as planned.');
    else if (S.docker?.ok === false) notes.push("Can't reach Docker right now, so app status may be stale.");
    region('r-apps-note', notes.map((n) => `<div class="banner">${n}</div>`).join(''));

    const selected = r.sub ? findApp(r.sub) : null;
    const linked = new Set();
    if (selected) {
      for (const [a, b] of S.apps.links || []) {
        if (a === selected.id) linked.add(b);
        if (b === selected.id) linked.add(a);
      }
    }
    const show = (a) => f === 'all' || (f === 'running' && (a.status === 'running' || a.status === 'starting')) || (f === 'down' && DOWNISH.has(a.status)) || (f === 'planned' && a.status === 'planned');
    const groups = S.apps.groups.map((g) => {
      const vis = g.apps.filter(show);
      if (!vis.length) return '';
      const n = (s) => g.apps.filter((a) => s(a)).length;
      const bits = [];
      if (n((a) => a.status === 'running')) bits.push(`${n((a) => a.status === 'running')} running`);
      if (n((a) => DOWNISH.has(a.status))) bits.push(`${n((a) => DOWNISH.has(a.status))} down`);
      if (n((a) => a.status === 'planned')) bits.push(`${n((a) => a.status === 'planned')} planned`);
      return `<section class="group" aria-labelledby="g-${esc(g.id)}">
        <div class="group-head"><h2 id="g-${esc(g.id)}">${esc(g.name)}</h2><span class="muted" style="font-size:13px">${bits.join(', ')}</span></div>
        <div class="cards">${vis.map((a) => card(a, selected?.id === a.id, linked.has(a.id))).join('')}</div>
      </section>`;
    }).join('');
    region('r-groups', groups || '<div class="empty">Nothing matches this filter.</div>');

    const box = document.getElementById('r-drawer-box');
    if (!selected) {
      box.className = 'drawer-empty';
      region('r-drawer-box', `<span class="kicker">Details</span><span>Pick an app to see what it does, how it is doing, and its latest log lines.</span>`);
      return;
    }
    if (selected.container && S.detail.key !== selected.container.id) loadDetail(selected);
    const tone = DOWNISH.has(selected.status) ? 'red' : '';
    box.className = `drawer open panel lg ${tone}`;
    region('r-drawer-box', `<div class="in" data-app="${esc(selected.id)}">
      <div class="drawer-head"><div id="r-d-title"></div><a class="icon-btn" href="#/apps" aria-label="Close details">${icon('close', 16)}</a></div>
      <div id="r-d-body" style="display:flex;flex-direction:column;gap:16px"></div>
      ${selected.container ? `<div style="display:flex;flex-direction:column;gap:8px">
        <div class="row-head"><span class="kicker">Last log lines</span><span class="chips"><button type="button" class="chip" data-action="refresh-logs">Refresh</button><button type="button" class="chip" data-action="copy-logs">Copy</button></span></div>
        <pre class="logs" id="r-logs" tabindex="0" aria-label="Log lines"></pre>
        <div id="r-logs-more"></div>
      </div>` : ''}
      <div id="r-d-actions" style="display:flex;flex-direction:column;gap:10px"></div>
    </div>`);
    drawer(selected);
  },
};

function card(a, current, linked) {
  const href = `#/apps/${encodeURIComponent(a.id)}`;
  const cur = current ? 'aria-current="true"' : '';
  if (a.status === 'planned') {
    return `<a class="card plan ${linked ? 'linked' : ''}" href="${href}" ${cur}><span class="in">
      <span class="card-name">${esc(a.name)}</span><span class="card-role">${esc(a.description)}</span><span class="card-state">Planned</span></span></a>`;
  }
  const down = DOWNISH.has(a.status);
  const warn = a.status === 'paused' || a.status === 'starting';
  const c = a.container || {};
  let state;
  if (down) state = a.status === 'down' ? `Down${/Exited \(\d+\) (.*)/.test(c.status || '') ? ', ' + /Exited \(\d+\) (.*)/.exec(c.status)[1] : ''}` : STATUS_WORD[a.status];
  else if (a.status === 'running') state = `${isNum(c.cpu) ? fixed(c.cpu, c.cpu < 10 ? 1 : 0) + '% CPU' : 'Running'}${isNum(c.mem) ? ', ' + bytes(c.mem) : ''}`;
  else state = STATUS_WORD[a.status] || a.status;
  const dot = down ? 'red' : warn ? 'amber' : '';
  return `<a class="card ${down ? 'is-down' : warn ? 'is-warn' : ''} ${linked ? 'linked' : ''}" href="${href}" ${cur}><span class="in">
    <span class="card-top"><span class="card-name">${esc(a.name)}</span><span class="dot ${dot}" aria-hidden="true"></span></span>
    <span class="card-role">${esc(a.description)}</span>
    <span class="card-state num">${esc(state)}<span class="sr-only">, status ${esc(STATUS_WORD[a.status] || a.status)}</span></span></span></a>`;
}

function drawer(app) {
  const c = app.container;
  const group = S.apps.groups.find((g) => g.id === app.group);
  const d = S.detail.key === c?.id ? S.detail.data : null;
  const ins = d?.inspect || {};
  const down = DOWNISH.has(app.status);
  const kicker = `${STATUS_WORD[app.status] || app.status}, ${group?.name || ''}`;
  region('r-d-title', `<span class="kicker ${down ? 'red' : app.status === 'planned' ? '' : ''}">${esc(kicker)}</span><h2 id="d-title">${esc(app.name)}</h2>`);

  let sentence;
  if (app.status === 'planned') {
    sentence = `<p style="margin:0;font-size:16px">${esc(app.description)}. Planned, not deployed yet.</p>
      <p class="soft" style="margin:0;font-size:14px">It shows up here as soon as a container named <code>${esc(app.id)}</code> runs, or any container with the label <code>stx.app=${esc(app.id)}</code>.</p>`;
  } else if (down) {
    const reason = ins.finished_at && toEpoch(ins.finished_at)
      ? `Stopped ${ago(ins.finished_at)}. ${exitReason(ins.exit_code, ins.oom_killed)}`
      : stoppedSentence(c);
    sentence = `<p style="margin:0;font-size:16px">${esc(reason)}</p>`;
  } else {
    const up = toEpoch(ins.started_at);
    sentence = `<p style="margin:0;font-size:16px">${esc(app.description)}.${up ? ` Up for ${esc(duration(Date.now() / 1000 - up))}.` : ''}${c?.health === 'healthy' ? ' Health check passing.' : ''}</p>`;
  }

  const name = (id) => findApp(id)?.name || id;
  const talks = app.links.map((id) => `<a href="#/apps/${encodeURIComponent(id)}">${esc(name(id))}</a>`);
  const usedBy = (S.apps.links || []).filter(([, b]) => b === app.id).map(([a]) => `<a href="#/apps/${encodeURIComponent(a)}">${esc(name(a))}</a>`);
  const url = appUrl(app);
  const facts = [];
  if (url) facts.push(['Address', `<a href="${esc(url)}" target="_blank" rel="noopener">${esc(url.replace(/^https?:\/\//, ''))}</a>`]);
  else if (app.port) facts.push(['Port', esc(app.port)]);
  if (c) {
    facts.push(['Image', esc(c.image || ins.image || '–')]);
    if (c.state === 'running') {
      facts.push(['Processor', `${fixed(c.cpu)}% of the machine`]);
      facts.push(['Memory', `${bytes(c.mem)}${c.mem_limit && c.mem_limit < (S.host?.memory?.total || Infinity) ? ` of ${bytes(c.mem_limit)} limit` : ''}`]);
    }
    if (down && ins.started_at && ins.finished_at && toEpoch(ins.finished_at) > toEpoch(ins.started_at)) {
      facts.push(['Ran for', esc(duration(toEpoch(ins.finished_at) - toEpoch(ins.started_at)))]);
    }
    if (isNum(ins.restart_count)) facts.push(['Restarts', esc(ins.restart_count)]);
    if (ins.restart_policy) facts.push(['Restart policy', esc(ins.restart_policy)]);
    facts.push(['Container', `<code>${esc(c.name)}</code>`]);
  }
  if (talks.length) facts.push(['Talks to', talks.join(', ')]);
  if (usedBy.length) facts.push(['Used by', usedBy.join(', ')]);
  region('r-d-body', sentence + (facts.length ? `<dl class="facts">${facts.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('')}</dl>` : ''));

  if (c) {
    const L = S.logs;
    let logs;
    if (L.key !== c.id || (L.loading && !L.lines.length)) logs = '<span class="ts">Loading…</span>';
    else if (L.error) logs = `<span class="err">${esc(L.error)}</span>`;
    else if (!L.lines.length) logs = '<span class="ts">No log lines yet.</span>';
    else {
      const shown = L.lines.slice(-(L.tail >= 2000 ? 2000 : 40));
      logs = shown.map((l) => {
        const cls = /\b(error|fatal|panic|exception|fail(ed)?)\b/i.test(l.text) || l.stream === 'stderr' && /err/i.test(l.text) ? 'err' : /\bwarn(ing)?\b/i.test(l.text) ? 'warn' : '';
        return `<div${cls ? ` class="${cls}"` : ''}><span class="ts">${l.ts ? esc(clock(l.ts)) : ''}</span> ${esc(l.text)}</div>`;
      }).join('');
    }
    region('r-logs', logs);
    region('r-logs-more', L.tail >= 2000 ? `<span class="muted" style="font-size:13px">Showing the last ${L.lines.length} lines.</span>` : `<button type="button" class="chip" data-action="more-logs">Show all logs</button>`);
  }
  region('r-d-actions', app.status === 'planned' ? '' : `
    <div style="display:flex;flex-wrap:wrap;gap:10px">
      <button type="button" class="btn" disabled>Restart</button>
      ${url ? `<a class="btn" href="${esc(url)}" target="_blank" rel="noopener">Open ${esc(app.name)}</a>` : ''}
    </div>
    <span class="muted" style="font-size:13px">Restart arrives with actions in a later version.${down && c ? ` For now: <code>docker start ${esc(c.name)}</code>` : ''}</span>`);
}

// -------------------------------------------------------------------- system
views.system = {
  shell(r) {
    return `
    <div class="page-head"><div><span class="kicker">System</span><h1 id="r-sys-title"></h1></div></div>
    <nav class="seg tabs" aria-label="System view">
      ${['performance', 'laptop', 'network'].map((t) => `<a href="#/system/${t}" ${r.sub === t ? 'aria-current="page"' : ''}>${t[0].toUpperCase() + t.slice(1)}</a>`).join('')}
    </nav>
    <div id="r-sys" style="display:flex;flex-direction:column;gap:24px"></div>`;
  },
  update(r) {
    const h = S.host || {};
    const temp = h.cpu_temp;
    const title = !S.host ? 'Waiting for data'
      : h.cpu?.throttling ? 'Slowing down to stay cool'
      : isNum(temp) && temp >= 85 ? 'Running hot'
      : isNum(h.cpu?.percent) && h.cpu.percent >= 70 ? 'Working hard'
      : 'Running cool and quiet';
    region('r-sys-title', esc(title));
    region('r-sys', r.sub === 'laptop' ? laptopTab() : r.sub === 'network' ? networkTab() : performanceTab());
  },
};

function gauge(label, value, unit, note, opts) {
  return panel(`<div class="gauge-ring">${ring({ value: opts.frac * 100, ...opts, label: `${label} ${value} ${unit}` })}<div class="num">${value}<small>${unit ? ' ' + unit : ''}</small></div></div>
    <span class="label" style="color:${opts.labelColor || 'var(--cyan)'}">${label.toUpperCase()}</span><span class="note">${note}</span>`, 'sm gauge');
}

function performanceTab() {
  const h = S.host || {};
  const cpu = h.cpu || {}, mem = h.memory || {}, p = h.power || {};
  const temp = h.cpu_temp;
  const fan = maxFan();
  const gauges = `<div class="gauges">
    ${gauge('Processor', isNum(cpu.percent) ? Math.round(cpu.percent) : '–', '%', `${S.identity.cpu_threads || '?'} threads, ${isNum(cpu.freq_mhz) ? (cpu.freq_mhz / 1000).toFixed(1) + ' GHz' : '–'}`, { frac: (cpu.percent || 0) / 100 })}
    ${gauge('Memory', gib(mem.used), 'GB', `of ${gib(mem.total)} GB${mem.swap_used ? `, ${bytes(mem.swap_used)} swap` : ''}`, { frac: (mem.percent || 0) / 100, color: 'var(--text)' })}
    ${gauge('Heat', isNum(temp) ? Math.round(temp) : '–', '°C', fan == null ? 'No fan sensor' : fan === 0 ? 'Fans resting' : `Fans ${fan.toLocaleString()} rpm`, { frac: (temp || 0) / 100, color: 'var(--amber)', labelColor: 'var(--amber)' })}
    ${gauge('Power', isNum(p.watts) ? Math.round(p.watts) : '–', 'W', isNum(p.monthly_cost) ? `${p.source === 'battery' ? 'Whole system' : 'CPU package'}, ~$${p.monthly_cost.toFixed(2)}/month` : 'No sensor', { frac: (p.watts || 0) / 45 })}
  </div>`;
  const range = S.ui.range;
  const lines = [
    { label: 'Processor %', color: 'var(--cyan)', points: series('cpu', range) },
    { label: 'Memory %', color: 'var(--text)', points: series('mem', range) },
  ];
  const histPanel = panel(`<div class="row-head"><span class="kicker">History</span>${rangeSeg('range')}</div>${chart(lines, { max: 100 })}${chartLegend(lines, range)}`);
  const cores = (cpu.per_core || []).map((v, i) => `<div class="core-bar"><div class="track" title="Thread ${i}: ${Math.round(v)}%"><div class="fill" style="height:${clamp(v, 0, 100)}%"></div></div><span class="num">${Math.round(v)}</span></div>`).join('');
  const thr = cpu.throttle;
  const load = cpu.load ? cpu.load.map((l) => l.toFixed(2)).join(', ') : '–';
  const corePanel = panel(`<div class="row-head"><span class="kicker">Each thread</span><span class="muted" style="font-size:13px">%</span></div>
    <div class="cores">${cores || '<span class="empty">–</span>'}</div>
    <dl class="facts">
      <dt>Load average</dt><dd class="num">${load} <span class="muted">(1, 5, 15 min)</span></dd>
      <dt>Throttling</dt><dd>${!thr ? 'Not reported' : cpu.throttling ? 'Happening now' : thr.core + thr.package === 0 ? 'None since boot' : `${(thr.core + thr.package).toLocaleString()} times since boot`}</dd>
      <dt>Processes</dt><dd class="num">${h.processes?.count ?? '–'}</dd>
    </dl>
    <span class="muted" style="font-size:13px">Load average is how many tasks wanted the CPU at once. Under ${S.identity.cpu_threads || 12} means there is room to spare.</span>`);
  const key = S.ui.procs === 'mem' ? 'by_mem' : 'by_cpu';
  const names = new Map(S.containers.map((c) => [c.id, c.name]));
  const rows = (h.processes?.[key] || []).map((pr) => `<tr><td>${esc(pr.name)}${pr.container ? `<span class="tag">${esc(names.get(pr.container) || 'container')}</span>` : ''}<br><span class="muted" style="font-size:12px">${esc(pr.user || '')} · pid ${pr.pid}</span></td><td class="r num">${fixed(pr.cpu)}%</td><td class="r num muted">${bytes(pr.rss)}</td></tr>`).join('');
  const procs = panel(`<div class="row-head"><span class="kicker">Busiest right now</span>
    <div class="seg" role="group" aria-label="Sort processes"><button type="button" data-action="procs" data-value="cpu" aria-pressed="${S.ui.procs === 'cpu'}">By CPU</button><button type="button" data-action="procs" data-value="mem" aria-pressed="${S.ui.procs === 'mem'}">By memory</button></div></div>
    <table class="procs"><thead><tr><th>Process</th><th class="r">CPU</th><th class="r">Memory</th></tr></thead><tbody>${rows || '<tr><td colspan="3" class="empty">–</td></tr>'}</tbody></table>`);
  return gauges + `<div class="two">${histPanel}${corePanel}</div>` + procs;
}

function laptopTab() {
  const h = S.host || {};
  const ps = h.power_supply;
  const b = battery();
  let battery_panel;
  if (!ps) {
    battery_panel = panel('<span class="kicker">Battery</span><span class="muted">No power supply information found in /sys/class/power_supply.</span>');
  } else {
    const ac = ps.ac_online;
    let note;
    if (!ac) note = `Wall power may be out.${isNum(b?.power_w) ? ` Drawing ${b.power_w.toFixed(1)} W.` : ''}`;
    else if (b?.threshold_end) note = `Charging stops at ${b.threshold_end}% to protect the battery.`;
    else if (b && /not charging|full/i.test(b.status || '')) note = `Holding at ${b.capacity}%: the charge limit set in the BIOS is doing its job.`;
    else if (b?.status === 'Charging') note = 'Charging.';
    else note = 'Plugged in.';
    battery_panel = panel(`<div style="display:flex;align-items:center;gap:18px;flex-wrap:wrap">
      ${b ? `<div class="gauge-ring" style="width:112px;height:112px">${ring({ value: b.capacity, color: ac ? 'var(--cyan)' : 'var(--amber)', outer: false, label: `Battery at ${b.capacity} percent` })}<div class="num" style="font-size:26px">${b.capacity ?? '–'}%</div></div>` : ''}
      <div style="display:flex;flex-direction:column;gap:2px;flex:1;min-width:180px"><span class="kicker ${ac ? '' : 'amber'}">${ac ? 'Wall power' : 'On battery'}</span><span style="font:600 22px/1.2 var(--head)">${ac ? 'On wall power' : 'Running on battery'}</span><span class="soft" style="font-size:14px">${esc(note)}</span></div>
    </div>`, ac ? '' : 'amber');
  }
  const facts = [];
  if (b) {
    facts.push(['Battery health', `${isNum(b.health) ? b.health.toFixed(0) + '% of its original capacity' : '–'}${isNum(b.cycle_count) ? `, ${b.cycle_count} cycles` : ''}`]);
    if (b.threshold_start && b.threshold_end) facts.push(['Charge window', `${b.threshold_start}–${b.threshold_end}%`]);
  }
  facts.push(['Lid', h.lid ? h.lid[0].toUpperCase() + h.lid.slice(1) : 'Not reported']);
  for (const g of h.gpus || []) {
    const label = g.name ? `${g.vendor} ${g.name.replace(/^(Intel|NVIDIA|AMD)\s+/, '')}` : `${g.vendor} GPU`;
    let state;
    if (g.vendor === 'Intel') state = g.driver && g.render_node ? `Ready for Quick Sync (${g.render_node}, ${g.driver})` : g.driver ? `Driver ${g.driver}, no render node` : 'No driver loaded';
    else if (g.runtime_status === 'suspended') state = `Asleep, saving power${g.driver ? ` (${g.driver})` : ''}`;
    else if (g.runtime_status === 'active') state = `Awake${g.driver ? ` (${g.driver})` : ''}`;
    else state = `${g.runtime_status || 'Unknown state'}${g.driver ? `, ${g.driver}` : ', no driver'}`;
    facts.push([esc(label), esc(state)]);
  }
  if ((h.gpus || []).length === 0) facts.push(['Graphics', 'No GPU found on the PCI bus']);
  facts.push(['Transcoding', h.render_node ? '<code>/dev/dri/renderD128</code> is present' : 'No <code>/dev/dri/renderD128</code> yet']);
  const thr = h.cpu?.throttle;
  facts.push(['Throttling', !thr ? 'Not reported' : h.cpu.throttling ? 'Happening now' : thr.core + thr.package === 0 ? 'None since boot' : `${(thr.core + thr.package).toLocaleString()} times since boot`]);

  const temps = (h.temps || []).filter((t) => !/^Core \d+$/.test(t.label) || t.chip !== 'coretemp');
  const coreTemps = (h.temps || []).filter((t) => t.chip === 'coretemp' && /^Core \d+$/.test(t.label));
  const hottestCore = coreTemps.length ? Math.max(...coreTemps.map((t) => t.c)) : null;
  const sensorCells = [
    ...temps.map((t) => `<div class="cell"><span class="note">${esc(t.chip)} · ${esc(t.label)}</span><span class="value num" style="${t.crit && t.c >= t.crit - 10 ? 'color:var(--amber)' : ''}">${Math.round(t.c)}<small> °C</small></span></div>`),
    ...(hottestCore != null ? [`<div class="cell"><span class="note">coretemp · hottest core</span><span class="value num">${Math.round(hottestCore)}<small> °C</small></span></div>`] : []),
    ...(h.fans || []).map((f) => `<div class="cell"><span class="note">${esc(f.chip)} · ${esc(f.label)}</span><span class="value num">${f.rpm.toLocaleString()}<small> rpm</small></span></div>`),
  ].join('');
  return battery_panel
    + panel(`<span class="kicker">This laptop</span><dl class="facts">${facts.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('')}</dl>`)
    + `<section style="display:flex;flex-direction:column;gap:10px"><h2>Sensors</h2>${sensorCells ? `<div class="sensors">${sensorCells}</div>` : '<span class="empty">No hwmon sensors found.</span>'}</section>`;
}

function networkTab() {
  const t = netTotals();
  const rangeKey = S.ui.netRange;
  const cells = `<div class="cells">
    <div class="cell"><span class="label">Down</span><span class="value num">${fixed(mbit(t.rx))} <small>Mb/s</small></span></div>
    <div class="cell"><span class="label">Up</span><span class="value num">${fixed(mbit(t.tx))} <small>Mb/s</small></span></div></div>`;
  const nics = (S.host?.network || []).map((n) => {
    const kind = n.kind === 'ethernet' ? 'Ethernet' : n.kind === 'wifi' ? 'Wi-Fi' : n.kind === 'tailscale' ? 'Tailscale' : n.kind;
    const speed = n.speed_mbps ? ` at ${n.speed_mbps >= 1000 ? n.speed_mbps / 1000 + ' Gb/s' : n.speed_mbps + ' Mb/s'}` : '';
    return `<div class="list-row"><span class="ring-dot ${n.state === 'up' || n.state === 'unknown' ? 'on' : ''}" aria-hidden="true"></span>
      <span style="flex:1">${esc(kind)}${esc(speed)} <span class="muted" style="font-size:12px">${esc(n.name)}${n.ipv4 ? ', ' + esc(n.ipv4) : ''}</span></span>
      <span class="num muted" style="font-size:13px">↓ ${fixed(mbit(n.rx_rate))} ↑ ${fixed(mbit(n.tx_rate))} Mb/s</span></div>`;
  }).join('');
  const toMbit = (pts) => pts.map(([ts, v]) => [ts, isNum(v) ? v * 8 / 1e6 : null]);
  const lines = [
    { label: 'Down Mb/s', color: 'var(--cyan)', points: toMbit(series('rx', rangeKey)) },
    { label: 'Up Mb/s', color: 'var(--amber)', points: toMbit(series('tx', rangeKey)) },
  ];
  const tn = S.tailnet;
  let ts;
  if (!tn) ts = '<span class="muted">Tailscale status is not available. stx-monitor looks for <code>/run/tailscale/tailscaled.sock</code> on the host.</span>';
  else {
    ts = `<span class="muted" style="font-size:13px">Tailnet ${esc(tn.tailnet || '')}, ${tn.state === 'Running' ? 'connected' : esc(tn.state || 'unknown')}</span>` + tn.devices.map((d) => `<div class="list-row"><span class="ring-dot ${d.online ? 'on' : ''}" aria-hidden="true"></span>
      <span style="flex:1">${esc(d.name)} <span class="muted" style="font-size:12px">${d.self ? 'this server' : esc(d.os || '')}${d.ip ? ' · ' + esc(d.ip) : ''}</span></span>
      <span style="font-size:13px;color:${d.online ? 'var(--text)' : 'var(--muted)'}">${d.online ? 'Online' : d.last_seen ? `Seen ${esc(ago(d.last_seen))}` : 'Offline'}</span></div>`).join('');
  }
  return cells
    + panel(`<div class="row-head"><span class="kicker">Traffic</span>${rangeSeg('netRange')}</div>${chart(lines)}${chartLegend(lines, rangeKey)}`)
    + `<div class="two">${panel(`<span class="kicker">Network cards</span><div>${nics || '<span class="empty">No interfaces found.</span>'}</div>`)}${panel(`<span class="kicker">Your devices on Tailscale</span><div>${ts}</div>`)}</div>`;
}

// ------------------------------------------------------------------- storage
views.storage = {
  shell() {
    return `<div class="page-head"><div><span class="kicker">Storage</span><h1 id="r-st-title"></h1></div></div>
      <div class="disks" id="r-disks"></div>
      <section style="display:flex;flex-direction:column;gap:10px"><h2>Mounted filesystems</h2><div id="r-mounts" class="mounts"></div></section>
      <div class="cells" id="r-io"></div>`;
  },
  update() {
    const h = S.host || {};
    const disks = h.disks || [];
    const idle = disks.filter((d) => d.state !== 'mounted');
    region('r-st-title', !S.host ? 'Waiting for data' : idle.length ? `${plural(idle.length, 'drive')} waiting to be set up` : `${plural(disks.length, 'drive')}, all in use`);
    const mounts = h.mounts || [];
    const io = h.disk_io || {};
    region('r-disks', disks.map((d) => {
      const kind = `${d.kind === 'hdd' ? 'hard drive' : 'SSD'}, ${d.usb ? 'over USB' : 'internal'}`;
      const model = d.model || d.name;
      if (d.state !== 'mounted') {
        const fs = d.filesystems?.length ? d.filesystems.join(', ') : null;
        const dev = d.partitions?.length ? `/dev/${d.partitions[0]}` : `/dev/${d.name}`;
        return `<div class="planned">
          <div class="row-head"><span class="disk-name" style="color:var(--text)">${esc(model)}</span><span class="kicker amber">${fs ? 'Not mounted' : 'Not set up'}</span></div>
          <span class="soft" style="font-size:14px">${driveSize(d.size)} ${esc(kind)}, <code>/dev/${esc(d.name)}</code>. ${fs ? `It has a ${esc(fs)} filesystem but nothing is using it.` : 'No filesystem on it yet.'}</span>
          <details class="howto"><summary>How to set it up</summary><ol>
            <li>See every drive and what is on it: <code>lsblk -f</code></li>
            ${fs ? '' : `<li>Give it a partition table and one partition (<b>erases the drive</b>): <code>sudo parted /dev/${esc(d.name)} --script mklabel gpt mkpart data ext4 0% 100%</code></li>
            <li>Format the partition as ext4, Linux's standard filesystem: <code>sudo mkfs.ext4 -L data /dev/${esc(d.name)}1</code></li>`}
            <li>Make a folder to mount it on: <code>sudo mkdir -p /mnt/data</code></li>
            <li>Find its UUID, a name that never changes: <code>sudo blkid ${esc(fs ? dev : `/dev/${d.name}1`)}</code></li>
            <li>Add a line to <code>/etc/fstab</code> so it mounts at boot: <code>UUID=… /mnt/data ext4 defaults,nofail 0 2</code>${d.usb ? ' (<code>nofail</code> lets the server boot when the USB drive is unplugged)' : ''}</li>
            <li>Mount everything in fstab now: <code>sudo systemctl daemon-reload &amp;&amp; sudo mount -a</code></li>
          </ol></details></div>`;
      }
      const ms = mounts.filter((m) => d.mounts.includes(m.mountpoint)).sort((a, b) => (b.total || 0) - (a.total || 0));
      const main = ms[0] || {};
      const isSystem = d.mounts.includes('/');
      const dio = io[d.name] || {};
      return panel(`<div class="gauge-ring">${ring({ value: main.percent, color: main.percent >= 90 ? 'var(--red)' : main.percent >= 80 ? 'var(--amber)' : 'var(--cyan)', label: `${model} ${pct(main.percent)} full` })}<div style="flex-direction:column"><span class="num">${pct(main.percent)}</span><small style="font-size:11px">full</small></div></div>
        <div style="display:flex;flex-direction:column;gap:2px;min-width:0">
          <span class="kicker">${isSystem ? 'System drive' : esc(d.mounts.join(', '))}</span>
          <span class="disk-name">${esc(model)}</span>
          <span class="soft" style="font-size:13px">${bytes(main.used)} of ${bytes(main.total)} used</span>
          <span class="muted" style="font-size:12px">${driveSize(d.size)} ${esc(kind)}, ${esc(d.name)}</span>
          <span class="muted num" style="font-size:12px">Reading ${rate(dio.read_rate)} · Writing ${rate(dio.write_rate)}</span>
        </div>`, 'disk');
    }).join('') || '<span class="empty">No physical drives found in /sys/block.</span>');
    region('r-mounts', mounts.map((m) => `<div class="mount">
      <span><b style="font-weight:600">${esc(m.mountpoint)}</b> <span class="muted" style="font-size:12px">${esc(m.fstype)}${m.readonly ? ', read-only' : ''}</span></span>
      <span class="bar" role="img" aria-label="${pct(m.percent)} full"><i class="${m.percent >= 90 ? 'red' : m.percent >= 80 ? 'amber' : ''}" style="width:${clamp(m.percent || 0, 0, 100)}%"></i></span>
      <span class="num muted" style="font-size:13px;text-align:right">${bytes(m.used)} of ${bytes(m.total)}, ${bytes(m.free)} free</span></div>`).join('') || '<span class="empty">No mounted filesystems found.</span>');
    const sum = (k) => Object.values(io).reduce((a, v) => a + (v[k] || 0), 0);
    region('r-io', `<div class="cell"><span class="label">Reading</span><span class="value num">${fixed(sum('read_rate') / 1e6)} <small>MB/s</small></span></div>
      <div class="cell"><span class="label">Writing</span><span class="value num">${fixed(sum('write_rate') / 1e6)} <small>MB/s</small></span></div>`);
  },
};

// ------------------------------------------------------------------ activity
const ACTIVITY_FILTERS = [['all', 'Everything'], ['app', 'Apps'], ['power', 'Power and heat'], ['storage', 'Storage'], ['system', 'System']];
views.activity = {
  shell() {
    return `<div class="page-head"><div><span class="kicker">Activity</span><h1 id="r-act-title"></h1></div><div class="chips" role="group" aria-label="Show activity" id="r-act-filter"></div></div>
      <ol class="timeline" id="r-timeline"></ol><div class="foot" id="r-act-foot"></div>`;
  },
  update() {
    const f = S.ui.activity;
    region('r-act-filter', ACTIVITY_FILTERS.map(([id, label]) => `<button type="button" class="chip" data-action="activity" data-value="${id}" aria-pressed="${f === id}">${label}</button>`).join(''));
    const evs = S.events.filter((e) => f === 'all' || e.kind === f || (f === 'power' && e.kind === 'heat')).slice().reverse();
    const today = evs.filter((e) => new Date(e.time * 1000).toDateString() === new Date().toDateString());
    region('r-act-title', today.length === evs.length ? 'What happened today' : 'What happened lately');
    region('r-timeline', evs.map((e) => `<li class="${esc(e.level)}"><span class="mark" aria-hidden="true"><span class="diamond"></span></span>
      <span style="display:flex;flex-direction:column"><span class="ev-time num">${esc(clock(e.time, true))}</span><span class="ev-title">${e.container ? `<a href="#/apps/${encodeURIComponent(appIdForContainer(e.container) || '')}" style="color:inherit;text-decoration:none">${esc(e.title)}</a>` : esc(e.title)}</span>${e.detail ? `<span class="ev-detail">${esc(e.detail)}</span>` : ''}</span></li>`).join('') || '<li class="empty">Nothing yet.</li>');
    const d = S.docker || {};
    region('r-act-foot', `Keeps the last 200 events in memory. Docker ${d.enabled === false ? 'is turned off' : d.ok ? `${esc(d.info?.version || '')} connected${d.events ? ', live events on' : ''}` : 'not reachable'}. Build ${esc(S.build.sha || '')}.`);
  },
};
function appIdForContainer(id) { return allApps().find((a) => a.container?.id === id)?.id; }

// ================================================================ the shell
let mounted = null;
function render() {
  const r = route();
  const key = r.section === 'system' ? `system/${r.sub}` : r.section;
  const main = document.getElementById('main');
  if (mounted !== key) {
    const sectionChanged = !mounted || mounted.split('/')[0] !== r.section;
    mounted = key;
    main.innerHTML = views[r.section].shell(r);
    if (sectionChanged) { window.scrollTo(0, 0); }
    document.title = `${SECTIONS.find((s) => s.id === r.section).label} · ${hostName()}`;
  }
  views[r.section].update(r);
  chrome(r);
}

function chrome(r) {
  const navLink = (s, cls) => `<a class="${cls}" href="#/${s.id}" ${r.section === s.id ? 'aria-current="page"' : ''}>${icon(s.id, cls === 'nav' ? 20 : 22)}${s.label}</a>`;
  region('rail-links', SECTIONS.map((s) => `<li>${navLink(s, 'nav')}</li>`).join(''));
  region('tab-links', SECTIONS.map((s) => navLink(s, '')).join(''));
  const id = S.identity;
  for (const el of document.querySelectorAll('[data-bind="hostname"]')) el.textContent = hostName();
  const machine = [id.product ? `${(id.vendor || '').replace(/ Inc\.?$/, '')} ${id.product}`.trim() : null, id.os].filter(Boolean).join(', ');
  for (const el of document.querySelectorAll('[data-bind="machine"]')) el.textContent = machine || '';
  const up = S.host?.uptime_s;
  for (const el of document.querySelectorAll('[data-bind="uptime"]')) el.textContent = isNum(up) ? `Up ${duration(up)}` : '';
  for (const el of document.querySelectorAll('[data-bind="build"]')) el.textContent = S.build.sha ? `Build ${S.build.sha}` : '';
  for (const el of document.querySelectorAll('[data-bind="live"]')) {
    el.classList.toggle('off', !S.connected && !DEMO);
    el.classList.toggle('demo', DEMO);
  }
  for (const el of document.querySelectorAll('[data-bind="live-text"]')) el.textContent = DEMO ? 'Demo data' : S.connected ? 'Live' : S.loaded ? 'Reconnecting' : 'Connecting';
}

// ============================================================ data loading
function applyOverview(o) {
  Object.assign(S, {
    identity: o.identity || {}, host: o.host, containers: o.containers || [], apps: o.apps || S.apps,
    history: o.history || S.history, events: o.events || [], tailnet: o.tailnet, docker: o.docker || {},
    build: o.build || {}, owner: o.owner || '', loaded: true,
  });
}
function applyTick(d) {
  S.host = d.host;
  S.containers = d.containers || S.containers;
  S.apps = d.apps || S.apps;
  S.docker = d.docker || S.docker;
  if (d.point) {
    S.history.live.push(d.point);
    if (S.history.live.length > LIVE_MAX) S.history.live.splice(0, S.history.live.length - LIVE_MAX);
  }
  render();
}
function applyEvent(ev) {
  if (S.events.some((e) => e.id === ev.id && e.time === ev.time)) return;
  S.events.push(ev);
  if (S.events.length > 200) S.events.shift();
  // A container we are looking at changed: reload its details.
  const r = route();
  const app = r.section === 'apps' && r.sub ? findApp(r.sub) : null;
  if (app?.container && ev.container === app.container.id) loadDetail(app, true);
  render();
}

async function getJSON(path) {
  if (api) return api.get(path);
  const res = await fetch(path, { cache: 'no-store' });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

async function loadOverview() {
  try {
    applyOverview(await getJSON('api/overview'));
    render();
  } catch (err) {
    console.warn('overview failed', err);
  }
}

async function loadDetail(app, force = false) {
  const c = app?.container;
  if (!c) return;
  if (!force && S.detail.key === c.id && (S.detail.data || S.detail.loading)) return;
  S.detail = { key: c.id, data: S.detail.key === c.id ? S.detail.data : null, loading: true, error: null };
  if (S.logs.key !== c.id) S.logs = { key: c.id, lines: [], tail: 200, loading: true, error: null };
  const [detail] = await Promise.allSettled([getJSON(`api/containers/${encodeURIComponent(c.id)}`), loadLogs(c.id)]);
  if (S.detail.key !== c.id) return;
  S.detail.loading = false;
  if (detail.status === 'fulfilled') S.detail.data = detail.value;
  else S.detail.error = String(detail.reason);
  render();
}

async function loadLogs(id, tail = S.logs.key === id ? S.logs.tail : 200) {
  S.logs = { ...S.logs, key: id, tail, loading: true, error: null };
  try {
    const res = await getJSON(`api/containers/${encodeURIComponent(id)}/logs?tail=${tail}`);
    if (S.logs.key !== id) return;
    S.logs = { key: id, lines: res.lines || [], tail, loading: false, error: null };
  } catch (err) {
    if (S.logs.key !== id) return;
    S.logs = { ...S.logs, loading: false, error: `Could not load logs: ${err.message || err}` };
  }
  render();
  const pre = document.getElementById('r-logs');
  if (pre) pre.scrollTop = pre.scrollHeight;
}

function connect() {
  if (api) { api.subscribe({ tick: applyTick, event: applyEvent }); S.connected = true; return; }
  const es = new EventSource('api/stream');
  es.addEventListener('tick', (e) => { S.connected = true; applyTick(JSON.parse(e.data)); });
  es.addEventListener('event', (e) => applyEvent(JSON.parse(e.data)));
  es.onopen = () => { const was = S.connected; S.connected = true; if (!was) loadOverview(); };
  es.onerror = () => { S.connected = false; chrome(route()); };
}

// ================================================================== events
document.addEventListener('click', async (e) => {
  const btn = e.target.closest('[data-action]');
  if (!btn) return;
  const { action, value, key } = btn.dataset;
  if (action === 'app-filter') S.ui.appFilter = value;
  else if (action === 'activity') S.ui.activity = value;
  else if (action === 'procs') S.ui.procs = value;
  else if (action === 'range') S.ui[key] = value;
  else if (action === 'refresh-logs' && S.logs.key) { await loadLogs(S.logs.key); return; }
  else if (action === 'more-logs' && S.logs.key) { await loadLogs(S.logs.key, 2000); return; }
  else if (action === 'copy-logs') {
    const text = S.logs.lines.map((l) => `${l.ts || ''} ${l.text}`).join('\n');
    try { await navigator.clipboard.writeText(text); btn.textContent = 'Copied'; } catch { btn.textContent = 'Copy failed'; }
    setTimeout(() => { btn.textContent = 'Copy'; }, 1500);
    return;
  }
  render();
});

document.addEventListener('keydown', (e) => {
  const r = route();
  if (e.key === 'Escape' && r.section === 'apps' && r.sub) location.hash = '#/apps';
});

window.addEventListener('hashchange', () => {
  const r = route();
  render();
  if (r.section === 'apps' && r.sub && matchMedia('(max-width: 899px)').matches) document.getElementById('r-drawer-box')?.scrollTo?.(0, 0);
});

// Keep the clock and "x minutes ago" text fresh between ticks, and pick up
// the 24-hour history and Tailscale changes once a minute.
setInterval(() => render(), 30_000);
setInterval(() => loadOverview(), 60_000);

async function boot() {
  if (DEMO) {
    const demo = await import('./demo.js');
    api = demo.createDemo();
  }
  render();
  await loadOverview();
  const r = route();
  if (r.section === 'apps' && r.sub) loadDetail(findApp(r.sub));
  connect();
}
boot();
