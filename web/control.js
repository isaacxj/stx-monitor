// Control center: app actions, server actions, autopilot, history and account.
// Every action asks for confirmation first, then runs on the server; progress
// arrives as "action" messages on the live stream.

import {
  DEMO, S, ago, bytes, clock, confirmAction, esc, findApp, getJSON, hooks, hostName, icon, panel, postJSON,
  region, stoppedSentence, toast, views,
} from './core.js';
import { addPasskey, passkeysSupported } from './login.js';

const PROTECTED_NOTE = {
  'stx-monitor': 'This console, cannot stop itself',
  'stx-docker-proxy': 'The console needs it to see apps',
  'stx-docker-control': 'The console needs it for actions',
};
const VERB_DONE = { start: 'started', stop: 'stopped', restart: 'restarted', update: 'updated' };
const VERB_ING = { start: 'Starting', stop: 'Stopping', restart: 'Restarting', update: 'Updating' };

function displayName(name) {
  const c = S.control?.containers.find((x) => x.name === name);
  return c?.display || findApp(name)?.name || name;
}

function usedBy(name) {
  const c = S.control?.containers.find((x) => x.name === name);
  const id = c?.app;
  if (!id) return [];
  return (S.apps.links || []).filter(([, b]) => b === id).map(([a]) => findApp(a)?.name || a);
}

// What each confirm dialog says. Kept here so the Apps drawer, the Control
// center and failure reports all ask the same way.
function describe(verb, target) {
  const name = displayName(target);
  const users = usedBy(target);
  switch (verb) {
    case 'start':
      return { title: `Start ${name}?`, body: `${esc(name)} starts again with the same settings.`, confirmLabel: `Start ${name}` };
    case 'stop':
      return {
        title: `Stop ${name}?`,
        body: `${esc(name)} stays off until you start it again.`,
        warning: users.length ? `${esc(users.join(', '))} ${users.length === 1 ? 'uses' : 'use'} it and may show errors while it is off.` : '',
        confirmLabel: `Stop ${name}`, danger: true,
      };
    case 'restart':
      if (target === 'stx-monitor') return { title: 'Restart stx-monitor?', body: 'This console restarts. The page reconnects by itself in a few seconds.', confirmLabel: 'Restart stx-monitor' };
      return {
        title: `Restart ${name}?`,
        body: `${esc(name)} will be unavailable for a few seconds, then start again with the same settings.`,
        warning: target.startsWith('stx-docker') ? 'The console loses sight of your apps for a few seconds.' : '',
        confirmLabel: `Restart ${name}`,
      };
    case 'update':
      return {
        title: `Update ${name}?`,
        body: `Downloads the newest version of its image and recreates ${esc(name)} with the same settings. It is unavailable for up to a minute.`,
        confirmLabel: `Update ${name}`,
      };
    case 'prune-images': {
      const size = S.control?.prune_bytes;
      return {
        title: 'Clean up unused images?',
        body: `Deletes Docker images that no container uses${size ? `, about ${bytes(size)}` : ''}. Apps keep running; anything deleted downloads again when it is needed.`,
        confirmLabel: 'Clean up',
      };
    }
    case 'restart-docker':
      return {
        title: 'Restart Docker?', danger: true, kicker: 'Risky',
        body: 'Docker restarts, and every app with it.',
        warning: 'Everything, including this console, is offline for about 30 seconds.',
        confirmLabel: 'Restart Docker',
      };
    case 'reboot':
      return {
        title: `Reboot ${hostName()}?`, danger: true, kicker: 'Risky', typed: hostName(),
        body: 'The whole server restarts. Apps with a restart policy come back by themselves.',
        warning: 'The console is back in about 90 seconds.',
        confirmLabel: 'Reboot now',
      };
    default:
      return { title: `${verb} ${name}?`, body: '', confirmLabel: 'Confirm' };
  }
}

const pending = new Map(); // action id -> label, for the progress toast

