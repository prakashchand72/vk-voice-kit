#!/bin/bash
# wake-supervisor — keeps exactly ONE wake listener alive across Hammerspoon reloads.
# Uses a PID file to avoid the pgrep-matches-bash-wrapper problem.
export PATH=/opt/homebrew/bin:$PATH
PY="$HOME/.voice-kit/venv/bin/python"
SCRIPT="$HOME/.voice-kit/vk-wake.py"
LOG="$HOME/.voice-kit/wake.log"
PIDFILE="$HOME/.voice-kit/wake.pid"

is_running() {
  [ -f "$PIDFILE" ] || return 1
  local pid=$(cat "$PIDFILE" 2>/dev/null)
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

while true; do
  if is_running; then
    sleep 5
    continue
  fi
  # Start via Hammerspoon (inherits mic permission), capture the real python PID
  echo "[$(date '+%H:%M:%S')] supervisor: starting listener" >> "$LOG"
  hs -c "hs.task.new('/bin/bash', nil, {'-c', 'echo \$\$ > $PIDFILE; export PATH=/opt/homebrew/bin:\$PATH; exec $PY $SCRIPT'}):start()" 2>/dev/null
  sleep 8
done
