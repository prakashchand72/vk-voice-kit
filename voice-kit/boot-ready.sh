#!/bin/bash
# boot-ready.sh - ensures everything is running and opens the dashboard
export PATH=/opt/homebrew/bin:$PATH

# 1. Ensure Hammerspoon is running (starts wake listener via supervisor)
if ! pgrep -x Hammerspoon > /dev/null 2>&1; then
  open -a Hammerspoon
  sleep 2
fi

# 2. Ensure wake supervisor is running
if ! pgrep -f "wake-supervisor" > /dev/null 2>&1; then
  launchctl load ~/Library/LaunchAgents/com.prakkash.vk-wake-supervisor.plist 2>/dev/null
fi

# 3. Ensure dashboard is running
if ! pgrep -f "vk-dashboard" > /dev/null 2>&1; then
  launchctl load ~/Library/LaunchAgents/com.prakkash.vk-dashboard.plist 2>/dev/null
fi

# 4. Ensure whisper-server is running
if ! pgrep -f "whisper-server" > /dev/null 2>&1; then
  launchctl load ~/Library/LaunchAgents/com.prakkash.vk-whisper-server.plist 2>/dev/null
fi

# 5. Open dashboard in browser
sleep 2
open http://localhost:8787

echo "boot-ready: all services started, dashboard opened"
