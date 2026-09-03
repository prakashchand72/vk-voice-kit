#!/usr/bin/env node
// vk-tools — local MCP server for the vk voice kit (Jarvis brain tools).
// Gives opencode (or any MCP client) system volume + Chrome/YouTube media
// control + app launching on this Mac. No dependencies — stdio JSON-RPC.
//
// Register in opencode config (~/.config/opencode/opencode.json):
//   "mcp": { "vk-tools": { "type": "local", "command": ["node", "/ABS/PATH/vk-tools.js"], "enabled": true } }
//
// Chrome media tools need (one-time): View > Developer > "Allow JavaScript
// from Apple Events" in Chrome, and grant the host (Terminal/opencode) permission
// to control Chrome when macOS asks.

import { execFile } from "node:child_process"
import { homedir } from "node:os"

const CHROME = process.env.VK_BROWSER || "Google Chrome"
const HOME = homedir()

const run = (cmd, args, env) =>
  new Promise((resolve) => {
    execFile(cmd, args, { env: { ...process.env, ...env } }, (err, stdout, stderr) =>
      resolve({ ok: !err, stdout: (stdout || "").toString().trim(), stderr: (stderr || "").toString().trim() })
    )
  })

const osa = (program) => run("osascript", ["-e", program])
const vol = (program) => osa(program)

const chromeScript = 'tell application "' + CHROME + '"\ntry\n  set r to execute active tab of front window javascript (system attribute "VK_JS")\n  return r\non error errMsg\n  return "ERR: " & errMsg\nend try\nend tell'

const chrome = async (js) => {
  const r = await run("osascript", ["-e", chromeScript], { VK_JS: js })
  if (!r.ok) return { kind: "error", text: r.stderr || "osascript failed" }
  const out = r.stdout.replace(/\s+/g, " ").trim()
  if (out.startsWith("ERR:")) return { kind: "blocked", text: out.slice(4).trim() }
  return { kind: out, text: out }
}

const J = {
  play:   "var v=document.querySelector('video');if(!v){'no-video'}else if(v.paused){v.play();'playing'}else{'already-playing'}",
  pause:  "var v=document.querySelector('video');if(!v){'no-video'}else if(v.paused){'already-paused'}else{v.pause();'paused'}",
  next:   "var b=document.querySelector('.ytp-next-button');if(!b){'no-next'}else{b.click();'next'}",
  prev:   "var b=document.querySelector('.ytp-prev-button');if(!b){'no-prev'}else{b.click();'prev'}",
  skipad: "var b=document.querySelector('.ytp-ad-skip-button,.ytp-skip-ad-button');if(!b){'no-ad'}else{b.click();'skipped-ad'}",
  now:    "var v=document.querySelector('video');if(!v){'no-video'}else{document.title+' | '+Math.round(v.volume*100)+'%'}",
}

const BLOCKED_MSG = `Chrome automation is blocked. Grant ${CHROME} permission (allow the macOS prompt) and enable its menu: View > Developer > Allow JavaScript from Apple Events.`

const APPS = {
  chrome: "Google Chrome", browser: "Google Chrome", "google chrome": "Google Chrome",
  safari: "Safari", firefox: "Firefox",
  spotify: "Spotify", music: "Music", "apple music": "Music",
  mail: "Mail", messages: "Messages", notes: "Notes", reminders: "Reminders",
  calendar: "Calendar", photos: "Photos",
  terminal: "Terminal", kitty: "kitty", iterm: "iTerm",
  "vs code": "Visual Studio Code", vscode: "Visual Studio Code", code: "Visual Studio Code",
  finder: "Finder", settings: "System Settings", "system settings": "System Settings",
  calculator: "Calculator", maps: "Maps", podcast: "Podcasts",
}

const mediaOk = {
  playing: "▶ playing", "already-playing": "▶ already playing",
  paused: "⏸ paused", "already-paused": "⏸ already paused",
  next: "⏭ next track", prev: "⏮ previous track",
  "skipped-ad": "⏭ skipped the ad",
  "no-video": "📺 no video found in the active Chrome tab",
  "no-next": "⏭ no next track available",
  "no-prev": "⏮ no previous track available",
  "no-ad": "🎉 no ad to skip",
}

const TOOLS = [
  { name: "media_play", description: "Play/pause-resume the video in the ACTIVE Chrome tab (YouTube etc). Needs Chrome open.", inputSchema: { type: "object", properties: {} } },
  { name: "media_pause", description: "Pause the video in the ACTIVE Chrome tab. Needs Chrome open.", inputSchema: { type: "object", properties: {} } },
  { name: "media_next", description: "Next track/video (clicks YouTube's next button) in the active Chrome tab.", inputSchema: { type: "object", properties: {} } },
  { name: "media_previous", description: "Previous track/video in the active Chrome tab.", inputSchema: { type: "object", properties: {} } },
  { name: "media_skip_ad", description: "Skip the YouTube ad if one is showing.", inputSchema: { type: "object", properties: {} } },
  { name: "now_playing", description: "Return the title and volume of what is playing in the active Chrome tab.", inputSchema: { type: "object", properties: {} } },
  { name: "volume_up", description: "Raise macOS system output volume.", inputSchema: { type: "object", properties: { delta: { type: "number", description: "How many steps (default 10)" } } } },
  { name: "volume_down", description: "Lower macOS system output volume.", inputSchema: { type: "object", properties: { delta: { type: "number", description: "How many steps (default 10)" } } } },
  { name: "set_volume", description: "Set macOS system output volume to an absolute level 0-100.", inputSchema: { type: "object", properties: { level: { type: "number", description: "0 to 100" } }, required: ["level"] } },
  { name: "mute", description: "Mute macOS output.", inputSchema: { type: "object", properties: {} } },
  { name: "unmute", description: "Unmute macOS output.", inputSchema: { type: "object", properties: {} } },
  { name: "open_app", description: `Launch a macOS app. Recognized names: ${Object.keys(APPS).join(", ")}. Unknown names are tried as-is via 'open -a'.`, inputSchema: { type: "object", properties: { app: { type: "string", description: "App name or synonym" } }, required: ["app"] } },
]

