#!/usr/bin/env python3
"""
vk-dashboard — full control dashboard for the vk voice kit.

Serves a local web page (http://localhost:8787) with:
  - Voice state (listening / recording / transcribing / muted / off)
  - Active Hermes processes (running commands, with stop button)
  - Live conversation stream (full session history, not just final reply)
  - Process status (running / finished / error)
  - Control buttons (stop process, mute/unmute, clear session)
  - Live log tail

Data comes from the voice kit's state files and process table.
Updates live via Server-Sent Events (no refresh).

Usage:
  vk-dashboard            run in the foreground
  vk-dashboard --port N   run on a custom port (default 8787)
"""

import os
import re
import json
import time
import signal
import subprocess
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOME = os.path.expanduser("~")
KIT = os.path.join(HOME, ".voice-kit")
WAKE_STATE = os.path.join(KIT, "wake-state")
LAST_REPLY = os.path.join(KIT, "last-reply.txt")
HERMES_LOG = os.path.join(KIT, "hermes.log")
SESSION_FILE = os.path.join(KIT, "hermes-session")
WAKE_PID = os.path.join(KIT, "wake.pid")
MUTE_FLAG = os.path.join(KIT, "wake-muted")
HERMES_BIN = os.path.join(HOME, ".local/bin/hermes")

PORT = 8787
LOG_TAIL = 60
CONV_TAIL = 100


def read(path, default=""):
    try:
        with open(path, "r") as f:
            return f.read()
    except Exception:
        return default


def voice_state():
    s = read(WAKE_STATE, "off").strip().lower()
    if s in ("listening", "recording", "transcribing", "muted", "off"):
        return s
    return "off"


def last_command():
    log = read(HERMES_LOG)
    m = re.findall(r"^---- .*? >>> (.*)$", log, re.M)
    return m[-1].strip() if m else ""


def last_reply():
    return read(LAST_REPLY).strip()


def log_tail():
    lines = read(HERMES_LOG).splitlines()
    return lines[-LOG_TAIL:]


def hermes_running():
    out = subprocess.run(["pgrep", "-f", "hermes"], capture_output=True, text=True).stdout.strip()
    return bool(out)


def opencode_running():
    out = subprocess.run(["pgrep", "-x", "opencode"], capture_output=True, text=True).stdout.strip()
    return bool(out)


def session_id():
    return read(SESSION_FILE).strip()


def active_hermes_processes():
    """Return list of active hermes -z processes with pid, command, elapsed."""
    try:
        out = subprocess.run(
            ["ps", "-eo", "pid,etime,command"],
            capture_output=True, text=True, timeout=5
        ).stdout
        procs = []
        for line in out.splitlines():
            if "hermes" in line and ("-z" in line or "--resume" in line) and "grep" not in line:
                parts = line.strip().split(None, 2)
                if len(parts) >= 3:
                    pid, elapsed, cmd = parts
                    # Extract the prompt from -z "..."
                    prompt = ""
                    m = re.search(r'-z\s+"([^"]*)"', cmd)
                    if m:
                        prompt = m.group(1)[:80]
                    procs.append({
                        "pid": int(pid),
                        "elapsed": elapsed,
                        "prompt": prompt,
                        "cmd": cmd[:120]
                    })
        return procs
    except Exception:
        return []


