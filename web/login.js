// Sign-in page and passkey (WebAuthn) helpers.
// The browser's navigator.credentials API works with binary ArrayBuffers;
// the server speaks JSON with base64url strings, so we convert both ways.

import { S, esc, hooks, hostName, icon, postJSON, views } from './core.js';

const b64uToBuf = (s) => {
  const str = atob(s.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - (s.length % 4)) % 4));
  return Uint8Array.from(str, (c) => c.charCodeAt(0)).buffer;
};
const bufToB64u = (buf) => btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');

export function passkeysSupported() {
  return window.isSecureContext && !!window.PublicKeyCredential && !!navigator.credentials;
}

function onApple() { return /iPhone|iPad|Macintosh/.test(navigator.userAgent); }
function passkeyLabel() {
  if (/iPhone|iPad/.test(navigator.userAgent)) return 'Sign in with Face ID';
  return 'Sign in with a passkey';
}

function credentialJSON(cred) {
  const r = cred.response;
  const response = { clientDataJSON: bufToB64u(r.clientDataJSON) };
  if (r.attestationObject) {
    response.attestationObject = bufToB64u(r.attestationObject);
    if (r.getTransports) response.transports = r.getTransports();
  } else {
    response.authenticatorData = bufToB64u(r.authenticatorData);
    response.signature = bufToB64u(r.signature);
    response.userHandle = r.userHandle ? bufToB64u(r.userHandle) : null;
  }
  return {
    id: cred.id, rawId: bufToB64u(cred.rawId), type: cred.type, response,
    authenticatorAttachment: cred.authenticatorAttachment || null,
    clientExtensionResults: cred.getClientExtensionResults ? cred.getClientExtensionResults() : {},
  };
}

async function passkeySignIn(remember) {
  const { flow, options } = await postJSON('api/auth/passkey/begin');
  const publicKey = {
    ...options,
    challenge: b64uToBuf(options.challenge),
    allowCredentials: (options.allowCredentials || []).map((c) => ({ ...c, id: b64uToBuf(c.id) })),
  };
  const cred = await navigator.credentials.get({ publicKey });
  return postJSON('api/auth/passkey/finish', { flow, credential: credentialJSON(cred), remember });
}

// Register a passkey for the signed-in user on this device. Returns its name.
export async function addPasskey() {
  if (!passkeysSupported()) throw new Error('Passkeys need HTTPS. Open the console at its Tailscale address.');
  const { flow, options } = await postJSON('api/auth/passkeys/register/begin');
  const publicKey = {
    ...options,
    challenge: b64uToBuf(options.challenge),
    user: { ...options.user, id: b64uToBuf(options.user.id) },
    excludeCredentials: (options.excludeCredentials || []).map((c) => ({ ...c, id: b64uToBuf(c.id) })),
  };
  let cred;
  try {
    cred = await navigator.credentials.create({ publicKey });
  } catch (err) {
    if (err.name === 'InvalidStateError') throw new Error('This device already has a passkey for this console.');
    if (err.name === 'NotAllowedError') throw new Error('Cancelled.');
    throw err;
  }
  const device = /iPhone/.test(navigator.userAgent) ? 'iPhone' : /iPad/.test(navigator.userAgent) ? 'iPad'
    : /Android/.test(navigator.userAgent) ? 'Android phone' : /Macintosh/.test(navigator.userAgent) ? 'Mac'
    : /Windows/.test(navigator.userAgent) ? 'Windows PC' : 'This device';
  const added = await postJSON('api/auth/passkeys/register/finish', { flow, credential: credentialJSON(cred), name: device });
  return added.name;
}

const rings = (size) => `<svg viewBox="0 0 400 400" aria-hidden="true" class="login-rings" style="max-width:${size}px">
  <circle cx="200" cy="200" r="196" fill="none" stroke="var(--line)" stroke-width="1"/>
  <g class="spin-login"><circle cx="200" cy="200" r="180" fill="none" stroke="#3FD8FF" stroke-opacity="0.45" stroke-width="12" stroke-dasharray="1.5 9"/></g>
  <circle cx="200" cy="200" r="158" fill="none" stroke="var(--dash)" stroke-width="1" stroke-dasharray="80 12 18 12"/>
  <circle cx="200" cy="200" r="132" fill="none" stroke="var(--track)" stroke-width="10"/>
  <circle cx="200" cy="200" r="132" fill="none" stroke="#3FD8FF" stroke-opacity="0.7" stroke-width="10" stroke-dasharray="200 629" transform="rotate(-90 200 200)"/>
  <circle cx="200" cy="200" r="110" fill="none" stroke="var(--track)" stroke-width="10"/>
  <circle cx="200" cy="200" r="110" fill="none" stroke="#DDF7FF" stroke-opacity="0.5" stroke-width="10" stroke-dasharray="120 571" transform="rotate(30 200 200)"/>
  <circle cx="200" cy="200" r="62" fill="none" stroke="#3FD8FF" stroke-width="2"/>
  <circle cx="200" cy="200" r="36" fill="none" stroke="#3FD8FF" stroke-width="5" stroke-dasharray="10 6"/>
  <circle cx="200" cy="200" r="12" fill="#3FD8FF"/></svg>`;

