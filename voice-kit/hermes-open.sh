#!/bin/bash
# hermes-open — open (or bring to front) the visible "Hermes Voice" terminal.
# Used by the voice kit so voice replies appear in a real terminal.
SCRIPT="$HOME/.voice-kit/hermes-term.sh"
N=$(osascript -e 'tell application "Terminal" to count windows whose name contains "Hermes Voice"' 2>/dev/null)
if [ -n "$N" ] && [ "$N" -gt 0 ] 2>/dev/null; then
  osascript -e 'tell application "Terminal" to activate' \
    -e 'tell application "Terminal" to set index of (first window whose name contains "Hermes Voice") to 1' 2>/dev/null
else
  osascript -e "tell application \"Terminal\" to do script \"$SCRIPT\"" 2>/dev/null
fi