def conversation_history():
    """Read the current Hermes session conversation (user + assistant messages)."""
    sid = session_id()
    if not sid:
        return []
    try:
        # Try to read from hermes session export or state
        result = subprocess.run(
            [HERMES_BIN, "sessions", "export", sid],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            # Parse the exported conversation
            lines = result.stdout.splitlines()
            conv = []
            for line in lines:
                if line.startswith("USER:"):
                    conv.append({"role": "user", "text": line[5:].strip()})
                elif line.startswith("ASSISTANT:"):
                    conv.append({"role": "assistant", "text": line[11:].strip()})
            return conv[-CONV_TAIL:]
    except Exception:
        pass
    return []


def process_status():
    """Check if any hermes -z process is currently running."""
    procs = active_hermes_processes()
    return {
        "active": len(procs) > 0,
        "count": len(procs),
        "processes": procs
    }


def snapshot():
    return {
        "voice_state": voice_state(),
        "last_command": last_command(),
        "last_reply": last_reply(),
        "log_tail": log_tail(),
        "hermes_running": hermes_running(),
        "opencode_running": opencode_running(),
        "session_id": session_id(),
        "process": process_status(),
        "conversation": conversation_history(),
        "ts": int(time.time()),
    }


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>vk · Hermes Voice Control</title>
<style>
  :root { --bg:#0b0f14; --panel:#121820; --panel2:#0e141b; --line:#1f2933;
          --txt:#e6edf3; --dim:#7d8a99; --accent:#58a6ff; --green:#3fb950;
          --amber:#d29922; --red:#f85149; --purple:#bc8cff; --muted:#6e7681; }
  * { box-sizing:border-box; }
  body { margin:0; min-height:100vh; color:var(--txt);
         font:14px/1.55 -apple-system,'SF Mono',Menlo,monospace;
         background:
           radial-gradient(1200px 600px at 80% -10%, rgba(88,166,255,.08), transparent 60%),
           radial-gradient(900px 500px at -10% 110%, rgba(188,140,255,.06), transparent 60%),
           var(--bg); }
  .wrap { max-width:1200px; margin:0 auto; padding:24px; }
  header { display:flex; align-items:center; gap:14px; flex-wrap:wrap; margin-bottom:18px; }
  header h1 { font-size:17px; margin:0; font-weight:650; letter-spacing:.3px; }
  header h1 .sub { color:var(--dim); font-weight:400; font-size:12px; }
  .spacer { flex:1; }
  .pill { display:inline-flex; align-items:center; gap:9px; padding:7px 14px;
          border-radius:999px; border:1px solid var(--line); background:var(--panel);
          font-weight:650; text-transform:uppercase; letter-spacing:.6px; font-size:12px; }
  .pill .dot { width:10px; height:10px; border-radius:50%; background:var(--dim); }
  .pill.on  { color:var(--green); border-color:rgba(63,185,80,.4); }
  .pill.on .dot { background:var(--green); box-shadow:0 0 10px var(--green); }
  .pill.rec { color:var(--red); border-color:rgba(248,81,73,.45); }
  .pill.rec .dot { background:var(--red); box-shadow:0 0 10px var(--red); animation:pulse 1s infinite; }
  .pill.busy{ color:var(--amber); border-color:rgba(210,153,34,.45); }
  .pill.busy .dot { background:var(--amber); box-shadow:0 0 10px var(--amber); animation:pulse 1.2s infinite; }
  .pill.muted{ color:var(--muted); border-color:var(--line); }
  .pill.muted .dot { background:var(--muted); }
  .pill.off { color:var(--dim); }
  @keyframes pulse { 50% { opacity:.3; } }
  .wave { display:inline-flex; align-items:center; gap:3px; height:18px; opacity:0; transition:opacity .3s; }
  .wave.active { opacity:1; }
  .wave i { width:3px; border-radius:2px; background:var(--red); animation:bar 1s ease-in-out infinite; }
  .wave i:nth-child(1){ animation-delay:0s; } .wave i:nth-child(2){ animation-delay:.15s; }
  .wave i:nth-child(3){ animation-delay:.3s; } .wave i:nth-child(4){ animation-delay:.45s; }
  .wave i:nth-child(5){ animation-delay:.6s; }
  @keyframes bar { 0%,100%{ height:4px; } 50%{ height:18px; } }
  .chips { display:flex; gap:8px; flex-wrap:wrap; }
  .chip { font-size:11px; padding:3px 10px; border-radius:20px; border:1px solid var(--line); color:var(--dim); background:var(--panel); }
  .chip.good { color:var(--green); border-color:rgba(63,185,80,.4); }
  .chip.bad  { color:var(--red);  border-color:rgba(248,81,73,.4); }
  .grid { display:grid; grid-template-columns:1fr 1fr; gap:16px; }
  @media (max-width:820px){ .grid{ grid-template-columns:1fr; } }
  .card { background:linear-gradient(180deg, var(--panel), var(--panel2));
          border:1px solid var(--line); border-radius:12px; padding:16px 18px;
          box-shadow:0 1px 0 rgba(255,255,255,.03) inset; }
  .card h2 { font-size:11px; color:var(--dim); text-transform:uppercase; letter-spacing:.8px; margin:0 0 10px; display:flex; align-items:center; justify-content:space-between; gap:8px; }
  .card h2 .live { color:var(--green); font-size:9px; letter-spacing:.5px; display:none; }
  .card h2 .live.show { display:inline; }
  .cmd { font-size:15px; color:var(--accent); word-break:break-word; background:rgba(88,166,255,.06); border:1px solid rgba(88,166,255,.15); border-radius:8px; padding:10px 12px; }
  .reply { white-space:pre-wrap; color:var(--txt); max-height:300px; overflow:auto; font-size:13px; }
  .empty { color:var(--dim); font-style:italic; }
  .full { grid-column:1 / -1; }
  .log { background:#080c11; border:1px solid var(--line); border-radius:9px; padding:10px 12px; height:340px; overflow:auto; font-size:12px; }
  .log .row { display:flex; gap:10px; padding:1px 0; }
  .log .t { color:#4b5563; flex:0 0 auto; }
  .log .in { color:var(--accent); }
  .log .out{ color:var(--green); }
  .log .meta{ color:var(--dim); }
  .controls { display:flex; gap:8px; flex-wrap:wrap; margin-top:12px; }
  .btn { padding:6px 14px; border-radius:8px; border:1px solid var(--line); background:var(--panel); color:var(--txt); font-size:12px; cursor:pointer; font-family:inherit; }
  .btn:hover { background:var(--panel2); border-color:var(--dim); }
  .btn.danger { color:var(--red); border-color:rgba(248,81,73,.4); }
  .btn.danger:hover { background:rgba(248,81,73,.1); }
  .btn.primary { color:var(--accent); border-color:rgba(88,166,255,.4); }
  .btn.primary:hover { background:rgba(88,166,255,.1); }
  .process { display:flex; align-items:center; gap:10px; padding:8px 10px; border:1px solid var(--line); border-radius:8px; margin-bottom:8px; background:var(--panel2); }
  .process .pid { color:var(--dim); font-size:11px; min-width:60px; }
  .process .elapsed { color:var(--amber); font-size:11px; min-width:80px; }
  .process .prompt { flex:1; color:var(--txt); font-size:12px; word-break:break-word; }
  .process .stop { padding:2px 8px; font-size:11px; }
  .conv { max-height:400px; overflow:auto; }
  .conv .msg { margin-bottom:12px; padding:10px 12px; border-radius:8px; }
  .conv .msg.user { background:rgba(88,166,255,.06); border:1px solid rgba(88,166,255,.15); }
  .conv .msg.assistant { background:rgba(63,185,80,.06); border:1px solid rgba(63,185,80,.15); }
  .conv .role { font-size:10px; text-transform:uppercase; letter-spacing:.5px; margin-bottom:4px; }
  .conv .msg.user .role { color:var(--accent); }
  .conv .msg.assistant .role { color:var(--green); }
  .conv .text { white-space:pre-wrap; font-size:13px; }
  footer { margin-top:18px; color:var(--dim); font-size:11px; display:flex; justify-content:space-between; flex-wrap:wrap; gap:8px; }
  footer .clock { color:var(--green); }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>🎙 vk · Hermes Voice <span class="sub">full control dashboard</span></h1>
    <span class="wave" id="wave"><i></i><i></i><i></i><i></i><i></i></span>
    <span class="pill off" id="pill"><span class="dot"></span><span id="state">—</span></span>
    <div class="spacer"></div>
    <div class="chips">
      <span class="chip" id="session">session —</span>
      <span class="chip" id="hermesChip">hermes —</span>
      <span class="chip" id="opencodeChip">opencode —</span>
    </div>
  </header>

  <div class="grid">
    <div class="card">
      <h2>Last command heard <span class="live" id="cmdLive">● live</span></h2>
      <div class="cmd" id="cmd"><span class="empty">waiting for a command…</span></div>
    </div>
    <div class="card">
      <h2>Last reply <span class="live" id="replyLive">● live</span></h2>
      <div class="reply" id="reply"><span class="empty">no reply yet</span></div>
    </div>
    <div class="card full">
      <h2>Active processes <span class="live show" id="procLive">● monitoring</span></h2>
      <div id="processes"><span class="empty">no active processes</span></div>
      <div class="controls">
        <button class="btn danger" onclick="stopAll()">Stop all</button>
        <button class="btn" onclick="toggleMute()" id="muteBtn">Mute</button>
        <button class="btn primary" onclick="clearSession()">Clear session</button>
      </div>
    </div>
    <div class="card full">
      <h2>Conversation <span class="live show">● live</span></h2>
      <div class="conv" id="conv"><span class="empty">no conversation yet</span></div>
    </div>
    <div class="card full">
      <h2>Live log · ~/.voice-kit/hermes.log <span class="live show">● streaming</span></h2>
      <div class="log" id="log"></div>
    </div>
  </div>

  <footer>
    <span>Data from the voice kit state files · updates every second</span>
    <span>last update <span class="clock" id="clock">—</span></span>
  </footer>
</div>

<script>
const $ = id => document.getElementById(id);
const stateMap = {
  listening:   { label:'Listening',   cls:'on' },
  recording:   { label:'Recording',   cls:'rec' },
  transcribing:{ label:'Transcribing',cls:'busy' },
  muted:       { label:'Muted',       cls:'muted' },
  off:         { label:'Off',         cls:'off' },
};
function fmtClock(ts){
  const d = new Date(ts*1000);
  return d.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', second:'2-digit'});
}
function escapeHtml(t){ return t.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

function render(s){
  const st = stateMap[s.voice_state] || stateMap.off;
  $('pill').className = 'pill ' + st.cls;
  $('state').textContent = st.label;
  $('wave').className = 'wave' + (s.voice_state === 'recording' ? ' active' : '');
  $('session').textContent = 'session ' + (s.session_id || '—');
  $('hermesChip').textContent = 'hermes ' + (s.hermes_running ? '● running' : '○ idle');
  $('hermesChip').className = 'chip ' + (s.hermes_running ? 'good' : '');
  $('opencodeChip').textContent = 'opencode ' + (s.opencode_running ? '● running' : '○ off');
  $('opencodeChip').className = 'chip ' + (s.opencode_running ? 'good' : '');
  $('cmd').innerHTML = s.last_command ? escapeHtml(s.last_command) : '<span class="empty">waiting for a command…</span>';
  $('reply').innerHTML = s.last_reply ? escapeHtml(s.last_reply) : '<span class="empty">no reply yet</span>';
  $('cmdLive').className = 'live' + (s.last_command ? ' show' : '');
  $('replyLive').className = 'live' + (s.last_reply ? ' show' : '');

  // processes
  const procs = s.process.processes || [];
  const procDiv = $('processes');
  if (procs.length === 0) {
    procDiv.innerHTML = '<span class="empty">no active processes</span>';
  } else {
    procDiv.innerHTML = procs.map(p => `
      <div class="process">
        <span class="pid">pid ${p.pid}</span>
        <span class="elapsed">${p.elapsed}</span>
        <span class="prompt">${escapeHtml(p.prompt || p.cmd)}</span>
        <button class="btn danger stop" onclick="stopProc(${p.pid})">stop</button>
      </div>
    `).join('');
  }
  $('procLive').className = 'live' + (procs.length > 0 ? ' show' : '');

  // conversation
  const conv = s.conversation || [];
  const convDiv = $('conv');
  if (conv.length === 0) {
    convDiv.innerHTML = '<span class="empty">no conversation yet</span>';
  } else {
    convDiv.innerHTML = conv.map(m => `
      <div class="msg ${m.role}">
        <div class="role">${m.role}</div>
        <div class="text">${escapeHtml(m.text)}</div>
      </div>
    `).join('');
    convDiv.scrollTop = convDiv.scrollHeight;
  }

  // log
  const log = $('log');
  log.innerHTML = s.log_tail.map(line => {
    let cls = 'meta';
    if (line.includes(' >>> ')) cls = 'in';
    else if (line.includes(' <<< ')) cls = 'out';
    return '<div class="row"><span class="t">' + fmtClock(s.ts) + '</span>' +
           '<span class="' + cls + '">' + escapeHtml(line) + '</span></div>';
  }).join('');
  log.scrollTop = log.scrollHeight;
  $('clock').textContent = fmtClock(s.ts);
}

async function stopProc(pid) {
  if (!confirm('Stop process ' + pid + '?')) return;
  await fetch('/api/stop/' + pid, {method:'POST'});
}
async function stopAll() {
  if (!confirm('Stop all Hermes processes?')) return;
  await fetch('/api/stop-all', {method:'POST'});
}
async function toggleMute() {
  await fetch('/api/mute', {method:'POST'});
}
async function clearSession() {
  if (!confirm('Clear current session? This starts a fresh conversation.')) return;
  await fetch('/api/clear-session', {method:'POST'});
}

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

    def do_POST(self):
        if self.path.startswith("/api/stop/"):
            try:
                pid = int(self.path.split("/")[-1])
                os.kill(pid, signal.SIGTERM)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"ok":true}')
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
        elif self.path == "/api/stop-all":
            try:
                subprocess.run(["pkill", "-f", "hermes.*-z"], capture_output=True)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"ok":true}')
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
        elif self.path == "/api/mute":
            try:
                mute_flag = os.path.join(KIT, "wake-muted")
                if os.path.exists(mute_flag):
                    os.remove(mute_flag)
                else:
                    open(mute_flag, "w").close()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"ok":true}')
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
        elif self.path == "/api/clear-session":
            try:
                session_file = os.path.join(KIT, "hermes-session")
                if os.path.exists(session_file):
                    os.remove(session_file)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"ok":true}')
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
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