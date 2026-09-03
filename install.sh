#!/bin/bash
# vk voice-kit installer — local voice control for opencode (macOS, Apple Silicon)
set -e

KIT="$HOME/.voice-kit"
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "🎙 vk voice-kit installer"
echo "========================="

# 1) dependencies
echo "==> Installing dependencies (whisper-cpp, sox, hammerspoon)…"
brew install whisper-cpp sox
brew install --cask hammerspoon

# 2) whisper model (~1.5GB, one-time)
mkdir -p "$KIT/models"
if [ ! -s "$KIT/models/ggml-large-v3-turbo.bin" ]; then
  echo "==> Downloading Whisper large-v3-turbo (~1.5GB, one-time)…"
  curl -L --progress-bar -o "$KIT/models/ggml-large-v3-turbo.bin" \
    "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin"
else
  echo "==> Whisper model already present, skipping download."
fi

# 3) install files
mkdir -p "$KIT" "$HOME/.hammerspoon" "$HOME/.config/opencode/plugin"
cp "$REPO_DIR/voice-kit/vk" "$KIT/vk"
chmod +x "$KIT/vk"
cp "$REPO_DIR/hammerspoon/init.lua" "$HOME/.hammerspoon/init.lua"
cp "$REPO_DIR/opencode-plugin/vk-loop.js" "$HOME/.config/opencode/plugin/vk-loop.js"

# 4) config (never overwrite an existing key)
if [ ! -f "$KIT/config" ]; then
  cp "$REPO_DIR/voice-kit/config.example" "$KIT/config"
  chmod 600 "$KIT/config"
  echo ""
  echo "⚠️  ACTION NEEDED: edit $KIT/config and paste your Groq API key"
  echo "   (free tier works — https://console.groq.com/keys)"
elif grep -q "gsk_" "$KIT/config" 2>/dev/null; then
  echo "==> Existing config with API key found — left untouched."
fi

# 5) launch Hammerspoon
open -a Hammerspoon 2>/dev/null || true

echo ""
echo "✅ Installed. Final steps (one-time, manual):"
echo "   1. System Settings → Privacy & Security → Accessibility  → enable Hammerspoon"
echo "   2. System Settings → Privacy & Security → Microphone     → enable Hammerspoon"
echo "   3. Click the 🎙 Hammerspoon menu-bar icon → Reload Config (if no alert appeared)"
echo "   4. Restart opencode (loads the vk-loop reply plugin)"
echo ""
echo "Usage:"
echo "   Hold F5 anywhere, speak, release  → verbatim text, auto-submitted to opencode"
echo "   Hold F6 anywhere, speak, release  → LLM-formatted text, auto-submitted to opencode"
echo "   Click 🎙 'hold to speak' on the floating window → same, mouse-driven"
echo ""
echo "CLI:  vk rec [secs] | vk mics | vk mic auto | vk fmt \"text\" | vk test"
