-- vk Hammerspoon config: push-to-talk dictation + dedicated reply window
-- F5 / right-⌘ / middle-mouse (hold) = dictate -> transcribe -> format -> send to opencode (background)
-- F6 / right-⌥ (hold) = same, but also presses Enter after pasting (auto-submit to opencode)
-- F7 = focus opencode console (kitty) when you want to see the full session
-- Reply window: dedicated draggable app-style window with rendered Markdown.
--   Drag via the header bar. ⤢ toggles full-screen. ✕ closes.

-- enable the `hs` CLI so the vk tooling can drive/debug Hammerspoon
pcall(function() require("hs.ipc").cliInstall() end)

local VK = os.getenv("HOME") .. "/.voice-kit/vk"
local VK_DIR_PY = os.getenv("HOME") .. "/.voice-kit/vk_cwd.py"
local HERMES = os.getenv("HOME") .. "/hermes/venv/bin/hermes"
local REPLY_FILE = os.getenv("HOME") .. "/.voice-kit/last-reply.txt"
local recordingTask = nil
local processing = false
local replyWebView = nil
local replyMaximized = false
local lastFrame = nil
dragTimer = nil
dragUpTap = nil
local lastWindowError = nil
local replyCleared = false
local speakHold = false
local startRecording, finishRecording

local function alert(msg)
  hs.alert.show(msg, { textStyle = { color = { white = 1 } }, textSize = 18 }, 1.2)
end

local SOUND_DIR = os.getenv("HOME") .. "/.voice-kit/sounds"
local function playSound(name)
  local path = SOUND_DIR .. "/" .. name .. ".wav"
  if hs.fs.attributes(path) then
    hs.sound.getByFile(path):play()
  end
end

-- ---------- text-to-speech (speaks the reply window aloud) ----------

local speechSynth = hs.speech.new()
speechSynth:rate(180)
speechSynth:volume(0.9)

-- persistent voice preference (survives reloads + reboots via macOS defaults)
local speechEnabled = hs.settings.get("vk.speechEnabled") ~= false

local function setSpeechLabel()
  if not replyWebView then return end
  local label = speechEnabled and "🔊 Voice" or "🔇 Mute"
  replyWebView:evaluateJavaScript(
    "var l=document.getElementById('vk-voice'); if(l) l.textContent='" .. label .. "';",
    function() end)
end

local function toggleSpeech()
  speechEnabled = not speechEnabled
  if not speechEnabled then speechSynth:stop() end
  hs.settings.set("vk.speechEnabled", speechEnabled)
  setSpeechLabel()
end

function vkSpeechEnabled() return speechEnabled end
function vkToggleSpeech() toggleSpeech() return speechEnabled end
function vkGeo()
  if not replyWebView then return nil end
  local tl = replyWebView:topLeft()
  local sz = replyWebView:size()
  return { x = tl.x, y = tl.y, w = sz.w, h = sz.h }
end

local function plainText(s)
  s = s:gsub("```[^\n]*\n.-```", " ")       -- fenced code blocks
  s = s:gsub("`([^`\n]+)`", "%1")           -- inline code
  s = s:gsub("%*%*([^*\n]+)%*%*", "%1")     -- bold
  s = s:gsub("%*([^*\n]+)%*", "%1")         -- italic
  s = s:gsub("!%[[^%]]*%]%([^%)]*%)", " ")  -- images
  s = s:gsub("%[([^%]]+)%]%([^%)]*%)", "%1")-- links
  s = s:gsub("https?://%S+", " ")           -- bare URLs
  s = s:gsub("\n%s*[|%s:%-]+%s*\n", "\n")   -- table separator rows
  s = s:gsub("\n[-*]%s+", "\n")             -- bullets
  s = s:gsub("[#>*_~|]", " ")               -- leftover markdown chars
  s = s:gsub("%s+", " ")
  return s
end

local function speakReply(text)
  if not speechEnabled then return end
  if not text or text == "" then return end
  text = text:gsub("%s*… %(full reply in .*%)%s*$", "")
  local clean = plainText(text)
  if clean:gsub("%s", "") == "" then return end
  speechSynth:stop()
  speechSynth:speak(clean)