export async function askAndRun(verb, target = '') {
  const answer = await confirmAction(describe(verb, target));
  if (!answer) return;
  try {
    const row = await postJSON('api/actions', { action: verb, target, confirm: typeof answer === 'string' ? answer : null });
    const label = verb in VERB_ING ? `${VERB_ING[verb]} ${displayName(target)}…` : verb === 'reboot' ? 'Rebooting…' : verb === 'restart-docker' ? 'Restarting Docker…' : 'Cleaning up images…';
    pending.set(row.id, row);
    toast(label, 'busy');
    onAction(row);
  } catch (err) {
    toast(err.message || String(err), 'error');
    loadControl();
  }
}

// A history row changed (started, finished, failed).
export function onAction(row) {
  if (S.control) {
    const i = S.control.history.findIndex((h) => h.id === row.id);
    if (i >= 0) S.control.history[i] = row;
    else S.control.history.unshift(row);
  }
  if (row.status !== 'running' && pending.has(row.id)) {
    pending.delete(row.id);
    const what = sentence(row);
    toast(row.status === 'ok' ? `Done: ${what}${row.detail ? `. ${row.detail}` : ''}` : `Didn't work: ${row.detail || what}`, row.status === 'ok' ? 'ok' : 'error');
    loadControl();
  }
  hooks.render();
}

export async function loadControl() {
  if (!DEMO && !S.session.authenticated) return;
  try {
    S.control = await getJSON('api/control');
    S.controlError = null;
  } catch (err) {
    S.controlError = err.message;
  }
  hooks.render();
  if (!S.account) loadAccount();
}

async function loadAccount() {
  try {
    S.account = await getJSON('api/auth/account');
  } catch {
    S.account = null;
  }
  hooks.render();
}

function sentence(h) {
  const name = h.target && h.target !== 'server' ? displayName(h.target) : '';
  switch (h.action) {
    case 'start': case 'stop': case 'restart': case 'update':
      return `${VERB_DONE[h.action]} ${name}`;
    case 'prune-images': return `cleaned up unused images${h.detail && h.status === 'ok' ? ` (${h.detail.replace(/^Freed /, 'freed ')})` : ''}`;
    case 'restart-docker': return 'restarted Docker';
    case 'reboot': return 'rebooted the server';
    case 'shutdown': return 'shut the server down';
    case 'autopilot': return (h.detail || 'changed autopilot').replace(/^Power-outage autopilot/, 'turned power-outage autopilot');
    default: return `${h.action} ${name}`.trim();
  }
}

function historyRow(h) {
  const who = h.actor === S.session.user ? 'You' : h.actor;
  const status = h.status === 'running' ? '<span class="muted">working…</span>'
    : h.status === 'failed' ? `<span class="red-text">failed: ${esc(h.detail)}</span>`
    : h.status === 'refused' ? `<span class="amber-text">refused: ${esc(h.detail)}</span>`
    : h.status === 'unknown' ? `<span class="muted">${esc(h.detail)}</span>` : '';
  const from = h.device ? ` <span class="muted">from ${esc(h.device.split(',')[0])}</span>` : '';
  return `<li><span class="when num">${esc(clock(h.started, true))}</span><span><b>${esc(who)}</b> ${esc(sentence(h))}${from}${status ? `<br>${status}` : ''}</span></li>`;
}

function note(c) {
  if (PROTECTED_NOTE[c.name]) return PROTECTED_NOTE[c.name];
  if (c.update?.available) return 'Update available';
  if (c.state !== 'running') {
    const report = S.reports.recent.find((r) => r.kind === 'container' && r.target === c.name);
    if (report?.cause) return report.cause;
    return stoppedSentence({ status: c.status, state: c.state }).replace(/^Stopped [^.]*\. ?/, '').replace(/\..*/, '') || 'Stopped';
  }
  return c.status || '';
}