let busy = false;

function setError(text) {
  const el = document.getElementById('login-error');
  if (el) { el.textContent = text || ''; el.hidden = !text; }
}

function locked() {
  return S.session.locked_for > 0;
}

views.login = {
  shell() {
    return `<div class="login">
      <div class="login-brand">${rings(380)}
        <div class="login-name"><span>${esc(hostName())}</span><span class="muted">Private console. Only reachable on your Tailscale network.</span></div>
      </div>
      <div class="panel lg login-panel"><div class="in" id="r-login"></div></div>
    </div>`;
  },
  update() {
    const s = S.session;
    const head = `<div><span class="kicker">Identity check</span><h1 class="login-title">Sign in to stx-monitor</h1></div>`;
    if (!s.checked) { document.getElementById('r-login').innerHTML = `${head}<span class="muted">Connecting…</span>`; return; }
    if (!s.has_password) {
      document.getElementById('r-login').innerHTML = `${head}
        <p style="margin:0">No password is set yet. On the server, run:</p>
        <pre class="logs" style="max-height:none">docker exec -it stx-monitor python -m monitor set-password</pre>
        <p class="muted" style="margin:0;font-size:14px">Then reload this page and sign in. After that you can add a passkey from Control.</p>
        <button type="button" class="btn" data-login="reload">Reload</button>`;
      return;
    }
    const el = document.getElementById('r-login');
    if (el.dataset.ready) { // keep what the user typed; only refresh the lockout state
      const btn = document.getElementById('login-submit');
      if (btn) btn.disabled = busy || locked();
      return;
    }
    el.dataset.ready = '1';
    const canPasskey = passkeysSupported() && s.passkeys > 0;
    el.innerHTML = `${head}
      ${canPasskey ? `<button type="button" class="btn passkey-btn" data-login="passkey">${icon(onApple() && /iPhone|iPad/.test(navigator.userAgent) ? 'face' : 'key', 22)}${passkeyLabel()}</button>
        <div class="or"><span></span>or use your password<span></span></div>` : ''}
      ${!window.isSecureContext ? '<p class="amber-text" style="margin:0;font-size:13px">Passkeys need HTTPS. Use the console\'s Tailscale address (https://…ts.net) to sign in with Face ID or Touch ID.</p>' : ''}
      <form id="login-form" class="login-form" novalidate>
        <label class="field"><span>Username</span><input id="login-user" name="username" type="text" autocomplete="username" autocapitalize="none" spellcheck="false" required></label>
        <label class="field"><span class="field-row"><span>Password</span><span class="muted" style="font-size:12px">Forgot it? Reset from the server</span></span>
          <input id="login-pass" name="password" type="password" autocomplete="current-password" required></label>
        <label class="check"><input id="login-remember" type="checkbox" checked>Keep me signed in on this device for 30 days</label>
        <p id="login-error" class="login-error" role="alert" hidden></p>
        <button type="submit" class="btn solid" id="login-submit">Sign in</button>
      </form>
      <p class="muted login-note">After 5 wrong passwords, sign-in pauses for 15 minutes. Every sign-in is recorded in Activity.</p>`;
    if (locked()) setError(`Too many wrong attempts. Try again in ${Math.ceil(s.locked_for / 60)} min.`);
    document.getElementById(canPasskey ? 'login-user' : 'login-user')?.focus();
  },
};

async function finish(promise) {
  busy = true;
  setError('');
  const btn = document.getElementById('login-submit');
  if (btn) btn.disabled = true;
  try {
    await promise;
    hooks.signedIn();
  } catch (err) {
    if (err.name === 'NotAllowedError') setError('Passkey sign-in was cancelled.');
    else setError(err.message || 'Sign-in failed.');
    if (err.data?.locked_for) S.session.locked_for = err.data.locked_for;
  } finally {
    busy = false;
    if (btn) btn.disabled = locked();
  }
}

document.addEventListener('submit', (e) => {
  if (e.target.id !== 'login-form') return;
  e.preventDefault();
  const username = document.getElementById('login-user').value;
  const password = document.getElementById('login-pass').value;
  if (!username || !password) { setError('Enter your username and password.'); return; }
  const remember = document.getElementById('login-remember').checked;
  finish(postJSON('api/auth/login', { username, password, remember }).finally(() => {
    const pass = document.getElementById('login-pass');
    if (pass) pass.value = '';
  }));
});

document.addEventListener('click', (e) => {
  const el = e.target.closest('[data-login]');
  if (!el) return;
  if (el.dataset.login === 'reload') location.reload();
  if (el.dataset.login === 'passkey') {
    const remember = document.getElementById('login-remember')?.checked ?? true;
    finish(passkeySignIn(remember));
  }
});
