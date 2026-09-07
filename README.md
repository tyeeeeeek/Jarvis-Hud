# J.A.R.V.I.S HUD

A local, voice-controlled desktop HUD inspired by Iron Man's J.A.R.V.I.S — built with
Electron, React, and a Python voice pipeline. Highlights:

- **A real tool-calling brain**: voice commands are routed through the Claude Code CLI
  (already authenticated on your machine) with a curated set of custom local tools —
  filesystem (scoped to safe folders), app launch/close, disk cleanup, weather, email
  drafts, YouTube playback, screen vision, reminders/notes, bank-statement spending
  summaries, and a "creator mode" that builds and shows you a webpage or dashboard.
  [Groq](https://console.groq.com) (fast cloud chit-chat, no tool access) is tried
  first if the Claude CLI can't be reached, then [Ollama](https://ollama.com) (fully
  offline chit-chat, also no tool access) if Groq isn't configured or also fails.
- **Real YouTube control**: "play [song] by [artist]" drives a dedicated, visible,
  Playwright-controlled Chromium window that actually searches and clicks play — not
  just a search page. "stop"/"close the browser" shut it back down; the next open
  request transparently relaunches a fresh window.
- **Creator mode**: ask Jarvis to build something ("build me a dashboard that shows
  ...") and it appears live inside the HUD in a sandboxed, animated panel, or opens as
  a full site in your browser. `build_finance_dashboard` grounds this specifically in
  your real synced spending data (never invented numbers).
- **Financial insights**: `get_financial_insights` reads the synced statement ledger for
  concrete tips — an outsized spending category, a real spike/drop vs. the prior period,
  and merchants that look like recurring subscriptions — not generic advice.
- **Basic terminal diagnostics**: `run_diagnostic_command` runs one of a fixed, curated,
  read-only set (disk/memory usage, top processes, network info, uptime, ping, listening
  ports) — the closest thing to "run a PowerShell/terminal command" Jarvis has, deliberately
  never a generic shell-exec (see "Safety model" below).
- **Manager mode**: "hire a developer/designer/researcher/analyst to ..." gives a job a
  name and routes it to the matching existing tool (`self_improve`, `build_creation`,
  `ask_claude_web`, `get_financial_insights`) — hiring never grants new capability, just a
  roster you can check with "who's on my team." See `hire_employee`/`list_employees`.
- **Homelab widget**: Synology NAS status (CPU/memory/storage, read-only), NAS folder
  sync (export/import/list a folder, jailed to one NAS folder you choose — see
  `sync_bank_data`'s automatic pull for the bank-statements use case), a real internet
  speed test (on demand or every 6 hours), and router-agnostic new-device LAN alerts
  (an nmap ping sweep from this PC, not a router API) over Telegram. See "Homelab (NAS
  + network)" below — the NAS piece needs some setup on your end.
- **Live weather widget** (via the free Open-Meteo API — no API key needed)
- **Time / date widget**
- **Voice wake word** ("Jarvis") with speech recognition
- **Draggable, glass-panel widgets** with smooth show/hide transitions and a
  right-click menu
- Full-screen animated "arc reactor" HUD with a live activity feed, an
  ambient particle field, a radar sweep and degree-tick ring, a one-time
  power-on boot sequence, live corner readouts (link/mic/session/widget
  status, not decorative fakes), a distinct amber "thinking" animation
  while the brain is working, and synthesized UI chirps (wake / thinking
  tick / response-ready — muteable from the titlebar, no shipped audio
  files) — see `src/sound.ts` and `src/components/ArcReactor.css`. Its
  **Phone Vision** and **Phone Camera** widgets mirror the phone HUD's
  camera Q&A and live camera feed here in real time (see below).
- **Phone HUD camera Q&A** (`/hud`) — an Iron-Man-style cyan HUD page for
  your phone: opens your phone's camera, runs local object detection
  (TensorFlow.js) with animated bounding boxes, and lets you ask any
  question — by voice or text — about what it sees, answered for real by
  Gemini vision, spoken back, in a genuine back-and-forth. See "Phone HUD
  (camera Q&A)" below.

Feel free to fork and extend it.

## Safety model

Jarvis's brain never gets a raw shell/code-execution tool, and never gets more than the
tools listed in `jarvis_mcp_server.py` — this is enforced at the CLI level with
`--tools "" --mcp-config ... --strict-mcp-config --allowedTools "mcp__jarvis__*"`, not
just prompted for (verified: built-in tools like Bash are completely unavailable to that
call, not merely denied). Every capability in `tools.py` is a specific, narrow, named
function:

- Filesystem tools are jailed to Desktop/Documents/Downloads/Pictures/Music/Videos/
  JarvisCreations — never an arbitrary path.
- Deletes go to the Recycle Bin only, never a permanent delete.
- App-closing is restricted to the same curated alias list as app-launching — no
  arbitrary process kill.
- Email is always a pre-filled draft — Jarvis never sends on your behalf.
- Screen vision ("eyes") is off by default and only ever screenshots at the exact
  moment you ask, never on a timer or in the background.
- Anything Jarvis builds (creator mode) renders in a sandboxed iframe
  (`sandbox="allow-scripts"`, no `allow-same-origin`) so generated code can't reach the
  app, your files, or your cookies.
- `run_diagnostic_command` is a fixed, named allowlist (disk/memory usage, processes,
  network info, uptime, whoami, listening ports, ping), never a general shell-exec — the
  brain can only pick a name from the list, the argv for each is hardcoded, `subprocess`
  is always called with a list (never `shell=True`/a joined string), and ping's hostname
  argument is the only free-text input, strictly validated before it's used.
- `system_power` (shutdown/restart/cancel) only ever calls the `shutdown` binary with a
  hardcoded flag (`-h`/`-r`/`-c`) plus a delay built from a number, never free text. On
  Linux it runs through a NOPASSWD sudoers rule scoped to that one binary specifically
  (see "Shut down / restart the PC") — nothing else gains elevated access. If
  `JARVIS_ADMIN_BOT_TOKEN` is set, shutdown/restart additionally require an explicit
  yes/no over the separate JarvisAdmin bot before running (see "Require approval first")
  — that bot has no capability of its own beyond answering a pending request, and a
  gated action never runs just because JarvisAdmin isn't configured or reachable.
- "Hiring an employee" (`hire_employee`) never grants new capability — it only routes a
  named job to one of the existing narrow tools above (`self_improve`, `build_creation`,
  `ask_claude_web`, `get_financial_insights`), each already scoped exactly as described here.
- The Synology NAS integration's status calls (`get_nas_status`) are read-only —
  CPU/memory/storage status, nothing else. Its folder-sync calls (`export_folder_to_nas`,
  `import_folder_from_nas`, `list_nas_folder`, and `sync_bank_data`'s automatic NAS pull)
  do read and write files, but only inside a single jailed NAS folder (`SYNOLOGY_BASE_PATH`)
  — the same "fixed set of folders, never an arbitrary path" jail `_SAFE_DIRS` applies to
  local filesystem tools. There is deliberately no tool that can reboot, shut down, or
  reconfigure the NAS, or reach any NAS path outside that one folder. The LAN device scan
  (`network_watch.py`) is a read-only ping sweep + reading this PC's own kernel neighbor
  table — it never touches the router or any discovered device beyond pinging it.

---

## Requirements

- **Node.js** 18 or newer — [nodejs.org](https://nodejs.org)
- **Python** 3.10–3.12 — [python.org](https://python.org)
- **Claude Code CLI**, installed and authenticated (`claude auth login` or similar) —
  this is the brain. Without it, Jarvis still runs but falls back to offline
  chat-only Ollama with no tool access.
- **Ollama** installed and running locally — [ollama.com](https://ollama.com) (used
  only as the offline fallback) — `ollama pull llama3.2`
- Windows is the primary tested platform (voice uses `pywin32`/SAPI as a TTS fallback,
  app-launch/close use Windows-specific mechanisms). Mac/Linux users can run the
  frontend fine; the Python backend needs porting for those OS-specific bits.

---

## 1. Install frontend dependencies

```bash
npm install
```

## 2. Install Python dependencies

It's strongly recommended to use a virtual environment:

```bash
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # Mac/Linux

pip install -r requirements.txt
playwright install chromium
```

> **Note on PyAudio:** on Windows, `pip install pyaudio` sometimes fails to build.
> If it does, install a prebuilt wheel instead:
> `pip install pipwin && pipwin install pyaudio`

> **`playwright install chromium`** downloads the dedicated automation browser used
> for real YouTube playback control — a one-time ~115MB download, separate from any
> browser you already have installed.

## 3. Set your location for the weather widget

Open `src/components/WeatherWidget.tsx` and edit these two lines near the top:

```ts
const LAT = 34.0522;
const LON = -118.2437;
const LOCATION_LABEL = "LOS ANGELES, CA";
```

Replace with your own latitude/longitude (search "[your city] latitude longitude" to
find these easily) and a label to display.

## 4. Make sure Ollama is running

```bash
ollama serve
```

(If you installed Ollama as a desktop app, it likely already runs in the background —
check for it in your system tray.)

## 5. Build and launch

```bash
npm run build
npm start
```

The first launch will automatically download the Vosk speech recognition model
(~128MB) — this only happens once.

Say **"Jarvis"** and wait for the HUD's arc reactor to flash, then ask a question or
say a command like "what time is it."

---

## Development mode (hot reload)

For active development, instead of `npm start`, run:

```bash
npm run electron:dev
```

This runs the Vite dev server and Electron together with hot module reload for the
frontend. You'll still need `jarvis.py` running separately in another terminal:

```bash
venv\Scripts\python.exe jarvis.py
```

---

## Project structure

```
├── electron.cjs            # Electron main process — window creation, spawns jarvis.py
├── preload.cjs              # Secure IPC bridge between renderer and main process
├── jarvis.py                 # Voice backend — wake word, speech recognition, TTS, WS server,
│                              # fast-path commands (time/date/media keys), brain dispatch
├── brain.py                  # Claude Code CLI tool-calling orchestration + activity streaming
├── tools.py                  # Every capability Jarvis can perform (the actual tool surface)
├── jarvis_mcp_server.py      # MCP stdio server exposing tools.py to the brain
├── browser_control.py        # Dedicated Playwright browser worker (YouTube playback)
├── sms.py                    # Twilio SMS bridge — text Jarvis, it texts back
├── telegram_bridge.py        # Free Telegram bridge — same idea, no Twilio needed
├── jarvis_cpu_alerts.py      # JarvisCPU_Alerts — dedicated bot for PC health alerts (two-way: texts back an on-demand check)
├── email_watcher.py          # Gmail + Outlook polling + importance triage (bills, offers, deliveries)
├── bot_events.py              # Shared in-process event bus every watcher/employee/bot publishes to
├── memory_store.py            # Long-term memory: SQLite + full-text search, explicit + passive capture
├── employees.py               # Real background job queue + worker loop backing hire_employee
├── dashboard_server.py        # Phone dashboard: Flask + SSE, Tailscale-bound, token-authenticated
├── dashboard/index.html       # The dashboard page itself (edited/served live, no build step)
├── dashboard/jarvis_hud.html  # Phone HUD: Iron-Man-style desk-view camera + object ID (served at /hud)
├── vision_service.py          # Gemini vision Q&A for the phone HUD's camera (inert without GEMINI_API_KEY)
├── gmail_service.py          # Gmail + Google Calendar OAuth, drafts, and event creation
├── outlook_service.py        # Outlook Mail + Calendar (Microsoft Graph) OAuth, drafts, and event creation
├── self_improve.md           # Instructions + hard guardrails for the nightly agent
├── run_self_improve.ps1      # Wrapper the JarvisSelfImprove scheduled task runs
├── plaid_service.py          # Bank-linking Flask service (finance widget)
├── statements_service.py     # bank-statement ledger: CSV/TXT/PDF/XLSX/OFX/QFX/images/ZIP (finance widget)
├── requirements.txt          # Python dependencies
├── src/
│   ├── main.tsx                    # React entry point
│   ├── index.css                   # Global styles (transparent background for Electron)
│   └── components/
│       ├── ArcReactor.tsx           # Main HUD component — layout, WebSocket connection
│       ├── ArcReactor.css           # All HUD visual styling
│       ├── DraggableWidget.tsx      # Drag/snap wrapper with animated show/hide
│       ├── useMountTransition.ts    # Small hook powering widget enter/exit animation
│       ├── WidgetContext.tsx        # Tracks widget positions for snap-to-edge behavior
│       ├── WeatherWidget.tsx        # Live weather via Open-Meteo
│       ├── TimeWidget.tsx           # Clock/date display
│       ├── FinanceWidget.tsx        # Bank-linking / spending display
│       ├── JarvisConsole.tsx        # Scrolling activity feed (heard / said / tool activity)
│       ├── VisionWidget.tsx         # Live mirror of the phone HUD's camera Q&A (see /hud)
│       ├── CameraFeedWidget.tsx     # Live mirror of the phone HUD's camera feed itself (~5fps JPEG relay)
│       ├── CreationPanel.tsx        # Sandboxed reveal panel for things Jarvis builds
│       └── ContextMenu.tsx          # Right-click show/hide widget menu
```

## Adding a new capability

Add a small, narrow function to `tools.py` with a clear docstring (the docstring is
what the brain reads to decide when to call it), then add it to the `_TOOL_FUNCS` list
in `jarvis_mcp_server.py`. That's it — no regex, no intent matching. Never widen an
existing tool into something that takes an arbitrary path/command/process name; add a
new, narrowly-scoped one instead.

---

## How the voice pipeline works

1. **`mic_thread`** continuously reads raw audio from your microphone.
2. **`recognition_thread`** feeds that audio into [Vosk](https://alphacephei.com/vosk/)
   (offline speech recognition) and watches for the word "Jarvis."
3. Once detected, the command is checked against a small set of instant fast-path
   phrases (time, date, greetings, exit, media keys) for a zero-latency reply. Anything
   else is handed to **`brain.py`**, which runs it through the Claude Code CLI with
   real tool-calling access to everything in `tools.py` (websites, YouTube, files,
   apps, weather, reminders, creator mode, and more) — falling back to a fast,
   tools-off **Groq** model if Claude Code can't be reached, then to a local,
   tools-off **Ollama** model if Groq isn't configured (no `GROQ_API_KEY`) or also
   fails.
4. While the brain works, each tool call it makes is streamed back to the HUD live as
   an "activity" caption (e.g. "Searching YouTube for ...") — you see what Jarvis is
   doing, not just a spinner.
5. The final reply is converted to speech via `edge-tts` (natural-sounding, requires
   internet for the TTS voice itself) with a fallback to Windows' built-in SAPI voice
   if `edge-tts` isn't available or fails.
6. Everything — status updates, live transcripts, spoken responses, tool activity, and
   anything Jarvis builds — is pushed to the HUD frontend in real time over a local
   WebSocket (`ws://localhost:8765`).

---

## Customizing

- **Widget positions** are saved to `localStorage` per-widget, so dragging a widget
  remembers its position between launches.
- **Adding a new widget**: create a new component following the pattern in
  `WeatherWidget.tsx` or `TimeWidget.tsx`, then wire it into `ArcReactor.tsx` the same
  way the existing widgets are wired in (add to `WidgetId`, `DEFAULT_VISIBILITY`,
  `WIDGET_LABELS`, `defaultPositions()`, and the render block).
- **Adding new voice commands**: see "Adding a new capability" above — add a function
  to `tools.py`, not a regex. Only the handful of truly instant commands (time, date,
  greetings, exit, media keys) belong in `jarvis.py`'s fast path directly.
- **Changing the wake word**: edit `WAKE_WORD` and `_WAKE_PHRASES` in `jarvis.py`.
- **Changing Jarvis's personality**: edit the `PERSONA` system prompt in `brain.py`.

---

## Text Jarvis (SMS)

Text your Jarvis and it'll actually do the thing and text you back — no public
webhook or exposing your PC to the internet required; it polls Twilio instead.

1. Create a free account at [twilio.com](https://www.twilio.com) and get a Twilio phone
   number (trial accounts get one free number and some free credit).
2. From the [Twilio Console](https://console.twilio.com), copy your **Account SID** and
   **Auth Token**.
3. In `.env`, set:
   ```
   USER_PHONE_NUMBER=+1XXXXXXXXXX     # your real cell number
   TWILIO_ACCOUNT_SID=...
   TWILIO_AUTH_TOKEN=...
   TWILIO_FROM_NUMBER=+1XXXXXXXXXX    # the Twilio number you were given
   ```
4. Restart Jarvis. The terminal should print `[SMS] Watching for texts from ...`.
5. **Trial accounts**: Twilio trial numbers can only text/call numbers you've verified
   in the console (Console → Phone Numbers → Verified Caller IDs) until you upgrade to
   a paid account. Verify your own number there first.

Texted commands go through the exact same Claude tool-calling brain as voice commands
— same tools, same safety boundaries — and the reply comes back as a text.

---

## Text Jarvis (Telegram)

Free forever, no card, no Twilio account needed — the tradeoff is you message a bot
inside the Telegram app instead of getting native texts.

1. Install [Telegram](https://telegram.org) if you don't have it (free, any phone).
2. In the app, message **@BotFather**, send `/newbot`, and follow the prompts (pick any
   name/username). It replies with a bot token like `123456:ABC-...`.
3. Send your new bot literally anything (e.g. "hi") so it knows who you are.
4. In `.env`, set:
   ```
   TELEGRAM_BOT_TOKEN=123456:ABC-...   # from BotFather
   TELEGRAM_CHAT_ID=...                # see below
   ```
   To find your chat ID: with the bot token set, visit
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser right after
   messaging the bot — your chat ID is the number at `result[0].message.chat.id`.
5. Restart Jarvis. The terminal should print `[Telegram] Watching for messages from
   chat ...`.

Jarvis only ever acts on messages from that one chat ID — a leaked bot token or a
guessed username can't hand anyone else command access.

---

## PC health alerts (JarvisCPU_Alerts)

A second, dedicated Telegram bot just for PC health status — separate from the
command bot above so alerts never get mixed in with a two-way command chat. It
rides along on the existing 2-hour `check_system_health()` background check
(see `_health_watcher_thread` in `jarvis.py`):

- After **every** scheduled check, it sends a short summary (temperature +
  disk space) to Telegram.
- If a check finds a **critical** issue — overheating risk or disk space
  that's still critically low even after Jarvis auto-clears temp files/the
  recycle bin — it sends an alert **immediately**, rather than waiting for
  the next summary.

It's two-way for exactly one thing: text this bot anything and it replies
with a fresh, on-demand `check_system_health()` result right away, instead
of waiting for the next scheduled check. It still never accepts commands or
forwards text anywhere else — a leaked token can only be used to ask "how's
the PC doing", never to control anything.

1. Message **@BotFather** in Telegram, send `/newbot`, and follow the prompts
   to get a new bot token (a *different* bot than the one used for texting
   Jarvis above, so alerts stay on their own channel).
2. Send that new bot anything so it knows who you are.
3. In `.env`, set:
   ```
   JARVIS_CPU_ALERTS_BOT_TOKEN=123456:XYZ-...   # from BotFather
   JARVIS_CPU_ALERTS_CHAT_ID=                   # leave blank to reuse TELEGRAM_CHAT_ID
   ```
4. Restart Jarvis. No separate thread or startup log line needed — it's called
   directly from the existing health-check loop.

Fully inert (no-op, no errors) until `JARVIS_CPU_ALERTS_BOT_TOKEN` is set.

Don't want to wait for the next scheduled check or summary? Just ask Jarvis
("what's the status of the CPU alerts agent?" / "send a test message from the
watchdog bot") — `tools.agent_status()` reports whether each Telegram
sub-agent is configured and when it last actually delivered (from the shared
`~/.jarvis/telegram_delivery_log.jsonl`), and `tools.send_agent_test_message()`
forces a one-off test send from either bot on demand.

---

## Daily self-improvement reports (JarvisImprovement)

A third dedicated, send-only Telegram bot — separate from the command bot and
JarvisCPU_Alerts — just for the once-a-day summary of what the scheduled
self-improvement pass (see "24/7 self-improvement" below) safely added. It
sends exactly one message a day, right after that pass finishes:

- A bullet list of what got added, if anything.
- Or "nothing safe to improve found today" if the pass ran and genuinely
  found nothing worth doing.

It's two-way for exactly one thing: text this bot anything and it's treated
as an on-demand self-improvement request — identical to asking Jarvis
directly "improve on ..." — run through the same `self_improve()` (same
hard constraints, same syntax-check/build-verify gate before any commit).
It acks immediately, then reports back once the run finishes (which can
take a few minutes). A leaked token could waste a run, but can never run an
arbitrary command or step outside `self_improve()`'s own constraints.

1. Message **@BotFather** in Telegram, send `/newbot`, and follow the prompts
   to get a new bot token (a *different* bot than the ones used above, so
   this report stays on its own channel).
2. Send that new bot anything so it knows who you are.
3. In `.env`, set:
   ```
   JARVIS_IMPROVEMENT_BOT_TOKEN=123456:XYZ-...   # from BotFather
   JARVIS_IMPROVEMENT_CHAT_ID=                   # leave blank to reuse TELEGRAM_CHAT_ID
   ```
4. Restart Jarvis. No separate startup log line needed — it's called
   directly from `_improvement_watcher_thread` once the daily pass finishes.

Same on-demand tools as JarvisCPU_Alerts above apply here too — ask for its
status or a test send any time rather than waiting for the next 6 AM report.

Fully inert (no-op, no errors) until `JARVIS_IMPROVEMENT_BOT_TOKEN` is set.

---

## Cybersecurity watchdog (JarSecurity)

A fourth dedicated, send-only Telegram bot — separate from the command bot
and the other watchdogs above — for periodic cybersecurity sweeps of this
machine. Runs immediately on startup, then every 4 hours (see
`_security_watcher_thread` in `jarvis.py`), and logs every run to
`~/.jarvis/security_log.jsonl`:

- **Open ports** — lists every listening TCP/UDP port (`ss -tulpn` /
  `netstat -an`), same read-only style as `run_diagnostic_command`.
- **Suspicious processes** — flags anything matching a curated list of
  known cryptominer/backdoor process names, or any process executing out
  of a world-writable temp directory (`/tmp`, `/dev/shm`, `/var/tmp` — a
  classic dropper location). Report-only: nothing is ever killed.
- **Network scan for intruders** — re-runs the same LAN scan
  `scan_network` already does, flagging any brand-new device as a possible
  intruder.
- **uBlock Origin Lite** — verifies the ad/tracker blocker is installed in
  the dedicated Jarvis browser window (`browser_control.py`), and installs
  it automatically (straight from its own official GitHub releases,
  structurally verified before ever being loaded) if it isn't. Close and
  reopen the browser window after a fresh install for it to take effect.

After **every** scheduled sweep, it sends a short summary to Telegram. If a
sweep finds anything **critical** — a suspicious process or a new LAN
device — it sends an alert **immediately**, rather than waiting for the
next summary. It's two-way for exactly one thing: text this bot anything
and it replies with a read-only status update on recent sweeps and jobs
(from `~/.jarvis/security_log.jsonl` and the shared delivery log) —
deliberately not a fresh sweep itself, since a full sweep touches the
network and the browser and can take a while, so this reply is instant. It
still never accepts commands or forwards text anywhere else, so a leaked
token can only be used to ask "what have you found lately", never to
control anything.

1. Message **@BotFather** in Telegram, send `/newbot`, and follow the
   prompts to get a new bot token (a *different* bot than the ones above,
   so these alerts stay on their own channel).
2. Send that new bot anything so it knows who you are.
3. In `.env`, set:
   ```
   JARVIS_SECURITY_BOT_TOKEN=123456:XYZ-...   # from BotFather
   JARVIS_SECURITY_CHAT_ID=                   # leave blank to reuse TELEGRAM_CHAT_ID
   ```
4. Restart Jarvis. No separate startup log line needed — it's called
   directly from `_security_watcher_thread`.

Same on-demand tools as the other watchdogs above apply here too — ask
Jarvis to run a security check now (`run_security_check`), check whether
uBlock is installed (`verify_ublock_origin`) or install it
(`install_ublock_origin`), or ask for this agent's status
(`agent_status`) / a test send (`send_agent_test_message`) any time
rather than waiting for the next scheduled sweep.

Fully inert (no-op, no errors) until `JARVIS_SECURITY_BOT_TOKEN` is set —
the sweep itself still runs and logs locally either way, only the Telegram
alerts are gated on that.

---

## Shut down / restart the PC

`system_power` (already wired to voice/text/Telegram, no setup needed on Windows)
lets Jarvis shut down, restart, or cancel a pending shutdown of the machine it's
running on — "shut down my PC", "restart", "cancel shutdown". Defaults to a
1-minute delay (say "now"/"immediately" to skip that); either way it also stops
Jarvis's own backend, since it's the same machine.

**On Linux, this needs a one-time setup step.** Jarvis runs as your ordinary
desktop user, and whether that user's session is treated as "active" by
polkit (and so gets a passwordless shutdown) depends on exactly how the
backend process was started — not something to depend on, especially since a
shutdown/restart triggered remotely over Telegram has no way to respond to a
graphical password prompt if one pops up. So on Linux, `system_power` always
runs `shutdown` through `sudo -n` (non-interactive — fails fast with a clear
message instead of ever hanging on a prompt) with a narrow, single-binary
NOPASSWD rule:

```
echo "$USER ALL=(root) NOPASSWD: /usr/sbin/shutdown" | sudo tee /etc/sudoers.d/jarvis-shutdown
sudo chmod 0440 /etc/sudoers.d/jarvis-shutdown
sudo visudo -c   # validates every file in sudoers.d -- confirm it says "parsed OK"
```

### Real actions from your phone (scoped NOPASSWD, no password stored anywhere)

`get_disk_health`, `run_security_audit` (lynis), and `run_rootkit_scan` (rkhunter)
all need root for their real, full-depth results. They already work without
this setup — they just fall back to a plain non-root run (lynis skips root-only
checks; smartctl and rkhunter report a clear permission-denied message) — but
for the real thing, triggerable from your phone over Telegram with no password
prompt, add the same kind of narrow NOPASSWD rule `system_power` already uses
above. **No password is stored anywhere, ever** — the OS itself grants
passwordless root for these three exact commands only; everything else on this
account still needs a real password same as before:

```
sudo tee /etc/sudoers.d/jarvis-diagnostics <<'EOF'
tyler-kennedy ALL=(root) NOPASSWD: /usr/sbin/smartctl -a /dev/*
tyler-kennedy ALL=(root) NOPASSWD: /usr/bin/rkhunter --check --sk --nocolors
tyler-kennedy ALL=(root) NOPASSWD: /usr/sbin/lynis audit system --quick --no-colors --no-log --report-file *
EOF
sudo chmod 0440 /etc/sudoers.d/jarvis-diagnostics
sudo visudo -c   # confirm it says "parsed OK"
```

This is deliberately NOT what `run_admin_action` uses (that tool runs arbitrary,
Jarvis-decided commands — there's no way to scope a NOPASSWD rule to "whatever
command Jarvis picks" without it being equivalent to disabling sudo protection
for this account entirely, so it still requires a real password, or the
JarvisAdmin approval gate plus a manually-granted sudo session, same as before).

### Require approval first (JarvisAdmin bot)

A third, dedicated Telegram bot — separate from the two-way command bot and the
alert-only bots above — whose only job is asking permission before a
high-consequence action runs. Right now that's `system_power`'s shutdown/restart
(not cancel, which only reduces risk); the same `jarvis_admin.request_approval()`
call other tools could opt into later.

1. Message **@BotFather** in Telegram, send `/newbot`, and follow the prompts —
   name it something like "Jarvis Admin" so it's visually distinct from your
   command bot. It replies with a token like `123456:ABC-...`.
2. Send your new bot literally anything (e.g. "hi") so it's allowed to message
   you back — Telegram bots can't DM someone who hasn't messaged them first.
3. In `.env`, set:
   ```
   JARVIS_ADMIN_BOT_TOKEN=123456:ABC-...   # from BotFather, step 1
   ```
   `JARVIS_ADMIN_CHAT_ID` isn't needed — it defaults to your existing
   `TELEGRAM_CHAT_ID` (the same Telegram account, so the same numeric chat ID
   applies to any bot you message). Set `JARVIS_ADMIN_CHAT_ID` explicitly only
   if you want approvals to go to a different account than your command bot.
4. Restart Jarvis. Ask it to shut down or restart — instead of running
   immediately, JarvisAdmin will message you asking "Approval needed: Jarvis
   wants to shutdown this PC in 1 minute(s)." Reply **YES** or **NO** there
   (not on the command bot) within 3 minutes (`JARVIS_ADMIN_APPROVAL_TIMEOUT_SECONDS`
   to change that) — anything else is ignored and it keeps waiting. Jarvis's
   reply on the command bot won't come back until you've answered or the
   timeout passes, since that's the whole point: a human in the loop before it
   runs.

This is opt-in: leave `JARVIS_ADMIN_BOT_TOKEN` unset and shutdown/restart behave
exactly as before, no approval step. A leaked JarvisAdmin token can only be used
to approve/deny a request it's told about while one is pending — it never grants
any other capability, and it never falls back to asking on the main bot if it
isn't configured (a gated action simply won't run rather than asking somewhere
else).

This grants passwordless `sudo` for exactly one binary (`/usr/sbin/shutdown`,
with any arguments) — nothing else gains elevated access. `system_power`
itself already only ever calls it with `-h`/`-r`/`-c` plus a delay it
constructs from a number, never free-text passed through from what you say to
Jarvis. Until this is set up, shutdown/restart/cancel requests fail with a
clear "passwordless sudo isn't set up yet" message rather than hanging.

---

## Homelab (NAS + network)

Jarvis can report on your Synology NAS and your LAN — a "Homelab" HUD widget plus
voice/text tools (`get_nas_status`, `check_internet_speed`, `scan_network`), backed
by three modules: `synology_service.py`, `speedtest_service.py`, `network_watch.py`.

**Router note:** this deliberately does *not* depend on any router API. Most
routers (especially ISP-provided ones like an Xfinity gateway) expose no usable
local API, so instead `network_watch.py` scans the LAN directly from this PC — an
`nmap` ping sweep of your subnet, then reading back IP/MAC pairs from this
machine's own kernel neighbor table. That works identically regardless of router
brand or what's plugged into it (including a plain unmanaged switch, which doesn't
segment the network), and needs no router configuration at all.

### Synology NAS status (read-only)

1. **Create a dedicated, low-privilege DSM user for Jarvis** — DSM's web UI:
   Control Panel → User & Group → Create. It only needs to be able to log in and
   view system status; it does **not** need admin/administrators-group membership.
   Using your own admin account would work too, but isn't recommended — least
   privilege matters if `.env` ever leaks, even though Jarvis only ever calls
   read-only status APIs with it (there is no NAS power-control tool).
2. In `.env`, set:
   ```
   SYNOLOGY_HOST=10.0.0.x        # the NAS's LAN IP, or its Tailscale IP (100.x.x.x)
   SYNOLOGY_PORT=5001            # DSM's default HTTPS port
   SYNOLOGY_USER=jarvis-readonly # the dedicated user from step 1
   SYNOLOGY_PASSWORD=...
   ```
   Since this PC and the NAS are both already on the same Tailscale tailnet, the
   Tailscale IP works exactly as well as the LAN IP here — it's just an HTTPS
   request to whatever host you put in `SYNOLOGY_HOST`; no separate setup needed on
   either side. Use the Tailscale IP if you'd rather not expose DSM on the LAN at
   all, or want NAS status to keep working if Jarvis is ever off your home network.
3. Restart Jarvis and ask "what's my NAS status" or check the Homelab widget.

This has been verified live against a real DS720+ over Tailscale — if it errors
for you, the raw DSM response is included in the error message; paste that back
and it's a quick fix (or ask Jarvis to `self_improve` it).

### NAS folder sync (export/import/list, and automatic bank statement pull)

Lets Jarvis and your NAS hand folders back and forth, jailed to one NAS folder you
choose so this can never touch anything else on the NAS.

1. On the NAS, create a shared folder (DSM: Control Panel → Shared Folder → Create),
   e.g. `JarvisSync`, and give the DSM user from the status setup above read/write
   permission on it (Control Panel → Shared Folder → Edit → Permissions).
2. Set `SYNOLOGY_BASE_PATH=/JarvisSync` (or whatever you named it) in `.env`. Every
   folder-sync call below is relative to this path — there is no way to reach
   anywhere else on the NAS from here.
3. Ask Jarvis things like:
   - "Export my Bank Statements folder to the NAS" — uploads
     `~/Desktop/Bank Statements` to `SYNOLOGY_BASE_PATH/Bank Statements` on the NAS
     (`export_folder_to_nas`). Works for any folder inside desktop/documents/
     downloads/pictures/music/videos/creations, not just that one.
   - "What's in the Bank Statements folder on the NAS" — lists it (`list_nas_folder`).
   - "Grab my Bank Statements folder from the NAS" — downloads it back down into the
     matching local folder (`import_folder_from_nas`).
   - "Sync my bank data" — `sync_bank_data` automatically pulls anything new from
     `SYNOLOGY_BASE_PATH/Bank Statements` on the NAS into `~/JarvisStatements` first
     (alongside its existing Telegram-import step), then parses everything found
     there, so a CSV dropped into that NAS folder from your phone or any other
     device is picked up with one command — see "Local bank statement import" above
     for supported formats.

These calls copy files; they never delete the source, on either side, so "moving" a
folder today means exporting/importing and then deleting the original yourself if
you want it gone from one side.

### New-device LAN alerts + internet speed

1. Install nmap: `sudo apt install nmap` (needed for `scan_network`/new-device
   alerts; everything else in this project still works fine without it).
2. Install speedtest-cli into the venv: it's already in `requirements.txt`, so a
   normal `pip install -r requirements.txt` picks it up; on an existing install,
   `venv/bin/pip install speedtest-cli` (Windows: `venv\Scripts\pip`).
3. Restart Jarvis. The terminal should print a `[Homelab]` line showing what's
   configured/available.

Once nmap is installed, a background scan runs every 15 minutes (immediately on
startup, too) and any device seen for the first time triggers a Telegram alert
through the same bridge "Text Jarvis" already uses — no extra bot to set up. A
real internet speed test runs automatically every 6 hours (logged, not spoken),
or ask Jarvis any time for an on-demand one — takes about 15-30 seconds. The
Homelab widget also has a manual "RUN SPEED TEST" / "SCAN NOW" button for either.

`LAN_SUBNET_OVERRIDE` in `.env` only needs setting if subnet auto-detection
guesses wrong (e.g. your network isn't a /24) — leave it blank first and check the
terminal output before touching it.

### Local AI/monitoring services watchdog (Ollama, Prometheus)

A background check (`check_ai_services`, every 10 minutes) hits Ollama's and
Prometheus's own health endpoints (`localhost:11434`/`localhost:9090`) and, if
either is down, automatically tries `restart_ai_service` once — a Docker
container restart if one named `ollama`/`prometheus` exists, otherwise a systemd
unit restart (`ollama.service`/`prometheus.service`). The outcome (restarted, or
why it couldn't) is sent over Telegram/SMS the same way other watchdogs alert,
and the Homelab widget's "LOCAL AI SERVICES" section shows live UP/DOWN status
with its own manual RESTART button. This is deliberately narrow — only these two
exact named services are ever restartable (see `tools._AI_SERVICES`), never an
arbitrary systemctl/docker target.

If either service runs as a system-level (not `--user`) systemd unit, restarting
it needs root, same as `system_power`'s shutdown — add a scoped NOPASSWD rule so
it can happen without a password prompt:

```
sudo tee /etc/sudoers.d/jarvis-ai-services <<'EOF'
tyler-kennedy ALL=(root) NOPASSWD: /usr/bin/systemctl restart ollama.service
tyler-kennedy ALL=(root) NOPASSWD: /usr/bin/systemctl restart prometheus.service
EOF
sudo chmod 0440 /etc/sudoers.d/jarvis-ai-services
sudo visudo -c   # confirm it says "parsed OK"
```

Without this, a down system-level service gets a clear "passwordless sudo isn't
set up yet" message instead of restarting — never a hang, never a silent no-op.

---

## Connect email + calendar (Gmail & Outlook)

Jarvis can watch your inbox (voice + text alerts on anything that looks like a bill
due, a job offer, a delivery update, or a finished-task notification), write real
email drafts for you to review and send yourself, and create real calendar events —
on Gmail, Outlook, or both. Each provider is independent: set up one, both, or
neither. These are **separate** connections from anything this chat session uses —
Jarvis needs to keep working even when no Claude Code session is open.

**Draft-only, always.** Neither integration is ever granted a scope capable of
sending mail (`gmail.compose`/`Mail.ReadWrite`, never `gmail.send`/`Mail.Send`) —
so a draft Jarvis writes always lands in your Drafts folder for you to review and
send yourself, never automatically. Calendar events are created directly (like a
reminder or a note) since they're trivially reversible — just delete the event if
Jarvis got it wrong.

### Gmail + Google Calendar

1. Go to the [Google Cloud Console](https://console.cloud.google.com/), create a
   project (or use an existing one).
2. Enable both the **Gmail API** and the **Google Calendar API** for it (APIs &
   Services → Enable APIs → search each by name).
3. Configure the OAuth consent screen (APIs & Services → OAuth consent screen) —
   choose "External," fill in the required fields, and add your own Google account as
   a test user. (This stays in testing mode indefinitely for personal use — no Google
   review needed.)
4. Create credentials (APIs & Services → Credentials → Create Credentials → OAuth
   client ID → Desktop app).
5. Download the resulting JSON and save it as `credentials.json` directly in this
   project folder.
6. Restart Jarvis. The first time it needs Gmail/Calendar, a browser window opens
   asking you to sign in and approve access to mail (read + drafts) and calendar
   events — after that it's cached in `token.json` and never asks again. If you had
   an older `token.json` from before calendar support was added, delete it once so
   you're prompted for the new scope too.

### Outlook + Outlook Calendar

1. Go to the [Azure Portal](https://portal.azure.com/) → **App registrations** → New
   registration. Name it anything (e.g. "Jarvis"), and under "Supported account
   types" choose "Accounts in any organizational directory and personal Microsoft
   accounts."
2. After creating it, open **Authentication** and turn on "Allow public client
   flows" → Yes → Save. (This lets Jarvis use device-code login with no client
   secret — nothing sensitive to store or leak.)
3. Copy the **Application (client) ID** from the app's Overview page into
   `OUTLOOK_CLIENT_ID` in `.env`.
4. One-time login — run this from an interactive terminal (not as the background
   service):
   ```
   venv/bin/python -c "import outlook_service; outlook_service.ensure_authenticated()"
   ```
   It prints a URL and a short code — visit the URL on any device (your phone is
   fine) and enter the code. Once approved, the result is cached in
   `token_outlook.json` and this never needs repeating (until the refresh token
   itself expires, which Microsoft accounts rarely do for an active app).
5. Restart Jarvis.

### Using it

Just ask/text Jarvis naturally: *"draft an email to jane@company.com about the
budget"*, *"add a dentist appointment tomorrow at 2pm to my calendar"*, *"put that
on both my Gmail and Outlook calendars."* Important-mail alerts run automatically
in the background on whichever provider(s) are configured, using the real Claude
CLI to write a natural summary (falls back to your local Ollama model if the CLI
isn't available) rather than a flat classifier dump.

---

## Hire an employee

Say or text *"hire a developer to improve the weather widget"*, *"hire a designer to
build me a habit tracker"*, *"hire a researcher to find out..."*, or *"hire an analyst
for financial tips"* and Jarvis puts a named employee (Dev-1, Design-1, Research-1,
Finance-1, ...) on it in the background — the reply comes back immediately, not after
the job finishes, so a `self_improve`/`build_creation` job that takes several minutes
never blocks the conversation. Ask *"who's on my team"* / *"is Dev-1 done yet"* any
time to check status (queued/running/done/failed) and the result once it lands.

Employees can also hire each other for a concrete, narrow case: if an analyst's report
turns up a real spending anomaly, it automatically hires a researcher to look into it.
Hiring never grants any new capability beyond what the four roles already do
(`self_improve`/`build_creation`/`ask_claude_web`/`get_financial_insights`) — it's a
name, a queue slot, and a background thread, nothing more.

---

## Phone dashboard

A real, self-hosted control panel reachable from your phone, styled as a Teams-style
contact list rather than the PC HUD's cyan sci-fi theme — a distinct, sleeker look on
purpose. **Requires Tailscale** to be installed and signed in on your phone with the
same account as this machine (the same tailnet the NAS integration already uses) —
the dashboard binds to this machine's Tailscale IP specifically, never the public
internet or even the plain LAN, so it's only reachable from devices you've actually
approved onto your tailnet.

- **Chats tab** — every bot (J.A.R.V.I.S, JarvisCPU_Alerts, JarvisImprovement,
  JarSecurity, JarvisAdmin) as a contact with a live status dot; tap one to open a
  real chat thread and message it directly — the exact same reply logic each bot's
  Telegram channel already uses, just reachable from here too. Tap a contact's name
  to see their profile (role, configured status). JarvisAdmin is the one exception —
  by design it's not a general chat bot (see its own module docstring), so instead of
  a message box it shows any pending approval request live, with real Approve/Deny
  buttons.
- **Home tab** — at-a-glance widgets (PC health, security, NAS, this month's spending),
  quick-action buttons (check health, run a security sweep, sync bank data — all live,
  results reflected in the widgets within seconds), and a live activity feed of
  everything happening across the whole backend below.
- **Finance tab** — a real financial dashboard grounded in your synced bank statements
  (from the NAS `Bank Statements` folder or Telegram uploads, same ledger
  `get_spending_summary`/`get_recurring_charges` already use): total spent with a
  trend arrow, a category breakdown, a monthly trend chart, top merchants, recurring
  charges, and a history of anomalies Jarvis has actually flagged. Read-only display —
  never consumes the finance watchdog's own "alert once" state, so opening this tab
  never silently swallows a real alert you'd otherwise have gotten.
- **Tools tab** — every one of Jarvis's ~60 named tools, searchable, with an
  auto-generated form and a Run button.
- **Team tab** — hire an employee and watch it go queued → running → done live.

A banner appears at the top of every tab whenever JarvisAdmin has a pending approval
waiting, so you're never stuck on the wrong screen not knowing something needs a
decision.

1. Make sure Tailscale is connected on both this machine (`tailscale status`) and
   your phone (Tailscale app, signed into the same account).
2. Start/restart Jarvis. The startup log prints the dashboard's URL and the path to
   its access token, e.g.:
   ```
   [Dashboard] Phone dashboard -> http://100.x.x.x:8767 (token in ~/.jarvis/dashboard_token.txt)
   ```
   A token is generated automatically the first time (or set `DASHBOARD_TOKEN`
   yourself in `.env` to pick your own) — cat that file once to get it, e.g.:
   `cat ~/.jarvis/dashboard_token.txt`.
3. Open that URL on your phone's browser, paste the token in once — it's cached in
   the browser after that.

The page itself (`dashboard/index.html`) is read fresh from disk on every request —
you (or a future "improve the dashboard" self-improve pass) can keep editing it and
the changes show up immediately, no rebuild or restart needed. `/api/run` only ever
calls a tool already registered in `jarvis_mcp_server.py`'s tool list, by exact name —
the same registry your voice/Telegram commands use, never a raw command channel — so
every tool's existing safety behavior (jailed folders, curated app lists, JarvisAdmin's
approval gate inside shutdown/restart, draft-only email) applies exactly the same way
here.

---

## Phone HUD (camera Q&A)

A second phone page, separate from the Teams-style dashboard above and
reachable at the same Tailscale server plus `/hud` — reuses the same
access token, no extra setup beyond enabling HTTPS below. This one is
a cyan sci-fi HUD: monospace type, a live clock, a bordered camera
viewport with animated local bounding boxes, a typewriter-effect
transcript, and a bottom waveform visualizer.

Point your phone's camera at anything and ask a real question about
it — by voice or by typing — and get a real, grounded answer back, not
a scripted demo response. "What kind of plant is this?", "is this
outlet safe to use?", "how many of these are in the bag?", "what does
this label say?" — genuinely open-ended, the same way you'd ask a
person looking over your shoulder.

### Enable HTTPS (required for camera access)

Phone browsers refuse camera and speech-recognition access on a plain-HTTP
page, so the `/hud` page needs the dashboard served over real HTTPS — not
just reachable, actually secure — or the camera simply won't open.
Tailscale can issue a real, browser-trusted cert for this machine's
MagicDNS name for free, and Jarvis provisions/renews it automatically once
it's turned on:

1. Turn on **HTTPS Certificates** for your tailnet (one-time): visit
   [login.tailscale.com/admin/dns](https://login.tailscale.com/admin/dns)
   and enable it under "HTTPS Certificates."
2. Make sure **MagicDNS** is also enabled on the same page (it usually is
   by default) — the cert is issued for this machine's MagicDNS name
   (e.g. `my-pc.tailnetname.ts.net`), not its bare Tailscale IP.
3. Restart Jarvis. The startup log switches from `http://100.x.x.x:8767`
   to `https://my-pc.tailnetname.ts.net:8767` automatically
   (`tailscale_service.ensure_https_cert`) — no cert files to manage
   yourself, it's fetched into `~/.jarvis/https/` and renewed on its own
   before it expires.
4. Open that `https://...` URL (not the IP) on your phone. The first
   visit may show a brief "connecting" delay while the browser verifies
   the cert chain; after that it's identical to any normal HTTPS site —
   no manual trust/install step, since it's a real Let's Encrypt cert via
   Tailscale, not self-signed.

If you skip this, both dashboard pages still work over plain HTTP exactly
as before — you'll just get a clear camera-permission failure if you try
to open desk view on `/hud`.

---

### Requires a Gemini API key to actually answer questions

**This is the one piece you need to install/set up — everything else is
already wired in.** The camera opens and shows live local bounding boxes
(TensorFlow.js `coco-ssd`, runs entirely in your phone's browser, no
network round-trip, no key needed) either way, but *answering a question
about what it sees* needs a real vision-capable model — there's
deliberately no local-only fallback that guesses, since a wrong guess is
worse than an honest "not configured yet."

1. Get a free key at [aistudio.google.com/apikey](https://aistudio.google.com/apikey)
   (Google account, no credit card for the free tier).
2. In `.env`, set `GEMINI_API_KEY=...`.
3. Restart Jarvis. No package install needed — `vision_service.py` already
   uses `requests`, already in `requirements.txt`.

Without a key, asking a question gets a spoken "vision isn't configured
yet" instead of a real answer, so you'll notice immediately if this step
got missed.

**State machine**, voice-driven (say the phrase, or tap the matching chip
at the bottom of the screen if speech recognition isn't available/reliable
on your phone's browser — you can also just type a question into the text
box, which works regardless of the browser's speech-recognition support):

1. **Idle** — say *"Jarvis, are you there?"* (or tap the chip / press
   Space on desktop). Plays an activation chirp, opens the camera, and
   JARVIS says "Online. Ask me anything about what I see."
2. **Camera active** — ask anything, by voice or by typing — whatever you
   say/type that isn't a stop phrase is treated as a question. One frame
   is captured at that moment and sent with your exact question to Gemini
   (`vision_service.py`'s `answer_question`); the HUD shows an
   amber "analyzing" state briefly while it thinks, plays a confirm chirp,
   then speaks the real answer and goes back to listening for your next
   question — a real back-and-forth, not a single scripted exchange.
3. Say *"close camera"* to drop back to camera-off/still-awake, or
   *"Jarvis, sleep"* to reset all the way back to idle.

**Notes:**
- The clock shows real local time; add `?demo=1` to the URL
  (`/hud?demo=1`) to instead start it ticking from 23:43.
- The three UI chirps (activation/scan/confirm) are synthesized in the
  browser with the Web Audio API, not shipped `.wav` files — nothing to
  license or host.
- Speech recognition (`webkitSpeechRecognition`) needs a Chromium-based
  mobile browser; Safari/iOS support is inconsistent — the text box next
  to the camera works everywhere regardless.
- Every recognized question gets a spoken answer, including "close
  camera" and "Jarvis, sleep" (both now confirm out loud, not just in
  the transcript text) — and one heard while a previous answer is still
  in flight is queued rather than dropped, answered automatically the
  moment the current one finishes. Mobile Chrome is known to silently
  kill a "continuous" speech-recognition session (backgrounding, a
  network hiccup, or just idling a while) without always saying so —
  `startListening`/`_startRecognizer` track whether a session is
  actually alive and both self-heal on `onend` and carry an 8-second
  watchdog that force-restarts it if it's gone quiet, so the mic doesn't
  need a page reload to start working again.
- Every answered question is also shared with the desktop HUD's **Phone
  Vision** widget, over the same shared event bus dashboard_server.py's
  watchdogs already use (`bot_events` → `jarvis.py`'s `_on_bot_event` →
  its own WebSocket feed). One Jarvis, not two brains that don't know
  what the other saw.
- Whenever the camera is open (from "Jarvis, are you there?" until
  "close camera"/"Jarvis, sleep"), a small downscaled frame is mirrored
  live to the desktop HUD's **Phone Camera** widget (~5fps, a JPEG
  relay, not a WebRTC video call) — a *separate, dedicated* path
  (`dashboard_server.py`'s `register_frame_handler` / `/api/hud/stream`
  → `jarvis.py`'s `_on_camera_frame`) rather than the same `bot_events`
  bus the Q&A above uses, since 5 images a second through that shared
  bus would spam the main Teams-style dashboard's Live Activity feed and
  evict everything else from its 200-event replay history within
  seconds. Frames are relayed only, never written to disk anywhere —
  same "never on a timer, never stored" spirit as the existing screen
  vision tool. The widget clears the instant you close the camera (an
  explicit signal, not a guessed timeout), so it never shows a frozen
  stale frame pretending to still be live.
- Nothing here is wired into the voice-command brain's tool list — it's
  purely a dashboard-page feature (camera access + local CV + Gemini via
  `vision_service.py`), so it doesn't change what "Jarvis" can do from
  voice/Telegram/SMS.

---

## Long-term memory + daily briefing

Jarvis has real memory now, not just a short recap of the last few conversation
turns — it persists across days and restarts (`memory_store.py`, SQLite + full-text
search, no extra dependency or external service). Say *"remember that I..."* to save
something explicitly, or just mention it in passing — Jarvis also passively decides
what's worth keeping from ordinary conversation on its own, and quietly recalls
anything relevant to what you're currently asking, even from a totally separate
conversation days later. Ask *"what do you remember about me"* any time to see it.

Once a day at 7 AM, Jarvis also sends one cohesive **daily briefing** — PC health,
security, your finances, what your hired employee team got done overnight, and
anything it's remembered lately — synthesized into a single natural message instead
of four separate watchdog pings.

**Passive memory capture needs Ollama running** (`ollama.com`, `ollama serve`) — it's
listed as a project requirement, but wasn't actually installed on this machine as of
this port; without it, the explicit `remember_this`/`recall_memory` tools (which go
through the real Claude CLI, same as every other command) still work fully, but the
background "catch things I didn't explicitly ask to be remembered" capture silently
does nothing until Ollama is installed and running.

---

## "Jarvis, improve on..."

Say or text something like *"Jarvis, improve your ability to run PowerShell commands"*
or *"Jarvis, improve on playing YouTube videos without the window closing abruptly"*
and it triggers a real, on-demand Claude Code pass scoped to exactly that — not a
description of what it could do, an actual verified code change, committed to git.
Same hard constraints and build/syntax verification gate as the nightly pass below,
just triggered live instead of on a schedule. Can take a few minutes for anything
nontrivial; you'll see a live activity indicator while it works.

---

## Always-on backend

As of this update, closing the HUD window only closes the *visual* window — the
Python backend (voice, texting, email watching, reminders) keeps running in the
background regardless. `jarvis.py` refuses to start a second time if it's already
running, so it's always safe to have both the Electron app and a background instance
around at once; only one ever actually processes commands.

**To make it start automatically at every login** (so you never have to open the HUD
at all for texting/email-watching to work): create a shortcut to
`launch_jarvis_backend.vbs` in your Windows Startup folder
(`Win+R` → `shell:startup` → paste a shortcut to that file in there). This isn't set
up automatically yet — ask if you want it done.

**To start it manually right now**: double-click `launch_jarvis_backend.vbs`, or run
`venv\Scripts\pythonw.exe jarvis.py` from the project folder.
**To stop it**: find `pythonw.exe` in Task Manager and end it (there's currently no
in-app "quit backend" command — worth adding if you find yourself doing this often).

---

## 24/7 self-improvement

As long as the always-on backend (`jarvis.py`) is running, a background thread
(`_improvement_watcher_thread`) fires once every 24 hours, starting at **6 AM local
time**, and runs Claude Code autonomously against this project folder for **up to 2
hours** to find and fix bugs, tidy things up, and add small useful capabilities — with
full local file/command access and **no approval step**, per your call on how much
autonomy to give it. This is cross-platform (plain Python scheduling, no OS task
scheduler needed) and reuses the exact same mechanism as the on-demand "Jarvis,
improve on..." command (`tools.run_daily_self_improve()` / `self_improve.md`), just
unfocused and time-boxed instead of scoped to one thing you asked for live. When it
finishes — whether it made changes, found nothing safe to improve, or hit the 2-hour
cap — the **JarvisImprovement** Telegram bot (see above) sends a one-message summary
of whatever got added that day.

On Windows, a Scheduled Task (`JarvisSelfImprove`, historically nightly at 3 AM) can
also run the same pass via `run_self_improve.ps1` if you'd rather trigger it outside
`jarvis.py`'s own scheduling — the two aren't mutually exclusive, but running both
means two passes a day.

What keeps this from being reckless:
- **Hard-coded guardrails it cannot override** (see `self_improve.md`): it can never
  add a shell-exec tool to the brain's toolset, widen the filesystem safe-dirs list to
  an arbitrary path, make deletes bypass the Recycle Bin, let app-closing target
  anything outside the curated list, make email auto-send, or weaken the creator-mode
  iframe sandboxing.
- **A verification gate before every commit** — it must run `npm run build` /
  Python syntax checks on anything it changes, and revert rather than commit if
  something doesn't pass.
- **Git.** Every change lands as its own commit with a clear message. Nothing it does
  is ever unrecoverable — `git log` shows every change, `git revert <hash>` undoes any
  of them.

**To check on it**: run logs land in `~/.jarvis/self_improve_logs/`; git history
(`git log --oneline`) shows exactly what it's changed over time.
**To run it manually** (instead of waiting for 3 AM): `schtasks /Run /TN "JarvisSelfImprove"`.
**To change the schedule**: `schtasks /Change /TN "JarvisSelfImprove" /ST 02:00` (or any
time — note it only fires if the PC is on at that time).
**To turn it off**: `schtasks /Delete /TN "JarvisSelfImprove" /F` (or `/Disable` instead
of `/Delete` to keep it around but paused).

---

## Troubleshooting

**"Electron not found" / npx prompts to install a new Electron version every launch**
Run `npm install` — this ensures the project's own pinned Electron version (from
`package.json`) is installed locally instead of falling back to a fresh download.

**HUD shows "SENSOR OFFLINE" on the weather widget**
Check your internet connection — the weather widget calls the free Open-Meteo API
directly from the browser and needs network access.

**Jarvis Console shows "OFFLINE" and never connects**
Make sure `jarvis.py` is actually running (check the terminal for
`[WS] WebSocket server -> ws://localhost:8765`). If it's not starting, check that all
Python dependencies installed correctly and that you're using the virtual environment
where you installed them.

**Jarvis feels "dumb" / says it can't do things it should be able to (offline fallback)**
This means `brain.py` couldn't reach the Claude Code CLI, so Jarvis silently dropped to
the tools-off Groq fallback (or straight to Ollama if `GROQ_API_KEY` isn't set). Make
sure `claude` is installed and authenticated (`claude auth login`), and that
`venv/Scripts/python.exe -m pip show mcp` shows the package installed. Check the
`jarvis.py` terminal output for `[Brain] Launch error`.

**"I don't have a Bash tool" / a command Jarvis should clearly be able to do fails**
That's the safety sandbox working as intended if it's a real shell command — Jarvis
deliberately has no shell-exec tool. If it's something that should be a real capability
(e.g. "close Chrome" but Chrome isn't in `_APP_ALIASES`/`_APP_IMAGE_NAMES` in
`tools.py`), add it there.

**YouTube playback never starts / a Chromium window doesn't appear**
The first `play_youtube` call downloads nothing new (Chromium was installed via
`playwright install chromium`) but does launch a real, visible browser window — check
it didn't open behind the HUD. If it errors, re-run `playwright install chromium`.

**AI responses say "I can't reach my brain right now, and my main brain is offline too"**
The Claude CLI, Groq, and the Ollama fallback all failed (or Groq was never
configured). For Groq: check `GROQ_API_KEY` is set in `.env` and valid at
[console.groq.com/keys](https://console.groq.com/keys). For Ollama: run
`ollama serve` and `ollama pull llama3.2` (or update `OLLAMA_MODEL` in `jarvis.py` to
whatever model you've pulled instead).

---

## License

This project is released open source for anyone to use, modify, and build on. No
warranty is provided — this was built as a personal passion project and shared as-is.
Attribution appreciated but not required.
