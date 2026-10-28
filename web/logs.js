// Logs: live output from the server journal and every container, failure
// reports, and the event timeline (what used to be the Activity page).

import {
  S, allApps, bytes, clock, duration, esc, getJSON, hooks, net, panel, region, route, toast, views,
} from './core.js';

const MAX_DOM_LINES = 1500;
const SOURCE_COLORS = { kernel: 'var(--violet)', systemd: 'var(--violet)', 'systemd-logind': 'var(--violet)',
  dockerd: 'var(--cyan-soft)', containerd: 'var(--cyan-soft)', tailscaled: 'var(--cyan-soft)', 'stx-monitor': 'var(--cyan)' };
const EVENT_FILTERS = [['all', 'Everything'], ['app', 'Apps'], ['power', 'Power and heat'], ['storage', 'Storage'],
  ['security', 'Sign-ins'], ['system', 'System']];

const L = { lines: [], matched: 0, total: 0, sources: { containers: [], journal: null }, stream: null, key: '', loading: false };

// ----------------------------------------------------------------- streaming
export function closeLogStream() {
  L.stream?.close?.();
  L.stream = null;
}

function filterKey() {
  return `${S.ui.logSource}|${S.ui.logLevel}|${S.ui.logQ}`;
}

function query() {
  return new URLSearchParams({ source: S.ui.logSource, level: S.ui.logLevel, q: S.ui.logQ }).toString();
}

async function reload() {
  closeLogStream();
  L.key = filterKey();
  L.loading = true;
  drawLines(true);
  try {
    const res = await getJSON(`api/logs?${query()}&limit=1000`);
    L.lines = res.lines;
    L.matched = res.matched;
    L.total = res.total;
    L.sources = { containers: res.containers || [], journal: res.journal, journal_error: res.journal_error };
  } catch (err) {
    L.lines = [];
    L.error = err.message;
  }
  L.loading = false;
  hooks.render();
  drawLines(true);
  if (S.ui.follow) follow();
}

function follow() {
  closeLogStream();
  const add = (batch) => {
    L.lines.push(...batch);
    L.matched += batch.length;
    if (L.lines.length > MAX_DOM_LINES) L.lines.splice(0, L.lines.length - MAX_DOM_LINES);
    appendLines(batch);
  };
  if (net.api) { L.stream = net.api.followLogs?.(S.ui, add); return; }
  const es = new EventSource(`api/logs/stream?${query()}`);
  es.addEventListener('lines', (e) => add(JSON.parse(e.data)));
  L.stream = es;
}

// ------------------------------------------------------------------- drawing
function lineHTML(l) {
  const t = new Date(l.ts * 1000);
  const time = t.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
  const color = l.origin === 'container' ? 'var(--text)' : SOURCE_COLORS[l.source] || 'var(--cyan-soft)';
  return `<div class="log-line lvl-${l.level}"><span class="lt num">${time}</span><span class="ls" style="color:${color}">${esc(l.source)}</span><span class="ll">${l.level === 'info' ? '' : esc(l.level)}</span><span class="lm">${esc(l.text)}</span></div>`;
}

function atBottom(box) { return box.scrollHeight - box.scrollTop - box.clientHeight < 40; }

function drawLines(scroll) {
  const box = document.getElementById('log-box');
  if (!box) return;
  if (L.loading && !L.lines.length) { box.innerHTML = '<div class="empty" style="padding:12px 14px">Loading…</div>'; return; }
  box.innerHTML = L.lines.map(lineHTML).join('') || `<div class="empty" style="padding:12px 14px">${L.error ? esc(L.error) : 'No lines match.'}</div>`;
  if (scroll) box.scrollTop = box.scrollHeight;
  drawCount();
}

function appendLines(batch) {
  const box = document.getElementById('log-box');
  if (!box) return;
  const stick = atBottom(box);
  if (box.querySelector('.empty')) box.innerHTML = '';
  box.insertAdjacentHTML('beforeend', batch.map(lineHTML).join(''));
  while (box.childElementCount > MAX_DOM_LINES) box.firstElementChild.remove();
  if (stick && S.ui.follow) box.scrollTop = box.scrollHeight;
  drawCount();
}

