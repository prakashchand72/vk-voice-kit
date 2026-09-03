// vk-loop — voice loop for opencode (part of ~/.voice-kit)
// On session.idle: saves opencode's reply + pops a macOS notification.
// Watches inbox.txt: injects voice-dictated prompts straight into the session.

import { writeFileSync, appendFileSync, readFileSync, statSync, watchFile } from "node:fs"
import { createHash } from "node:crypto"
import { exec } from "node:child_process"
import { homedir } from "node:os"

const KIT = `${homedir()}/.voice-kit`
// legacy global inbox (Hammerspoon fallback when no project dir is detected)
const INBOX = `${KIT}/inbox.txt`
// per-project inbox: voice is routed to the directory you're working in, so each
// opencode instance only ever reads prompts meant for ITS own project directory.
const OWN_DIR = process.cwd().replace(/\/+$/, "")
const OWN_HASH = createHash("sha1").update(OWN_DIR).digest("hex").slice(0, 12)
const OWN_INBOX = `${KIT}/inboxes/${OWN_HASH}.txt`
log("info", `project inbox: ${OWN_INBOX} (cwd=${OWN_DIR})`)

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
  // session the CURRENT opencode process actually has active (seen via events).
  // This is what voice must target — NOT a stale session another instance left on disk.
  let activeSessionId = null

  async function resolveSessionId() {
    if (activeSessionId) return activeSessionId
    if (currentSessionId) return currentSessionId
    // Reuse the most recently updated session of THIS project instead of creating
    // a brand-new one (avoids spawning a hidden duplicate session every time voice
    // arrives before the TUI fires a session event).
    try {
      const res = await client.session.list({})
      const sessions = (res?.data || []).filter((s) => s && s.id)
      sessions.sort((a, b) => (b.time?.updated || 0) - (a.time?.updated || 0))
      if (sessions.length) {
        currentSessionId = sessions[0].id
        log("info", `reusing existing session ${currentSessionId}`)
        return currentSessionId
      }
    } catch (err) {
      log("error", `session.list failed: ${err.message}`)
    }
    // No session exists at all yet in this project — only now create one.
    try {
      const created = await client.session.create({})
      currentSessionId = created?.data?.id || null
      if (currentSessionId) log("info", `created new session ${currentSessionId}`)
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

  // --- watch the inbox file(s) for voice commands ---
  // Own project inbox (primary) + legacy global inbox (fallback). Each path has
  // its own mtime so the two never interfere.
  const watchTargets = [OWN_INBOX, INBOX]
  const lastMtime = {}

  function handleInbox(target) {
    try {
      const st = statSync(target)
      const mtime = st.mtimeMs || 0
      if (mtime === lastMtime[target]) return
      lastMtime[target] = mtime
      const text = readFileSync(target, "utf8").trim()
      if (!text) return
      log("info", `inbox received (${text.length} chars) from ${target}`)
      submitPrompt(text)
    } catch {
      // file may not exist yet
    }
  }

  for (const t of watchTargets) {
    try {
      watchFile(t, { interval: 100 }, () => handleInbox(t))
    } catch (err) {
      log("error", `watchFile(${t}) failed: ${err.message}`)
    }
  }

  // process any inbox written before this plugin loaded (e.g. first launch)
  setTimeout(() => { for (const t of watchTargets) handleInbox(t) }, 1000)

  return {
    event: async ({ event }) => {
      try {
        log("debug", `event: ${event.type}`)

        // keep track of the active session id (events are THIS process only)
        if (event.type === "session.idle") {
          const sid = event.properties?.sessionID || activeSessionId
          activeSessionId = sid
          currentSessionId = sid || currentSessionId
        } else if (event.type === "session.created" || event.type === "session.updated") {
          const sid = event.properties?.info?.id || activeSessionId
          activeSessionId = sid
          currentSessionId = sid || currentSessionId
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
