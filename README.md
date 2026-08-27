# J.A.R.V.I.S HUD

A local, voice-controlled desktop HUD inspired by Iron Man's J.A.R.V.I.S — built with
Electron, React, and a Python voice pipeline. Highlights:

- **A real tool-calling brain**: voice commands are routed through the Claude Code CLI
  (already authenticated on your machine) with a curated set of custom local tools —
  filesystem (scoped to safe folders), app launch/close, disk cleanup, weather, email
  drafts, YouTube playback, screen vision, reminders/notes, bank-statement spending
  summaries, and a "creator mode" that builds and shows you a webpage or dashboard.
  [Ollama](https://ollama.com) is kept only as an offline chit-chat fallback (no tool
  access) if the Claude CLI can't be reached.
- **Real YouTube control**: "play [song] by [artist]" drives a dedicated, visible,
  Playwright-controlled Chromium window that actually searches and clicks play — not
  just a search page.
- **Creator mode**: ask Jarvis to build something ("build me a dashboard that shows
  ...") and it appears live inside the HUD in a sandboxed, animated panel, or opens as
  a full site in your browser.
- **Live weather widget** (via the free Open-Meteo API — no API key needed)
- **Time / date widget**
- **Voice wake word** ("Jarvis") with speech recognition
- **Draggable, glass-panel widgets** with smooth show/hide transitions and a
  right-click menu
- Full-screen animated "arc reactor" HUD with a live activity feed

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
├── email_watcher.py          # Gmail polling + importance triage (bills, offers, deliveries)
├── self_improve.md           # Instructions + hard guardrails for the nightly agent
├── run_self_improve.ps1      # Wrapper the JarvisSelfImprove scheduled task runs
├── plaid_service.py          # Bank-linking Flask service (finance widget)
├── statements_service.py     # CSV bank-statement ledger (finance widget)
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
   apps, weather, reminders, creator mode, and more) — falling back to a local,
   tools-off **Ollama** model only if Claude Code can't be reached.
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

## Watch my email

Jarvis can watch your inbox and proactively tell you (voice + text) about anything
that looks like a bill due, a job offer, a delivery update, or a finished-task
notification. This is a **separate** Gmail connection from anything this chat session
uses — Jarvis needs to keep watching even when no Claude Code session is open.

1. Go to the [Google Cloud Console](https://console.cloud.google.com/), create a
   project (or use an existing one).
2. Enable the **Gmail API** for it (APIs & Services → Enable APIs → search "Gmail
   API").
3. Configure the OAuth consent screen (APIs & Services → OAuth consent screen) —
   choose "External," fill in the required fields, and add your own Google account as
   a test user. (This stays in testing mode indefinitely for personal use — no Google
   review needed.)
4. Create credentials (APIs & Services → Credentials → Create Credentials → OAuth
   client ID → Desktop app).
5. Download the resulting JSON and save it as `credentials.json` directly in this
   project folder.
6. Restart Jarvis. The first time, a browser window will open asking you to sign in
   and approve access — after that it's cached in `token.json` and never asks again.

Classification runs on your local Ollama model (not Claude) since it polls every
couple of minutes — cheap and private, no per-check API cost.

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

A Windows Scheduled Task (`JarvisSelfImprove`, nightly at 3 AM) runs Claude Code
autonomously against this project folder to find and fix bugs, tidy things up, and add
small useful capabilities — with full local file/command access and **no approval
step**, per your call on how much autonomy to give it.

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
the tools-off Ollama fallback. Make sure `claude` is installed and authenticated
(`claude auth login`), and that `venv/Scripts/python.exe -m pip show mcp` shows the
package installed. Check the `jarvis.py` terminal output for `[Brain] Launch error`.

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
Both the Claude CLI and the Ollama fallback failed. For Ollama specifically: run
`ollama serve` and `ollama pull llama3.2` (or update `OLLAMA_MODEL` in `jarvis.py` to
whatever model you've pulled instead).

---

## License

This project is released open source for anyone to use, modify, and build on. No
warranty is provided — this was built as a personal passion project and shared as-is.
Attribution appreciated but not required.