function appRow(c) {
  const down = c.state !== 'running';
  const btn = (verb, label, cls = '') => `<button type="button" class="btn sm ${cls}" data-action="run" data-verb="${verb}" data-target="${esc(c.name)}">${label}</button>`;
  const actions = [];
  if (c.actions.includes('start')) actions.push(btn('start', 'Start', 'solid'));
  if (c.actions.includes('update') && c.update?.available) actions.push(btn('update', 'Update', 'accent'));
  if (c.actions.includes('restart')) actions.push(btn('restart', 'Restart', 'quiet'));
  if (c.actions.includes('stop')) actions.push(btn('stop', 'Stop', 'quiet'));
  if (down) actions.push(`<a class="btn sm quiet" href="#/logs?source=${encodeURIComponent(c.name)}">Logs</a>`);
  const state = down ? `Down${c.status ? `, ${esc(c.status.replace(/^Exited \(\d+\) /, ''))}` : ''}` : c.health === 'unhealthy' ? 'Unhealthy' : 'Running';
  return `<tr class="${down ? 'is-down' : ''}">
    <td><span class="app-cell"><span class="dot ${down ? 'red' : c.health === 'unhealthy' ? 'amber' : ''}" aria-hidden="true"></span><b>${esc(c.display)}</b></span></td>
    <td><span class="state-cell"><span class="${down ? 'red-text' : ''}">${state}</span><span class="muted">${esc(note(c))}</span></span></td>
    <td><span class="row-actions">${actions.join('') || '<span class="muted" style="font-size:13px">No actions</span>'}</span></td>
  </tr>`;
}

function serverPanel(ctl) {
  const ap = ctl.autopilot;
  const helper = ctl.capabilities.helper;
  const levels = [10, 15, 20, 25, 30, 40, 50];
  const disabled = helper ? '' : 'disabled';
  return panel(`
    <h2>Server</h2>
    <div class="setting">
      <span class="setting-text"><b>Power-outage autopilot</b><span class="muted">${ap.enabled
        ? `On battery, stops the apps and shuts down cleanly at
           <label class="inline-select"><span class="sr-only">Shutdown level</span><select data-change="autopilot-level">${levels.map((l) => `<option value="${l}" ${l === ap.threshold ? 'selected' : ''}>${l}%</option>`).join('')}</select></label>`
        : 'Off. The server runs until the battery empties.'}</span></span>
      <button type="button" role="switch" class="switch" aria-checked="${ap.enabled}" aria-label="Power-outage autopilot" data-action="autopilot"><span></span></button>
    </div>
    ${!helper && ap.enabled ? '<span class="amber-text" style="font-size:13px">Autopilot can stop the apps, but needs stx-helper to shut the server down.</span>' : ''}
    <div class="setting">
      <span class="setting-text"><b>Clean up unused images</b><span class="muted">${ctl.prune_bytes ? `Frees about ${bytes(ctl.prune_bytes)}` : 'Deletes images no container uses'}</span></span>
      <button type="button" class="btn sm" data-action="run" data-verb="prune-images" ${disabled}>Clean up</button>
    </div>
    <div class="setting">
      <span class="setting-text"><b>Back up app settings</b><span class="muted">Set up backups first</span></span>
      <button type="button" class="btn sm" disabled>Back up now</button>
    </div>
    <div class="risky">
      <span class="kicker red">Risky</span>
      <div class="row-actions" style="justify-content:flex-start">
        <button type="button" class="btn danger-outline" data-action="run" data-verb="restart-docker" ${disabled}>Restart Docker</button>
        <button type="button" class="btn danger-fill" data-action="run" data-verb="reboot" ${disabled}>Reboot server</button>
      </div>
      <span class="muted" style="font-size:13px">Rebooting asks you to type ${esc(ctl.hostname || hostName())} to confirm. The console is back in about 90 seconds.</span>
    </div>`);
}

