"""Local web UI for local-sync: discovery switch, pairing, plan, sync.

Loopback only, protected by a random token in the URL. Every action is a
deliberate click — nothing pairs or syncs without the human pressing a button.
"""

from __future__ import annotations

import json
import secrets
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .core import Store, ctl, ensure_daemon, log

PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>local-sync</title>
<style>
  :root{--bg:#14161a;--card:#1b1e24;--line:#2a2f38;--txt:#e6e8ee;--dim:#98a0ae;--acc:#7aa2f7;--ok:#5fb87a;--warn:#d8a657;--err:#e06c75}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--txt);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
  main{max-width:760px;margin:0 auto;padding:28px 20px 60px}
  h1{font-size:18px;margin:0 0 4px;font-weight:600}
  h2{font-size:13px;text-transform:uppercase;letter-spacing:.08em;color:var(--dim);margin:0 0 12px;font-weight:600}
  .sub{color:var(--dim);font-size:12px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 18px;margin-top:16px}
  .row{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}
  .peer{padding:12px 0;border-top:1px solid var(--line)}
  .peer:first-child{border-top:0;padding-top:0}
  .name{font-weight:600}
  .tag{font-size:11px;padding:2px 7px;border-radius:99px;border:1px solid var(--line);color:var(--dim)}
  .tag.paired{color:var(--ok);border-color:#2f5a3d}
  .tag.found{color:var(--warn);border-color:#5a4a2f}
  button{background:#232833;color:var(--txt);border:1px solid var(--line);border-radius:8px;padding:7px 12px;font:inherit;cursor:pointer}
  button:hover{border-color:#3a4150;background:#282e3a}
  button.primary{background:var(--acc);border-color:var(--acc);color:#0d1117;font-weight:600}
  button.danger{color:var(--err)}
  button:disabled{opacity:.45;cursor:default}
  input[type=password],input[type=text]{background:#11141a;border:1px solid var(--line);color:var(--txt);border-radius:8px;padding:8px 10px;font:inherit;width:100%}
  label.switch{display:flex;align-items:center;gap:10px;cursor:pointer;font-weight:600}
  .hint{color:var(--dim);font-size:12px;margin-top:6px}
  pre{white-space:pre-wrap;word-break:break-word;background:#11141a;border:1px solid var(--line);border-radius:8px;padding:12px;font-size:12px;color:var(--dim);margin:0;max-height:320px;overflow:auto}
  .pw{display:none;margin-top:10px;gap:8px;align-items:center}
  .pw.on{display:flex}
  .msg{margin-top:10px;font-size:12px}
  .msg.err{color:var(--err)} .msg.ok{color:var(--ok)}
  .empty{color:var(--dim);font-size:13px}
</style></head>
<body><main>
  <h1>local-sync</h1>
  <div class="sub" id="me">загрузка…</div>

  <section class="card">
    <div class="row">
      <label class="switch"><input type="checkbox" id="search"> Поиск устройств</label>
      <span class="sub" id="searchState"></span>
    </div>
    <div class="hint">Выключи — тебя перестанут находить в сети. Спаренные машины продолжат синхронизироваться.</div>
  </section>

  <section class="card">
    <h2>Устройства</h2>
    <div id="peers"></div>
  </section>

  <section class="card">
    <h2>Запросы на подключение</h2>
    <div id="pending"><div class="empty">нет</div></div>
  </section>

  <section class="card">
    <h2>Результат</h2>
    <pre id="out">—</pre>
  </section>
</main>
<script>
const TOKEN = new URLSearchParams(location.search).get("t") || "";
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

async function api(path, body) {
  const res = await fetch(path + "?t=" + encodeURIComponent(TOKEN), {
    method: body === undefined ? "GET" : "POST",
    headers: {"Content-Type": "application/json", "X-Local-Sync-Token": TOKEN},
    body: body === undefined ? undefined : JSON.stringify(body)
  });
  if (res.status === 401) throw new Error("неверный токен интерфейса");
  return await res.json();
}

function show(payload, isError) {
  $("out").textContent = typeof payload === "string" ? payload : JSON.stringify(payload, null, 2);
  $("out").style.color = isError ? "var(--err)" : "var(--dim)";
}

function pwBlock(id, label, cb) {
  return `<div class="pw" id="pw-${id}">
    <input type="password" id="pwi-${id}" placeholder="${esc(label)}" autocomplete="new-password">
    <button class="primary" onclick="${cb}">ОК</button>
  </div>`;
}

function renderPeers(peers, pairedIds) {
  if (!peers.length) { $("peers").innerHTML = '<div class="empty">никого не видно — включи поиск на обеих машинах</div>'; return; }
  $("peers").innerHTML = peers.map(p => {
    const paired = p.paired;
    const id = p.id.slice(0, 8);
    return `<div class="peer">
      <div class="row">
        <div><span class="name">${esc(p.name)}</span>
          <span class="sub"> ${esc(p.profile)} · ${esc(p.addr)}:${esc(p.api_port)}</span></div>
        <div class="row">
          <span class="tag ${paired ? "paired" : "found"}">${paired ? "спарен" : "найден"}</span>
          ${paired
            ? `<button onclick="plan('${esc(p.name)}')">План</button>
               <button class="primary" onclick="sync('${esc(p.name)}')">Синхронизировать</button>`
            : `<button class="primary" onclick="togglePw('${id}')">Подключить</button>`}
        </div>
      </div>
      ${paired ? "" : pwBlock(id, "общий пароль (одинаковый на обеих машинах)", `pair('${esc(p.name)}','${id}')`)}
    </div>`;
  }).join("");
}

function renderPending(items) {
  if (!items.length) { $("pending").innerHTML = '<div class="empty">нет</div>'; return; }
  $("pending").innerHTML = items.map(p => {
    const id = String(p.pair_id || p.id).slice(0, 8);
    const psk = p.kind === "psk";
    return `<div class="peer">
      <div class="row">
        <div><span class="name">${esc(p.name)}</span> <span class="sub">${esc(p.profile)}</span></div>
        <div class="row">
          ${psk ? "" : `<span class="sub">код ${esc(p.code)}</span>`}
          <button class="primary" onclick="${psk ? `togglePwPending('${id}')` : `acceptCode('${esc(p.name)}','${esc(p.code)}')`}">
            ${psk ? "Ввести пароль" : "Подтвердить"}
          </button>
          <button class="danger" onclick="deny('${esc(p.pair_id || "")}')">Отклонить</button>
        </div>
      </div>
      ${psk ? pwBlock("p" + id, "пароль, который ввели на другой машине", `acceptPsk('${esc(p.name)}','p${id}')`) : ""}
    </div>`;
  }).join("");
}

function togglePw(id) { const el = $("pw-" + id); if (el) el.classList.toggle("on"); }
function togglePwPending(id) { const el = $("pw-p" + id); if (el) el.classList.toggle("on"); }

async function pair(peer, id) {
  const password = $("pwi-" + id).value;
  if (!password) return show("введи пароль", true);
  try { show((await api("/api/pair", {peer, password})).result); }
  catch (e) { show(e.message, true); }
  refresh();
}
async function acceptPsk(peer, id) {
  const password = $("pwi-" + id).value;
  if (!password) return show("введи пароль", true);
  try { show((await api("/api/accept", {peer, password})).result); }
  catch (e) { show(e.message, true); }
  refresh();
}
async function acceptCode(peer, code) {
  try { show((await api("/api/accept", {peer, code})).result); }
  catch (e) { show(e.message, true); }
  refresh();
}
async function deny(pairId) {
  try { show((await api("/api/deny", {pair_id: pairId})).result); }
  catch (e) { show(e.message, true); }
  refresh();
}
async function plan(peer) {
  try { show((await api("/api/plan", {peer})).result, false); }
  catch (e) { show(e.message, true); }
}
async function sync(peer) {
  if (!confirm("Применить синхронизацию с " + peer + "?")) return;
  try { show((await api("/api/sync", {peer})).result); }
  catch (e) { show(e.message, true); }
  refresh();
}

$("search").addEventListener("change", async (e) => {
  try { await api("/api/search", {enabled: e.target.checked}); }
  catch (err) { show(err.message, true); }
  refresh();
});

async function refresh() {
  try {
    const status = (await api("/api/status")).result;
    $("me").textContent = `${status.me.name} · профиль ${status.me.profile} · ${status.profile_dir}`;
    $("search").checked = !!status.search;
    $("searchState").textContent = status.search ? "идёт" : "выключен";
    const peers = (await api("/api/peers")).result;
    renderPeers(peers.peers || [], []);
    renderPending(peers.pending || []);
  } catch (e) {
    $("me").textContent = "ошибка: " + e.message;
  }
}
refresh();
setInterval(refresh, 3000);
</script></body></html>
"""


class UiHandler(BaseHTTPRequestHandler):
    store: Store
    token: str

    def log_message(self, fmt, *args):
        pass

    def _allowed(self) -> bool:
        host = self.client_address[0]
        if host not in ("127.0.0.1", "::1"):
            return False
        supplied = self.headers.get("X-Local-Sync-Token") or (parse_qs(urlparse(self.path).query).get("t") or [""])[0]
        return bool(supplied) and secrets.compare_digest(supplied, self.token)

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: dict) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        if not self._allowed():
            return self._json(401, {"ok": False, "error": "unauthorized"})
        if path == "/api/status":
            return self._call("status", {})
        if path == "/api/peers":
            return self._call("peers", {})
        return self._json(404, {"ok": False, "error": "unknown route"})

    def do_POST(self) -> None:
        if not self._allowed():
            return self._json(401, {"ok": False, "error": "unauthorized"})
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._json(400, {"ok": False, "error": "bad json"})
        routes = {
            "/api/search": ("search", lambda p: {"enabled": bool(p.get("enabled"))}),
            "/api/pair": ("pair_psk_begin", lambda p: {"peer": p.get("peer", ""), "password": p.get("password", "")}),
            "/api/accept": ("pair_psk_accept", lambda p: {"peer": p.get("peer", ""), "password": p.get("password", "")}),
            "/api/deny": ("deny", lambda p: {"pair_id": p.get("pair_id", "")}),
            "/api/plan": ("plan", lambda p: {"peer": p.get("peer", "")}),
            "/api/sync": ("sync", lambda p: {"peer": p.get("peer", "")}),
        }
        route = routes.get(path)
        if not route:
            return self._json(404, {"ok": False, "error": "unknown route"})
        op, shape = route
        args = shape(payload)
        if path == "/api/accept" and not args.get("password"):
            op = "pair_accept"  # code-based confirmation
            args = {"peer": args["peer"], "code": payload.get("code", "")}
        return self._call(op, args)

    def _call(self, op: str, args: dict) -> None:
        answer = ctl(self.store, op, **args)
        if answer is None:
            return self._json(503, {"ok": False, "error": "демон не отвечает"})
        if not answer.get("ok"):
            return self._json(200, {"ok": False, "error": answer.get("error", "ошибка")})
        return self._json(200, {"ok": True, "result": answer["result"]})


class UiServer:
    def __init__(self, store: Store, port: int = 45873, bind: str = "127.0.0.1"):
        self.store = store
        self.token = secrets.token_urlsafe(16)
        handler = type("BoundUiHandler", (UiHandler,), {"store": store, "token": self.token})
        self.server = ThreadingHTTPServer((bind, port), handler)
        self.port = self.server.server_port

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?t={self.token}"

    def serve_forever(self) -> None:
        self.server.serve_forever()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def run_ui(store: Store, port: int = 45873, open_browser: bool = True, wait: float = 20.0) -> int:
    status = ensure_daemon(store, timeout=wait)
    if status is None:
        log("daemon is not running and did not start — check ~/.local-sync/daemon.log")
        return 1
    server = UiServer(store, port=port)
    print(f"local-sync UI: {server.url}")
    if open_browser:
        webbrowser.open(server.url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.stop()
    return 0
