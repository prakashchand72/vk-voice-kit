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
_CONV_SUPPRESSED = False


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
    global _CONV_SUPPRESSED
    if _CONV_SUPPRESSED:
        return []
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
    tts_muted = os.path.exists(os.path.join(KIT, "tts-muted"))
    wake_state = voice_state()
    processing = wake_state == "transcribing"
    return {
        "voice_state": wake_state,
        "last_command": last_command(),
        "last_reply": last_reply(),
        "log_tail": log_tail(),
        "hermes_running": hermes_running(),
        "opencode_running": opencode_running(),
        "session_id": session_id(),
        "process": process_status(),
        "conversation": conversation_history(),
        "tts_muted": tts_muted,
        "processing": processing,
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
    --bg:#04050a; --panel:rgba(13,18,32,.72); --panel2:rgba(10,14,26,.85);
    --line:rgba(27,37,64,.6); --line2:rgba(36,50,89,.8);
    --txt:#e2e8ff; --dim:#6b7a9e; --faint:#3d4a6b;
    --cy:#00f0ff; --mg:#ff2bd6; --pp:#b388ff; --gr:#00ff9c;
    --amb:#ffd23f; --red:#ff3860; --soft:#7aa2ff;
    --glow-cy:0 0 20px rgba(0,240,255,.15),0 0 60px rgba(0,240,255,.05);
    --glow-gr:0 0 20px rgba(0,255,156,.15),0 0 60px rgba(0,255,156,.05);
    --glow-red:0 0 20px rgba(255,56,96,.15),0 0 60px rgba(255,56,96,.05);
    --glow-amb:0 0 20px rgba(255,210,63,.15),0 0 60px rgba(255,210,63,.05);
    --radius:14px;
  }
  *{box-sizing:border-box;scrollbar-width:thin;scrollbar-color:#2a3760 transparent}
  ::-webkit-scrollbar{width:5px;height:5px}::-webkit-scrollbar-thumb{background:#2a3760;border-radius:3px}
  ::-webkit-scrollbar-track{background:transparent}
  body{margin:0;height:100vh;overflow:hidden;color:var(--txt);background:var(--bg);
    font:12px/1.45 'Inter','SF Pro Text','Segoe UI',system-ui,sans-serif;
    -webkit-font-smoothing:antialiased}

  /* ambient background */
  .cyber-bg{position:fixed;inset:0;z-index:-3;background:
    radial-gradient(1000px 600px at 10% -5%,rgba(0,240,255,.08),transparent 55%),
    radial-gradient(800px 500px at 95% 10%,rgba(255,43,214,.08),transparent 55%),
    radial-gradient(900px 700px at 50% 105%,rgba(179,136,255,.06),transparent 55%),
    var(--bg)}
  .grid-overlay{position:fixed;inset:0;z-index:-2;pointer-events:none;opacity:.1;
    background-image:linear-gradient(rgba(0,240,255,.25) 1px,transparent 1px),
    linear-gradient(90deg,rgba(0,240,255,.25) 1px,transparent 1px);
    background-size:48px 48px;
    mask-image:radial-gradient(ellipse at 50% 40%,#000 15%,transparent 70%);
    -webkit-mask-image:radial-gradient(ellipse at 50% 40%,#000 15%,transparent 70%)}
  .scanlines{position:fixed;inset:0;z-index:50;pointer-events:none;opacity:.035;
    background:repeating-linear-gradient(0deg,transparent 0 3px,rgba(0,0,0,.6) 3px 4px)}
  .glow-pulse{position:fixed;width:500px;height:500px;border-radius:50%;z-index:-1;filter:blur(100px);opacity:.1;transition:background 1.2s;pointer-events:none}

  .wrap{max-width:1440px;margin:0 auto;padding:12px 16px;height:100vh;display:flex;flex-direction:column;gap:10px}

  /* ── header ── */
  .core-row{display:flex;align-items:center;gap:18px;flex-shrink:0;
    padding:12px 18px;border:1px solid var(--line);border-radius:var(--radius);
    background:linear-gradient(135deg,rgba(13,18,32,.85),rgba(10,14,26,.9));
    backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);
    box-shadow:0 4px 30px rgba(0,0,0,.4),inset 0 1px 0 rgba(255,255,255,.04);
    position:relative;overflow:hidden}
  .core-row::before{content:'';position:absolute;top:0;left:0;right:0;height:1px;
    background:linear-gradient(90deg,transparent,var(--cy),var(--mg),var(--pp),transparent);opacity:.7}
  .core-row::after{content:'';position:absolute;bottom:0;left:0;right:0;height:1px;
    background:linear-gradient(90deg,transparent,rgba(0,240,255,.2),transparent);opacity:.3}

  .ai-core{width:52px;height:52px;flex:0 0 auto;position:relative;display:grid;place-items:center}
  .ai-core .ring{position:absolute;inset:0;border-radius:50%;border:1.5px solid rgba(0,240,255,.4);
    animation:spin 8s linear infinite}
  .ai-core .ring.r2{inset:5px;border-color:rgba(255,43,214,.35);animation:spin 5s linear reverse infinite}
  .ai-core .ring.r3{inset:11px;border-color:rgba(179,136,255,.4);animation:spin 3.5s linear infinite}
  .ai-core .eye{width:16px;height:16px;border-radius:50%;
    background:radial-gradient(circle at 35% 30%,#fff,#00f0ff 40%,#0088ff 75%,#001a33);
    box-shadow:0 0 14px var(--cy),0 0 32px rgba(0,240,255,.4);transition:all .4s}
  .ai-core .scan{position:absolute;left:0;right:0;height:2px;
    background:linear-gradient(90deg,transparent,var(--cy),transparent);
    top:0;animation:scanY 3s ease-in-out infinite;opacity:.6}
  .ai-core.state-rec .eye{background:radial-gradient(circle at 35% 30%,#fff,#ff3860 40%,#b3002d 75%,#33000d);
    box-shadow:0 0 18px var(--red),0 0 40px rgba(255,56,96,.5)}
  .ai-core.state-busy .eye{background:radial-gradient(circle at 35% 30%,#fff,#ffd23f 40%,#cc8800 75%,#332200);
    box-shadow:0 0 18px var(--amb),0 0 40px rgba(255,210,63,.5)}
  .ai-core.state-muted .eye{background:radial-gradient(circle at 35% 30%,#aab,#5c6a8c 40%,#2a3760 75%,#111);box-shadow:none}
  @keyframes spin{to{transform:rotate(360deg)}}
  @keyframes scanY{0%,100%{top:0;opacity:.2}50%{top:calc(100% - 2px);opacity:.8}}

  .core-id{flex:1;min-width:140px}
  .core-id .t1{font-size:18px;font-weight:900;letter-spacing:3px;text-transform:uppercase;
    background:linear-gradient(90deg,var(--cy) 0%,var(--pp) 50%,var(--mg) 100%);
    -webkit-background-clip:text;background-clip:text;color:transparent;
    filter:drop-shadow(0 0 8px rgba(0,240,255,.3))}
  .core-id .t1 .blink{width:8px;height:15px;display:inline-block;background:var(--cy);
    margin-left:5px;vertical-align:-2px;animation:blink 1.1s steps(2) infinite;
    box-shadow:0 0 8px var(--cy);border-radius:1px}
  @keyframes blink{50%{opacity:0}}
  .core-id .t2{font-size:9px;color:var(--dim);letter-spacing:2.5px;text-transform:uppercase;margin-top:3px;opacity:.8}

  .stat-pill{display:inline-flex;align-items:center;gap:8px;padding:7px 14px;border-radius:99px;
    border:1px solid var(--line);background:rgba(10,14,26,.6);font-weight:700;letter-spacing:1.2px;
    font-size:10px;text-transform:uppercase;color:var(--dim);white-space:nowrap;
    backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);transition:all .3s}
  .stat-pill .c{width:10px;height:10px;border-radius:50%;background:var(--faint);transition:all .4s}
  .stat-pill.on{color:var(--gr);border-color:rgba(0,255,156,.4);box-shadow:var(--glow-gr)}
  .stat-pill.on .c{background:var(--gr);box-shadow:0 0 10px var(--gr);animation:pulse 2s infinite}
  .stat-pill.rec{color:var(--red);border-color:rgba(255,56,96,.5);box-shadow:var(--glow-red)}
  .stat-pill.rec .c{background:var(--red);box-shadow:0 0 12px var(--red);animation:pulse 1s infinite}
  .stat-pill.busy{color:var(--amb);border-color:rgba(255,210,63,.45);box-shadow:var(--glow-amb)}
  .stat-pill.busy .c{background:var(--amb);box-shadow:0 0 10px var(--amb);animation:pulse 1.3s infinite}
  .stat-pill.muted{color:var(--dim);border-color:var(--line)}
  .stat-pill.off{color:var(--faint)}
  @keyframes pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.3;transform:scale(.8)}}

  .eq{display:inline-flex;align-items:flex-end;gap:2px;height:22px;opacity:0;transition:opacity .3s}
  .eq.active{opacity:1}
  .eq i{width:3px;background:linear-gradient(180deg,var(--cy),var(--mg));border-radius:2px;
    animation:eq .8s ease-in-out infinite;box-shadow:0 0 4px rgba(0,240,255,.4)}
  .eq i:nth-child(1){animation-delay:0s}.eq i:nth-child(2){animation-delay:.08s}
  .eq i:nth-child(3){animation-delay:.16s}.eq i:nth-child(4){animation-delay:.24s}
  .eq i:nth-child(5){animation-delay:.32s}.eq i:nth-child(6){animation-delay:.4s}
  .eq i:nth-child(7){animation-delay:.48s}.eq i:nth-child(8){animation-delay:.56s}
  @keyframes eq{0%,100%{height:3px}26%{height:22px}50%{height:8px}74%{height:16px}}

  .chips{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
  .chip{font-size:9px;padding:4px 10px;border-radius:12px;border:1px solid var(--line);
    color:var(--dim);background:rgba(10,14,26,.5);letter-spacing:.5px;text-transform:uppercase;
    backdrop-filter:blur(6px);-webkit-backdrop-filter:blur(6px);transition:all .2s}
  .chip b{color:var(--soft);font-weight:700}
  .chip.good{border-color:rgba(0,255,156,.35);color:var(--gr)}
  .chip.good b{color:var(--gr)}

  /* ── grid ── */
  .grid{flex:1;display:grid;grid-template-columns:repeat(12,1fr);grid-template-rows:auto auto 1fr;gap:10px;min-height:0}

  .card{position:relative;border:1px solid var(--line);border-radius:var(--radius);padding:12px 14px;overflow:hidden;
    background:linear-gradient(160deg,rgba(13,18,32,.78),rgba(10,14,26,.88));
    backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);
    box-shadow:0 4px 24px rgba(0,0,0,.3),inset 0 1px 0 rgba(255,255,255,.03);
    display:flex;flex-direction:column;min-height:0;transition:border-color .3s,box-shadow .3s}
  .card:hover{border-color:var(--line2);box-shadow:0 4px 30px rgba(0,0,0,.35),0 0 20px rgba(0,240,255,.04),inset 0 1px 0 rgba(255,255,255,.04)}
  .card::before{content:'';position:absolute;top:0;left:0;right:0;height:1px;
    background:linear-gradient(90deg,transparent,rgba(0,240,255,.4),rgba(255,43,214,.4),transparent);opacity:.5}

  .card h2{font-size:9px;color:var(--dim);text-transform:uppercase;letter-spacing:1.8px;margin:0 0 10px;
    display:flex;align-items:center;gap:7px;flex-shrink:0;font-weight:600}
  .card h2 .sq{width:7px;height:7px;background:var(--cy);box-shadow:0 0 8px var(--cy);transform:rotate(45deg);border-radius:1px}
  .card h2 .live{color:var(--gr);font-size:8px;letter-spacing:.5px;opacity:0;margin-left:auto;font-weight:700}
  .card h2 .live.show{opacity:1;animation:pulse 2s infinite}

  .span3{grid-column:span 3}.span4{grid-column:span 4}.span5{grid-column:span 5}
  .span6{grid-column:span 6}.span7{grid-column:span 7}.span8{grid-column:span 8}
  .span12{grid-column:span 12}

  .cmd{font-size:12px;color:var(--cy);word-break:break-word;font-family:'SF Mono','JetBrains Mono',Menlo,monospace;
    background:rgba(0,240,255,.04);border:1px solid rgba(0,240,255,.12);border-radius:10px;padding:10px 12px;
    box-shadow:inset 0 0 20px rgba(0,240,255,.02);flex:1;overflow:auto;line-height:1.5;min-height:180px}
  .cmd::before{content:'▸ ';color:var(--mg);font-weight:700}
  .reply{white-space:pre-wrap;color:var(--txt);font-size:11px;line-height:1.55;flex:1;overflow:auto;min-height:180px}
  .empty{color:var(--faint);font-style:italic;opacity:.6}
  .log{background:rgba(4,6,12,.6);border:1px solid var(--line);border-radius:10px;padding:10px 12px;flex:1;overflow:auto;
    font-size:10px;font-family:'SF Mono',Menlo,monospace;line-height:1.6}
  .log .row{display:flex;gap:10px;padding:2px 0;border-bottom:1px solid rgba(27,37,64,.3)}
  .log .t{color:var(--faint);flex:0 0 auto;font-size:9px;opacity:.7}
  .log .in{color:var(--cy)}
  .log .out{color:var(--gr)}
  .log .meta{color:var(--dim)}

  .controls{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px;flex-shrink:0}
  .btn{padding:6px 14px;border-radius:9px;border:1px solid var(--line);background:var(--panel);color:var(--txt);
    font-size:10px;font-weight:600;cursor:pointer;font-family:inherit;letter-spacing:.3px;transition:all .2s;
    backdrop-filter:blur(6px);-webkit-backdrop-filter:blur(6px)}
  .btn:hover{border-color:var(--cy);box-shadow:0 0 14px rgba(0,240,255,.12);transform:translateY(-1px)}
  .btn:active{transform:translateY(0)}
  .btn.danger{color:var(--red);border-color:rgba(255,56,96,.35)}
  .btn.danger:hover{background:rgba(255,56,96,.08);box-shadow:0 0 14px rgba(255,56,96,.15)}
  .btn.primary{color:var(--cy);border-color:rgba(0,240,255,.35)}
  .btn.primary:hover{background:rgba(0,240,255,.08);box-shadow:0 0 14px rgba(0,240,255,.15)}

  .input-row{display:flex;gap:6px;margin-top:8px;flex-shrink:0}
  .input-row input{flex:1;padding:7px 14px;border-radius:9px;border:1px solid var(--line);
    background:rgba(10,14,26,.6);color:var(--txt);font-size:11px;font-family:inherit;outline:none;
    transition:border-color .2s,box-shadow .2s;backdrop-filter:blur(6px);-webkit-backdrop-filter:blur(6px)}
  .input-row input:focus{border-color:var(--cy);box-shadow:0 0 12px rgba(0,240,255,.1)}
  .input-row input::placeholder{color:var(--faint)}

  .process{display:flex;align-items:center;gap:10px;padding:8px 12px;border:1px solid var(--line);border-radius:10px;
    margin-bottom:6px;background:var(--panel2);transition:border-color .2s}
  #processes{min-height:100px}
  .process:hover{border-color:var(--line2)}
  .process .pid{color:var(--dim);font-size:9px;min-width:50px;font-family:'SF Mono',monospace}
  .process .elapsed{color:var(--amb);font-size:9px;min-width:60px;font-family:'SF Mono',monospace}
  .process .prompt{flex:1;color:var(--txt);font-size:10px;word-break:break-word}
  .process .stop{padding:3px 10px;font-size:9px}

  .conv{flex:1;overflow:auto;min-height:0;padding-right:4px}
  .conv .msg{margin-bottom:10px;padding:10px 12px;border-radius:10px;transition:border-color .2s}
  .conv .msg.user{background:rgba(0,240,255,.04);border:1px solid rgba(0,240,255,.14)}
  .conv .msg.user:hover{border-color:rgba(0,240,255,.25)}
  .conv .msg.assistant{background:rgba(0,255,156,.04);border:1px solid rgba(0,255,156,.14)}
  .conv .msg.assistant:hover{border-color:rgba(0,255,156,.25)}
  .conv .role{font-size:8px;text-transform:uppercase;letter-spacing:1.2px;margin-bottom:5px;font-weight:700}
  .conv .msg.user .role{color:var(--cy)}
  .conv .msg.assistant .role{color:var(--gr)}
  .conv .text{white-space:pre-wrap;font-size:11px;line-height:1.55}

  footer{flex-shrink:0;display:flex;justify-content:space-between;align-items:center;
    padding:0 4px;color:var(--faint);font-size:9px;letter-spacing:.4px}
  footer .clock{color:var(--cy);font-family:'SF Mono',monospace}
  .tag{position:absolute;top:0;right:14px;font-size:7px;letter-spacing:2px;color:var(--faint);
    text-transform:uppercase;opacity:.5;font-weight:600}
</style>
</head>
<body>
<div class="cyber-bg"></div>
<div class="grid-overlay"></div>
<div class="scanlines"></div>
<div class="glow-pulse" id="glow"></div>
<div class="wrap">
  <div class="core-row">
    <div class="ai-core off" id="core"><div class="ring"></div><div class="ring r2"></div><div class="ring r3"></div><div class="scan"></div><div class="eye"></div></div>
    <div class="core-id"><div class="t1">SEBASTIAN<span class="blink"></span></div><div class="t2">VOICE CONTROL MATRIX</div></div>
    <div class="eq" id="wave"><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i></div>
    <span class="stat-pill off" id="pill"><span class="c"></span><span id="state">—</span></span>
    <div class="chips"><span class="chip" id="session">SESS <b>—</b></span><span class="chip" id="hermesChip">HERMES <b>—</b></span><span class="chip" id="opencodeChip">OPENCODE <b>—</b></span></div>
  </div>
  <div class="grid">
    <div class="card span3"><span class="tag">IN//</span><h2><span class="sq"></span>Command <span class="live show" id="cmdLive">● LIVE</span></h2><div class="cmd" id="cmd"><span class="empty">waiting…</span></div></div>
    <div class="card span5"><span class="tag">OUT//</span><h2><span class="sq"></span>Reply <span class="live show" id="replyLive">● LIVE</span></h2><div class="reply" id="reply"><span class="empty">no reply</span></div></div>
    <div class="card span4"><span class="tag">PROC//</span><h2><span class="sq"></span>Processes <span class="live show" id="procLive">● MON</span></h2>
      <div id="processes"><span class="empty">none</span></div>
      <div class="controls"><button class="btn danger" onclick="stopAll()">STOP ALL</button><button class="btn" onclick="toggleMute()" id="muteBtn">MUTE</button><button class="btn" onclick="toggleTTS()" id="ttsBtn">TTS ON</button><button class="btn danger" onclick="stopTTS()">STOP TTS</button><button class="btn primary" onclick="clearSession()">CLEAR SESS</button><button class="btn danger" onclick="clearLog()">CLEAR LOG</button></div>
      <div class="input-row"><input type="text" id="cmdInput" placeholder="Type command..." onkeydown="if(event.key==='Enter')sendCommand()"><button class="btn primary" onclick="sendCommand()">SEND</button></div>
    </div>
    <div class="card span8"><span class="tag">CHAT//</span><h2><span class="sq"></span>Conversation <span class="live show">● LIVE</span></h2><div class="conv" id="conv"><span class="empty">no conversation</span></div></div>
    <div class="card span4"><span class="tag">LOG//</span><h2><span class="sq"></span>hermes.log <span class="live show">● LIVE</span></h2><div class="log" id="log"></div></div>
  </div>
  <footer><span>VK DASHBOARD · SSE · 1s</span><span>SYNC <b class="clock" id="clock">—</b></span></footer>
</div>
<script>
const $=id=>document.getElementById(id);
const stateMap={listening:{label:'LISTENING',cls:'on'},recording:{label:'REC',cls:'rec'},transcribing:{label:'TRANSCRIBE',cls:'busy'},muted:{label:'MUTED',cls:'muted'},off:{label:'OFF',cls:'off'}};
function fmtClock(ts){const d=new Date(ts*1000);return d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'})}
function escapeHtml(t){return t.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}
function setGlow(cls){const g=$('glow');const map={on:'background:radial-gradient(circle,#00ff9c,rgba(0,255,156,0));opacity:.12',rec:'background:radial-gradient(circle,#ff3860,rgba(255,56,96,0));opacity:.18',busy:'background:radial-gradient(circle,#ffd23f,rgba(255,210,63,0));opacity:.15',off:'background:radial-gradient(circle,#5c6a8c,transparent);opacity:.08'};g.style.cssText=map[cls]||map.off}
function render(s){
  const st=stateMap[s.voice_state]||stateMap.off;
  $('pill').className='stat-pill '+st.cls;$('state').textContent=st.label;
  $('core').className='ai-core '+st.cls;setGlow(st.cls);
  $('wave').className='eq'+(s.voice_state==='recording'?' active':'');
  $('session').innerHTML='SESS <b>'+(s.session_id||'—')+'</b>';
  $('hermesChip').innerHTML='HERMES <b>'+(s.hermes_running?'● ON':'○ OFF')+'</b>';
  $('hermesChip').className='chip'+(s.hermes_running?' good':'');
  $('opencodeChip').innerHTML='OPENCODE <b>'+(s.opencode_running?'● RUN':'○ OFF')+'</b>';
  $('opencodeChip').className='chip'+(s.opencode_running?' good':'');
  $('cmd').innerHTML=s.last_command?escapeHtml(s.last_command):'<span class="empty">waiting…</span>';
  $('reply').innerHTML=s.last_reply?escapeHtml(s.last_reply):'<span class="empty">no reply</span>';
  $('cmdLive').className='live'+(s.last_command?' show':'');
  $('replyLive').className='live'+(s.last_reply?' show':'');
  const procs=s.process.processes||[];const pD=$('processes');
  pD.innerHTML=procs.length===0?'<span class="empty">none</span>':procs.map(p=>'<div class="process"><span class="pid">'+p.pid+'</span><span class="elapsed">'+p.elapsed+'</span><span class="prompt">'+escapeHtml(p.prompt||p.cmd)+'</span><button class="btn danger stop" onclick="stopProc('+p.pid+')">STOP</button></div>').join('');
  $('procLive').className='live'+(procs.length?' show':'');
  $('ttsBtn').textContent=s.tts_muted?'TTS OFF':'TTS ON';
  $('ttsBtn').className='btn'+(s.tts_muted?' danger':'');
  $('state').textContent=s.processing?'TRANSCRIBING':$('state').textContent;
  const conv=s.conversation||[];const cD=$('conv');
  if(conv.length===0)cD.innerHTML='<span class="empty">no conversation</span>';
  else{cD.innerHTML=conv.map(m=>{let t=m.text.replace(/^↪ restored workspace dir:.*\n?/gm,'').replace(/^↪ .*\n?/gm,'');return '<div class="msg '+m.role+'"><div class="role">'+(m.role==='user'?'▼ HUMAN':'▲ HERMES')+'</div><div class="text">'+escapeHtml(t)+'</div></div>'}).join('');cD.scrollTop=cD.scrollHeight}
  const log=$('log');
  log.innerHTML=s.log_tail.map(l=>{let cls='meta';if(l.includes(' >>> '))cls='in';else if(l.includes(' <<< '))cls='out';return '<div class="row"><span class="t">'+fmtClock(s.ts)+'</span><span class="'+cls+'">'+escapeHtml(l)+'</span></div>'}).join('');
  log.scrollTop=log.scrollHeight;$('clock').textContent=fmtClock(s.ts);
}
async function stopProc(pid){if(!confirm('Stop '+pid+'?'))return;await fetch('/api/stop/'+pid,{method:'POST'})}
async function stopAll(){if(!confirm('Stop all?'))return;await fetch('/api/stop-all',{method:'POST'})}
async function toggleMute(){await fetch('/api/mute',{method:'POST'})}
async function toggleTTS(){await fetch('/api/tts-mute',{method:'POST'})}
async function stopTTS(){await fetch('/api/tts-stop',{method:'POST'})}
async function clearSession(){if(!confirm('Clear session?'))return;await fetch('/api/clear-session',{method:'POST'})}
async function clearLog(){if(!confirm('Clear log?'))return;await fetch('/api/clear-log',{method:'POST'})}
async function sendCommand(){const inp=$('cmdInput');const txt=inp.value.trim();if(!txt)return;inp.value='';await fetch('/api/send',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:txt})})}
const es=new EventSource('/api/stream');es.onmessage=e=>render(JSON.parse(e.data));es.onerror=()=>{};
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
        global _CONV_SUPPRESSED
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
        elif self.path == "/api/tts-mute":
            try:
                tts_mute_flag = os.path.join(KIT, "tts-muted")
                if os.path.exists(tts_mute_flag):
                    os.remove(tts_mute_flag)
                else:
                    open(tts_mute_flag, "w").close()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"ok":true}')
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
        elif self.path == "/api/tts-stop":
            try:
                tts_stop_flag = os.path.join(KIT, "tts-stop")
                open(tts_stop_flag, "w").close()
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
                # Reset in-memory caches so the stream empties immediately.
                _CONV_CACHE.update(sid=None, time=0.0, log_size=0, data=[])
                _sid_cache.update(sid=None, ts=0.0)
                # Re-enable the stream for the new session
                _CONV_SUPPRESSED = False
                # Delete the current session from the store so the `sessions
                # list` fallback does not immediately repopulate the stream.
                sid = _most_recent_session_id()
                if sid:
                    subprocess.run(
                        [HERMES_BIN, "sessions", "delete", sid, "--yes"],
                        capture_output=True, text=True, timeout=15
                    )
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"ok":true}')
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
        elif self.path == "/api/clear-log":
            try:
                # Reset in-memory conversation cache
                _CONV_CACHE.update(sid=None, time=0.0, log_size=0, data=[])
                # Suppress the session-export fallback so the stream stays empty
                _CONV_SUPPRESSED = True
                # Truncate the hermes.log file
                with open(HERMES_LOG, "w") as f:
                    f.write("")
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"ok":true}')
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
        elif self.path == "/api/send":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length)) if length else {}
                text = (body.get("text") or "").strip()
                if not text:
                    self.send_response(400)
                    self.end_headers()
                    self.wfile.write(b'{"error":"empty text"}')
                    return
                # Build the hermes command, same as sendToHermes in init.lua
                sid_file = os.path.join(KIT, "hermes-session")
                saved = ""
                if os.path.exists(sid_file):
                    with open(sid_file) as f:
                        saved = f.read().strip()
                resume = ""
                if saved:
                    # Check session still exists
                    r = subprocess.run(
                        [HERMES_BIN, "sessions", "list"],
                        capture_output=True, text=True, timeout=10
                    )
                    if saved in r.stdout:
                        resume = "--resume " + saved
                # Log the command
                import datetime
                ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                with open(HERMES_LOG, "a") as f:
                    f.write("---- {} >>> {}\n".format(ts, text))
                # Pull DEEPSEEK_API_KEY from ~/.zshrc (same as sendToHermes in init.lua)
                import subprocess as _sp
                key = ""
                try:
                    r = _sp.run(
                        ["grep", "-oE", "DEEPSEEK_API_KEY=\"[^\"]*\"", os.path.expanduser("~/.zshrc")],
                        capture_output=True, text=True, timeout=5
                    )
                    if r.stdout:
                        key = r.stdout.split('"')[1] if '"' in r.stdout else ""
                except Exception:
                    pass
                env = os.environ.copy()
                if key:
                    env["DEEPSEEK_API_KEY"] = key
                # Run hermes in background
                cmd = [HERMES_BIN, "-z", text, "-t", "all",
                       "-m", "meituan/longcat-2.5-preview:free", "--provider", "nous"]
                if resume:
                    cmd.insert(3, "--resume=" + saved)
                subprocess.Popen(
                    cmd,
                    stdout=open(HERMES_LOG, "a"),
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    env=env
                )
                # Save new session id if we didn't have one
                if not saved:
                    r = subprocess.run(
                        [HERMES_BIN, "sessions", "list"],
                        capture_output=True, text=True, timeout=10
                    )
                    lines = r.stdout.strip().splitlines()
                    if len(lines) >= 3:
                        new_id = lines[2].split()[-1]
                        with open(sid_file, "w") as f:
                            f.write(new_id)
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