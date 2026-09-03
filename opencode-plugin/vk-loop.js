// vk-loop — voice loop for opencode (part of ~/.voice-kit)
// On session.idle: saves opencode's reply + pops a macOS notification.
// Watches inbox.txt: injects voice-dictated prompts straight into the session.

import { writeFileSync, appendFileSync, readFileSync, statSync, watchFile } from "node:fs"
import { exec } from "node:child_process"
import { homedir } from "node:os"

const KIT = `${homedir()}/.voice-kit`
const INBOX = `${KIT}/inbox.txt`

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

  let currentSessionId = null

  async function resolveSessionId() {
    if (currentSessionId) return currentSessionId
    try {
      const res = await client.session.list({})
      const sessions = res?.data || []
      if (sessions.length) {
        sessions.sort((a, b) => (b.time?.updated || 0) - (a.time?.updated || 0))
        currentSessionId = sessions[0].id
        return currentSessionId
      }
    } catch (err) {
      log("error", `session.list failed: ${err.message}`)
    }
    try {
      const created = await client.session.create({})
      currentSessionId = created?.data?.id || null
    } catch (err) {
      log("error", `session.create failed: ${err.message}`)
    }
    return currentSessionId
  }

  async function submitPrompt(text) {
    const sessionId = await resolveSessionId()
    if (!sessionId) {
      log("error", "no session id available to send prompt")
      return
    }
    try {
      await client.session.promptAsync({
        path: { id: sessionId },
        body: { parts: [{ type: "text", text }] },
      })
      log("info", `prompt sent to session ${sessionId} (${text.length} chars)`)
    } catch (err) {
      log("error", `promptAsync failed: ${err.message}`)
    }
  }

  // --- watch the inbox file for voice commands ---
  let lastInboxMtime = 0

  function handleInbox() {
    try {
      const st = statSync(INBOX)
      const mtime = st.mtimeMs || 0
      if (mtime === lastInboxMtime) return
      lastInboxMtime = mtime
      const text = readFileSync(INBOX, "utf8").trim()
      if (!text) return
      log("info", `inbox received (${text.length} chars)`)
      submitPrompt(text)
    } catch {
      // file may not exist yet
    }
  }

  try {
    watchFile(INBOX, { interval: 100 }, handleInbox)
  } catch (err) {
    log("error", `watchFile failed: ${err.message}`)
  }

  // process any inbox written before this plugin loaded (e.g. first launch)
  setTimeout(() => handleInbox(), 1000)

  return {
    event: async ({ event }) => {
      try {
        log("debug", `event: ${event.type}`)

        // keep track of the active session id
        if (event.type === "session.idle") {
          currentSessionId = event.properties?.sessionID || currentSessionId
        } else if (event.type === "session.created" || event.type === "session.updated") {
          currentSessionId = event.properties?.info?.id || currentSessionId
        }

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