const nowVolume = async () => {
  const r = await vol('output volume of (get volume settings)')
  const n = parseInt(r.stdout, 10)
  return Number.isFinite(n) ? n : null
}

async function callTool(name, args = {}) {
  switch (name) {
    case "media_play": {
      const r = await chrome(J.play)
      if (r.kind === "blocked") return err(BLOCKED_MSG)
      return ok(mediaOk[r.kind] || r.text)
    }
    case "media_pause": {
      const r = await chrome(J.pause)
      if (r.kind === "blocked") return err(BLOCKED_MSG)
      return ok(mediaOk[r.kind] || r.text)
    }
    case "media_next": {
      const r = await chrome(J.next)
      if (r.kind === "blocked") return err(BLOCKED_MSG)
      return ok(mediaOk[r.kind] || r.text)
    }
    case "media_previous": {
      const r = await chrome(J.prev)
      if (r.kind === "blocked") return err(BLOCKED_MSG)
      return ok(mediaOk[r.kind] || r.text)
    }
    case "media_skip_ad": {
      const r = await chrome(J.skipad)
      if (r.kind === "blocked") return err(BLOCKED_MSG)
      return ok(mediaOk[r.kind] || r.text)
    }
    case "now_playing": {
      const r = await chrome(J.now)
      if (r.kind === "blocked") return err(BLOCKED_MSG)
      if (r.kind === "no-video") return ok("📺 no video in the active Chrome tab")
      return ok("🎵 " + (r.text || "").slice(0, 120))
    }
    case "mute": await vol("set volume output muted true"); return ok("🔇 muted")
    case "unmute": await vol("set volume output muted false"); return ok("🔊 unmuted")
    case "volume_up": case "volume_down": {
      const cur = (await nowVolume()) ?? 50
      const step = Math.max(1, Math.min(100, Math.abs(parseInt(args.delta, 10) || 10)))
      const next = name === "volume_up" ? Math.min(100, cur + step) : Math.max(0, cur - step)
      await vol(`set volume output volume ${next}`)
      return ok(`🔊 volume ${next}%`)
    }
    case "set_volume": {
      const level = Math.max(0, Math.min(100, parseInt(args.level, 10)))
      if (!Number.isFinite(level)) return err("set_volume needs a numeric level 0-100")
      await vol(`set volume output volume ${level}`)
      return ok(`🔊 volume ${level}%`)
    }
    case "open_app": {
      const want = String(args.app || "").trim().toLowerCase()
      if (!want) return err("open_app needs an app name")
      const real = APPS[want] || (APPS[want.replace(/\s+/g, " ")] || args.app)
      const r = await run("open", ["-a", real])
      return r.ok ? ok(`🚀 opened ${real}`) : err(`could not open "${args.app}" — ${r.stderr}`)
    }
    default:
      return err(`unknown tool: ${name}`)
  }
}

const ok = (text) => ({ isError: false, content: [{ type: "text", text }] })
const err = (text) => ({ isError: true, content: [{ type: "text", text }] })

// ---- minimal MCP stdio transport (newline-delimited JSON-RPC) ----
const send = (obj) => process.stdout.write(JSON.stringify(obj) + "\n")

process.stdin.setEncoding("utf8")
let buf = ""
process.stdin.on("data", (chunk) => {
  buf += chunk
  let nl
  while ((nl = buf.indexOf("\n")) >= 0) {
    const line = buf.slice(0, nl).trim()
    buf = buf.slice(nl + 1)
    if (!line) continue
    let msg
    try { msg = JSON.parse(line) } catch { continue }
    if (!msg || msg.method === undefined || msg.id === undefined) continue
    ;(async () => {
      try {
        if (msg.method === "initialize") {
          send({ jsonrpc: "2.0", id: msg.id, result: { protocolVersion: msg.params?.protocolVersion || "2024-11-05", capabilities: { tools: {} }, serverInfo: { name: "vk-tools", version: "0.1.0" } } })
        } else if (msg.method === "ping") {
          send({ jsonrpc: "2.0", id: msg.id, result: {} })
        } else if (msg.method === "tools/list") {
          send({ jsonrpc: "2.0", id: msg.id, result: { tools: TOOLS } })
        } else if (msg.method === "tools/call") {
          const r = await callTool(msg.params?.name, msg.params?.arguments || {})
          send({ jsonrpc: "2.0", id: msg.id, result: r })
        } else if (msg.method === "resources/list" || msg.method === "prompts/list") {
          send({ jsonrpc: "2.0", id: msg.id, result: { resources: [], prompts: [] } })
        } else {
          send({ jsonrpc: "2.0", id: msg.id, result: {} })
        }
      } catch (e) {
        send({ jsonrpc: "2.0", id: msg.id, result: { isError: true, content: [{ type: "text", text: String(e && e.message || e) }] } })
      }
    })()
  }
})

// allow direct sanity check (read-only): node vk-tools.js self-test
if (process.argv[2] === "self-test") {
  console.log(`vk-tools: ${TOOLS.length} tools registered`)
  const r = await callTool("now_playing", {})
  console.log("now_playing →", JSON.stringify(r))
}