function accountPanel() {
  const a = S.account;
  if (!a) return '';
  const supported = passkeysSupported();
  const keys = a.passkeys.map((p) => `<li class="list-row"><span style="flex:1">${icon('key', 18)} ${esc(p.name)}
      <span class="muted" style="font-size:12px">added ${esc(clock(p.created, true))}${p.last_used ? `, used ${esc(ago(p.last_used))}` : ', not used yet'}</span></span>
      <button type="button" class="btn sm quiet" data-action="remove-passkey" data-id="${esc(p.id)}" data-name="${esc(p.name)}">Remove</button></li>`).join('');
  const sessions = a.sessions.map((s) => `<li class="list-row"><span class="ring-dot ${s.current ? 'on' : ''}" aria-hidden="true"></span>
      <span style="flex:1">${esc(s.device)} <span class="muted" style="font-size:12px">${esc(s.ip)}${s.current ? ', this device' : `, seen ${esc(ago(s.last_seen))}`}</span></span>
      <span class="muted" style="font-size:12px">${s.remember ? '30 days' : 'until the browser closes'}</span></li>`).join('');
  const signIns = a.sign_ins.slice(0, 6).map((x) => `<li class="list-row"><span class="when num">${esc(clock(x.time, true))}</span>
      <span style="flex:1" class="${x.ok ? '' : 'red-text'}">${x.ok ? 'Signed in' : 'Failed'} with ${x.method === 'passkey' ? 'a passkey' : 'the password'}
      <span class="muted" style="font-size:12px">${esc(x.device)}, ${esc(x.ip)}</span></span></li>`).join('');
  return `<section class="account" aria-labelledby="acct-h">
    <div class="row-head"><h2 id="acct-h">Account and security</h2>
      <span class="muted" style="font-size:14px">Signed in as ${esc(a.user || S.session.user || '')} · <button type="button" class="link-btn" data-action="sign-out">Sign out</button></span></div>
    <div class="two">
      ${panel(`<div class="row-head"><span class="kicker">Passkeys</span>
          ${supported ? '<button type="button" class="btn sm accent" data-action="add-passkey">Add a passkey on this device</button>' : ''}</div>
        <span class="muted" style="font-size:13px">Sign in with Face ID, Touch ID or Windows Hello instead of the password.</span>
        ${supported ? '' : '<span class="amber-text" style="font-size:13px">This browser can only add passkeys over HTTPS. Open the console at its Tailscale address (https://…ts.net).</span>'}
        <ul class="plain-list">${keys || '<li class="empty">No passkeys yet.</li>'}</ul>`)}
      ${panel(`<div class="row-head"><span class="kicker">Signed in on</span>
          ${a.sessions.length > 1 ? '<button type="button" class="btn sm quiet" data-action="sign-out-others">Sign out everywhere else</button>' : ''}</div>
        <ul class="plain-list">${sessions}</ul>
        <span class="kicker" style="margin-top:6px">Recent sign-ins</span>
        <ul class="plain-list">${signIns || '<li class="empty">None yet.</li>'}</ul>
        <span class="muted" style="font-size:13px">Forgot the password? On the server: <code>docker exec -it stx-monitor python -m monitor set-password</code></span>`)}
    </div>
  </section>`;
}

views.control = {
  shell() {
    return `
      <div class="page-head"><div><span class="kicker">Control</span><h1>Control center</h1></div>
        <span class="muted" style="font-size:14px">Every action is recorded. Each one asks you to confirm first.</span></div>
      <div id="r-ctl-notes"></div>
      <div class="control-layout">
        <section aria-labelledby="ctl-apps" class="control-apps">
          <div class="row-head"><h2 id="ctl-apps">Apps</h2><button type="button" class="btn sm quiet" data-action="check-updates">Check for updates</button></div>
          <div class="table-box"><table class="ctl-table"><thead><tr><th scope="col">App</th><th scope="col">State</th><th scope="col" class="r">Actions</th></tr></thead><tbody id="r-ctl-rows"></tbody></table></div>
        </section>
        <div class="control-side">
          <div id="r-ctl-server"></div>
          <section aria-labelledby="ctl-hist" class="history"><h2 id="ctl-hist">History</h2><ol class="plain-list hist" id="r-ctl-history"></ol></section>
        </div>
      </div>
      <div id="r-ctl-account"></div>`;
  },
  mount() { loadControl(); },
  update() {
    const ctl = S.control;
    if (!ctl) {
      region('r-ctl-rows', `<tr><td colspan="3" class="empty">${S.controlError ? esc(S.controlError) : 'Loading…'}</td></tr>`);
      return;
    }
    const notes = [];
    if (!ctl.capabilities.control) notes.push('App actions are off: stx-monitor can\'t reach the control proxy (stx-docker-control). Check that it is running.');
    if (!ctl.capabilities.helper) notes.push('Server actions and updates need stx-helper. On the server, run: <code>sudo ~/stx-monitor/scripts/install-helper.sh</code>');
    region('r-ctl-notes', notes.map((n) => `<div class="banner">${n}</div>`).join(''));
    const rows = [...ctl.containers].sort((a, b) => (a.state === 'running') - (b.state === 'running') || a.display.localeCompare(b.display));
    region('r-ctl-rows', rows.map(appRow).join('') || '<tr><td colspan="3" class="empty">No containers.</td></tr>');
    region('r-ctl-server', serverPanel(ctl));
    region('r-ctl-history', ctl.history.slice(0, 12).map(historyRow).join('') || '<li class="empty">Nothing yet.</li>');
    region('r-ctl-account', accountPanel());
  },
};

