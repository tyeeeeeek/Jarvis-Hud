# Porting J.A.R.V.I.S HUD to this machine (Ubuntu / NUC11)

You're Claude Code, running on a NUC11 with Ubuntu, and the user has plugged in a USB
drive with this project on it. This project is a voice-and-text AI assistant (Electron
+ React HUD, Python backend) that was built and fully working on a Windows machine.
Your job: get it running here, adapting the Windows-specific pieces to Linux, and
**actually verify each piece works on this real hardware** — that verification is the
whole reason this is being handed to you instead of finished remotely.

Read this entire file before starting. It's long because the user cares a lot about
the safety design of this project specifically — read the "Non-negotiable safety
constraints" section as carefully as the setup steps.

## What this project is

A local AI assistant ("Jarvis") with:
- A Claude Code CLI tool-calling brain (`brain.py` + `tools.py` + `jarvis_mcp_server.py`)
  — voice/text commands get real tool access (files, apps, browser, YouTube, weather,
  email drafts, reminders, etc.), locked down so the brain can NEVER use a raw
  shell-exec tool, only the specific named functions in `tools.py`.
- Wake-word voice control (Vosk, offline) + TTS (edge-tts, with a local fallback).
- Real YouTube playback control via a dedicated Playwright browser.
- "Creator mode" — it can build and show the user a webpage/dashboard live.
- Texting Jarvis via Telegram (already configured, portable — same bot token works
  from any machine) and optionally Twilio SMS.
