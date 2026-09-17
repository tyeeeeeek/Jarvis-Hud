# J.A.R.V.I.S HUD

A local, voice-controlled desktop HUD inspired by Iron Man's J.A.R.V.I.S — built with
Electron, React, and a Python voice pipeline. This is the **starter kit**: the HUD
shell, the voice pipeline, and a small set of capabilities that need zero personal
accounts or tokens (desktop app/media control, web browsing, weather, a map widget,
and local conversational memory). Everything homelab- or account-specific (Telegram,
email, a NAS, remote fleet control, Pi-hole, bank-linking, and so on) is left for you
to add yourself, the same way the ones already here were added — see "Adding a new
capability" below.

Highlights:

- **A real tool-calling brain**: voice commands are routed through the Claude Code CLI
  (already authenticated on your machine) with a curated set of custom local tools —
  app launch/close, media control, web search/browsing, weather, a map widget, and
  local conversational memory. [Groq](https://console.groq.com) (fast cloud chit-chat,
  no tool access) is tried first if the Claude CLI can't be reached, then
  [Ollama](https://ollama.com) (fully offline chit-chat, also no tool access) if Groq
  isn't configured or also fails.
- **Real YouTube control**: "play [song] by [artist]" drives a dedicated, visible,
  Playwright-controlled Chromium window that actually searches and clicks play — not
  just a search page. "stop"/"close the browser" shut it back down; the next open
  request transparently relaunches a fresh window.
- **Live weather widget** (via the free Open-Meteo API — no API key needed) and a
  **map widget** (free OpenStreetMap/Open-Meteo geocoding and radar, no API key
  needed to get started — an optional [MapTiler](https://www.maptiler.com) key gives
  a nicer vector basemap).
- **Time / date widget**.
- **Long-term local memory** — SQLite + full-text search, entirely local, no external
  service. Say *"remember that I..."* to save something explicitly, or just mention it
  in passing; ask *"what do you remember about me"* any time to see it.
- **Voice wake word** ("Jarvis") with fully offline speech recognition (Vosk).
- Full-screen animated "arc reactor" HUD with a live activity feed, an ambient
  particle field, a radar-sweep tick ring, a one-time power-on boot sequence, a
  distinct "thinking" animation while the brain is working, and synthesized UI
  chirps (wake / thinking tick / response-ready — muteable from the titlebar, no
  shipped audio files) — see `src/sound.ts` and `src/components/ArcReactor.css`.
  A decorative, fully self-contained 3D "Jarvis Core" model (Three.js) is included
  too, purely for show — no external data feed.

Feel free to fork and extend it.

## Safety model

Jarvis's brain never gets a raw shell/code-execution tool, and never gets more than
the tools listed in `jarvis_mcp_server.py` — this is enforced at the CLI level with
`--tools "" --mcp-config ... --strict-mcp-config --allowedTools "mcp__jarvis__*"`, not
just prompted for (verified: built-in tools like Bash are completely unavailable to
that call, not merely denied). Every capability in `tools.py` is a specific, narrow,
named function — no capability accepts an arbitrary path/command/process name. Keep
that pattern when you add your own: a new narrow function, never a widened existing
one.

---

## Requirements

- **Node.js** 18 or newer — [nodejs.org](https://nodejs.org)
- **Python** 3.10–3.12 — [python.org](https://python.org)
- **Claude Code CLI**, installed and authenticated (`claude auth login` or similar) —
  this is the brain. Without it, Jarvis still runs but falls back to offline
  chat-only Ollama with no tool access.
- **Ollama** installed and running locally — [ollama.com](https://ollama.com) (used
  only as the offline fallback, and for passive long-term-memory capture) —
  `ollama pull llama3.2`
- Runs on **Windows** (SAPI TTS fallback, Windows-specific app launch/close) and
  **Linux** (`espeak`/`arecord`-based fallbacks) — `preflight_check.sh` checks what's
  missing on a fresh Linux machine before you start. Mac isn't tested.

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
find these easily) and a label to display. `src/components/MapWidget.tsx` has its
own matching `DEFAULT_TARGET` a few lines down (`lat`/`lon`/`name`) — update it
to the same coordinates so the map widget opens centered on your area too.

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
├── browser_control.py        # Dedicated Playwright browser worker (YouTube playback, web browsing)
├── bot_events.py              # Shared in-process event bus (e.g. show_map -> the HUD's WebSocket)
├── memory_store.py            # Long-term memory: SQLite + full-text search, explicit + passive capture
├── tts_service.py             # edge-tts wrapper (with a local SAPI/espeak fallback in jarvis.py)
├── preflight_check.sh         # Run first on a fresh Linux machine to check for missing dependencies
├── requirements.txt          # Python dependencies
├── src/
│   ├── main.tsx                    # React entry point
│   ├── index.css                   # Global styles (transparent background for Electron)
│   └── components/
│       ├── ArcReactor.tsx           # Main HUD component — layout, dock, WebSocket connection
│       ├── ArcReactor.css           # All HUD visual styling
│       ├── HudPage.tsx              # Full-screen page container each dock icon opens
│       ├── WeatherWidget.tsx        # Live weather via Open-Meteo
│       ├── TimeWidget.tsx           # Clock/date display
│       ├── MapWidget.tsx            # Map + weather radar, via OpenStreetMap/Open-Meteo
│       ├── JarvisCore.tsx           # Decorative 3D model (Three.js), no external data
│       ├── JarvisConsole.tsx        # Scrolling activity feed (heard / said / tool activity)
│       ├── AuroraField.tsx          # Ambient background particle/aurora effect
│       └── useMountTransition.ts    # Small hook powering page enter/exit animation
```

## Adding a new capability

Add a small, narrow function to `tools.py` with a clear docstring (the docstring is
what the brain reads to decide when to call it), then add it to the `_TOOL_FUNCS` list
in `jarvis_mcp_server.py`. That's it — no regex, no intent matching. Never widen an
existing tool into something that takes an arbitrary path/command/process name; add a
new, narrowly-scoped one instead.

This is exactly how you'd wire in your own accounts/integrations (Telegram, email, a
NAS, a smart-home hub, whatever): write a small service module (following the pattern
of `browser_control.py` or `memory_store.py` — plain functions, config from `.env`,
inert/gracefully-degraded if unconfigured), then expose the pieces you want the brain
to call as functions in `tools.py`.

---

## How the voice pipeline works

1. **`mic_thread`** continuously reads raw audio from your microphone.
2. **`recognition_thread`** feeds that audio into [Vosk](https://alphacephei.com/vosk/)
   (offline speech recognition) and watches for the word "Jarvis."
3. Once detected, the command is checked against a small set of instant fast-path
   phrases (time, date, greetings, exit, media keys) for a zero-latency reply. Anything
   else is handed to **`brain.py`**, which runs it through the Claude Code CLI with
   real tool-calling access to everything in `tools.py` — falling back to a fast,
   tools-off **Groq** model if Claude Code can't be reached, then to a local,
   tools-off **Ollama** model if Groq isn't configured (no `GROQ_API_KEY`) or also
   fails.
4. While the brain works, each tool call it makes is streamed back to the HUD live as
   an "activity" caption (e.g. "Searching YouTube for ...") — you see what Jarvis is
   doing, not just a spinner.
5. The final reply is converted to speech via `edge-tts` (natural-sounding, requires
   internet for the TTS voice itself) with a fallback to the OS's built-in TTS
   (Windows SAPI / Linux `espeak`) if `edge-tts` isn't available or fails.
6. Everything — status updates, live transcripts, spoken responses, and tool activity
   — is pushed to the HUD frontend in real time over a local WebSocket
   (`ws://localhost:8765`).

---

## Customizing

- **Adding a new page/widget**: create a new component following the pattern in
  `WeatherWidget.tsx` or `TimeWidget.tsx`, then wire it into `ArcReactor.tsx` — add it
  to `PageId`, `PAGE_LABELS`, `DOCK_ICONS`, and `pageContent`.
- **Adding new voice commands**: see "Adding a new capability" above — add a function
  to `tools.py`, not a regex. Only the handful of truly instant commands (time, date,
  greetings, exit, media keys) belong in `jarvis.py`'s fast path directly.
- **Changing the wake word**: edit `WAKE_WORD` and `_WAKE_PHRASES` in `jarvis.py`.
- **Changing Jarvis's personality**: edit the `PERSONA` system prompt in `brain.py`.

---

## Always-on backend

Closing the HUD window only closes the *visual* window — the Python backend (voice,
the WebSocket server) keeps running in the background regardless. `jarvis.py` refuses
to start a second time if it's already running, so it's always safe to have both the
Electron app and a background instance around at once; only one ever actually
processes commands.

**Windows — to start it automatically at every login**: create a shortcut to
`launch_jarvis_backend.vbs` in your Windows Startup folder (`Win+R` → `shell:startup`
→ paste a shortcut to that file in there).
**To start it manually**: double-click `launch_jarvis_backend.vbs`, or run
`venv\Scripts\pythonw.exe jarvis.py` from the project folder.
**To stop it**: find `pythonw.exe` in Task Manager and end it.

**Linux — to start it automatically**: run it as a `systemd --user` service (create a
small `.service` unit that runs `venv/bin/python jarvis.py` from this folder, then
`systemctl --user enable --now jarvis-backend`), or add it to your desktop session's
autostart. **To start it manually**: `venv/bin/python jarvis.py` from the project
folder. **To stop it**: `systemctl --user stop jarvis-backend`, or just `Ctrl+C` /
kill the process if you started it manually.

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
it didn't open behind the HUD. If it errors, re-run `playwright install chromium`. On
Linux, make sure a real desktop session is active (`$DISPLAY`/`$WAYLAND_DISPLAY` set)
— Playwright's non-headless Chromium needs one.

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
