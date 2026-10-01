#!/usr/bin/env python3
"""
vk-dashboard — live status dashboard for the vk voice kit.

Serves a local web page (http://localhost:8787) that shows, in real time:
  - voice state (listening / recording / transcribing / off)
  - the last command Hermes heard
  - the last reply Hermes produced
  - a live tail of ~/.voice-kit/hermes.log
  - whether Hermes is running and whether opencode is running

Data comes from the voice kit's own state files, so it reflects exactly what
the kit is doing. The page updates live via Server-Sent Events (no refresh).

Usage:
  vk-dashboard            run in the foreground
  vk-dashboard --port N   run on a custom port (default 8787)
"""

import os
import re
import json
import time
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOME = os.path.expanduser("~")
KIT = os.path.join(HOME, ".voice-kit")
WAKE_STATE = os.path.join(KIT, "wake-state")
LAST_REPLY = os.path.join(KIT, "last-reply.txt")
HERMES_LOG = os.path.join(KIT, "hermes.log")
SESSION_FILE = os.path.join(KIT, "hermes-session")
REPLY_WAITING = os.path.join(KIT, "reply-waiting")

PORT = 8787
LOG_TAIL = 40


def read(path, default=""):
    try:
        with open(path, "r") as f:
            return f.read()
    except Exception:
        return default


def voice_state():
    s = read(WAKE_STATE, "off").strip().lower()
    if s in ("listening", "recording", "off"):
        return s
    return "off"


def last_command():
    """Last '>>>' line from hermes.log."""
    log = read(HERMES_LOG)
    m = re.findall(r"^---- .*? >>> (.*)$", log, re.M)
    return m[-1].strip() if m else ""


def last_reply():
    return read(LAST_REPLY).strip()


def log_tail():
    lines = read(HERMES_LOG).splitlines()
    return lines[-LOG_TAIL:]


def hermes_running():
    out = os.popen("pgrep -f 'hermes' >/dev/null 2>&1; echo $?").read().strip()
    return out == "0"


def opencode_running():
    out = os.popen("pgrep -x opencode >/dev/null 2>&1; echo $?").read().strip()
    return out == "0"


def session_id():
    return read(SESSION_FILE).strip()


def snapshot():
    return {
        "voice_state": voice_state(),
        "last_command": last_command(),
        "last_reply": last_reply(),
        "log_tail": log_tail(),
        "hermes_running": hermes_running(),
        "opencode_running": opencode_running(),
        "session_id": session_id(),
        "ts": int(time.time()),
    }


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>vk · Hermes Voice Dashboard</title>
<style>
  :root { --bg:#0d1117; --panel:#161b22; --line:#21262d; --txt:#e6edf3;
          --dim:#8b949e; --accent:#58a6ff; --green:#3fb950; --amber:#d29922;
          --red:#f85149; --purple:#bc8cff; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--txt);
         font:14px/1.5 -apple-system,'SF Mono',Menlo,monospace; }
  header { padding:18px 24px; border-bottom:1px solid var(--line);
           display:flex; align-items:center; gap:14px; flex-wrap:wrap; }
  header h1 { font-size:16px; margin:0; font-weight:600; }
  .dot { width:12px; height:12px; border-radius:50%; display:inline-block;
         background:var(--dim); }
  .dot.on { background:var(--green); box-shadow:0 0 8px var(--green); }
  .dot.rec { background:var(--red); box-shadow:0 0 8px var(--red);
             animation:pulse 1s infinite; }
  .dot.busy { background:var(--amber); box-shadow:0 0 8px var(--amber);
              animation:pulse 1.2s infinite; }
  @keyframes pulse { 50% { opacity:.35; } }
  .state { font-weight:600; text-transform:uppercase; letter-spacing:.5px; }
  .wrap { display:grid; grid-template-columns:1fr 1fr; gap:16px; padding:20px 24px; }
  @media (max-width:800px){ .wrap{ grid-template-columns:1fr; } }
  .card { background:var(--panel); border:1px solid var(--line); border-radius:10px;
          padding:16px; }
  .card h2 { font-size:12px; color:var(--dim); text-transform:uppercase;
             letter-spacing:.6px; margin:0 0 10px; }
  .cmd { font-size:15px; color:var(--accent); word-break:break-word; }
  .reply { white-space:pre-wrap; color:var(--txt); max-height:300px; overflow:auto; }
  .empty { color:var(--dim); font-style:italic; }
  .log { background:#0a0e13; border:1px solid var(--line); border-radius:8px;
         padding:10px; height:340px; overflow:auto; font-size:12px; }
  .log .in { color:var(--accent); }
  .log .out { color:var(--green); }
  .log .meta { color:var(--dim); }
  .chips { display:flex; gap:8px; flex-wrap:wrap; margin-top:10px; }
  .chip { font-size:11px; padding:3px 9px; border-radius:20px; border:1px solid var(--line); }
  .chip.good { color:var(--green); border-color:var(--green); }
  .chip.bad { color:var(--red); border-color:var(--red); }
  .chip.dim { color:var(--dim); }
  .full { grid-column:1 / -1; }
  footer { padding:10px 24px; color:var(--dim); font-size:11px; }