function drawCount() {
  const el = document.getElementById('log-count');
  if (!el) return;
  const where = L.sources.journal ? `Server journal and ${L.sources.containers.length} containers` : `${L.sources.containers.length} containers${L.sources.journal === false ? ' (no server journal found)' : ''}`;
  el.innerHTML = `<span>Showing ${Math.min(L.lines.length, MAX_DOM_LINES).toLocaleString()} of ${L.matched.toLocaleString()} matching lines</span><span>${esc(where)}</span>`;
}

// ------------------------------------------------------------------- reports
async function report(id) {
  if (!S.reportCache[id]) {
    try {
      S.reportCache[id] = await getJSON(`api/reports/${id}`);
      const r = S.reports.recent.find((x) => x.id === id);
      if (r && !r.seen) { r.seen = true; S.reports.unseen = Math.max(0, S.reports.unseen - 1); }
    } catch (err) {
      S.reportCache[id] = { error: err.message };
    }
    hooks.render();
  }
  return S.reportCache[id];
}

export function onReport(r) {
  S.reports.recent = [r, ...S.reports.recent.filter((x) => x.id !== r.id)].slice(0, 20);
  S.reports.unseen += r.seen ? 0 : 1;
  toast(`Failure report saved: ${r.title}`, 'error');
  hooks.render();
}

function reportList(selected) {
  return `<ul class="report-list">${S.reports.recent.map((r) => `<li><a class="report-item ${r.id === selected ? 'current' : ''} ${r.kind === 'host' ? 'host' : ''}" href="#/logs/failures/${r.id}" ${r.id === selected ? 'aria-current="true"' : ''}>
    <span class="report-top"><b>${esc(r.title)}</b><span class="muted num">${esc(clock(r.time, true))}</span></span>
    <span class="report-sum">${!r.seen ? '<span class="new">New</span> ' : ''}${esc(r.summary)}</span></a></li>`).join('') || '<li class="empty">No failures recorded. Good.</li>'}</ul>`;
}

function reportText(rep) {
  const d = rep.data || {};
  const lines = [`${rep.title} (${new Date(rep.time * 1000).toLocaleString()})`, rep.summary, ''];
  if (rep.kind === 'container') {
    lines.push(`Exit code: ${d.exit_code}${d.oom ? ' (out of memory)' : ''}`, `Image: ${d.container?.image || ''}`);
    if (d.stats?.mem) lines.push(`Memory: ${bytes(d.stats.mem)}${d.memory_limit ? ` of ${bytes(d.memory_limit)}` : ''}`);
  }
  lines.push('', 'Suggested fixes:', ...(d.suggestions || []).map((s) => `- ${s}`), '', 'Last lines:');
  for (const l of d.logs || d.last_lines || []) lines.push(`${l.ts} ${l.source ? l.source + ' ' : ''}${l.text}`);
  return lines.join('\n');
}

