-- vk Hammerspoon config: push-to-talk dictation + dedicated reply window
-- F5 / right-⌘ (hold) = dictate -> transcribe -> format -> paste into focused app
-- F6 / right-⌥ (hold) = same, but also presses Enter after pasting (auto-submit to opencode)
-- Reply window: dedicated draggable app-style window with rendered Markdown.
--   Drag via the header bar. ⤢ toggles full-screen. ✕ closes.

-- enable the `hs` CLI so the vk tooling can drive/debug Hammerspoon
pcall(function() require("hs.ipc").cliInstall() end)

local VK = os.getenv("HOME") .. "/.voice-kit/vk"
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
local replyPoll = nil

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
speechSynth:rate(210)
speechSynth:volume(0.9)

local function plainText(s)
  s = s:gsub("```[^\n]*\n.-```", " ")       -- fenced code blocks
  s = s:gsub("`([^`\n]+)`", "%1")           -- inline code
  s = s:gsub("%*%*([^*\n]+)%*%*", "%1")     -- bold
  s = s:gsub("%*([^*\n]+)%*", "%1")         -- italic
  s = s:gsub("!%[[^%]]*%]%([^%)]*%)", " ")  -- images
  s = s:gsub("%[([^%]]+)%]%([^%)]*%)", "%1")-- links
  s = s:gsub("\n%s*[|%s:%-]+%s*\n", "\n")   -- table separator rows
  s = s:gsub("\n[-*]%s+", "\n")             -- bullets
  s = s:gsub("[#>*_~|]", " ")               -- leftover markdown chars
  s = s:gsub("%s+", " ")
  return s
end

local function speakReply(text)
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
    <a href="#">🧹 Clear</a>
    <a href="#">⤢ Maximize</a>
    <a href="#">✕ Close</a>
  </div>
</body>
</html>]]
end

