#!/bin/bash
# hermes-oneshot — run a voice command through Hermes and show the reply in this
# Terminal window. Reads the command from /tmp/vk-voice-text.txt (avoids shell
# quoting issues), runs the same one-shot pipeline the voice kit used before,
# prints the reply, and keeps the window open so you can read it.
printf '\033]0;Hermes Voice\007'
export DEEPSEEK_API_KEY=$(grep -oE 'DEEPSEEK_API_KEY="[^"]*"' ~/.zshrc | head -n1 | cut -d'"' -f2)
cd ~/hermes
H=~/.local/bin/hermes
SID_FILE=~/.voice-kit/hermes-session
LOG=~/.voice-kit/hermes.log
TXT=""
[ -f /tmp/vk-voice-text.txt ] && TXT=$(cat /tmp/vk-voice-text.txt)
[ -z "$TXT" ] && TXT="(empty voice command)"
echo "---- $(date '+%F %T') >>> $TXT" >> "$LOG"
SAVED=""; [ -s "$SID_FILE" ] && SAVED=$(cat "$SID_FILE")
RESUME=""; HAD=0
if [ -n "$SAVED" ] && $H sessions list 2>/dev/null | grep -q "$SAVED"; then RESUME="--resume $SAVED"; HAD=1; fi
OUT=$($H -z "$TXT" $RESUME -m deepseek-v4-flash --provider deepseek 2>>"$LOG")
if [ "$HAD" = 0 ]; then NEWID=$($H sessions list 2>/dev/null | sed -n '3p' | awk '{print $NF}'); [ -n "$NEWID" ] && printf '%s' "$NEWID" > "$SID_FILE"; fi
echo "---- $(date '+%F %T') <<< $OUT" >> "$LOG"
printf '%s' "$OUT" > ~/.voice-kit/last-reply.txt
touch ~/.voice-kit/reply-waiting
echo ""
echo "=============================================="
echo "$OUT"
echo "=============================================="
echo ""
echo "(press any key to close this window)"
read -n 1