function reportDetail(rep, full) {
  if (!rep) return '<div class="empty">Loading…</div>';
  if (rep.error) return `<div class="empty">${esc(rep.error)}</div>`;
  const d = rep.data || {};
  const sugg = d.suggestions || [];
  const fix = `<div class="fix"><span class="muted" style="font-size:12px">${rep.kind === 'host' ? 'How to prevent it' : 'Suggested fix'}</span>
    <span>${esc(sugg[0] || '')}</span>${sugg.length > 1 ? `<ul>${sugg.slice(1).map((x) => `<li>${esc(x)}</li>`).join('')}</ul>` : ''}</div>`;
  const copy = '<button type="button" class="btn sm quiet" data-action="copy-report">Copy report</button>';
  if (rep.kind === 'host') {
    const steps = (d.timeline || []).map((t, i, arr) => `<li class="tone-${t.tone}${i === arr.length - 1 ? ' last' : ''}"><span class="mark" aria-hidden="true"><span class="diamond"></span></span><span class="num ev-time">${esc(clock(t.time, true))}</span><span>${esc(t.what)}</span></li>`).join('');
    const lastLines = (d.last_lines || []).slice(full ? 0 : -6);
    return panel(`
      <div><span class="kicker red">Failure report</span><span class="report-title">${esc(rep.title)}</span>
        <span class="muted" style="font-size:14px">${esc(clock(d.went_dark, true))} to ${esc(clock(d.back, true))}. Off for ${esc(duration(d.offline_seconds))}.</span></div>
      <p class="soft" style="margin:0">${esc(rep.summary)}</p>
      <ol class="steps">${steps}</ol>
      <div><span class="kicker" style="color:var(--muted)">Last lines before it went dark</span>
        <pre class="logs">${lastLines.map((l) => `<div class="${l.level === 'error' ? 'err' : l.level === 'warn' ? 'warn' : ''}"><span class="ts">${esc(new Date(l.ts * 1000).toLocaleTimeString([], { hour12: false }))}</span> ${esc(l.source)}  ${esc(l.text)}</div>`).join('')}</pre></div>
      ${fix}
      <div class="row-actions" style="justify-content:flex-start">
        ${(d.suggestions || []).some((s) => /autopilot/i.test(s)) ? '<a class="btn sm solid" href="#/control">Open autopilot settings</a>' : ''}
        <button type="button" class="btn sm quiet" data-action="report-full">${full ? 'Show fewer lines' : `Show all ${(d.last_lines || []).length} lines`}</button>${copy}
      </div>`, 'red report');
  }
  const c = d.container || {};
  const live = S.containers.find((x) => x.name === c.name);
  const down = live && live.state !== 'running';
  const memLine = d.stats?.mem ? `${bytes(d.stats.mem)}${d.memory_limit ? ` / ${bytes(d.memory_limit)}` : ''}` : '–';
  const logLines = (d.logs || []).slice(full ? 0 : -8);
  return panel(`
    <div><span class="kicker red">Failure report</span><span class="report-title">${esc(rep.title)}</span>
      <span class="muted" style="font-size:13px">${esc(clock(rep.time, true))}${d.ran_for ? `, after running ${esc(duration(d.ran_for))}` : ''}${d.repeats_in_window > 1 ? `, ${d.repeats_in_window} times in 10 min` : ''}</span></div>
    <p class="soft" style="margin:0">${esc(rep.summary)}</p>
    <div class="cells three">
      <div class="cell"><span class="note">Memory</span><span class="plain ${d.oom ? 'red-text' : ''}">${esc(memLine)}</span></div>
      <div class="cell"><span class="note">Processor</span><span class="plain">${d.stats?.cpu != null ? `${d.stats.cpu.toFixed(0)}%` : '–'}</span></div>
      <div class="cell"><span class="note">Exit code</span><span class="plain">${esc(d.exit_code ?? '–')}</span></div>
    </div>
    ${fix}
    ${full || logLines.length ? `<pre class="logs">${logLines.map((l) => `<div class="${/error|fatal|exception/i.test(l.text) ? 'err' : /warn/i.test(l.text) ? 'warn' : ''}"><span class="ts">${l.ts ? esc(new Date(l.ts).toLocaleTimeString([], { hour12: false })) : ''}</span> ${esc(l.text)}</div>`).join('')}</pre>` : ''}
    <div class="row-actions" style="justify-content:flex-start">
      ${down ? `<button type="button" class="btn sm solid" data-action="run" data-verb="start" data-target="${esc(c.name)}">Start ${esc(allApps().find((a) => a.container?.name === c.name)?.name || c.name)}</button>` : ''}
      <button type="button" class="btn sm quiet" data-action="report-full">${full ? 'Show fewer lines' : `Show its last ${(d.logs || []).length} lines`}</button>${copy}
    </div>`, 'red report');
}

