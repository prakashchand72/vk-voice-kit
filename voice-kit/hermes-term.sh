#!/bin/bash
# hermes-term — open a visible Hermes terminal that follows the voice-kit session.
# Used by the voice kit so "Sebastian" replies appear in a real terminal you can watch.
export DEEPSEEK_API_KEY=$(grep -oE 'DEEPSEEK_API_KEY="[^"]*"' ~/.zshrc | head -n1 | cut -d'"' -f2)
cd ~/hermes
SID=""
[ -s ~/.voice-kit/hermes-session ] && SID=$(cat ~/.voice-kit/hermes-session)
if [ -n "$SID" ]; then
  exec ~/.local/bin/hermes --resume "$SID"
else
  exec ~/.local/bin/hermes
fi
