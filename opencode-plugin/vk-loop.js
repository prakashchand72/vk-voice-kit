// vk-loop — voice reply loop for opencode (part of ~/.voice-kit)
// On session.idle: saves opencode's reply + pops a macOS notification.

import { writeFileSync, appendFileSync } from "node:fs"
import { exec } from "node:child_process"
import { homedir } from "node:os"

const KIT = `${homedir()}/.voice-kit`

function log(level, msg) {
  try {
    appendFileSync(`${KIT}/vk-loop.log`, `${new Date().toISOString()} [${level}] ${msg}\n`)
  } catch {}
}

function notify(title, subtitle) {
  const esc = (s) => String(s).replace(/\\/g, "\\\\").replace(/"/g, '\\"').replace(/'/g, "")
  exec(
    `osascript -e 'display notification "${esc(subtitle)}" with title "${esc(title)}" sound name "Glass"'`,
    (err) => { if (err) log("error", `notify failed: ${err.message}`) }
  )
}

function truncate(s, n) {
  s = String(s).replace(/\s+/g, " ").trim()
  return s.length > n ? s.slice(0, n) + "…" : s
}

export const VKLoop = async ({ client }) => {
  log("info", "vk-loop plugin loaded")

  return {
    event: async ({ event }) => {
      try {
        log("debug", `event: ${event.type}`)
        if (event.type !== "session.idle") return
        const sessionId = event.properties?.sessionID
        log("info", `idle fired, sessionID=${sessionId}`)
        if (!sessionId) return

        const res = await client.session.messages({ path: { id: sessionId } })
        const messages = res?.data || []
        log("info", `fetched ${messages.length} messages`)

        const last = messages.filter((m) => m.info?.role === "assistant").pop()
        if (!last) {
          log("info", "no assistant message found")
          return
        }
        const parts = last.parts || []
        const texts = parts.filter((p) => p.type === "text").map((p) => p.text || "")
        const text = texts.join("\n").trim()
        log("info", `last assistant: parts=${parts.length} types=${parts.map((p) => p.type).join(",")} textLen=${text.length}`)

        if (!text) {
          log("info", "assistant message had no text parts")
          return
        }

        writeFileSync(`${KIT}/last-reply.txt`, text)
        writeFileSync(`${KIT}/reply-waiting`, new Date().toISOString())
        log("info", `reply saved (${text.length} chars) — floating window will show it`)
      } catch (err) {
        log("error", `event handler failed: ${err.message}`)
      }
    },
  }
}
