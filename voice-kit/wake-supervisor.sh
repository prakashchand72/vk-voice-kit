#!/bin/bash
# wake-supervisor — keeps exactly ONE wake listener alive.
# Uses heartbeat file (not just PID) to detect dead/stale processes.
export PATH=/opt/homebrew/bin:$PATH
PY="$HOME/.voice-kit/venv/bin/python"
SCRIPT="$HOME/.voice-kit/vk-wake.py"
LOG="$HOME/.voice-kit/wake.log"
PIDFILE="$HOME/.voice-kit/wake.pid"
HEARTBEAT="$HOME/.voice-kit/wake-heartbeat"
STDERR_LOG="$HOME/.voice-kit/wake-stderr.log"

is_alive() {
  [ -f "$HEARTBEAT" ] || return 1
  local last=$(cat "$HEARTBEAT" 2>/dev/null | cut -d. -f1)
  [ -n "$last" ] || return 1
  local now=$(date +%s)
  local age=$((now - last))
  [ "$age" -lt 5 ]
}

cleanup_stale() {
  rm -f "$PIDFILE" "$HEARTBEAT"
  local pids=$(pgrep -f "vk-wake.py" 2>/dev/null)
  if [ -n "$pids" ]; then
    echo "[$(date '+%H:%M:%S')] supervisor: killing stale pids: $pids" >> "$LOG"
    echo "$pids" | xargs kill -9 2>/dev/null
    sleep 1
  fi
}

while true; do
  if is_alive; then
    sleep 3
    continue
  fi

  echo "[$(date '+%H:%M:%S')] supervisor: listener dead (heartbeat stale), restarting" >> "$LOG"
  cleanup_stale

  # start via Hammerspoon (inherits mic permission), capture stderr
  hs -c "hs.task.new('/bin/bash', nil, {'-c', 'echo \$\$ > $PIDFILE; export PATH=/opt/homebrew/bin:\$PATH; exec $PY $SCRIPT 2>>$STDERR_LOG'}):start()" 2>/dev/null
  sleep 8
done