- Gmail watching for important mail (not yet configured — no credentials.json on this
  USB, the user hasn't set that up yet).
- An on-demand and nightly-scheduled **self-improvement agent**: Claude Code running
  autonomously against this repo to fix bugs/add small capabilities, gated by a
  build/syntax check before every commit, everything tracked in git.

The codebase already had most Windows-only code paths guarded/branched for
cross-platform use before this handoff (see `IS_WINDOWS` checks in `jarvis.py` and
`tools.py`, `sys_platform` markers in `requirements.txt`) — but **none of it has been
tested on Linux**. Expect to find and fix real issues. That's expected, not a sign
something went wrong in the port.

## Non-negotiable safety constraints — read before changing anything

This project's entire design philosophy is: the brain gets broad *capability* through
many narrow, named, safe tools — never broad *access* through a generic executor. When
adapting code for Linux, preserve this exactly:

- Never add a general shell-exec / arbitrary-command tool to the set exposed in
  `jarvis_mcp_server.py`. If a Linux equivalent of a Windows capability needs a
  different implementation (e.g. `close_app` using `pkill` instead of `taskkill`),
  implement it as a specific branch inside the existing narrow function — don't
  generalize the function itself into something that takes an arbitrary command.
- Filesystem tools (`create_file`, `delete_item`, etc. in `tools.py`) stay jailed to
  the `_SAFE_DIRS` dict — adjust the paths for Linux's home directory layout
  (`~/Desktop`, `~/Documents`, etc. — note some Ubuntu installs don't create all of
  these by default; `os.makedirs(..., exist_ok=True)` already handles that) but never
  make it resolve an arbitrary path.
- Deletes stay Recycle-Bin-only (`send2trash` already works cross-platform via the
  freedesktop.org trash spec — verify it actually lands in the Nautilus/Files trash).
- `close_app` stays restricted to the curated `_APP_ALIASES`/`_APP_IMAGE_NAMES` list —
  verify/expand `_APP_ALIASES_LINUX` in `tools.py` against what's actually installed on
  this machine, but keep it a curated list, not a lookup of arbitrary running
  processes.
- `draft_email` stays draft-only, never auto-send.
- Creator-mode output (`CreationPanel.tsx`) stays iframe-sandboxed
  (`sandbox="allow-scripts"`, no `allow-same-origin`) — don't touch this when adapting
  the frontend build for Linux.
- Before committing any change (setup-related or otherwise): run a Python syntax
  check on changed `.py` files and `npm run build` on frontend changes. If it fails,
  fix it or revert — never leave the repo in a broken state.
- Commit your work to git incrementally with clear messages as you go, same as the
  existing history in this repo (`git log` to see the pattern). This machine now needs
  to become the source of truth going forward, or you can set up a way to sync back to
  the Windows machine if the user wants that (ask them, don't assume).

If you're ever unsure whether an adaptation is safe, stop and ask the user rather than
guessing — same rule the nightly self-improvement agent follows (see
`self_improve.md`).

## Critical: this machine is REPLACING the Windows machine as the live Jarvis, not running alongside it

The user's Windows PC currently has a live `jarvis.py` running in the background,
polling the SAME Telegram bot (and possibly Twilio number, and Gmail) that's
configured in the `.env` on this USB. **If two machines both poll the same Telegram
bot / phone number / inbox at once, every text gets processed and replied to twice**
(each machine tracks its own independent read-offset, so both will see and act on the
same incoming message) — and worse, any command with a real side effect (creating a
file, sending something, running the self-improve agent) would happen twice
independently. This is not a hypothetical, it will definitely happen if both are live
at once.

So: you can freely set up, build, and test almost everything here WITHOUT this risk —
running `tools.py` functions directly in a Python shell, testing `build_creation`,
testing `play_youtube`, even running `jarvis.py` itself as long as you either (a) use
placeholder/blank Telegram+Twilio+Gmail values in a *copy* of `.env` for initial
testing, or (b) confirm with the user first that the Windows instance is stopped.

**Before you ever start `jarvis.py` here using the REAL `.env` from the USB (real
Telegram token, etc.) as a persistent, ongoing background process**: stop and ask the
user to confirm the Windows machine's Jarvis backend is stopped. They can do this
themselves (Task Manager → end any `pythonw.exe` processes, and remove
`JarvisBackend.lnk` from `shell:startup` so it doesn't restart), or ask their Windows
Claude Code session to do it. Don't just assume it's fine to proceed — this is exactly
the kind of "genuinely unsure, ask the user" moment called out above, and getting it
wrong means the user gets duplicate texts and possibly duplicate real actions taken on
their behalf.

Once you've confirmed the cutover and this machine is the sole active instance, that's
the point where you also set up the autostart systemd service (see below) so it
becomes the permanent home for this.

## What's already been made Linux-aware (verify, don't assume correct)

- `jarvis.py`: `win32com`/SAPI TTS is Windows-only-guarded; added an `espeak`/`espeak-ng`
  fallback for Linux (primary TTS is always edge-tts, which needs no OS-specific code
  — the fallback only matters if edge-tts/pygame audio output fails).
- `tools.py`: `launch_app`/`close_app` branch on `IS_WINDOWS`; `_APP_ALIASES_LINUX` is a
  **guess** at common GNOME/Ubuntu app names (gnome-terminal, nautilus,
  gnome-calculator, etc.) — check what's actually installed
  (`which gnome-terminal nautilus gnome-calculator code google-chrome firefox`) and fix
  the map. `clean_disk` uses `gio trash --empty` if available, else clears
  `~/.local/share/Trash` directly.
- `brain.py`/`tools.py`: `CLAUDE_CLI` now uses `shutil.which("claude")` first (should
  find whatever `claude` install this machine already has), falls back to
  `~/.local/bin/claude`. `VENV_PYTHON` in `brain.py` now uses `venv/bin/python` on
  non-Windows.
- `electron.cjs`: `startPython()` now checks for `venv/bin/python` on non-Windows
  before falling back to bare `python3`.
- `requirements.txt`: `pywin32` is marked `; sys_platform == "win32"` so
  `pip install -r requirements.txt` shouldn't try to install it here.

## What almost certainly needs real work here (not guessed at, not tested)

- **Audio hardware**: a NUC11 typically has no built-in mic or speakers. If none are
  plugged in, voice wake-word/TTS simply won't have hardware to use — confirm with the
  user whether a USB mic/speakers (or a headset) are connected before spending time
  debugging PyAudio. If there's no audio hardware, that's fine — Telegram texting,
  email watching, and self-improvement don't need it at all.
- **PyAudio/ALSA/PulseAudio**: `pip install pyaudio` frequently needs
  `sudo apt install portaudio19-dev` first on Ubuntu. Check mic input works
  (`arecord -l` or similar) before assuming PyAudio errors are a code problem.
- **Display for Playwright** (`browser_control.py`, YouTube playback): launches a real,
  visible, non-headless Chromium. This needs an actual display server (X11/Wayland)
  running — fine if this is a normal desktop Ubuntu install with a monitor attached,
  but will fail if this is a headless/server install. Also needs
  `playwright install chromium` (and possibly `playwright install-deps` for missing
  system libraries — Ubuntu often needs this).
- **Startup/scheduling**: the Windows side used a Startup-folder shortcut (backend
  autostart at login) and Windows Task Scheduler (nightly self-improvement at 3 AM).
  Linux equivalents:
  - Backend autostart: a systemd `--user` service, or a `.desktop` file in
    `~/.config/autostart/`. A systemd user service is more robust (auto-restart on
    crash, proper logging via `journalctl --user`) — prefer that.
  - Nightly self-improve: a systemd `--user` timer, or a cron entry
    (`crontab -e`, `0 3 * * * ...`). Base the actual prompt/constraints on
    `self_improve.md` in this repo — reuse it, don't rewrite it from scratch.
- **Claude Code auth on this machine**: confirm `claude` is authenticated here
  (`claude auth status` or similar) — it may need its own login even if it's the same
  Anthropic account as the Windows machine.

## Setup steps

0. Run the preflight check first (it's also on the USB root, no cloning needed):
   ```bash
   bash /media/<mount>/preflight_check.sh
   ```
   Fix anything it reports missing before continuing. In particular: if Node is
   missing or older than 18, install it via NodeSource, not the default Ubuntu apt
   package (which is typically far too old):
   ```bash
   curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
   sudo apt install -y nodejs
   ```
1. Copy the project off the USB (don't run it directly from the USB — it's likely
   NTFS and slower, and file permissions/symlinks behave oddly on NTFS-on-Linux):
   ```bash
   mkdir -p ~/jarvis-hud-opensource
   git clone /media/<mount>/jarvis-hud.bundle ~/jarvis-hud-opensource
   cp /media/<mount>/.env ~/jarvis-hud-opensource/.env
   cp -r /media/<mount>/vosk-model-en-us-0.22-lgraph ~/jarvis-hud-opensource/
   cd ~/jarvis-hud-opensource
   ```
2. Python side:
   ```bash
   sudo apt install -y portaudio19-dev python3-venv python3-pip espeak-ng alsa-utils xdg-utils
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   playwright install chromium
   playwright install-deps   # installs the system libs headed Chromium needs; needs sudo
   ```
3. Frontend side:
   ```bash
   npm install
   npm run build
   ```
4. **Stop — this is the coordination checkpoint from above.** Before running
   `jarvis.py` with the real `.env` (the one with a real Telegram bot token in it),
   confirm with the user that the Windows instance is stopped. Don't skip this and
   don't assume; ask explicitly. Once confirmed:
   ```bash
   venv/bin/python jarvis.py
   ```
   Watch the terminal output — it should print the WebSocket server line, and
   `[Telegram] Watching for messages from chat ...`. Message the existing Telegram bot
   from the user's phone and confirm you get exactly one reply (not zero, not two) —
   that's the best end-to-end proof the brain, MCP tools, and Telegram bridge all
   actually work here, it doesn't depend on audio hardware at all, and the cutover was
   clean.
5. Only after that works, test voice (if mic/speakers are present) and the full
   Electron HUD (`npm start`).
6. Set up the systemd user service(s) for autostart / nightly self-improve as
   described above, once everything's confirmed working manually.
7. Report back to the user what worked, what needed real fixes, and what (if
   anything) still needs hardware they don't have yet (mic, speakers, etc.).

Commit your setup fixes to git as you go, with the same care and commit-message style
you see in the existing history.
