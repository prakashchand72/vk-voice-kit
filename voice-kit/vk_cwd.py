#!/usr/bin/env python3
"""vk_cwd — best-guess current working directory of the user's active terminal.

Resolution order:
  1. kitty remote-control socket (if enabled): exact focused window -> its cwd.
  2. kitty process scan: windows that are running opencode, else all kitty
     windows -> pick the most recently started one.
Prints one cwd line, or nothing.
"""
import subprocess, re, sys, os

SOCKET = "/tmp/kitty.sock"

def run(cmd):
    try:
        return subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL)
    except Exception:
        return ""

def via_socket():
    out = run(["kitty", "@", "--to", "unix:" + SOCKET, "ls"])
    if not out.strip():
        return None
    try:
        import json
        data = json.loads(out)
        active = str(data.get("active_window_id"))
        for w in data.get("windows", []) or []:
            if str(w.get("id")) == active:
                cwd = w.get("cwd") or ""
                if cwd and os.path.isdir(cwd):
                    return cwd
    except Exception:
        pass
    return None

def proc_rows():
    out = run(["ps", "-axo", "pid=,ppid=,etime=,command="])
    rows = []
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        pid, ppid, etime, cmd = parts[0], parts[1], parts[2], parts[3]
        rows.append({"pid": pid, "ppid": ppid, "etime": etime, "cmd": cmd})
    return rows

def seconds(etime):
    # etime formats: "MM:SS", "HH:MM:SS", "D-HH:MM:SS"
    m = re.match(r"(?:(\d+)-)?(?:(\d+):)?(\d+):(\d+)", etime)
    if not m:
        return 0
    d, h, mm, s = m.groups()
    return int(d or 0) * 86400 + int(h or 0) * 3600 + int(mm or 0) * 60 + int(s or 0)

def parent_map(rows):
    return {r["pid"]: r["ppid"] for r in rows}

def has_descendant(rows, pmap, root_pid, needle):
    seen, stack = set(), [root_pid]
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        for r in rows:
            if r["pid"] == cur and needle in r["cmd"]:
                return True
        stack.extend(r["pid"] for r in rows if r["ppid"] == cur)
    return False

def windows_via_ps():
    rows = proc_rows()
    pmap = parent_map(rows)
    wins = []
    for r in rows:
        if "kitten run-shell" in r["cmd"]:
            m = re.search(r"--cwd=([^ ]+)", r["cmd"])
            cwd = m.group(1) if m else None
            if cwd and os.path.isdir(cwd):
                wins.append({"pid": r["pid"], "cwd": cwd, "etime": r["etime"]})
    if not wins:
        return None
    running_opencode = [w for w in wins if has_descendant(rows, pmap, w["pid"], "opencode")]
    pool = running_opencode or wins
    pool.sort(key=lambda w: seconds(w["etime"]))  # smallest elapsed = newest
    return pool[0]["cwd"]

def main():
    cwd = via_socket()
    if cwd:
        print(cwd)
        return
    cwd = windows_via_ps()
    if cwd:
        print(cwd)

if __name__ == "__main__":
    main()