// --------------------------------------------------------------------- views
function sourceOptions() {
  const containers = new Set([...L.sources.containers, ...S.containers.map((c) => c.name)]);
  const name = (n) => allApps().find((a) => a.container?.name === n)?.name || n;
  const opts = [['all', 'Everything'], ['server', 'Server (system journal)'], ['docker', 'Docker'],
    ...[...containers].sort().map((n) => [n, name(n)])];
  if (!opts.some(([v]) => v === S.ui.logSource)) opts.push([S.ui.logSource, S.ui.logSource]);
  return opts.map(([v, label]) => `<option value="${esc(v)}" ${v === S.ui.logSource ? 'selected' : ''}>${esc(label)}</option>`).join('');
}

let full = false;

views.logs = {
  shell(r) {
    const view = r.sub.split('/')[0] || 'output';
    const tab = (id, label) => `<a href="#/logs${id === 'output' ? '' : '/' + id}" ${view === id ? 'aria-current="page"' : ''}>${label}</a>`;
    const head = `<div class="page-head"><div><span class="kicker">Logs</span><h1>${view === 'events' ? 'What happened' : view === 'failures' ? 'Failure reports' : 'Server output'}</h1></div>
      <nav class="seg tabs" aria-label="Log view">${tab('output', 'Output')}${tab('failures', 'Failures<span id="r-fail-count" class="red-text tab-count"></span>')}${tab('events', 'Events')}</nav></div>`;
    if (view === 'events') {
      return `${head}<div class="chips" role="group" aria-label="Show events" id="r-ev-filter"></div><ol class="timeline" id="r-timeline"></ol><div class="foot" id="r-act-foot"></div>`;
    }
    if (view === 'failures') {
      return `${head}<div class="failures-layout"><aside class="failures-list" aria-label="Failure reports" id="r-fail-list"></aside><section id="r-fail-detail" class="failures-detail" aria-live="polite"></section></div>`;
    }
    return `${head}
      <div class="log-tools">
        <label class="field compact"><span>Source</span><select id="log-source" data-log="source">${sourceOptions()}</select></label>
        <div class="chips" role="group" aria-label="Level" id="r-log-level"></div>
        <label class="field compact grow"><span>Search</span><input id="log-q" type="search" placeholder="Search log text" value="${esc(S.ui.logQ)}" data-log="q"></label>
        <button type="button" class="chip follow" data-log="follow" aria-pressed="${S.ui.follow}" id="log-follow"><span class="live-dot" aria-hidden="true"></span>${S.ui.follow ? 'Following live' : 'Paused'}</button>
        <a class="chip" id="log-download" href="api/logs/download?${query()}" download>Download</a>
      </div>
      <div class="logs-layout">
        <section aria-label="Log lines" class="log-panel">
          <div class="log-line log-head"><span>Time</span><span>Source</span><span>Level</span><span>Message</span></div>
          <div id="log-box" class="log-box" tabindex="0"></div>
          <div class="log-foot" id="log-count"></div>
        </section>
        <aside class="log-aside" aria-labelledby="fr-h"><h2 id="fr-h">Failure reports</h2><div id="r-fail-aside"></div></aside>
      </div>`;
  },
  mount(r) {
    full = false;
    const view = r.sub.split('/')[0] || 'output';
    if (view === 'output') {
      const src = r.params.get('source');
      if (src) S.ui.logSource = src;
      reload();
    }
  },
  update(r) {
    const view = r.sub.split('/')[0] || 'output';
    region('r-fail-count', S.reports.unseen ? String(S.reports.unseen) : '');
    if (view === 'events') return eventsView();
    if (view === 'failures') {
      const id = Number(r.sub.split('/')[1]) || S.reports.recent[0]?.id;
      region('r-fail-list', reportList(id));
      if (!id) { region('r-fail-detail', '<div class="empty">Nothing has failed yet.</div>'); return; }
      const rep = S.reportCache[id];
      if (!rep) report(id);
      region('r-fail-detail', reportDetail(rep, full));
      return;
    }
    if (filterKey() !== L.key) { reload(); return; }
    const lv = S.ui.logLevel;
    region('r-log-level', [['all', 'All'], ['warn', 'Warnings'], ['error', 'Errors']].map(([v, label]) =>
      `<button type="button" class="chip lvl-${v}" data-log="level" data-value="${v}" aria-pressed="${lv === v}">${label}</button>`).join(''));
    const top = S.reports.recent[0];
    if (top && !S.reportCache[top.id]) report(top.id);
    region('r-fail-aside', reportList(top?.id) + (top ? reportDetail(S.reportCache[top.id], false) : ''));
  },
};

