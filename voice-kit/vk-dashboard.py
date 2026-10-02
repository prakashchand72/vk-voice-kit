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

# Short cache for the conversation stream: only re-export a Hermes session
# when something actually changed (log grew, or an active -z command is running
# and >3s passed), so an idle dashboard does not fork `hermes` every second.
_CONV_CACHE = {"sid": None, "time": 0.0, "log_size": 0, "data": []}


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


# Cache for the conversation stream: only re-export a Hermes session when
# something actually changed (session id moved, log grew, or simply N seconds
# elapsed), so an idle dashboard does not fork `hermes` every second.
_sid_cache = {"sid": None, "ts": 0.0}


def _most_recent_session_id():
    """Best-effort: pick a live hermes -z / gateway session id to display.

    Reads the session id embedded in any running `hermes ... -z <sid>` or
    `--resume <sid>` command first (that's the conversation Prakash actually
    speaks to), then falls back to `hermes sessions list`. Caches the lookup
    for a few seconds so every snapshot tick doesn't spawn hermes.
    """
    import time as _t
    now = _t.time()
    if _sid_cache["sid"] and (now - _sid_cache["ts"]) < 4.0:
        return _sid_cache["sid"]
    sid = ""
    # 1) from a running command line (looks like `-z <sid>` after -z)
    try:
        out = subprocess.run(["ps", "-eo", "command"],
                             capture_output=True, text=True, timeout=5).stdout
        for line in out.splitlines():
            if "hermes" not in line:
                continue
            m = re.search(r'--resume\s+(\S+)', line)
            if m and re.fullmatch(r"\d{8}_\d{6}_[0-9a-f]{6,}", m.group(1)):
                sid = m.group(1)
                break
            m = re.search(r'-z\s+(\d{8}_\d{6}_[0-9a-f]{6,})', line)
            if m:
                sid = m.group(1)
                break
    except Exception:
        pass
    # 2) fall back to `hermes sessions list` (newest first)
    if not sid:
        try:
            out = subprocess.run([HERMES_BIN, "sessions", "list"],
                                 capture_output=True, text=True, timeout=10).stdout
            for line in out.splitlines()[2:]:
                parts = line.split()
                if parts and re.fullmatch(r"\d{8}_\d{6}_[0-9a-f]{6,}", parts[-1]):
                    sid = parts[-1]
                    break
        except Exception:
            pass
    _sid_cache["sid"], _sid_cache["ts"] = sid, now
    return sid


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
                    # Extract the prompt from -z "..." — ps strips shell quotes,
                    # so match the unquoted value: everything after -z up to the
                    # next flag (-t / -m / --provider / --resume) or end of line.
                    prompt = ""
                    m = re.search(r'-z\s+"([^"]*)"', cmd)
                    if m:
                        prompt = m.group(1)[:80]
                    if not prompt:
                        m = re.search(r'-z\s+(.*?)(?=\s+-{1,2}[a-zA-Z]\s|\s*$)', cmd)
                        if m:
                            prompt = m.group(1).strip()[:80]
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
        sid = _most_recent_session_id()
    if not sid:
        return []
    now = time.time()
    log_size = 0
    try:
        log_size = os.path.getsize(HERMES_LOG)
    except Exception:
        pass
    cache = _CONV_CACHE
    # Re-export only when the session id changed, the log grew (new activity),
    # or more than a short window passed — the SSE loop ticks every second.
    if (cache["sid"] == sid and cache["log_size"] == log_size
            and (now - cache["time"]) < 4.0):
        return cache["data"]
    try:
        # Try to read from hermes session export or state
        result = subprocess.run(
            [HERMES_BIN, "sessions", "export", "--session-id", sid,
             "--format", "jsonl", "--yes", "-"],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode == 0:
            import json as _json
            # Parse the exported conversation (JSONL document with messages)
            doc = _json.loads(result.stdout)
            msgs = doc.get("messages") or []
            conv = []
            for m in msgs:
                role = m.get("role")
                if role not in ("user", "assistant"):
                    continue
                text = (m.get("content") or "").strip()
                if not text:
                    continue
                conv.append({"role": role, "text": text})
            conv = conv[-CONV_TAIL:]
            cache.update(sid=sid, log_size=log_size, time=now, data=conv)
            return conv
    except Exception:
        pass
    return cache["data"]


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
<title>vk · SEBASTIAN</title>
<style>
  :root{
    --bg:#05060a; --bg2:#0a0d16; --panel:#0d1220; --panel2:#0a0e1a;
    --line:#1b2540; --line2:#243259;
    --txt:#d9e2ff; --dim:#5c6a8c; --faint:#3a4563;
    --cy:#00f0ff; --mg:#ff2bd6; --pp:#b388ff; --gr:#00ff9c;
    --amb:#ffd23f; --red:#ff3860; --soft:#7aa2ff;
  }
  *{box-sizing:border-box; scrollbar-width:thin; scrollbar-color:#2a3760 #0a0e1a;}
  ::-webkit-scrollbar{width:8px;height:8px}::-webkit-scrollbar-thumb{background:#2a3760;border-radius:4px}
  body{margin:0;min-height:100vh;color:var(--txt);overflow-x:hidden;background:var(--bg);
    font:14px/1.55 'Inter','SF Pro Text','Segoe UI',system-ui,sans-serif;}
  /* ambient cyberpunk backdrop */
  .cyber-bg{position:fixed;inset:0;z-index:-2;background:
    radial-gradient(900px 500px at 15% 0%, rgba(0,240,255,.10), transparent 55%),
    radial-gradient(700px 500px at 95% 15%, rgba(255,43,214,.10), transparent 60%),
    radial-gradient(800px 600px at 60% 100%, rgba(179,136,255,.08), transparent 60%),
    var(--bg);}
  .grid-overlay{position:fixed;inset:0;z-index:-1;pointer-events:none;opacity:.16;
    background-image:linear-gradient(rgba(0,240,255,.35) 1px,transparent 1px),
      linear-gradient(90deg,rgba(0,240,255,.35) 1px,transparent 1px);
    background-size:42px 42px;
    mask-image:radial-gradient(circle at 50% 30%,#000 20%,transparent 75%);
    -webkit-mask-image:radial-gradient(circle at 50% 30%,#000 20%,transparent 75%);}
  .scanlines{position:fixed;inset:0;z-index:40;pointer-events:none;opacity:.05;
    background:repeating-linear-gradient(0deg,transparent 0 2px,#000 2px 4px);}
  .glow-pulse{position:fixed;width:700px;height:700px;border-radius:50%;z-index:-1;filter:blur(90px);opacity:.14;transition:background 1s;}

  .wrap{max-width:1240px;margin:0 auto;padding:26px;position:relative;}

  /* ── header / AI-core matrix ── */
  .core-row{display:flex;align-items:center;gap:22px;flex-wrap:wrap;margin-bottom:22px;
    position:relative;padding:18px 22px;border:1px solid var(--line);border-radius:16px;
    background:linear-gradient(180deg,rgba(13,18,32,.9),rgba(10,14,26,.9));
    box-shadow:0 0 42px rgba(0,240,255,.07), inset 0 1px 0 rgba(255,255,255,.04);}
  .core-row::before{content:'';position:absolute;left:22px;right:22px;top:-1px;height:1px;
    background:linear-gradient(90deg,transparent,var(--cy),var(--mg),transparent);opacity:.8;}
  .ai-core{width:76px;height:76px;flex:0 0 auto;position:relative;display:grid;place-items:center;}
  .ai-core .ring{position:absolute;inset:0;border-radius:50%;border:1px solid rgba(0,240,255,.5);
    animation:spin 6s linear infinite;}
  .ai-core .ring.r2{inset:6px;border-color:rgba(255,43,214,.45);animation:spin 4s linear reverse infinite;}
  .ai-core .ring.r3{inset:14px;border-color:rgba(179,136,255,.5);animation:spin 3s linear infinite;}
  .ai-core .eye{width:22px;height:22px;border-radius:50%;background:radial-gradient(circle at 35% 30%,#fff,#00f0ff 40%,#0088ff 75%,#001a33);
    box-shadow:0 0 18px var(--cy),0 0 40px rgba(0,240,255,.5);transition:all .4s;}
  .ai-core .scan{position:absolute;left:0;right:0;height:2px;background:linear-gradient(90deg,transparent,var(--cy),transparent);
    top:0;animation:scanY 2.6s ease-in-out infinite;opacity:.7;}
  .ai-core.state-rec .eye{background:radial-gradient(circle at 35% 30%,#fff,#ff3860 40%,#b3002d 75%,#33000d);box-shadow:0 0 22px var(--red),0 0 48px rgba(255,56,96,.6);}
  .ai-core.state-busy .eye{background:radial-gradient(circle at 35% 30%,#fff,#ffd23f 40%,#cc8800 75%,#332200);box-shadow:0 0 22px var(--amb),0 0 48px rgba(255,210,63,.6);}
  .ai-core.state-muted .eye{background:radial-gradient(circle at 35% 30%,#aab,#5c6a8c 40%,#2a3760 75%,#111);box-shadow:none;}
  @keyframes spin{to{transform:rotate(360deg)}}
  @keyframes scanY{0%,100%{top:0;opacity:.3}50%{top:calc(100% - 2px);opacity:.9}}
  .core-id{flex:1;min-width:220px;}
  .core-id .t1{font-size:20px;font-weight:800;letter-spacing:2px;text-transform:uppercase;
    background:linear-gradient(90deg,var(--cy),var(--pp),var(--mg));-webkit-background-clip:text;background-clip:text;color:transparent;}
  .core-id .t1 .blink{width:9px;height:max(1em,16px);display:inline-block;background:var(--cy);
    margin-left:5px;vertical-align:-2px;animation:blink 1.1s steps(2) infinite;box-shadow:0 0 8px var(--cy);}
  @keyframes blink{50%{opacity:0}}
  .core-id .t2{font-size:11px;color:var(--dim);letter-spacing:3px;text-transform:uppercase;margin-top:3px;}

  .stat-pill{display:inline-flex;align-items:center;gap:10px;padding:9px 16px;border-radius:99px;
    border:1px solid var(--line);background:rgba(10,14,26,.8);font-weight:700;letter-spacing:1.2px;
    font-size:12px;text-transform:uppercase;color:var(--dim);position:relative;white-space:nowrap;}
  .stat-pill .c{width:11px;height:11px;border-radius:50%;background:var(--faint);transition:all .4s;}
  .stat-pill.on{color:var(--gr);border-color:rgba(0,255,156,.5);box-shadow:0 0 22px rgba(0,255,156,.18),inset 0 0 18px rgba(0,255,156,.06);}
  .stat-pill.on .c{background:var(--gr);box-shadow:0 0 12px var(--gr);animation:pulse 2s infinite;}
  .stat-pill.rec{color:var(--red);border-color:rgba(255,56,96,.6);box-shadow:0 0 26px rgba(255,56,96,.3),inset 0 0 20px rgba(255,56,96,.08);}
  .stat-pill.rec .c{background:var(--red);box-shadow:0 0 14px var(--red);animation:pulse 1s infinite;}
  .stat-pill.busy{color:var(--amb);border-color:rgba(255,210,63,.55);box-shadow:0 0 24px rgba(255,210,63,.22);}
  .stat-pill.busy .c{background:var(--amb);box-shadow:0 0 12px var(--amb);animation:pulse 1.3s infinite;}
  .stat-pill.muted{color:var(--dim);border-color:var(--line);}
  .stat-pill.off{color:var(--faint);}
  @keyframes pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.35;transform:scale(.82)}}

  .eq{display:inline-flex;align-items:flex-end;gap:3px;height:26px;opacity:0;transition:opacity .3s;}
  .eq.active{opacity:1}
  .eq i{width:4px;background:linear-gradient(180deg,var(--cy),var(--mg));border-radius:2px;animation:eq 0.9s ease-in-out infinite;}
  .eq i:nth-child(1){animation-delay:0s}.eq i:nth-child(2){animation-delay:.1s}
  .eq i:nth-child(3){animation-delay:.2s}.eq i:nth-child(4){animation-delay:.3s}
  .eq i:nth-child(5){animation-delay:.4s}.eq i:nth-child(6){animation-delay:.5s}
  .eq i:nth-child(7){animation-delay:.6s}
  @keyframes eq{0%,100%{height:4px}26%{height:26px}50%{height:12px}74%{height:22px}}

  .chips{display:flex;gap:9px;flex-wrap:wrap;align-items:center;}
  .chip{font-size:10.5px;padding:5px 12px;border-radius:20px;border:1px solid var(--line);
    color:var(--dim);background:rgba(10,14,26,.7);letter-spacing:.6px;text-transform:uppercase;}
  .chip b{color:var(--soft);font-weight:700}
  .chip.good{border-color:rgba(0,255,156,.45);color:var(--gr)}
  .chip.good b{color:var(--gr)}

  /* ── cards ── */
  .grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;}
  @media(max-width:860px){.grid{grid-template-columns:1fr}}
  .card{position:relative;border:1px solid var(--line);border-radius:14px;padding:16px 18px;overflow:hidden;
    background:linear-gradient(180deg,rgba(13,18,32,.88),rgba(10,14,26,.9));
    box-shadow:0 12px 40px rgba(0,0,0,.35), inset 0 1px 0 rgba(255,255,255,.03);}
  .card::before{content:'';position:absolute;left:0;right:0;top:0;height:1px;
    background:linear-gradient(90deg,transparent,rgba(0,240,255,.5),rgba(255,43,214,.5),transparent);opacity:.6}
  .card h2{font-size:10px;color:var(--dim);text-transform:uppercase;letter-spacing:1.6px;margin:0 0 12px;
    display:flex;align-items:center;gap:8px;}
  .card h2 .sq{width:7px;height:7px;background:var(--cy);box-shadow:0 0 8px var(--cy);transform:rotate(45deg);}
  .card h2 .live{color:var(--gr);font-size:9px;letter-spacing:.5px;opacity:0;margin-left:auto}
  .card h2 .live.show{opacity:1;animation:pulse 2s infinite}
  .full{grid-column:1/-1}
  .cmd{font-size:15px;color:var(--cy);word-break:break-word;font-family:'SF Mono','JetBrains Mono',Menlo,monospace;
    background:rgba(0,240,255,.05);border:1px solid rgba(0,240,255,.16);border-radius:10px;padding:12px 14px;
    box-shadow:inset 0 0 22px rgba(0,240,255,.03);}
  .cmd::before{content:'▸ ';color:var(--mg);font-weight:700}
  .reply{white-space:pre-wrap;color:var(--txt);max-height:320px;overflow:auto;font-size:13px;line-height:1.6;}
  .empty{color:var(--faint);font-style:italic;}
  .log{background:#06080f;border:1px solid var(--line);border-radius:10px;padding:12px 14px;height:320px;overflow:auto;font-size:12px;font-family:'SF Mono',Menlo,monospace;}
  .log .row{display:flex;gap:12px;padding:2px 0;border-bottom:1px dashed rgba(27,37,64,.5)}
  .log .t{color:var(--faint);flex:0 0 auto;font-size:11px}
  .log .in{color:var(--cy)}
  .log .out{color:var(--gr)}
  .log .meta{color:var(--dim)}
  .controls{display:flex;gap:9px;flex-wrap:wrap;margin-top:14px}
  .btn{padding:8px 16px;border-radius:10px;border:1px solid var(--line);background:var(--panel);color:var(--txt);
    font-size:12px;font-weight:600;cursor:pointer;font-family:inherit;letter-spacing:.4px;transition:all .18s}
  .btn:hover{border-color:var(--cy);box-shadow:0 0 16px rgba(0,240,255,.18);transform:translateY(-1px)}
  .btn.danger{color:var(--red);border-color:rgba(255,56,96,.45)}
  .btn.danger:hover{background:rgba(255,56,96,.12);box-shadow:0 0 16px rgba(255,56,96,.3)}
  .btn.primary{color:var(--cy);border-color:rgba(0,240,255,.4)}
  .btn.primary:hover{background:rgba(0,240,255,.1)}
  .process{display:flex;align-items:center;gap:12px;padding:10px 12px;border:1px solid var(--line);border-radius:10px;margin-bottom:9px;
    background:var(--panel2);box-shadow:inset 0 0 24px rgba(0,240,255,.02)}
  .process .pid{color:var(--dim);font-size:11px;min-width:64px;font-family:'SF Mono',Monospace}
  .process .elapsed{color:var(--amb);font-size:11px;min-width:86px;font-family:'SF Mono',Monospace}
  .process .prompt{flex:1;color:var(--txt);font-size:12px;word-break:break-word}
  .process .stop{padding:3px 10px;font-size:11px}
  .conv{max-height:420px;overflow:auto}
  .conv .msg{margin-bottom:12px;padding:11px 14px;border-radius:11px;position:relative}
  .conv .msg.user{background:rgba(0,240,255,.06);border:1px solid rgba(0,240,255,.18)}
  .conv .msg.assistant{background:rgba(0,255,156,.06);border:1px solid rgba(0,255,156,.18)}
  .conv .role{font-size:9.5px;text-transform:uppercase;letter-spacing:1px;margin-bottom:5px;font-weight:700}
  .conv .msg.user .role{color:var(--cy)}
  .conv .msg.assistant .role{color:var(--gr)}
  .conv .text{white-space:pre-wrap;font-size:13px;line-height:1.6}
  footer{margin-top:20px;color:var(--faint);font-size:11px;display:flex;justify-content:space-between;flex-wrap:wrap;gap:8px;letter-spacing:.4px}
  footer .clock{color:var(--cy);font-family:'SF Mono',Monospace}
  .tag{position:absolute;top:-1px;right:18px;font-size:8px;letter-spacing:2px;color:var(--faint);text-transform:uppercase}
</style>
</head>
<body>
<div class="cyber-bg"></div>
<div class="grid-overlay"></div>
<div class="scanlines"></div>
<div class="glow-pulse" id="glow"></div>

<div class="wrap">
  <div class="core-row">
    <div class="ai-core off" id="core">
      <div class="ring"></div><div class="ring r2"></div><div class="ring r3"></div>
      <div class="scan"></div><div class="eye"></div>
    </div>
    <div class="core-id">
      <div class="t1">SEBASTIAN<span class="blink"></span></div>
      <div class="t2">VOICE CONTROL MATRIX · VK CORE ONLINE</div>
    </div>
    <div class="eq" id="wave"><i></i><i></i><i></i><i></i><i></i><i></i><i></i></div>
    <span class="stat-pill off" id="pill"><span class="c"></span><span id="state">—</span></span>
    <div class="chips">
      <span class="chip" id="session">SESS <b>—</b></span>
      <span class="chip" id="hermesChip">HERMES <b>—</b></span>
      <span class="chip" id="opencodeChip">OPENCODE <b>—</b></span>
    </div>
  </div>

  <div class="grid">
    <div class="card">
      <span class="tag">IN//</span>
      <h2><span class="sq"></span>Last command <span class="live" id="cmdLive">● LIVE</span></h2>
      <div class="cmd" id="cmd"><span class="empty">waiting for a command…</span></div>
    </div>
    <div class="card">
      <span class="tag">OUT//</span>
      <h2><span class="sq"></span>Last reply <span class="live" id="replyLive">● LIVE</span></h2>
      <div class="reply" id="reply"><span class="empty">no reply yet</span></div>
    </div>

    <div class="card full">
      <span class="tag">PROC//</span>
      <h2><span class="sq"></span>Active processes <span class="live show" id="procLive">● MONITORING</span></h2>
      <div id="processes"><span class="empty">no active processes</span></div>
      <div class="controls">
        <button class="btn danger" onclick="stopAll()">STOP ALL</button>
        <button class="btn" onclick="toggleMute()" id="muteBtn">MUTE</button>
        <button class="btn primary" onclick="clearSession()">CLEAR SESSION</button>
      </div>
    </div>

    <div class="card full">
      <span class="tag">CHAT//</span>
      <h2><span class="sq"></span>Conversation stream <span class="live show">● LIVE</span></h2>
      <div class="conv" id="conv"><span class="empty">no conversation yet</span></div>
    </div>

    <div class="card full">
      <span class="tag">LOG//</span>
      <h2><span class="sq"></span>Live log · hermes.log <span class="live show">● STREAMING</span></h2>
      <div class="log" id="log"></div>
    </div>
  </div>

  <footer>
    <span>DATA: VOICE KIT STATE FILES · UPDATES EVERY 1s · SSE</span>
    <span>LAST SYNC <b class="clock" id="clock">—</b></span>
  </footer>
</div>

<script>
const $=id=>document.getElementById(id);
const stateMap={
  listening:{label:'LISTENING',cls:'on'},
  recording:{label:'REC●RDING',cls:'rec'},
  transcribing:{label:'TRANSCRIBE',cls:'busy'},
  muted:{label:'MUTED',cls:'muted'},
  off:{label:'OFFLINE',cls:'off'}};
function fmtClock(ts){const d=new Date(ts*1000);return d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'})}
function escapeHtml(t){return t.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}
function setGlow(cls){const g=$('glow');const map={on:'background:radial-gradient(circle,#00ff9c,rgba(0,255,156,.0));opacity:.12',rec:'background:radial-gradient(circle,#ff3860,rgba(255,56,96,0));opacity:.18',busy:'background:radial-gradient(circle,#ffd23f,rgba(255,210,63,0));opacity:.15',off:'background:radial-gradient(circle,#5c6a8c,transparent);opacity:.08'};g.style.cssText=map[cls]||map.off}
function render(s){
  const st=stateMap[s.voice_state]||stateMap.off;
  $('pill').className='stat-pill '+st.cls;
  $('state').textContent=st.label;
  $('core').className='ai-core '+st.cls;
  setGlow(st.cls);
  $('wave').className='eq'+(s.voice_state==='recording'?' active':'');
  $('session').innerHTML='SESS <b>'+(s.session_id||'—')+'</b>';
  $('hermesChip').innerHTML='HERMES <b>'+(s.hermes_running?'● ONLINE':'○ IDLE')+'</b>';
  $('hermesChip').className='chip'+(s.hermes_running?' good':'');
  $('opencodeChip').innerHTML='OPENCODE <b>'+(s.opencode_running?'● RUN':'○ OFF')+'</b>';
  $('opencodeChip').className='chip'+(s.opencode_running?' good':'');
  $('cmd').innerHTML=s.last_command?escapeHtml(s.last_command):'<span class="empty">waiting for a command…</span>';
  $('reply').innerHTML=s.last_reply?escapeHtml(s.last_reply):'<span class="empty">no reply yet</span>';
  $('cmdLive').className='live'+(s.last_command?' show':'');
  $('replyLive').className='live'+(s.last_reply?' show':'');

  const procs=s.process.processes||[];
  const pD=$('processes');
  pD.innerHTML=procs.length===0?'<span class="empty">no active processes</span>':procs.map(p=>
    '<div class="process"><span class="pid">PID '+p.pid+'</span><span class="elapsed">'+p.elapsed+'</span>'+
    '<span class="prompt">'+escapeHtml(p.prompt||p.cmd)+'</span>'+
    '<button class="btn danger stop" onclick="stopProc('+p.pid+')">STOP</button></div>').join('');
  $('procLive').className='live'+(procs.length?' show':'');

  const conv=s.conversation||[];
  const cD=$('conv');
  if(conv.length===0)cD.innerHTML='<span class="empty">no conversation yet</span>';
  else{cD.innerHTML=conv.map(m=>
    '<div class="msg '+m.role+'"><div class="role">'+(m.role==='user'?'▼ HUMAN':'▲ HERMES')+'</div>'+
    '<div class="text">'+escapeHtml(m.text)+'</div></div>').join('');cD.scrollTop=cD.scrollHeight}

  const log=$('log');
  log.innerHTML=s.log_tail.map(l=>{let cls='meta';if(l.includes(' >>> '))cls='in';else if(l.includes(' <<< '))cls='out';
    return '<div class="row"><span class="t">'+fmtClock(s.ts)+'</span><span class="'+cls+'">'+escapeHtml(l)+'</span></div>'}).join('');
  log.scrollTop=log.scrollHeight;
  $('clock').textContent=fmtClock(s.ts);
}
async function stopProc(pid){if(!confirm('Stop process '+pid+'?'))return;await fetch('/api/stop/'+pid,{method:'POST'})}
async function stopAll(){if(!confirm('Stop all Hermes processes?'))return;await fetch('/api/stop-all',{method:'POST'})}
async function toggleMute(){await fetch('/api/mute',{method:'POST'})}
async function clearSession(){if(!confirm('Clear current session? This starts a fresh conversation.'))return;await fetch('/api/clear-session',{method:'POST'})}
const es=new EventSource('/api/stream');
es.onmessage=e=>render(JSON.parse(e.data));
es.onerror=()=>{};
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