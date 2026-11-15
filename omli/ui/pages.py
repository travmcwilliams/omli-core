"""The UI's pages and scripts, as plain strings. Scripts are served from their own routes so
the Content-Security-Policy can forbid inline script; pages build every dynamic value with
``textContent``, never as HTML."""

from __future__ import annotations

_STYLE = """
:root { color-scheme: light dark; --bg:#fafafa; --fg:#1d1d1f; --muted:#6b6b70; --line:#d8d8dc;
        --accent:#2f5bd3; }
@media (prefers-color-scheme: dark) { :root { --bg:#161618; --fg:#ececf0; --muted:#9a9aa2;
        --line:#36363c; --accent:#8aa6ff; } }
body { margin:0; background:var(--bg); color:var(--fg);
       font:15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width:720px; margin:0 auto; padding:32px 16px; }
h1 { font-size:22px; margin:0 0 16px; }
p.muted, .muted { color:var(--muted); }
code, textarea, input { font:13px/1.4 ui-monospace, Consolas, monospace; }
code.block { display:block; padding:10px 12px; border:1px solid var(--line); border-radius:6px;
             overflow-wrap:anywhere; }
textarea, input[type=text] { width:100%; box-sizing:border-box; padding:8px;
             border:1px solid var(--line); border-radius:6px; background:transparent;
             color:inherit; }
button { font:inherit; padding:6px 14px; border-radius:6px; border:1px solid var(--accent);
         background:var(--accent); color:#fff; cursor:pointer; }
button.quiet { background:transparent; color:var(--accent); }
ul.projects { list-style:none; padding:0; }
ul.projects li { display:flex; gap:8px; align-items:center; justify-content:space-between;
                 padding:6px 0; border-bottom:1px solid var(--line); overflow-wrap:anywhere; }
.row { display:flex; gap:8px; margin:8px 0; }
.error { color:#c0392b; }
"""


def _page(title: str, body: str, script: str = "") -> str:
    tag = f'<script src="{script}"></script>' if script else ""
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            "<meta name=\"referrer\" content=\"no-referrer\">"
            f"<title>{title}</title><style>{_STYLE}</style></head>"
            f"<body><main>{body}</main>{tag}</body></html>")


def login_page(has_keys: bool) -> str:
    if not has_keys:
        return _page("OMLi sign in", (
            "<h1>Set up your login key</h1>"
            "<p>No login key is registered yet. In a terminal on this computer, run:</p>"
            "<code class=\"block\">omli-ui init</code>"
            "<p class=\"muted\">This makes your own key. Its private half stays in your home "
            "folder and never comes to the browser. Then reload this page.</p>"))
    return _page("OMLi sign in", (
        "<h1>Sign in</h1>"
        "<p><button id=\"get\">Get a sign-in code</button></p>"
        "<div id=\"step\" hidden>"
        "<p>In a terminal on this computer, run:</p>"
        "<code class=\"block\" id=\"cmd\"></code>"
        "<p>Paste what it prints here (the code works once, for two minutes):</p>"
        "<textarea id=\"token\" rows=\"4\" spellcheck=\"false\"></textarea>"
        "<p><button id=\"go\">Sign in</button></p></div>"
        "<p id=\"msg\" class=\"error\"></p>"), "/login.js")


LOGIN_JS = r"""
let challenge = null;
const msg = (t) => { document.getElementById('msg').textContent = t || ''; };
// A one-time ticket from `omli-ui login` arrives in the address fragment. Take it
// out of the address first, then trade it for a session and land on a clean address.
const ticketMatch = /^#ticket=([A-Za-z0-9_-]{1,200})$/.exec(location.hash);
if (location.hash) history.replaceState(null, '', '/login');
if (ticketMatch) {
  (async () => {
    const r = await fetch('/api/auth/redeem', {method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ticket: ticketMatch[1]})});
    if (r.ok) { location.replace('/'); return; }
    msg('That sign-in link has expired or was already used. Run omli-ui login again, or get a code below.');
  })();
}
document.getElementById('get').addEventListener('click', async () => {
  msg();
  const r = await fetch('/api/auth/challenge', {method: 'POST'});
  if (!r.ok) {
    const d = await r.json().catch(() => ({}));
    msg(r.status === 429 && d.error ? d.error : 'Could not get a code. Reload the page.');
    return;
  }
  challenge = (await r.json()).challenge;
  document.getElementById('cmd').textContent = 'omli-ui sign ' + challenge;
  document.getElementById('token').value = '';
  document.getElementById('step').hidden = false;
});
document.getElementById('go').addEventListener('click', async () => {
  msg();
  const token = document.getElementById('token').value.trim();
  const r = await fetch('/api/auth/login', {method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({challenge, token})});
  if (r.ok) { location.href = '/'; return; }
  const d = await r.json().catch(() => ({}));
  document.getElementById('step').hidden = true;
  msg(r.status === 429 && d.error ? d.error : 'That did not work. Get a new code and try again.');
});
"""


def app_page() -> str:
    return _page("OMLi", (
        "<div class=\"row\" style=\"justify-content:space-between;align-items:center\">"
        "<h1>OMLi</h1><button class=\"quiet\" id=\"logout\">Sign out</button></div>"
        "<p class=\"muted\">Signed in with key <code id=\"who\"></code></p>"
        "<p><a href=\"/mindmap\">Open the Mind-map</a></p>"
        "<h2>Projects</h2>"
        "<div class=\"row\"><input type=\"text\" id=\"path\" "
        "placeholder=\"Full path to a folder\"></div>"
        "<div class=\"row\"><button id=\"add\">Add folder</button>"
        "<button class=\"quiet\" id=\"scan\">Find git repositories in it</button></div>"
        "<p id=\"msg\" class=\"error\"></p>"
        "<ul class=\"projects\" id=\"list\"></ul>"), "/app.js")


APP_JS = r"""
const msg = (t) => { document.getElementById('msg').textContent = t || ''; };
async function post(url, body) {
  const r = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'},
                              body: JSON.stringify(body || {})});
  if (r.status === 401) { location.href = '/login'; return null; }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) { msg(data.error || 'That did not work.'); return null; }
  return data;
}
async function load() {
  const r = await fetch('/api/projects');
  if (r.status === 401) { location.href = '/login'; return; }
  const list = document.getElementById('list');
  list.replaceChildren();
  for (const path of (await r.json()).projects) {
    const li = document.createElement('li');
    const span = document.createElement('span');
    span.textContent = path;
    const btn = document.createElement('button');
    btn.className = 'quiet';
    btn.textContent = 'Remove';
    btn.addEventListener('click', async () => {
      msg(); if (await post('/api/projects/remove', {path})) load();
    });
    li.append(span, btn);
    list.append(li);
  }
}
document.getElementById('add').addEventListener('click', async () => {
  msg(); if (await post('/api/projects/add', {path: document.getElementById('path').value})) load();
});
document.getElementById('scan').addEventListener('click', async () => {
  msg();
  const d = await post('/api/projects/scan', {path: document.getElementById('path').value});
  if (d) { msg(d.added.length ? '' : 'No new repositories found.'); load(); }
});
document.getElementById('logout').addEventListener('click', async () => {
  await fetch('/api/auth/logout', {method: 'POST'}); location.href = '/login';
});
fetch('/api/auth/me').then(r => r.ok ? r.json() : null).then(d => {
  if (d) document.getElementById('who').textContent = d.pubkey_id;
});
load();
"""