</style>
</head>
<body>
<header>
  <h1>🎙 vk · Hermes Voice</h1>
  <span class="dot" id="dot"></span>
  <span class="state" id="state">—</span>
  <span class="chip dim" id="session">session —</span>
  <span class="chip" id="hermesChip">hermes —</span>
  <span class="chip" id="opencodeChip">opencode —</span>
</header>

<div class="wrap">
  <div class="card">
    <h2>Last command heard</h2>
    <div class="cmd" id="cmd"><span class="empty">waiting for a command…</span></div>
  </div>
  <div class="card">
    <h2>Last reply</h2>
    <div class="reply" id="reply"><span class="empty">no reply yet</span></div>
  </div>
  <div class="card full">
    <h2>Live log · ~/.voice-kit/hermes.log</h2>
    <div class="log" id="log"></div>
  </div>
</div>

<footer>Live dashboard · updates every second · data from the voice kit state files</footer>

<script>
const $ = id => document.getElementById(id);
const stateMap = {
  listening:  { label:'Listening',  cls:'on' },
  recording:  { label:'Recording',  cls:'rec' },
  transcribing:{label:'Transcribing',cls:'busy' },
  muted:      { label:'Muted',      cls:'' },
  off:        { label:'Off',        cls:'' },
};
function render(s){
  const st = stateMap[s.voice_state] || stateMap.off;
  $('dot').className = 'dot ' + st.cls;
  $('state').textContent = st.label;
  $('session').textContent = 'session ' + (s.session_id || '—');
  $('hermesChip').textContent = 'hermes ' + (s.hermes_running ? '● running' : '○ idle');
  $('hermesChip').className = 'chip ' + (s.hermes_running ? 'good' : 'dim');
  $('opencodeChip').textContent = 'opencode ' + (s.opencode_running ? '● running' : '○ off');
  $('opencodeChip').className = 'chip ' + (s.opencode_running ? 'bad' : 'good');
  $('cmd').innerHTML = s.last_command ? escapeHtml(s.last_command) : '<span class="empty">waiting for a command…</span>';
  $('reply').innerHTML = s.last_reply ? escapeHtml(s.last_reply) : '<span class="empty">no reply yet</span>';
  const log = $('log');
  log.innerHTML = s.log_tail.map(line => {
    let cls = 'meta';
    if (line.includes(' >>> ')) cls = 'in';
    else if (line.includes(' <<< ')) cls = 'out';
    return '<div class="' + cls + '">' + escapeHtml(line) + '</div>';
  }).join('');
  log.scrollTop = log.scrollHeight;
}
function escapeHtml(t){ return t.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

// SSE live stream
const es = new EventSource('/api/stream');
es.onmessage = e => render(JSON.parse(e.data));
es.onerror = () => { /* EventSource auto-reconnects */ };
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/state":
            body = json.dumps(snapshot()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                while True:
                    self.wfile.write(b"data: " + json.dumps(snapshot()).encode() + b"\n\n")
                    self.wfile.flush()
                    time.sleep(1)
            except (BrokenPipeError, ConnectionResetError):
                pass
        else:
            self.send_response(404)
            self.end_headers()


def main():
    global PORT
    import sys
    for i, a in enumerate(sys.argv):
        if a == "--port" and i + 1 < len(sys.argv):
            PORT = int(sys.argv[i + 1])
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print("vk-dashboard running at http://localhost:%d" % PORT, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