local function startDrag()
  local mouse = hs.mouse.getAbsolutePosition()
  local tl = replyWebView and replyWebView:topLeft()
  if not tl then return end
  local origin = { mx = mouse.x, my = mouse.y, wx = tl.x, wy = tl.y }

  if dragUpTap then dragUpTap:stop() end
  dragUpTap = hs.eventtap.new({ hs.eventtap.event.types.leftMouseUp }, function()
    if dragTimer then dragTimer:stop(); dragTimer = nil end
    if dragUpTap then dragUpTap:stop(); dragUpTap = nil end
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
end

-- dedicated user-content channel for webview buttons (never touches URL schemes)
vkUCC = hs.webview.usercontent.new("vkbridge")
vkUCC:setCallback(function(msg)
  local df = io.open(os.getenv("HOME") .. "/.voice-kit/vk-ucc.log", "a")
  if df then df:write("ucc fired: " .. tostring(type(msg)) .. "\n"); df:close() end
  local action = type(msg) == "table" and (msg.body or msg[1]) or msg
  if action == "max" then
    if replyWebView then
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
    end
  elseif action == "close" then
    hideReplyWindow()
  end
  return ""
end)

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
function vkLastError() return lastWindowError end
function vkIsCleared() return replyCleared end
function vkMdTest(doneFile)
  local sample = "Header line\n\n| Tool | Port | Result |\n|------|------|--------|\n| nmap | 22 | open |\n| sox | 0 | ok |\n\nTail line"
  local f = io.open(doneFile, "w")
  f:write(md(sample))
  f:close()
end

function vkTableCheck(doneFile)
  if not replyWebView then
    local f = io.open(doneFile, "w") f:write("no window") f:close() return
  end
  replyWebView:evaluateJavaScript("document.querySelectorAll('table').length + ' table / ' + document.querySelectorAll('th').length + ' headers'", function(v)
    local f = io.open(doneFile, "w")
    f:write("render check: " .. tostring(v))
    f:close()
  end)
end

function vkFrame() return (replyWebView and replyWebView:frame()) or lastFrame end
function vkClickClose()
  if not replyWebView then return "no window" end
  local f = replyWebView:frame() or lastFrame
  local cx, cy = f.x + f.w - 55, f.y + f.h - 28
  hs.mouse.absolutePosition({ x = cx, y = cy })
  local down = hs.eventtap.event.newMouseEvent(hs.eventtap.event.types.leftMouseDown, { x = cx, y = cy })
  down:post()
  hs.timer.usleep(120000)
  local up = hs.eventtap.event.newMouseEvent(hs.eventtap.event.types.leftMouseUp, { x = cx, y = cy })
  up:post()
  hs.timer.usleep(800000)
  return "after close click, visible: " .. tostring(replyWebView ~= nil)
end

function vkPollTest(doneFile)
  if not replyWebView then
    local f = io.open(doneFile, "w") f:write("no window") f:close() return
  end
  replyWebView:evaluateJavaScript("window.__vkcmd='close'")
  hs.timer.doAfter(0.8, function()
    local f = io.open(doneFile, "w")
    f:write("after JS close: visible=" .. tostring(replyWebView ~= nil))
    f:close()
  end)
end

function vkClickClose(doneFile)
  if not replyWebView then
    local f = io.open(doneFile, "w") f:write("no window") f:close() return
  end
  local f = replyWebView:frame() or lastFrame
  local cx, cy = f.x + f.w - 55, f.y + f.h - 28
  hs.mouse.absolutePosition({ x = cx, y = cy })
  hs.eventtap.event.newMouseEvent(hs.eventtap.event.types.leftMouseDown, { x = cx, y = cy }):post()
  hs.timer.doAfter(0.15, function()
    hs.eventtap.event.newMouseEvent(hs.eventtap.event.types.leftMouseUp, { x = cx, y = cy }):post()
    hs.timer.doAfter(0.8, function()
      local fo = io.open(doneFile, "w")
      fo:write("after close click, visible: " .. tostring(replyWebView ~= nil))
      fo:close()
    end)
  end)
end

function vkMaxTest(doneFile)
  if not replyWebView then
    local f = io.open(doneFile, "w") f:write("no window") f:close() return
  end
  local f0 = replyWebView:frame()
  hs.mouse.absolutePosition({ x = f0.x + f0.w - 160, y = f0.y + f0.h - 26 })
  hs.eventtap.event.newMouseEvent(hs.eventtap.event.types.leftMouseDown, { x = f0.x + f0.w - 160, y = f0.y + f0.h - 26 }):post()
  hs.timer.doAfter(0.12, function()
    hs.eventtap.event.newMouseEvent(hs.eventtap.event.types.leftMouseUp, { x = f0.x + f0.w - 160, y = f0.y + f0.h - 26 }):post()
    hs.timer.doAfter(0.5, function()
      local f1 = replyWebView:frame()
      local fo = io.open(doneFile, "w")
      fo:write(string.format("maximize: %.0f -> %.0f wide | %s", f0.w, f1.w, tostring(f1.w > f0.w + 100)))
      fo:close()
    end)
  end)
end

function vkDragTest(doneFile)
  if not replyWebView then
    local f = io.open(doneFile, "w") f:write("no window") f:close() return
  end
  local tl0 = replyWebView:topLeft()
  local sz = replyWebView:size()
  local sx, sy = tl0.x + sz.w / 2, tl0.y + 20
  hs.mouse.absolutePosition({ x = sx, y = sy })
  hs.eventtap.event.newMouseEvent(hs.eventtap.event.types.leftMouseDown, { x = sx, y = sy }):post()
  local i = 0
  local t
  t = hs.timer.doEvery(0.06, function()
    i = i + 1
    hs.mouse.absolutePosition({ x = sx + i * 20, y = sy + i * 10 })
    hs.eventtap.event.newMouseEvent(hs.eventtap.event.types.leftMouseDragged, { x = sx + i * 20, y = sy + i * 10 }):post()
    if i >= 10 then
      t:stop()
      hs.eventtap.event.newMouseEvent(hs.eventtap.event.types.leftMouseUp, { x = sx + 200, y = sy + 100 }):post()
      hs.timer.doAfter(0.3, function()
        local tl1 = replyWebView and replyWebView:topLeft()
        local fo = io.open(doneFile, "w")
        fo:write(string.format("before %.0f,%.0f after %.0f,%.0f dragged:%s",
          tl0.x, tl0.y, tl1 and tl1.x or -1, tl1 and tl1.y or -1,
          tostring(tl1 and (tl1.x - tl0.x > 100) or false)))
        fo:close()
      end)
    end
  end)
end

-- expose for manual testing/debug: hs -c 'vkShowReply()'
function vkShowReply() showReplyWindow() end
function vkIsReplyVisible() return replyWebView ~= nil end
function vkLastError() return lastWindowError end
function vkIsCleared() return replyCleared end
function vkMdTest(doneFile)
  local sample = "Header line\n\n| Tool | Port | Result |\n|------|------|--------|\n| nmap | 22 | open |\n| sox | 0 | ok |\n\nTail line"
  local f = io.open(doneFile, "w")
  f:write(md(sample))
  f:close()
end

function vkTableCheck(doneFile)
  if not replyWebView then
    local f = io.open(doneFile, "w") f:write("no window") f:close() return
  end
  replyWebView:evaluateJavaScript("document.querySelectorAll('table').length + ' table / ' + document.querySelectorAll('th').length + ' headers'", function(v)
    local f = io.open(doneFile, "w")
    f:write("render check: " .. tostring(v))
    f:close()
  end)
end

function vkFrame() return (replyWebView and replyWebView:frame()) or lastFrame end
function vkClickClose()
  if not replyWebView then return "no window" end
  local f = replyWebView:frame() or lastFrame
  local cx, cy = f.x + f.w - 55, f.y + f.h - 28
  hs.mouse.absolutePosition({ x = cx, y = cy })
  local down = hs.eventtap.event.newMouseEvent(hs.eventtap.event.types.leftMouseDown, { x = cx, y = cy })
  down:post()
  hs.timer.usleep(120000)
  local up = hs.eventtap.event.newMouseEvent(hs.eventtap.event.types.leftMouseUp, { x = cx, y = cy })
  up:post()
  hs.timer.usleep(800000)
  return "after close click, visible: " .. tostring(replyWebView ~= nil)
end

function vkPollTest()
  if not replyWebView then return "no window" end
  replyWebView:evaluateJavaScript("2+3", function(v)
    local f = io.open("/tmp/vk-js.log", "w")
    if f then f:write("real window 2+3 => " .. tostring(v)); f:close() end
  end)
  hs.timer.usleep(800000)
  return "check /tmp/vk-js.log"
end

function vkDragTest()
  if not replyWebView then return "no window" end
  local tl0 = replyWebView:topLeft()
  local sz = replyWebView:size()
  local sx, sy = tl0.x + sz.w / 2, tl0.y + 20
  local cur0 = hs.mouse.absolutePosition()
  hs.mouse.absolutePosition({ x = sx, y = sy })
  local down = hs.eventtap.event.newMouseEvent(hs.eventtap.event.types.leftMouseDown, { x = sx, y = sy })
  down:post()
  hs.timer.usleep(100000)
  for i = 1, 10 do
    hs.mouse.absolutePosition({ x = sx + i * 20, y = sy + i * 10 })
    local mv = hs.eventtap.event.newMouseEvent(hs.eventtap.event.types.leftMouseDragged, { x = sx + i * 20, y = sy + i * 10 })
    mv:post()
    hs.timer.usleep(40000)
  end
  local up = hs.eventtap.event.newMouseEvent(hs.eventtap.event.types.leftMouseUp, { x = sx + 200, y = sy + 100 })
  up:post()
  hs.timer.usleep(200000)
  hs.mouse.absolutePosition(cur0)
  local tl1 = replyWebView:topLeft()
  return string.format("before %.0f,%.0f -> after %.0f,%.0f | dragged: %s",
    tl0.x, tl0.y, tl1.x, tl1.y, tostring(tl1.x - tl0.x > 100))
end

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
  local lf = io.open("/tmp/vk-drag.log", "a")
  if lf then lf:write("tap fired\n"); lf:close() end
  if not replyWebView then return false end
  local p = e:location()
  local tl = replyWebView:topLeft()
  local sz = replyWebView:size()
  -- ✕ Close zone (bottom-right)
  if p.x >= tl.x + sz.w - 105 and p.x <= tl.x + sz.w - 12 and p.y >= tl.y + sz.h - 46 and p.y <= tl.y + sz.h - 6 then
    hideReplyWindow()
    return true
  end
  -- 🧹 Clear zone (left of ⤢): clears displayed content only, window stays
  if p.x >= tl.x + sz.w - 300 and p.x <= tl.x + sz.w - 220 and p.y >= tl.y + sz.h - 46 and p.y <= tl.y + sz.h - 6 then
    replyCleared = true
    showReplyWindow()
    return true
  end
  -- ⤢ Maximize zone (left of Clear)
  if p.x >= tl.x + sz.w - 205 and p.x <= tl.x + sz.w - 110 and p.y >= tl.y + sz.h - 46 and p.y <= tl.y + sz.h - 6 then
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

local function startRecording()
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

local function sendToOpencode(text, autoEnter)
  hs.pasteboard.setContents(text)
  local app = hs.application.get("kitty")
  if not app then
    -- no kitty: launch it, start opencode inside, then submit
    hs.execute("open -a kitty")
    hs.timer.doAfter(1.5, function()
      if hs.execute("pgrep -x opencode >/dev/null 2>&1; echo $?"):match("^0") == nil then
        local a = hs.application.get("kitty")
        if a then a:activate() end
        hs.timer.usleep(300000)
        hs.eventtap.keyStrokes("opencode\n")
      end
      hs.timer.doAfter(2.5, function()
        local a2 = hs.application.get("kitty")
        if a2 then a2:activate() end
        hs.timer.doAfter(0.4, function()
          hs.eventtap.keyStroke({ "cmd" }, "v")
          hs.timer.doAfter(0.15, function() hs.eventtap.keyStroke({}, "return") end)
        end)
      end)
    end)
    return
  end
  app:activate()
  hs.timer.doAfter(0.35, function()
    hs.eventtap.keyStroke({ "cmd" }, "v")
    if autoEnter then
      hs.timer.doAfter(0.15, function() hs.eventtap.keyStroke({}, "return") end)
    end
  end)
end

local function finishRecording(autoEnter, holdMode)
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
    alert("🚀 sending to opencode…")
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
      "\n\nF5 or right-⌘ = voice → opencode (verbatim)\nF6 or right-⌥ = voice → opencode (formatted)\nHold the key, speak, release. Task auto-submits.")
  end
end)

hs.timer.doAfter(0.5, function() mic() end)
alert("vk loaded — hold F5 / right-⌘ to talk")