end

local cachedMic, cachedMicAt = nil, 0
local function mic()
  if cachedMic and os.time() - cachedMicAt < 300 then return cachedMic end
  local out = hs.execute(VK .. " mic-resolve 2>/dev/null")
  if not out or out:gsub("%s+$", "") == "" then return nil end
  cachedMic = out:gsub("%s+$", "")
  cachedMicAt = os.time()
  return cachedMic
end

-- ---------- dedicated reply window (webview) ----------

local function hideReplyWindow()
  if replyWebView then
    replyWebView:delete()
    replyWebView = nil
    replyMaximized = false
  end
  if speechSynth then speechSynth:stop() end
end

local function escapeHTML(s)
  s = s:gsub("&", "&amp;"):gsub("<", "&lt;"):gsub(">", "&gt;")
  return s
end

-- tiny markdown renderer: code blocks, inline code, bold, italic, headings, bullets
local function md(s)
  s = escapeHTML(s)

  -- stash fenced code blocks first
  local blocks = {}
  local function stash(html)
    table.insert(blocks, html)
    return "\1" .. #blocks .. "\1"
  end
  s = s:gsub("```%w*\n(.-)\n?```", function(code) return stash("<pre>" .. code .. "</pre>") end)

  -- split a table row into trimmed cells (drops trailing-pipe artifact)
  local function rowCells(line)
    local row = line:gsub("^%s*|%s*", ""):gsub("%s*|%s*$", "")
    local cs = {}
    for cell in row:gmatch("([^|]*)") do
      cell = cell:gsub("^%s+", ""):gsub("%s+$", "")
      cs[#cs + 1] = cell
    end
    while cs[#cs] == "" do cs[#cs] = nil end
    return cs
  end

  -- markdown tables -> HTML tables
  local lines = {}
  for line in s:gmatch("([^\n]*)\n?") do table.insert(lines, line) end
  local out, i = {}, 1
  while i <= #lines do
    local line = lines[i]
    local isRow = line:match("^%s*|.-|%s*$") ~= nil
    local sepLine = i < #lines and lines[i + 1] or ""
    local isSep = sepLine:match("^%s*[%-:| ]*%s*$") ~= nil and sepLine:find("%-") ~= nil and sepLine:find("|") ~= nil
    if isRow and isSep then
      local t = { "<table>", "<tr>" }
      for _, cell in ipairs(rowCells(line)) do
        t[#t + 1] = "<th>" .. cell .. "</th>"
      end
      t[#t + 1] = "</tr>"
      i = i + 2
      while i <= #lines and lines[i]:match("^%s*|.-|%s*$") do
        t[#t + 1] = "<tr>"
        for _, cell in ipairs(rowCells(lines[i])) do
          t[#t + 1] = "<td>" .. cell .. "</td>"
        end
        t[#t + 1] = "</tr>"
        i = i + 1
      end
      t[#t + 1] = "</table>"
      out[#out + 1] = stash(table.concat(t))
    else
      out[#out + 1] = line
      i = i + 1
    end
  end
  s = table.concat(out, "\n")

  -- inline elements
  s = s:gsub("`([^`\n]+)`", "<code>%1</code>")
  s = s:gsub("%*%*([^*\n]+)%*%*", "<b>%1</b>")
  s = s:gsub("%*([^*\n]+)%*", "<em>%1</em>")
  s = s:gsub("\n###%s*([^\n]+)", "\n<h3>%1</h3>")
  s = s:gsub("\n##%s*([^\n]+)", "\n<h2>%1</h2>")
  s = s:gsub("\n#%s*([^\n]+)", "\n<h1>%1</h1>")
  s = s:gsub("\n[-*]%s+([^\n]+)", "\n<span class='li'>&bull; %1</span>")

  -- restore stashed blocks
  s = s:gsub("\1(%d+)\1", function(idx) return blocks[tonumber(idx)] or "" end)
  return s
end

local function replyHTML(text)
  return [[
<html>
<head>
<meta charset="utf-8">
<style>
  body { background:rgba(17,19,24,0.85); color:#e8ecf3; font-family:-apple-system,'Helvetica Neue',sans-serif;
         margin:0; font-size:15px; line-height:1.55; -webkit-user-select:text; cursor:default; }
  .hdr { display:flex; align-items:center; justify-content:space-between;
         padding:12px 18px; background:rgba(24,28,36,0.9); cursor:move; user-select:none;
         border-bottom:1px solid #232a36; }
  .title { font-size:13px; font-weight:600; color:#6db3ff; letter-spacing:0.2px; }
  .mic { font-size:12px; color:#cfe6ff; background:#1d3a5f; border:1px solid #2a3b52;
         border-radius:8px; padding:5px 12px; cursor:grab; }
  .wrap  { padding:16px 22px 58px; height:calc(100vh - 46px); box-sizing:border-box; overflow-y:auto; }
  .body  { white-space:pre-wrap; }
  h1,h2,h3 { color:#9cc7ff; margin:14px 0 6px; }
  code { background:#1d232e; color:#8fd3ff; padding:2px 6px; border-radius:5px;
         font-family:'SF Mono',Menlo,monospace; font-size:13px; }
  pre  { background:#0c0e12; border:1px solid #232a36; border-radius:8px; padding:12px;
         overflow-x:auto; font-family:'SF Mono',Menlo,monospace; font-size:13px; }
  .li  { display:block; padding-left:6px; }
  table { border-collapse:collapse; margin:10px 0; width:100%; font-size:13px; }
  th { background:#1d232e; color:#8fd3ff; text-align:left; padding:6px 10px;
       border:1px solid #2a3b52; }
  td { padding:6px 10px; border:1px solid #232a36; }
  tr:nth-child(even) td { background:#161a21; }
  .foot { position:fixed; bottom:12px; right:18px; }
  .foot a { color:#6db3ff; text-decoration:none; font-size:12px; margin-left:14px;
            padding:5px 10px; border:1px solid #2a3b52; border-radius:7px; background:#181c24; }
  .foot a:hover { background:#223048; }
</style>
</head>
<body>
  <div class="hdr">
    <span class="title">🤖 opencode replied</span>
    <span class="mic">🎙 hold to speak</span>
  </div>
  <div class="wrap"><div class="body">]] .. md(text) .. [[</div></div>
  <div class="foot">
    <a href="#" id="vk-voice">]] .. (speechEnabled and "🔊 Voice" or "🔇 Mute") .. [[</a>
    <a href="#">🧹 Clear</a>
    <a href="#">⤢ Maximize</a>
    <a href="#">✕ Close</a>
  </div>
</body>
</html>]]
end

local function showReplyWindow()
  local ok, err = pcall(function()
    local f = io.open(REPLY_FILE, "r")
    if not f then return end
    local text = f:read("*a") or ""
    f:close()
    text = text:gsub("%s+$", "")
    if text == "" then return end
    local speakText = text
    if #text > 6000 then text = text:sub(1, 6000) .. "\n\n… (full reply in ~/.voice-kit/last-reply.txt)" end
    if replyCleared then
      text = ""
      speakText = ""
    end

    hideReplyWindow()

    local screen = hs.screen.mainScreen():frame()
    local w, h = 640, 480
    local x = screen.x + screen.w - w - 30
    local y = screen.y + screen.h - h - 30
    lastFrame = { x = x, y = y, w = w, h = h }

    replyWebView = hs.webview.new(lastFrame, {
      windowMasks = hs.webview.windowMasks.titled
        + hs.webview.windowMasks.nonactivating
        + hs.webview.windowMasks.utility
        + hs.webview.windowMasks.resizable
        + hs.webview.windowMasks.closable
        + hs.webview.windowMasks.fullSizeContentView,
    })
    replyWebView:windowTitle("opencode reply")
    replyWebView:behavior(hs.canvas.windowBehaviors.canJoinAllSpaces
      + hs.canvas.windowBehaviors.fullScreenAuxiliary)
    replyWebView:transparent(true)
    replyWebView:bringToFront()

    replyWebView:navigationCallback(function(wv, action, navPolicy)
      -- clicks on ✕/⤢ are swallowed by the Lua click-zone watcher before they
      -- ever reach this webview, so normal page loads are all that arrive here
      if type(navPolicy) == "function" then
        navPolicy("allow")
      end
    end)

    replyWebView:html(replyHTML(text))
    replyWebView:show()
    if speakText ~= "" then
      speakReply(speakText)
    end
  end)
  if not ok then
    lastWindowError = tostring(err)
    hs.alert.show("vk window error: " .. tostring(err))
    print("vk window error: " .. tostring(err))
  end
end

-- expose for manual testing/debug: hs -c 'vkShowReply()'
function vkShowReply() showReplyWindow() end
function vkIsReplyVisible() return replyWebView ~= nil end

-- watch for new replies from the opencode plugin
replyWatcher = hs.pathwatcher.new(os.getenv("HOME") .. "/.voice-kit/", function(files)
  for _, f in pairs(files) do
    if f:match("last%-reply%.txt$") then
      replyCleared = false
      hs.timer.doAfter(0.2, showReplyWindow)
    end
  end
end)
replyWatcher:start()

-- drag: global click watcher — mousedown on the header strip starts a window drag
dragClickTap = hs.eventtap.new({ hs.eventtap.event.types.leftMouseDown }, function(e)
  if not replyWebView then return false end
  local p = e:location()
  local tl = replyWebView:topLeft()
  local sz = replyWebView:size()
  -- ✕ Close zone (bottom-right)
  if p.x >= tl.x + sz.w - 84 and p.x <= tl.x + sz.w - 14 and p.y >= tl.y + sz.h - 46 and p.y <= tl.y + sz.h - 6 then
    hideReplyWindow()
    return true
  end
  -- 🔊 Voice toggle zone (left of Clear)
  if p.x >= tl.x + sz.w - 353 and p.x <= tl.x + sz.w - 277 and p.y >= tl.y + sz.h - 46 and p.y <= tl.y + sz.h - 6 then
    toggleSpeech()
    return true
  end
  -- 🧹 Clear zone (left of ⤢): clears displayed content only, window stays
  if p.x >= tl.x + sz.w - 267 and p.x <= tl.x + sz.w - 192 and p.y >= tl.y + sz.h - 46 and p.y <= tl.y + sz.h - 6 then
    replyCleared = true
    showReplyWindow()
    return true
  end
  -- ⤢ Maximize zone (left of Clear)
  if p.x >= tl.x + sz.w - 182 and p.x <= tl.x + sz.w - 94 and p.y >= tl.y + sz.h - 46 and p.y <= tl.y + sz.h - 6 then
    if replyMaximized then
      replyWebView:topLeft({ x = lastFrame.x, y = lastFrame.y })
      replyWebView:size({ w = lastFrame.w, h = lastFrame.h })
      replyMaximized = false
    else
      local scr = hs.screen.mainScreen():frame()
      replyWebView:topLeft({ x = scr.x, y = scr.y })
      replyWebView:size({ w = scr.w, h = scr.h })
      replyMaximized = true
    end
    return true
  end
  -- 🎙 hold-to-speak zone (top-right of header)
  if p.x >= tl.x + sz.w - 150 and p.x <= tl.x + sz.w - 10 and p.y >= tl.y + 4 and p.y <= tl.y + 42 then
    speakHold = true
    startRecording()
    return true
  end
  if p.x >= tl.x and p.x <= tl.x + sz.w and p.y >= tl.y and p.y <= tl.y + 46 then
    local origin = { mx = p.x, my = p.y, wx = tl.x, wy = tl.y }
    if dragUpTap then dragUpTap:stop() end
    dragUpTap = hs.eventtap.new({ hs.eventtap.event.types.leftMouseUp }, function()
      if dragTimer then dragTimer:stop(); dragTimer = nil end
      if dragUpTap then dragUpTap:stop(); dragUpTap = nil end
      return true
    end)
    dragUpTap:start()
    if dragTimer then dragTimer:stop() end
    dragTimer = hs.timer.doEvery(0.016, function()
      local m = hs.mouse.absolutePosition()
      if replyWebView then
        replyWebView:topLeft({
          x = origin.wx + (m.x - origin.mx),
          y = origin.wy + (m.y - origin.my),
        })
      end
    end)
    return true -- swallow the click; it belongs to the drag
  end
  return false
end)
dragClickTap:start()

speakUpTap = hs.eventtap.new({ hs.eventtap.event.types.leftMouseUp }, function(e)
  if speakHold then
    speakHold = false
    finishRecording(true, nil)
    return true
  end
  return false
end)
speakUpTap:start()

-- ---------- push-to-talk ----------

startRecording = function()
  if recordingTask or processing then return end
  hideReplyWindow() -- clear the reply window while you talk
  local m = mic()
  if not m or m == "" then
    alert("vk: no microphone found")
    return
  end
  os.remove("/tmp/vk-hold.wav")
  recordingTask = hs.task.new("/bin/bash", nil, { "-c",
    string.format('export PATH=/opt/homebrew/bin:$PATH; sox -q -t coreaudio "%s" /tmp/vk-hold.wav', m) })
  recordingTask:start()
  playSound("start")
  alert("🎙 recording…")
end

-- Voice delivery is per-PROJECT. Hammerspoon detects the directory you are
-- working in (frontmost terminal) and writes the prompt to that project's own
-- inbox. Only the opencode instance running in that directory consumes it, so
-- voice follows you across projects instead of always landing in one fixed
-- directory. Inboxes: ~/.voice-kit/inboxes/<sha1(cwd)[:12]>.txt
local KIT_DIR = os.getenv("HOME") .. "/.voice-kit"
local INBOXES_DIR = KIT_DIR .. "/inboxes"
local LEGACY_INBOX = KIT_DIR .. "/inbox.txt"

local function shellq(s)
  return "'" .. tostring(s):gsub("'", "'\\''") .. "'"
end

-- canonicalize a directory path (strip trailing slashes) so hash/compare is stable
local function normDir(d)
  if not d then return d end
  return tostring(d):gsub("/+$", "")
end

local function projectHash(dir)
  local out = hs.execute("printf %s " .. shellq(normDir(dir)) .. " | shasum -a 1 | cut -c1-12")
  return (out or ""):gsub("%s+", "")
end

-- cwd of the directory the user is currently working in (frontmost terminal),
-- via the shared vk_cwd.py detector. nil when it can't be determined.
local function frontCwd()
  local out = hs.execute(VK_DIR_PY .. " 2>/dev/null")
  if not out then return nil end
  out = out:gsub("%s+$", "")
  if out == "" then return nil end
  return normDir(out)
end

local function inboxFor(dir)
  hs.execute("mkdir -p " .. shellq(INBOXES_DIR))
  return INBOXES_DIR .. "/" .. projectHash(dir) .. ".txt"
end

local function opencodeRunningIn(dir)
  local q = shellq(dir)
  local out = hs.execute(
    "for p in $(pgrep -x opencode); do if lsof -a -p $p -d cwd -Fn 2>/dev/null | grep -q '^n" .. dir .. "$'; then echo yes; break; fi; done")
  return out ~= nil and out:match("yes") ~= nil
end

-- opencode running anywhere (fallback path)
local function opencodeRunningAnywhere()
  local out = hs.execute("pgrep -x opencode >/dev/null 2>&1; echo $?")
  return out ~= nil and out:match("^0") ~= nil
end

-- Try to start an opencode instance inside `dir` (best-effort, no keystroke
-- injection into whatever else you're typing). Falls back to an alert if we
-- can't launch there automatically — the plugin catches up the moment you run
-- opencode in that project.
local function ensureOpencodeIn(dir)
  if opencodeRunningIn(dir) then return end
  -- kitty remote-control socket (if the user enabled it) — clean new window
  local ok = hs.execute("kitty @ --to unix:/tmp/kitty.sock launch --cwd " .. shellq(dir) .. " opencode 2>/dev/null; echo $?")
  if ok and ok:match("^0") then return end
  hs.alert.show("🎙 opencode isn't running in:\n" .. dir .. "\nRun 'opencode' there to receive voice.")
end

-- forward-declared; the real implementation (with the 🎯 picker) follows below
local targetDir

-- route voice text to the Hermes Agent (one-shot) and show its reply in the
-- floating window + TTS (same last-reply.txt path opencode replies use)
-- route voice text to the Hermes Agent (one-shot, SAME persistent session) and
-- show its reply in the floating window + TTS. Every press resumes the previous
-- session ID so the conversation stays continuous instead of starting fresh.
local HERMES_SESSION = os.getenv("HOME") .. "/.voice-kit/hermes-session"
local function sendToHermes(text)
  hs.alert.show("🎙 voice → Hermes Agent")
  -- GUI apps don't inherit the terminal's DEEPSEEK_API_KEY; pull it from ~/.zshrc
  local key = hs.execute("grep -oE 'DEEPSEEK_API_KEY=\"[^\"]*\"' ~/.zshrc | head -n1 | cut -d'\"' -f2 2>/dev/null")
  key = (key or ""):gsub("%s+", "")
  local SCRIPT = "cd ~/hermes\n"
    .. "export DEEPSEEK_API_KEY=" .. shellq(key) .. "\n"
    .. "H=" .. HERMES .. "\n"
    .. "SID_FILE=" .. HERMES_SESSION .. "\n"
    .. "LOG=~/.voice-kit/hermes.log\n"
    .. "TXT=" .. shellq(text) .. "\n"
    .. "echo \"---- $(date '+%F %T') >>> $TXT\" >> \"$LOG\"\n"
    .. "if [ -s \"$SID_FILE\" ]; then RESUME=\"--resume $(cat \"$SID_FILE\")\"; else RESUME=\"\"; fi\n"
    .. "OUT=$($H -z \"$TXT\" $RESUME -m deepseek-chat --provider deepseek 2>>\"$LOG\")\n"
    .. "NEWID=$($H sessions list 2>/dev/null | sed -n '3p' | awk '{print $NF}'); [ -n \"$NEWID\" ] && printf '%s' \"$NEWID\" > \"$SID_FILE\"\n"
    .. "echo \"---- $(date '+%F %T') <<< $OUT\" >> \"$LOG\"\n"
    .. "printf '%s' \"$OUT\"\n"
  hs.task.new("/bin/bash", function(_, stdout)
    local out = (stdout and stdout:gsub("%s+$", "") or "")
    if out == "" then
      hs.alert.show("Hermes: no reply")
      return
    end
    local f = io.open(REPLY_FILE, "w")
    if f then f:write(out); f:close() end
    hs.execute("touch " .. shellq(os.getenv("HOME") .. "/.voice-kit/reply-waiting"))
  end, { "-c", SCRIPT }):start()
end

local function sendToOpencode(text, autoEnter)
  if hs.settings.get("vk.sessionDir") == "hermes" then
    sendToHermes(text)
    return
  end
  local dir = normDir(targetDir() or frontCwd())
  local f
  if dir then
    f = io.open(inboxFor(dir), "w")
    ensureOpencodeIn(dir)
  else
    -- no project detected (e.g. frontmost app isn't a terminal): legacy path
    f = io.open(LEGACY_INBOX, "w")
    if not opencodeRunningAnywhere() then
      hs.execute("open -a kitty")
      hs.timer.doAfter(1.5, function()
        if not opencodeRunningAnywhere() then
          local a = hs.application.get("kitty")
          if a then a:activate() end
          hs.timer.usleep(300000)
          hs.eventtap.keyStrokes("opencode\n")
        end
      end)
    end
  end
  if f then f:write(text); f:close() end
  local clean = dir and dir:gsub("/+$", "") or ""
  local name = clean:match("[^/]+$") or "Auto"
  hs.alert.show("🎙 voice → " .. name)
end

-- test hook (hs -c 'vkTestSend("...")') — drives the exact sendToOpencode path
function vkTestSend(t)
  sendToOpencode(t or "[vk-test] pipeline ok", true)
end

-- ---------- explicit voice-target picker (🎯) ----------
-- Choose which opencode project/session voice is sent to. Pinned until changed;
-- "Auto" falls back to following the active terminal (frontCwd). Stored via
-- hs.settings so it survives reloads.
targetDir = function()
  local t = hs.settings.get("vk.sessionDir")
  if not t or t == "" then
    return normDir(os.getenv("HOME") .. "/projects/vk-voice-kit")
  end
  if t == "auto" or t == "hermes" then return nil end
  return normDir(t)
end

local function dirName(d)
  if not d then return "?" end
  local clean = d:gsub("/+$", "")
  return clean:match("[^/]+$") or d
end

local PRESET_DIRS = {}
local function presetDirs()
  local h = os.getenv("HOME")
  local vk = h .. "/projects/vk-voice-kit"
  local out = h .. "/projects/work_outside"
  local t = {}
  if hs.fs.attributes(vk, "directory") then t[#t + 1] = vk end
  if hs.fs.attributes(out, "directory") then t[#t + 1] = out end
  PRESET_DIRS = t
end

local function runningOpencodeDirs()
  local seen, dirs = {}, {}
  local out = hs.execute(
    "for p in $(pgrep -x opencode); do lsof -a -p $p -d cwd -Fn 2>/dev/null | grep '^n' | cut -c2-; done")
  if not out then return dirs end
  for line in out:gmatch("[^\n]+") do
    line = line:gsub("%s+$", "")
    if line ~= "" and not seen[line] then seen[line] = true; dirs[#dirs + 1] = line end
  end
  return dirs
end

local function targetMenu()
  local items = {}
  local raw = hs.settings.get("vk.sessionDir") or "auto"
  local cur = targetDir() -- effective target dir, nil when Auto/Hermes
  local modeName = raw == "hermes" and "Hermes Agent"
    or (raw == "auto" and "Auto" or dirName(raw))

  items[#items + 1] = {
    title = (raw == "auto" and "✓ " or "") .. "Auto · follow active terminal",
    fn = function() hs.settings.set("vk.sessionDir", "auto"); hs.alert.show("🎙 voice → Auto (follows active terminal)") end,
  }
  items[#items + 1] = {
    title = (raw == "hermes" and "✓ " or "") .. "Hermes Agent (voice → hermes)",
    fn = function() hs.settings.set("vk.sessionDir", "hermes"); hs.alert.show("🎙 voice → Hermes Agent") end,
  }
  items[#items + 1] = {
    title = "Hermes · Start new conversation",
    fn = function()
      hs.settings.set("vk.sessionDir", "hermes")
      os.remove(os.getenv("HOME") .. "/.voice-kit/hermes-session")
      hs.alert.show("🗨 Hermes: new conversation (next voice push starts fresh)")
    end,
  }
  items[#items + 1] = { title = "-" }

  local seen = {}
  if cur then seen[cur] = true end
  local function addDir(d)
    d = normDir(d)
    if not d or d == "" or seen[d] then return end
    seen[d] = true
    items[#items + 1] = {
      title = (cur == d and "✓ " or "") .. dirName(d) .. "   (" .. d .. ")",
      fn = function()
        hs.settings.set("vk.sessionDir", d)
        hs.alert.show("🎙 voice → " .. dirName(d))
      end,
    }
  end

  items[#items + 1] = { title = "Running opencode instances" }
  for _, d in ipairs(runningOpencodeDirs()) do addDir(d) end
  presetDirs()
  items[#items + 1] = { title = "Known projects" }
  for _, d in ipairs(PRESET_DIRS) do addDir(d) end

  items[#items + 1] = { title = "-" }
  items[#items + 1] = {
    title = "Current target: " .. modeName,
    disabled = true,
  }
  return items
end

vkTargetMenu = hs.menubar.new()
vkTargetMenu:setTitle("🎯")
vkTargetMenu:setMenu(function() return targetMenu() end)
if hs.fs.attributes and not hs.fs.attributes then end
presetDirs()

-- focus opencode (kitty) — the "come back and see the console" escape hatch
local function focusOpencode()
  local app = hs.application.get("kitty")
  if app then
    app:activate()
  else
    hs.execute("open -a kitty")
  end
end
vkConsole = hs.hotkey.bind({}, "F7", focusOpencode)

finishRecording = function(autoEnter, holdMode)
  speakHold = false
  if not recordingTask then return end
  recordingTask:terminate()
  recordingTask = nil
  processing = true
  playSound("stop")
  alert("🧠 transcribing…")

  hs.task.new("/bin/bash", function(_, stdout)
    processing = false
    local text = stdout and stdout:gsub("%s+$", "")
    if not text or text == "" or text:match("^⚠️") then
      cachedMic, cachedMicAt = nil, 0
      alert("vk: " .. (text and text or "no speech detected"))
      return
    end
    -- fast-path router handled it (e.g. "volume up", "next track"): the status
    -- was already written to the reply window, so do NOT send anything to opencode
    if text:match("^__VK_QUICK__") then
      return
    end
    sendToOpencode(text, true)
  end, { "-c", "export PATH=/opt/homebrew/bin:$PATH; " .. VK .. " hold " .. (holdMode or "") .. " 2>/dev/null" }):start()
end

vkF5 = hs.hotkey.bind({}, "F5", startRecording, function() finishRecording(true, "--raw") end)
vkF6 = hs.hotkey.bind({}, "F6", startRecording, function() finishRecording(true, nil) end)

-- modifier keys (right-⌘ / right-⌥) emit flagsChanged, not keyDown/keyUp, so they
-- need an event tap instead of hs.hotkey. keyCode tells us WHICH modifier changed,
-- and the flags tell us whether it was pressed (flag set) or released (flag clear).
local rcmdActive, raltActive = false, false
vkModTap = hs.eventtap.new({ hs.eventtap.event.types.flagsChanged }, function(e)
  local code = e:getKeyCode()
  local flags = e:getFlags()
  if code == hs.keycodes.map.rightcmd then
    if flags.cmd then
      if not rcmdActive and not recordingTask and not processing then
        rcmdActive = true
        startRecording()
      end
    else
      if rcmdActive then rcmdActive = false; finishRecording(true, "--raw") end
    end
    return true
  elseif code == hs.keycodes.map.rightalt then
    if flags.alt then
      if not raltActive and not recordingTask and not processing then
        raltActive = true
        startRecording()
      end
    else
      if raltActive then raltActive = false; finishRecording(true, nil) end
    end
    return true
  end
  return false
end)
vkModTap:start()

-- mouse middle button (hold) = verbatim voice (same as F5 / right-⌘)
local mmbHold = false
vkMmbTap = hs.eventtap.new({ hs.eventtap.event.types.otherMouseDown, hs.eventtap.event.types.otherMouseUp }, function(e)
  local button = e:getProperty(hs.eventtap.event.properties.mouseEventButtonNumber)
  if button ~= 2 then return false end
  local isDown = e:getType() == hs.eventtap.event.types.otherMouseDown
  if isDown then
    if not mmbHold and not recordingTask and not processing then
      mmbHold = true
      startRecording()
    end
  else
    if mmbHold then mmbHold = false; finishRecording(true, "--raw") end
  end
  return true
end)
vkMmbTap:start()

-- menu bar quick status
menubar = hs.menubar.new()
menubar:setTitle("🎙")
menubar:setClickCallback(function()
  local f = io.open(REPLY_FILE, "r")
  local text = f and f:read("*a") or ""
  if f then f:close() end
  if text and text:gsub("%s", "") ~= "" then
    replyCleared = false
    showReplyWindow() -- reopen the floating window with the most recent message
  else
    local out = hs.execute(VK .. " mic-resolve 2>/dev/null")
    hs.dialog.alert(200, 200, function() end,
      "vk voice kit", "No replies yet.\n\nActive mic: " .. (out and out:gsub("%s+$", "") or "?") ..
      "\n\nF5 / right-⌘ / middle-click = verbatim\nF6 / right-⌥ = formatted\nF7 = focus opencode console\nHold the key, speak, release. Task auto-submits.")
  end
end)

hs.timer.doAfter(0.5, function() mic() end)
alert("vk loaded — hold F5 / right-⌘ / middle-click to talk")