// ------------------------------------------------------------ control events
document.addEventListener('click', async (e) => {
  const el = e.target.closest('[data-action]');
  if (!el) return;
  const { action } = el.dataset;
  if (action === 'autopilot' && S.control) {
    const ap = S.control.autopilot;
    const turnOn = !ap.enabled;
    const ok = await confirmAction(turnOn
      ? { title: 'Turn on power-outage autopilot?', body: `On battery at ${ap.threshold}% or less, the apps stop and the server shuts down cleanly. It starts again by itself when power is back, if the BIOS is set to power on with AC.`, confirmLabel: 'Turn on' }
      : { title: 'Turn off power-outage autopilot?', body: 'The server keeps running on battery until it empties and loses power mid-write.', confirmLabel: 'Turn off', danger: true });
    if (!ok) return;
    try {
      S.control.autopilot = await postJSON('api/autopilot', { enabled: turnOn, threshold: ap.threshold });
      toast(`Autopilot ${turnOn ? 'on' : 'off'}`, 'ok');
    } catch (err) { toast(err.message, 'error'); }
    loadControl();
  } else if (action === 'check-updates') {
    try { await postJSON('api/updates/check'); toast('Checking each image for updates. This takes a minute.', 'busy'); } catch (err) { toast(err.message, 'error'); }
    setTimeout(loadControl, 30_000);
  } else if (action === 'add-passkey') {
    try {
      const name = await addPasskey();
      toast(`Passkey added: ${name}`, 'ok');
      loadAccount();
    } catch (err) { toast(err.message || 'The passkey was not added', 'error'); }
  } else if (action === 'remove-passkey') {
    const ok = await confirmAction({ title: `Remove the passkey "${el.dataset.name}"?`, body: 'That device can no longer sign in with it. You can add it again later.', confirmLabel: 'Remove', danger: true });
    if (!ok) return;
    try { await postJSON(`api/auth/passkeys/${encodeURIComponent(el.dataset.id)}/delete`); loadAccount(); } catch (err) { toast(err.message, 'error'); }
  } else if (action === 'sign-out-others') {
    const ok = await confirmAction({ title: 'Sign out everywhere else?', body: 'Every other phone, laptop and browser has to sign in again. This one stays signed in.', confirmLabel: 'Sign out others' });
    if (!ok) return;
    try { const r = await postJSON('api/auth/sessions/sign-out-others'); toast(`Signed out ${r.signed_out} other device${r.signed_out === 1 ? '' : 's'}`, 'ok'); loadAccount(); } catch (err) { toast(err.message, 'error'); }
  }
});

document.addEventListener('change', async (e) => {
  const el = e.target.closest('[data-change="autopilot-level"]');
  if (!el || !S.control) return;
  const threshold = Number(el.value);
  const ok = await confirmAction({ title: `Shut down at ${threshold}%?`, body: `On battery, autopilot stops the apps and shuts the server down when the battery reaches ${threshold}%.`, confirmLabel: 'Save' });
  if (!ok) { el.value = String(S.control.autopilot.threshold); return; }
  try { S.control.autopilot = await postJSON('api/autopilot', { enabled: true, threshold }); toast(`Autopilot shuts down at ${threshold}%`, 'ok'); } catch (err) { toast(err.message, 'error'); }
  loadControl();
});