function eventsView() {
  const f = S.ui.activity;
  region('r-ev-filter', EVENT_FILTERS.map(([id, label]) => `<button type="button" class="chip" data-log="events" data-value="${id}" aria-pressed="${f === id}">${label}</button>`).join(''));
  const evs = S.events.filter((e) => f === 'all' || e.kind === f || (f === 'power' && e.kind === 'heat')).slice().reverse();
  const appOf = (cid) => allApps().find((a) => a.container?.id === cid)?.id;
  region('r-timeline', evs.map((e) => {
    const link = e.report ? `#/logs/failures/${e.report}` : e.container && appOf(e.container) ? `#/apps/${encodeURIComponent(appOf(e.container))}` : null;
    const title = link ? `<a href="${link}" class="plain-link">${esc(e.title)}</a>` : esc(e.title);
    return `<li class="${esc(e.level)}"><span class="mark" aria-hidden="true"><span class="diamond"></span></span>
      <span style="display:flex;flex-direction:column"><span class="ev-time num">${esc(clock(e.time, true))}</span><span class="ev-title">${title}</span>${e.detail ? `<span class="ev-detail">${esc(e.detail)}</span>` : ''}</span></li>`;
  }).join('') || '<li class="empty">Nothing yet.</li>');
  const d = S.docker || {};
  region('r-act-foot', `Keeps the last 200 events here and 5,000 on disk. Docker ${d.enabled === false ? 'is turned off' : d.ok ? `${esc(d.info?.version || '')} connected${d.events ? ', live events on' : ''}` : 'not reachable'}. Build ${esc(S.build.sha || '')}.`);
}

// -------------------------------------------------------------------- events
let searchTimer = null;
document.addEventListener('change', (e) => {
  if (e.target.dataset?.log === 'source') { S.ui.logSource = e.target.value; reload(); hooks.render(); }
});
document.addEventListener('input', (e) => {
  if (e.target.dataset?.log !== 'q') return;
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { S.ui.logQ = e.target.value.trim(); reload(); syncDownload(); }, 300);
});
function syncDownload() {
  const a = document.getElementById('log-download');
  if (a) a.href = `api/logs/download?${query()}`;
}
document.addEventListener('click', async (e) => {
  const el = e.target.closest('[data-log], [data-action="report-full"], [data-action="copy-report"]');
  if (!el) return;
  if (el.dataset.action === 'report-full') { full = !full; hooks.render(); return; }
  if (el.dataset.action === 'copy-report') {
    const r = route();
    const id = Number(r.sub.split('/')[1]) || S.reports.recent[0]?.id;
    const rep = S.reportCache[id];
    if (!rep) return;
    try { await navigator.clipboard.writeText(reportText(rep)); toast('Report copied', 'ok'); } catch { toast('Copy failed', 'error'); }
    return;
  }
  const kind = el.dataset.log;
  if (kind === 'level') { S.ui.logLevel = el.dataset.value; reload(); syncDownload(); hooks.render(); }
  else if (kind === 'events') { S.ui.activity = el.dataset.value; hooks.render(); }
  else if (kind === 'follow') {
    S.ui.follow = !S.ui.follow;
    el.setAttribute('aria-pressed', String(S.ui.follow));
    el.innerHTML = `<span class="live-dot" aria-hidden="true"></span>${S.ui.follow ? 'Following live' : 'Paused'}`;
    if (S.ui.follow) reload(); else closeLogStream();
  }
});
