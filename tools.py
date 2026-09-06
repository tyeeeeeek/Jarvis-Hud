# ================================================================
#   J.A.R.V.I.S — Tool implementations
#
#   Every function here is a plain, synchronous, side-effecting
#   capability. jarvis_mcp_server.py registers each one as an MCP
#   tool so the Claude-powered brain (brain.py) can call them by
#   name. jarvis.py's own zero-latency fast-path (time/date/media
#   keys) also imports directly from here so there's exactly one
#   implementation of each capability, never two.
#
#   IMPORTANT: this module intentionally exposes NO *unsupervised*
#   shell-exec / run-arbitrary-command tool. Every capability is a
#   specific, named, scoped function. If a new capability is needed,
#   add a new narrow function here -- never widen one of these into
#   a general-purpose executor.
#
#   The one deliberate exception is run_admin_action(): real shell
#   access for one-off installs/admin commands that the fixed list in
#   run_diagnostic_command can't cover. It's still narrow in the way
#   that matters -- not a fixed command list, but a hard requirement
#   that a human approve the exact command, every single time, on a
#   separate dedicated channel (JarvisAdmin) before it runs. Added
#   2026-08-31 at the user's explicit request, after discussing scope
#   with them directly (see run_admin_action's docstring).
# ================================================================
import os, re, sys, json, time, uuid, shutil, signal, tempfile, ipaddress, threading, subprocess, urllib.parse, webbrowser
from datetime import datetime, timedelta

import requests

IS_WINDOWS = sys.platform == "win32"

try:
    import win32api; WIN32_AVAILABLE = True
except ImportError:
    WIN32_AVAILABLE = False

try:
    from send2trash import send2trash; SEND2TRASH_AVAILABLE = True
except ImportError:
    SEND2TRASH_AVAILABLE = False

try:
    import plaid_service; PLAID_AVAILABLE = True
except ImportError:
    PLAID_AVAILABLE = False

try:
    import statements_service; STATEMENTS_AVAILABLE = True
except ImportError:
    STATEMENTS_AVAILABLE = False

try:
    import gmail_service; GMAIL_AVAILABLE = bool(gmail_service.GMAIL_AVAILABLE)
except ImportError:
    GMAIL_AVAILABLE = False

try:
    import outlook_service; OUTLOOK_AVAILABLE = bool(outlook_service.OUTLOOK_AVAILABLE)
except ImportError:
    OUTLOOK_AVAILABLE = False

try:
    import telegram_bridge; TELEGRAM_AVAILABLE = True
except ImportError:
    TELEGRAM_AVAILABLE = False

import bot_events
import telegram_common
import jarvis_admin
import jarvis_cpu_alerts
import jarvis_improvement
import jarvis_security
import employees
import memory_store

try:
    import browser_control; BROWSER_CONTROL_AVAILABLE = True
except ImportError:
    BROWSER_CONTROL_AVAILABLE = False

import network_watch
import speedtest_service
import synology_service
import tailscale_service


HOME = os.path.expanduser("~")
CLAUDE_CLI = shutil.which("claude") or os.path.join(
    HOME, ".local", "bin", "claude.exe" if IS_WINDOWS else "claude")
OLLAMA_URL = "http://localhost:11434/api/generate"
VISION_MODEL = "llava"

# ---- Durable cross-process state -----------------------------------------
# Every tool call happens inside a fresh, short-lived MCP-server subprocess
# (Claude spawns one per command, it exits when that command finishes), so
# nothing kept in a plain module-level variable survives between commands.
# Anything that needs to persist (eyes on/off, reminders, notes) is kept in
# small JSON files under ~/.jarvis instead.
JARVIS_DIR = os.path.join(HOME, ".jarvis")
STATE_PATH = os.path.join(JARVIS_DIR, "state.json")
REMINDERS_PATH = os.path.join(JARVIS_DIR, "reminders.json")
NOTES_PATH = os.path.join(JARVIS_DIR, "notes.json")
CREATIONS_LOG_PATH = os.path.join(JARVIS_DIR, "creations_log.json")
_CREATIONS_LOG_MAX = 200


def _ensure_jarvis_dir():
    os.makedirs(JARVIS_DIR, exist_ok=True)


def _load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path, data):
    _ensure_jarvis_dir()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def _get_state():
    return _load_json(STATE_PATH, {})


def _set_state(key, value):
    state = _get_state()
    state[key] = value
    _save_json(STATE_PATH, state)


# ---- Conversation continuity -----------------------------------------
# brain.py runs every voice command as a brand-new, otherwise-stateless
# Claude Code CLI call (see its --no-session-persistence note) -- with
# nothing else in place, Jarvis would forget "it"/"that"/"like I said"
# the instant one command finished, even within the same sitting, and
# obviously across full app restarts. This on-disk log is the fix: brain.py
# records each (command, reply) pair here and reads the last few back in
# as context for the next command, so continuity survives both between
# commands and across separate app sessions.
CONVERSATION_LOG_PATH = os.path.join(JARVIS_DIR, "conversation_log.json")
_CONVERSATION_LOG_MAX_TURNS = 40       # how many turns are kept on disk
_CONVERSATION_CONTEXT_MAX_TURNS = 6    # how many of those go back into the next prompt
_CONVERSATION_FIELD_MAX_CHARS = 400    # cap per-field so the log can't grow unbounded


def record_conversation_turn(command: str, reply: str):
    """Not an MCP tool -- called directly by brain.py after each command
    finishes, so the next command (even in a future app session) can be
    given a short recap of what was just discussed."""
    if not command or not reply:
        return
    log = _load_json(CONVERSATION_LOG_PATH, [])
    log.append({
        "command": command[:_CONVERSATION_FIELD_MAX_CHARS],
        "reply": reply[:_CONVERSATION_FIELD_MAX_CHARS],
        "ts": time.time(),
    })
    _save_json(CONVERSATION_LOG_PATH, log[-_CONVERSATION_LOG_MAX_TURNS:])


def recent_conversation_context(max_turns: int = _CONVERSATION_CONTEXT_MAX_TURNS) -> str:
    """Not an MCP tool -- called directly by brain.py to build a short recap
    of recent turns (possibly from a previous session) to prepend to the
    next command."""
    log = _load_json(CONVERSATION_LOG_PATH, [])
    if not log:
        return ""
    recent = log[-max_turns:]
    lines = [f"User: {t.get('command', '')}\nYou: {t.get('reply', '')}" for t in recent]
    return "Recent conversation for context (most recent last):\n" + "\n".join(lines)


# ---- Safe directories -------------------------------------------------
# Every filesystem tool is jailed to this fixed set of folders. Never widen
# this to an arbitrary path -- add a new named key instead.
_SAFE_DIRS = {
    "desktop": os.path.join(HOME, "Desktop"),
    "documents": os.path.join(HOME, "Documents"),
    "downloads": os.path.join(HOME, "Downloads"),
    "pictures": os.path.join(HOME, "Pictures"),
    "music": os.path.join(HOME, "Music"),
    "videos": os.path.join(HOME, "Videos"),
    "creations": os.path.join(HOME, "JarvisCreations"),
}


def _sanitize_name(name):
    name = (name or "").strip().strip('"').strip("'")
    name = re.sub(r'[\\/:*?"<>|]', "", name).replace("..", "")
    return name.strip()


def _resolve_safe_path(name, location):
    loc = (location or "desktop").strip().lower()
    if loc not in _SAFE_DIRS:
        return None, f"I can only work in {', '.join(_SAFE_DIRS)} sir."
    clean = _sanitize_name(name)
    if not clean:
        return None, "I didn't catch a valid name for that sir."
    base = _SAFE_DIRS[loc]
    os.makedirs(base, exist_ok=True)
    target = os.path.join(base, clean)
    # belt-and-suspenders: resolved path must still live inside the safe dir
    if os.path.commonpath([os.path.abspath(target), os.path.abspath(base)]) != os.path.abspath(base):
        return None, "That name isn't allowed sir."
    return target, None


# ================================================================ FILESYSTEM
def create_folder(name: str, location: str = "desktop") -> str:
    """Create a new empty folder. location must be one of: desktop, documents,
    downloads, pictures, music, videos, creations."""
    target, err = _resolve_safe_path(name, location)
    if err:
        return err
    os.makedirs(target, exist_ok=True)
    return f"Created the folder {os.path.basename(target)} in {location} sir."


def create_file(name: str, location: str = "desktop", content: str = "") -> str:
    """Create a new text file, optionally with content already written into it.
    location must be one of: desktop, documents, downloads, pictures, music,
    videos, creations."""
    target, err = _resolve_safe_path(name, location)
    if err:
        return err
    if not os.path.splitext(target)[1]:
        target += ".txt"
    with open(target, "w", encoding="utf-8") as f:
        f.write(content or "")
    return f"Created {os.path.basename(target)} in {location} sir."


def list_directory(location: str = "desktop") -> str:
    """List the files and folders inside one of the safe directories: desktop,
    documents, downloads, pictures, music, videos, creations."""
    loc = (location or "desktop").strip().lower()
    if loc not in _SAFE_DIRS:
        return f"I can only look in {', '.join(_SAFE_DIRS)} sir."
    base = _SAFE_DIRS[loc]
    if not os.path.isdir(base):
        return f"Nothing there yet sir."
    entries = sorted(os.listdir(base))[:50]
    if not entries:
        return f"Your {location} is empty sir."
    return f"In {location}: " + ", ".join(entries)


def read_text_file(name: str, location: str = "desktop") -> str:
    """Read the contents of a small text file back (truncated if long).
    location must be one of: desktop, documents, downloads, pictures, music,
    videos, creations."""
    target, err = _resolve_safe_path(name, location)
    if err:
        return err
    if not os.path.isfile(target):
        return f"I couldn't find {name} in {location} sir."
    try:
        with open(target, "r", encoding="utf-8", errors="replace") as f:
            data = f.read(4000)
        return data if data.strip() else "That file is empty sir."
    except Exception as e:
        return f"I couldn't read that file sir: {e}"


def delete_item(name: str, location: str = "desktop") -> str:
    """Move a file or folder to the Recycle Bin (never a permanent delete).
    location must be one of: desktop, documents, downloads, pictures, music,
    videos, creations."""
    target, err = _resolve_safe_path(name, location)
    if err:
        return err
    if not os.path.exists(target):
        return f"I couldn't find {name} in {location} sir."
    if not SEND2TRASH_AVAILABLE:
        return "Recycle-bin support isn't installed sir, I won't delete that."
    try:
        send2trash(target)
        return f"Sent {os.path.basename(target)} to the Recycle Bin sir."
    except Exception as e:
        return f"I couldn't delete that sir: {e}"


# ================================================================ APPS
# Windows: alias -> the name os.startfile()/taskkill expects.
_APP_ALIASES_WIN = {
    "notepad": "notepad", "calculator": "calc", "calc": "calc",
    "file explorer": "explorer", "explorer": "explorer",
    "task manager": "taskmgr", "paint": "mspaint",
    "control panel": "control", "settings": "ms-settings:",
    "spotify": "spotify", "vs code": "code", "visual studio code": "code",
    "word": "winword", "excel": "excel", "chrome": "chrome",
    "command prompt": "cmd", "terminal": "wt", "firefox": "firefox",
    "edge": "msedge", "discord": "discord", "steam": "steam",
    "slack": "slack", "zoom": "zoom", "photos": "ms-photos:",
    "snipping tool": "snippingtool", "camera": "microsoft.windows.camera:",
    "powershell": "powershell", "windows powershell": "powershell",
    "pwsh": "pwsh", "powershell 7": "pwsh", "powershell core": "pwsh",
}
_APP_IMAGE_NAMES_WIN = {
    "notepad": "notepad.exe", "calc": "CalculatorApp.exe", "explorer": None,  # never kill explorer
    "taskmgr": "Taskmgr.exe", "mspaint": "mspaint.exe", "spotify": "Spotify.exe",
    "code": "Code.exe", "winword": "WINWORD.EXE", "excel": "EXCEL.EXE",
    "chrome": "chrome.exe", "cmd": "cmd.exe", "wt": "WindowsTerminal.exe",
    "firefox": "firefox.exe", "msedge": "msedge.exe", "discord": "Discord.exe",
    "steam": "steam.exe", "slack": "slack.exe", "zoom": "Zoom.exe",
    "snippingtool": "SnippingTool.exe", "powershell": "powershell.exe",
    "pwsh": "pwsh.exe",
}

# Linux: alias -> the actual binary name to launch/pkill. Distros vary widely
# in which terminal emulator ships by default (GNOME ships ptyxis or
# gnome-terminal depending on version, KDE ships konsole, minimal installs
# often only have xterm), so "terminal"/"command prompt" resolve dynamically
# via _resolve_linux_terminal() instead of a single hardcoded binary. Direct
# aliases for the well-known emulators are kept too, so "open konsole" still
# works even when it isn't the default. gedit was renamed gnome-text-editor
# upstream. spotify/vs code/discord/steam/slack/zoom aren't installed on this
# machine but are left as curated placeholders in case they're added later --
# launch_app already reports "doesn't appear to be installed" cleanly if the
# binary isn't found.
_TERMINAL_SENTINEL = "__linux_terminal__"
_LINUX_TERMINAL_CANDIDATES = [
    "ptyxis", "gnome-terminal", "konsole", "xfce4-terminal", "terminator",
    "tilix", "alacritty", "kitty", "xterm", "x-terminal-emulator",
]
_APP_ALIASES_LINUX = {
    "terminal": _TERMINAL_SENTINEL, "command prompt": _TERMINAL_SENTINEL,
    "gnome-terminal": "gnome-terminal", "gnome terminal": "gnome-terminal",
    "konsole": "konsole", "xterm": "xterm",
    "file explorer": "nautilus", "files": "nautilus", "explorer": "nautilus",
    "text editor": "gnome-text-editor", "gedit": "gnome-text-editor",
    "calculator": "gnome-calculator", "calc": "gnome-calculator",
    "settings": "gnome-control-center", "spotify": "spotify",
    "vs code": "code", "visual studio code": "code",
    "chrome": "google-chrome", "firefox": "firefox", "brave": "brave",
    "discord": "discord", "steam": "steam", "slack": "slack", "zoom": "zoom",
    # PowerShell Core (pwsh) is cross-platform -- "powershell" is the natural
    # spoken alias, "pwsh" the literal binary/package name. It's a CLI, not a
    # GUI app, so it's exempt from the display-server check below.
    "powershell": "pwsh", "pwsh": "pwsh", "windows powershell": "pwsh",
}
# Binaries that don't need an X11/Wayland display to run at all (pure CLI).
_LINUX_NO_DISPLAY_REQUIRED = {"pwsh"}

_APP_ALIASES = _APP_ALIASES_WIN if IS_WINDOWS else _APP_ALIASES_LINUX
_APP_IMAGE_NAMES = _APP_IMAGE_NAMES_WIN if IS_WINDOWS else None


def _resolve_linux_terminal() -> str | None:
    """Return the first installed terminal emulator binary from the
    candidate list, or None if none of them are on PATH."""
    for candidate in _LINUX_TERMINAL_CANDIDATES:
        if shutil.which(candidate):
            return candidate
    return None


def _linux_display_available() -> bool:
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def launch_app(name: str) -> str:
    """Launch a known desktop application by common name (e.g. notepad,
    calculator, file explorer, chrome, spotify, discord, vs code, terminal,
    gnome-terminal, konsole, xterm, powershell/pwsh)."""
    key = re.sub(r'^(the|a|an)\s+', '', (name or "").strip().lower())
    exe = _APP_ALIASES.get(key)
    if not exe:
        return f"I don't have {name} in my known app list sir. Try opening it by hand once and I'll remember it next time you ask me to add it."
    if not IS_WINDOWS and exe == _TERMINAL_SENTINEL:
        exe = _resolve_linux_terminal()
        if not exe:
            return (f"I couldn't find a terminal emulator installed sir -- "
                     f"tried {', '.join(_LINUX_TERMINAL_CANDIDATES)}.")
    if not IS_WINDOWS and exe not in _LINUX_NO_DISPLAY_REQUIRED and not _linux_display_available():
        return (f"I can't open {name} sir -- no display server is available in this "
                 f"session (neither $DISPLAY nor $WAYLAND_DISPLAY is set).")
    try:
        if IS_WINDOWS:
            os.startfile(exe)
        else:
            subprocess.Popen([exe], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              start_new_session=True)
        return f"Opening {name} sir."
    except FileNotFoundError:
        return f"{name} doesn't appear to be installed sir."
    except OSError as e:
        # os.startfile() on Windows (and Popen on Linux) raise OSError for
        # failures beyond "not installed" too -- e.g. a PowerShell/Terminal
        # install that's present but broken, permission denied, or (on
        # Windows) no handler registered for a URI-style target like
        # ms-settings:. Surface the real reason instead of always blaming
        # "not installed", which used to mask genuine, diagnosable failures.
        return f"I couldn't open {name} sir: {e.strerror or e}"
    except Exception as e:
        return f"I couldn't open {name} sir: {e}"


def _protected_browser_pids() -> set:
    """OS process IDs (if any) belonging to the dedicated Jarvis automation
    browser (browser_control.py) right now. Playwright's bundled Chromium
    binary is literally named chrome.exe on Windows -- the same image name as
    the user's ordinary, separate system Chrome -- so a plain image-name kill
    can't tell them apart on its own. close_app() consults this so "close
    chrome" only ever closes the user's *other* chrome.exe processes, never
    silently killing an in-progress play_youtube out from under Playwright."""
    if not BROWSER_CONTROL_AVAILABLE:
        return set()
    try:
        return browser_control.owned_pids()
    except Exception:
        return set()


def close_app(name: str) -> str:
    """Close a known desktop application by common name, gracefully (never a
    forced kill). Restricted to the same curated app list as launch_app."""
    key = re.sub(r'^(the|a|an)\s+', '', (name or "").strip().lower())
    exe = _APP_ALIASES.get(key)
    if not IS_WINDOWS and exe == _TERMINAL_SENTINEL:
        exe = _resolve_linux_terminal()
        if not exe:
            return (f"I couldn't find an installed terminal emulator to close sir -- "
                     f"tried {', '.join(_LINUX_TERMINAL_CANDIDATES)}.")
    # On Linux the binary name doubles as the pkill target; on Windows the
    # process image name is a separate lookup.
    image = (_APP_IMAGE_NAMES.get(exe) if exe else None) if IS_WINDOWS else exe
    if not image:
        return f"I won't close {name} sir -- it's not in my curated list of apps I'm allowed to close."
    protected = _protected_browser_pids()
    try:
        if IS_WINDOWS:
            if not protected:
                subprocess.run(["taskkill", "/IM", image], capture_output=True, timeout=10)
            else:
                # Same image name (chrome.exe) can legitimately belong to
                # both the user's regular Chrome and Jarvis's own automation
                # window -- fall back to killing every matching PID except
                # the protected ones individually, instead of the blanket
                # /IM sweep that can't distinguish them.
                listing = subprocess.run(
                    ["tasklist", "/FI", f"IMAGENAME eq {image}", "/FO", "CSV", "/NH"],
                    capture_output=True, text=True, timeout=10,
                )
                for line in listing.stdout.splitlines():
                    fields = [f.strip('"') for f in line.split('","')]
                    if len(fields) < 2:
                        continue
                    try:
                        pid = int(fields[1])
                    except ValueError:
                        continue
                    if pid not in protected:
                        subprocess.run(["taskkill", "/PID", str(pid)], capture_output=True, timeout=10)
        else:
            # taskkill /IM matches by image name regardless of path or args;
            # replicate that on Linux instead of a bare `pkill -x image`,
            # which (a) matches against the kernel's 15-char-truncated comm
            # field, so multi-word names like gnome-calculator never match,
            # and (b) requires zero args, which breaks GNOME apps that are
            # already running and get re-invoked with a subcommand (e.g.
            # `gnome-control-center bluetooth` for an already-open Settings
            # window). Match the full cmdline against an optional path
            # prefix + the image name + optional trailing args instead.
            pattern = rf"(.*/)?{re.escape(image)}( .*)?"
            if not protected:
                subprocess.run(["pkill", "-f", "-x", pattern], capture_output=True, timeout=10)
            else:
                listing = subprocess.run(["pgrep", "-f", "-x", pattern],
                                          capture_output=True, text=True, timeout=10)
                for line in listing.stdout.splitlines():
                    line = line.strip()
                    if not line.isdigit():
                        continue
                    pid = int(line)
                    if pid not in protected:
                        try:
                            os.kill(pid, signal.SIGTERM)
                        except OSError:
                            pass
        return f"Closing {name} sir."
    except Exception as e:
        return f"I couldn't close {name} sir: {e}"


# ================================================================ MEDIA CONTROL
_VK_MEDIA_PLAY_PAUSE, _VK_MEDIA_NEXT, _VK_MEDIA_PREV = 0xB3, 0xB0, 0xB1
_VK_VOLUME_UP, _VK_VOLUME_DOWN, _VK_VOLUME_MUTE = 0xAF, 0xAE, 0xAD
_MEDIA_KEYS = {
    "play_pause": (_VK_MEDIA_PLAY_PAUSE, "Toggling playback sir."),
    "next": (_VK_MEDIA_NEXT, "Skipping sir."),
    "previous": (_VK_MEDIA_PREV, "Going back sir."),
    "volume_up": (_VK_VOLUME_UP, "Turning it up sir."),
    "volume_down": (_VK_VOLUME_DOWN, "Turning it down sir."),
    "mute": (_VK_VOLUME_MUTE, "Muting sir."),
}


def media_control(action: str) -> str:
    """Control whatever currently owns system media playback (a YouTube tab,
    Spotify, etc). action must be one of: play_pause, next, previous,
    volume_up, volume_down, mute."""
    entry = _MEDIA_KEYS.get((action or "").strip().lower())
    if not entry or not WIN32_AVAILABLE:
        return "I couldn't control playback sir."
    key, reply = entry
    try:
        win32api.keybd_event(key, 0, 0, 0)
        win32api.keybd_event(key, 0, 2, 0)
    except Exception:
        return "I couldn't control playback sir."
    return reply


# ================================================================ BROWSER / WEB
_SITE_ALIASES = {
    "youtube": "youtube.com", "gmail": "mail.google.com", "email": "mail.google.com",
    "github": "github.com", "google": "google.com", "amazon": "amazon.com",
    "netflix": "netflix.com", "spotify": "open.spotify.com", "reddit": "reddit.com",
    "twitter": "x.com", "x": "x.com", "facebook": "facebook.com",
    "instagram": "instagram.com", "linkedin": "linkedin.com", "wikipedia": "wikipedia.org",
    "maps": "maps.google.com", "news": "news.google.com", "weather": "weather.com",
    "twitch": "twitch.tv", "chatgpt": "chat.openai.com",
}


def _resolve_url(target):
    target = (target or "").strip().lower().strip(". ")
    if not target:
        return "https://www.google.com"
    if target in _SITE_ALIASES:
        return "https://" + _SITE_ALIASES[target]
    if re.match(r'^[a-z0-9.-]+\.[a-z]{2,}(/.*)?$', target):
        return target if target.startswith("http") else f"https://{target}"
    return "https://www.google.com/search?q=" + urllib.parse.quote_plus(target)


# ---- Threat scanning (JarSecurity) -------------------------------------
# Gates every URL Jarvis's browser actually navigates to (open_website,
# search_web -- both funnel through _guarded_open_url below) as well as the
# on-demand scan_url_safety tool. Report/block-only: never touches, kills,
# or "cleans" anything, same philosophy as the rest of run_security_check().
# Deliberately just link scanning, not a shell-command scanner -- every
# command Jarvis can ever run is already a fixed, curated argv (see
# _DIAGNOSTIC_COMMANDS above), so there is no free-form "command" input to
# scan in the first place.
_THREAT_BLOCKLIST_PATH = os.path.join(JARVIS_DIR, "security_blocklist.txt")
# Free, no-API-key-required public feed of active malware-hosting domains
# (abuse.ch's URLhaus project) -- refreshed at most every _THREAT_BLOCKLIST_MAX_AGE
# seconds so a stale/unreachable feed never blocks navigation on its own.
_THREAT_BLOCKLIST_URL = "https://urlhaus.abuse.ch/downloads/hostfile/"
_THREAT_BLOCKLIST_MAX_AGE = 6 * 3600
# Curated extensions that mean a URL points straight at an executable or
# script rather than a normal page -- the classic "click this link, it runs
# on your machine" delivery format. Same curated-substring style as
# _SUSPICIOUS_PROCESS_NAMES above, not an exhaustive/generic block-everything
# rule.
_DANGEROUS_URL_EXTENSIONS = (
    ".exe", ".msi", ".scr", ".pif", ".bat", ".cmd", ".vbs", ".vbe",
    ".js", ".jse", ".wsf", ".ps1", ".psm1", ".jar", ".apk",
)
_threat_blocklist_cache = {"hosts": frozenset(), "mtime": None}


def _parse_hostfile(text):
    hosts = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            hosts.add(parts[1].lower())
    return hosts


def refresh_threat_blocklist(force: bool = False) -> dict:
    """Best-effort refresh of the local malicious-host cache from URLhaus's
    free public host list -- used by the URL threat check below to flag
    known malware-hosting domains before Jarvis ever navigates to them.
    Skipped (no network call) if the cache is already fresh unless
    force=True. Never raises -- a network hiccup just means the existing
    cache (or an empty one) keeps being used until the next sweep. Called
    automatically as part of run_security_check()'s scheduled sweep."""
    try:
        if not force and os.path.exists(_THREAT_BLOCKLIST_PATH):
            age = time.time() - os.path.getmtime(_THREAT_BLOCKLIST_PATH)
            if age < _THREAT_BLOCKLIST_MAX_AGE:
                return {"ok": True, "refreshed": False}
        r = requests.get(_THREAT_BLOCKLIST_URL, timeout=20)
        r.raise_for_status()
        hosts = _parse_hostfile(r.text)
        if not hosts:
            return {"ok": False, "error": "empty blocklist response"}
        _ensure_jarvis_dir()
        tmp = _THREAT_BLOCKLIST_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(sorted(hosts)))
        os.replace(tmp, _THREAT_BLOCKLIST_PATH)
        _threat_blocklist_cache["hosts"] = frozenset(hosts)
        _threat_blocklist_cache["mtime"] = os.path.getmtime(_THREAT_BLOCKLIST_PATH)
        return {"ok": True, "refreshed": True, "count": len(hosts)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _load_threat_blocklist_hosts():
    try:
        mtime = os.path.getmtime(_THREAT_BLOCKLIST_PATH)
    except OSError:
        return _threat_blocklist_cache["hosts"]
    if _threat_blocklist_cache["mtime"] != mtime:
        try:
            with open(_THREAT_BLOCKLIST_PATH, "r", encoding="utf-8") as f:
                _threat_blocklist_cache["hosts"] = frozenset(l.strip() for l in f if l.strip())
            _threat_blocklist_cache["mtime"] = mtime
        except Exception:
            pass
    return _threat_blocklist_cache["hosts"]


def _url_threat_verdict(url):
    """(blocked: bool, reason: str) for a single URL -- raw-IP host,
    punycode/homograph host, a direct link to a risky executable/script file
    type, or a host on the URLhaus blocklist. No network call unless the
    local blocklist cache is stale (see refresh_threat_blocklist)."""
    try:
        parsed = urllib.parse.urlsplit(url)
    except Exception:
        return False, ""
    host = (parsed.hostname or "").lower()
    if not host:
        return False, ""
    try:
        ipaddress.ip_address(host)
        return True, "that's a raw IP address rather than a normal domain name -- a common phishing/malware-hosting trick"
    except ValueError:
        pass
    if "xn--" in host:
        return True, "that domain uses punycode, often used to impersonate a trusted site with lookalike characters"
    path = (parsed.path or "").lower()
    for ext in _DANGEROUS_URL_EXTENSIONS:
        if path.endswith(ext):
            return True, f"that link points straight at a {ext} file, a common malware delivery format"
    refresh_threat_blocklist()
    hosts = _load_threat_blocklist_hosts()
    bare = host[4:] if host.startswith("www.") else host
    if host in hosts or bare in hosts:
        return True, "that domain is on the URLhaus known-malware-hosting blocklist"
    return False, ""


def scan_url_safety(url: str) -> str:
    """Read-only safety scan of a single link -- checks for a raw-IP host, a
    punycode/homograph domain, a direct link to a risky executable/script
    file type, or a host on the URLhaus known-malware blocklist. Never
    navigates anywhere itself -- open_website/search_web already run this
    same check automatically before opening anything. Use this whenever the
    user pastes a suspicious link and asks "is this safe"/"scan this link"."""
    url = (url or "").strip()
    if not url:
        return "What link should I scan sir?"
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"
    blocked, reason = _url_threat_verdict(url)
    if blocked:
        return f"I'd block that one sir -- {reason}."
    return "That link looks clean sir -- no known threats or risky patterns detected."


def _guarded_open_url(url):
    """Every browser_control.open_url() call in this module goes through
    here first -- blocks known-malicious/high-risk links before the browser
    ever navigates to them instead of just reporting on them after the
    fact."""
    blocked, reason = _url_threat_verdict(url)
    if blocked:
        _log_security_event({"blocked_url": url, "reason": reason})
        return f"I blocked that link sir -- {reason}."
    return browser_control.open_url(url)


def open_website(target: str) -> str:
    """Open a website in the dedicated, visible Jarvis browser window and
    bring it to the front -- a known site name (youtube, gmail, github,
    netflix, ...), a raw domain, or free text (which becomes a Google
    search). Reuses the same persistent, Jarvis-controlled Chromium window as
    play_youtube so it can be reliably re-focused, falling back to the OS
    default browser only if that dedicated window is unavailable. For
    actually searching-and-playing something on YouTube, use play_youtube
    instead. The target link is screened for known-malicious/high-risk
    patterns first (see scan_url_safety) and blocked rather than opened if
    it's flagged."""
    url = _resolve_url(target)
    if BROWSER_CONTROL_AVAILABLE:
        return _guarded_open_url(url)
    label = target if target in _SITE_ALIASES or "." in (target or "") else f"a search for {target}"
    try:
        if not webbrowser.open(url):
            return f"I couldn't find a browser to open {label} with sir."
        return f"Opening {label} sir."
    except Exception as e:
        return f"I couldn't open that sir: {e}"


def search_web(query: str) -> str:
    """Open a Google search results page for the given query in the
    dedicated Jarvis browser window."""
    url = "https://www.google.com/search?q=" + urllib.parse.quote_plus(query or "")
    if BROWSER_CONTROL_AVAILABLE:
        return _guarded_open_url(url)
    try:
        if not webbrowser.open(url):
            return "I couldn't find a browser to search with sir."
        return f"Searching for {query} sir."
    except Exception as e:
        return f"I couldn't search for that sir: {e}"


def read_webpage() -> str:
    """Read back the title and visible text of whatever page is currently
    open in the dedicated Jarvis browser window (the one open_website /
    play_youtube control) -- use this to summarize or answer questions about
    a page that was just opened."""
    if not BROWSER_CONTROL_AVAILABLE:
        return "I can't read browser pages sir -- the browser control module isn't available."
    return browser_control.read_page()


def play_youtube(query: str) -> str:
    """Search YouTube and actually start playing the first matching video (a
    song, an artist, a video title) in a dedicated automated browser window --
    not just opening a search page."""
    if not BROWSER_CONTROL_AVAILABLE:
        return open_website(f"https://www.youtube.com/results?search_query={urllib.parse.quote_plus(query or '')}")
    try:
        return browser_control.play_youtube(query or "")
    except Exception as e:
        return f"I couldn't play that on YouTube sir: {e}"


def youtube_control(action: str) -> str:
    """Control the dedicated YouTube automation browser window specifically
    (play_pause, next -- meaning restart the current video, mute, stop --
    always pauses, never toggles back to playing). For system-wide media keys
    (works on Spotify etc too) use media_control instead."""
    if not BROWSER_CONTROL_AVAILABLE:
        return "The YouTube browser isn't available sir."
    try:
        return browser_control.control(action or "")
    except Exception as e:
        return f"I couldn't do that sir: {e}"


def close_browser() -> str:
    """Close the dedicated Jarvis browser window (used by open_website,
    play_youtube, search_web). Safe to call any time -- the next open/play
    request transparently opens a fresh window."""
    if not BROWSER_CONTROL_AVAILABLE:
        return "The browser control module isn't available sir."
    try:
        return browser_control.close()
    except Exception as e:
        return f"I couldn't close the browser sir: {e}"


# ================================================================ WEATHER
_WMO_SPOKEN = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "foggy", 48: "foggy",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain",
    71: "light snow", 73: "snow", 75: "heavy snow",
    80: "light showers", 81: "showers", 82: "heavy showers",
    95: "thunderstorms",
}


def get_weather(city: str) -> str:
    """Look up the current weather for any city by name."""
    if not city:
        return "Which city sir?"
    try:
        geo = requests.get("https://geocoding-api.open-meteo.com/v1/search",
                            params={"name": city, "count": 1}, timeout=8).json()
        results = geo.get("results")
        if not results:
            return f"I couldn't find {city} sir."
        lat, lon, name = results[0]["latitude"], results[0]["longitude"], results[0].get("name", city)
        w = requests.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": lat, "longitude": lon,
            "current": "temperature_2m,weather_code,apparent_temperature",
            "temperature_unit": "fahrenheit",
        }, timeout=8).json()["current"]
        cond = _WMO_SPOKEN.get(w["weather_code"], "unclear conditions")
        return (f"It's currently {round(w['temperature_2m'])} degrees in {name} sir, "
                f"feels like {round(w['apparent_temperature'])}, with {cond}.")
    except Exception as e:
        return f"I couldn't reach the weather service sir: {e}"


def show_map(location: str) -> str:
    """Pull up a location on the desktop HUD's map widget -- geocodes the
    name (same free Open-Meteo geocoding get_weather uses, no API key)
    and pushes it to the HUD over the shared event bus every other
    watcher/tool publishes to. A rare, meaningful, one-shot event (nothing
    like the phone camera's high-frequency streams), so unlike those this
    goes through bot_events normally -- it's fine for it to also show up
    in the Live Activity feed like any other tool call."""
    if not location:
        return "What location sir?"
    try:
        geo = requests.get("https://geocoding-api.open-meteo.com/v1/search",
                            params={"name": location, "count": 1}, timeout=8).json()
        results = geo.get("results")
        if not results:
            return f"I couldn't find {location} sir."
        lat, lon = results[0]["latitude"], results[0]["longitude"]
        name = results[0].get("name", location)
        country = results[0].get("country", "")
        bot_events.publish("show_map", {"location": name, "country": country, "lat": lat, "lon": lon})
        return f"Pulling up {name} sir."
    except Exception as e:
        return f"I couldn't find that location sir: {e}"


def show_weather_radar(city: str = "") -> str:
    """Switch the desktop HUD's map widget into live precipitation radar
    mode (RainViewer's free public radar tiles, no API key), optionally
    re-centered on a named city first -- leave city blank to just toggle
    radar on wherever the map is already showing."""
    lat = lon = None
    name = city or None
    if city:
        try:
            geo = requests.get("https://geocoding-api.open-meteo.com/v1/search",
                                params={"name": city, "count": 1}, timeout=8).json()
            results = geo.get("results")
            if results:
                lat, lon = results[0]["latitude"], results[0]["longitude"]
                name = results[0].get("name", city)
        except Exception:
            pass
    bot_events.publish("show_weather_radar", {"location": name, "lat": lat, "lon": lon})
    return f"Showing the radar over {name} sir." if name else "Showing the radar sir."


# ================================================================ EMAIL (draft only, never sends)
def draft_email(recipient: str = "", subject: str = "", body: str = "", provider: str = "gmail") -> str:
    """Create a real email draft -- visible in the Drafts folder, ready
    for the user to review and send themselves. NEVER sends automatically;
    this is a hard constraint (see gmail_service/outlook_service module
    docstrings for how the OAuth scopes back that up structurally, not
    just by convention -- neither integration is ever granted a send
    scope). `provider` is "gmail", "outlook", or "both" (creates the same
    draft in each configured provider). Write the full email body
    yourself before calling this -- don't leave `body` empty unless the
    user explicitly wants a blank draft."""
    provider = (provider or "gmail").strip().lower()
    targets = ["gmail", "outlook"] if provider == "both" else [provider]
    results = []
    for p in targets:
        if p == "gmail":
            if not GMAIL_AVAILABLE:
                results.append("Gmail isn't connected sir -- see the README's email setup section.")
                continue
            r = gmail_service.create_draft(recipient, subject, body)
            results.append("Gmail draft created sir." if r.get("ok") else f"Gmail draft failed sir: {r.get('error')}")
        elif p == "outlook":
            if not OUTLOOK_AVAILABLE:
                results.append("Outlook isn't connected sir -- see the README's email setup section.")
                continue
            r = outlook_service.create_draft(recipient, subject, body)
            results.append("Outlook draft created sir." if r.get("ok") else f"Outlook draft failed sir: {r.get('error')}")
        else:
            results.append(f"I don't recognize \"{p}\" as an email provider sir -- gmail, outlook, or both.")
    return " ".join(results)


# ================================================================ CALENDAR (real events, both providers)
def create_calendar_event(title: str, start_iso: str, end_iso: str = "",
                           duration_minutes: float = 30, description: str = "",
                           attendees: str = "", provider: str = "gmail") -> str:
    """Create a real calendar event -- unlike draft_email, this genuinely
    goes on the calendar (same directness as set_reminder/add_note, since
    a calendar event is trivially reversible -- the user can just delete
    it -- unlike sending an email). start_iso (and end_iso, if given) must
    be full ISO 8601 datetimes, e.g. "2026-08-29T15:00:00" -- resolve any
    relative time ("tomorrow at 3pm") to a real date yourself using the
    current date/time you were given, don't pass one through literally.
    If end_iso is omitted, the event runs `duration_minutes` (default 30)
    after start_iso. `attendees` is a comma-separated list of email
    addresses (optional). `provider` is "gmail", "outlook", or "both"."""
    if not title or not start_iso:
        return "I need at least a title and a start time sir."
    if not end_iso:
        try:
            start_dt = datetime.fromisoformat(start_iso)
            end_iso = (start_dt + timedelta(minutes=float(duration_minutes or 30))).isoformat()
        except Exception:
            return f"I couldn't parse \"{start_iso}\" as a date/time sir -- use ISO 8601, e.g. 2026-08-29T15:00:00."

    provider = (provider or "gmail").strip().lower()
    targets = ["gmail", "outlook"] if provider == "both" else [provider]
    results = []
    for p in targets:
        if p == "gmail":
            if not GMAIL_AVAILABLE:
                results.append("Gmail Calendar isn't connected sir -- see the README's email setup section.")
                continue
            r = gmail_service.create_calendar_event(title, start_iso, end_iso, description, attendees)
            results.append("Added to your Google Calendar sir." if r.get("ok") else f"Google Calendar failed sir: {r.get('error')}")
        elif p == "outlook":
            if not OUTLOOK_AVAILABLE:
                results.append("Outlook Calendar isn't connected sir -- see the README's email setup section.")
                continue
            r = outlook_service.create_calendar_event(title, start_iso, end_iso, description, attendees)
            results.append("Added to your Outlook Calendar sir." if r.get("ok") else f"Outlook Calendar failed sir: {r.get('error')}")
        else:
            results.append(f"I don't recognize \"{p}\" as a calendar provider sir -- gmail, outlook, or both.")
    return " ".join(results)


def search_email(query: str = "", days: int = 7, provider: str = "gmail") -> str:
    """Search recent email -- real subjects/senders/snippets, not a
    placeholder. `query` is free text (matched against subject/sender/body
    the way each provider's own search works -- Gmail search syntax like
    "from:jane" or "is:unread" works directly for provider="gmail");
    leave blank to just list the most recent mail. `days` bounds how far
    back to look. `provider` is "gmail", "outlook", or "both". Use
    read_email with a result's id to get the full body of one message."""
    days = max(1, int(days or 7))
    provider = (provider or "gmail").strip().lower()
    targets = ["gmail", "outlook"] if provider == "both" else [provider]
    results = []
    for p in targets:
        if p == "gmail":
            if not GMAIL_AVAILABLE:
                results.append("Gmail isn't connected sir.")
                continue
            try:
                q = f"newer_than:{days}d" + (f" {query}" if query else "")
                msgs = gmail_service.list_recent_messages(query=q, max_results=10)
                if not msgs:
                    results.append("No matching Gmail messages sir.")
                else:
                    lines = [f'[{m["id"]}] {m["from"]}: "{m["subject"]}" -- {m["snippet"]}' for m in msgs]
                    results.append("Gmail:\n" + "\n".join(lines))
            except Exception as e:
                results.append(f"Gmail search failed sir: {e}")
        elif p == "outlook":
            if not OUTLOOK_AVAILABLE:
                results.append("Outlook isn't connected sir.")
                continue
            try:
                since = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
                msgs = outlook_service.list_recent_messages(since, max_results=10)
                if query:
                    ql = query.lower()
                    msgs = [m for m in msgs if ql in m["subject"].lower() or ql in m["from"].lower() or ql in m["snippet"].lower()]
                if not msgs:
                    results.append("No matching Outlook messages sir.")
                else:
                    lines = [f'[{m["id"]}] {m["from"]}: "{m["subject"]}" -- {m["snippet"]}' for m in msgs]
                    results.append("Outlook:\n" + "\n".join(lines))
            except Exception as e:
                results.append(f"Outlook search failed sir: {e}")
        else:
            results.append(f"I don't recognize \"{p}\" as an email provider sir.")
    return "\n".join(results)


def read_email(message_id: str, provider: str = "gmail") -> str:
    """Get the full plain-text body of one email message -- use this after
    search_email finds a message whose snippet isn't enough. `message_id`
    is the id search_email returned in brackets. `provider` must be
    "gmail" or "outlook" (not "both" -- a message id only belongs to one)."""
    provider = (provider or "gmail").strip().lower()
    if provider == "gmail":
        if not GMAIL_AVAILABLE:
            return "Gmail isn't connected sir."
        r = gmail_service.read_message_body(message_id)
    elif provider == "outlook":
        if not OUTLOOK_AVAILABLE:
            return "Outlook isn't connected sir."
        r = outlook_service.read_message_body(message_id)
    else:
        return f"I don't recognize \"{provider}\" as an email provider sir."
    if not r.get("ok"):
        return f"Couldn't read that message sir: {r.get('error')}"
    return f'From {r["from"]}, {r["date"]}: "{r["subject"]}"\n\n{r["body"]}'


def list_calendar_events(days_ahead: int = 7, provider: str = "gmail") -> str:
    """List real upcoming calendar events in the next `days_ahead` days --
    not a placeholder. `provider` is "gmail", "outlook", or "both"."""
    days_ahead = max(1, int(days_ahead or 7))
    provider = (provider or "gmail").strip().lower()
    targets = ["gmail", "outlook"] if provider == "both" else [provider]
    results = []
    for p in targets:
        if p == "gmail":
            if not GMAIL_AVAILABLE:
                results.append("Google Calendar isn't connected sir.")
                continue
            try:
                events = gmail_service.list_upcoming_events(days_ahead)
                if not events:
                    results.append("Nothing on your Google Calendar in that window sir.")
                else:
                    lines = [f'[{e["id"]}] {e["start"]}: "{e["summary"]}"' + (f" @ {e['location']}" if e["location"] else "") for e in events]
                    results.append("Google Calendar:\n" + "\n".join(lines))
            except Exception as e:
                results.append(f"Google Calendar failed sir: {e}")
        elif p == "outlook":
            if not OUTLOOK_AVAILABLE:
                results.append("Outlook Calendar isn't connected sir.")
                continue
            try:
                events = outlook_service.list_upcoming_events(days_ahead)
                if not events:
                    results.append("Nothing on your Outlook Calendar in that window sir.")
                else:
                    lines = [f'[{e["id"]}] {e["start"]}: "{e["summary"]}"' + (f" @ {e['location']}" if e["location"] else "") for e in events]
                    results.append("Outlook Calendar:\n" + "\n".join(lines))
            except Exception as e:
                results.append(f"Outlook Calendar failed sir: {e}")
        else:
            results.append(f"I don't recognize \"{p}\" as a calendar provider sir.")
    return "\n".join(results)


def update_calendar_event(event_id: str, provider: str = "gmail", title: str = "",
                           start_iso: str = "", end_iso: str = "", description: str = "") -> str:
    """Change an existing calendar event -- only pass the fields you want
    changed, everything else stays as-is. `event_id` comes from
    list_calendar_events' output. `provider` must be "gmail" or "outlook"
    (not "both" -- an event id only belongs to one calendar)."""
    provider = (provider or "gmail").strip().lower()
    if provider == "gmail":
        if not GMAIL_AVAILABLE:
            return "Google Calendar isn't connected sir."
        r = gmail_service.update_calendar_event(event_id, title, start_iso, end_iso, description)
    elif provider == "outlook":
        if not OUTLOOK_AVAILABLE:
            return "Outlook Calendar isn't connected sir."
        r = outlook_service.update_calendar_event(event_id, title, start_iso, end_iso, description)
    else:
        return f"I don't recognize \"{provider}\" as a calendar provider sir."
    return "Updated sir." if r.get("ok") else f"Couldn't update that event sir: {r.get('error')}"


def delete_calendar_event(event_id: str, provider: str = "gmail") -> str:
    """Delete a calendar event. `event_id` comes from list_calendar_events'
    output. `provider` must be "gmail" or "outlook"."""
    provider = (provider or "gmail").strip().lower()
    if provider == "gmail":
        if not GMAIL_AVAILABLE:
            return "Google Calendar isn't connected sir."
        r = gmail_service.delete_calendar_event(event_id)
    elif provider == "outlook":
        if not OUTLOOK_AVAILABLE:
            return "Outlook Calendar isn't connected sir."
        r = outlook_service.delete_calendar_event(event_id)
    else:
        return f"I don't recognize \"{provider}\" as a calendar provider sir."
    return "Deleted sir." if r.get("ok") else f"Couldn't delete that event sir: {r.get('error')}"


# ================================================================ DISK
def _dir_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _human_size(n):
    for unit in ["B", "KB", "MB", "GB"]:
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


_DISK_ROOT = "C:\\" if IS_WINDOWS else "/"


def check_disk_space() -> str:
    """Report free vs total disk space on the main drive, and how much
    reclaimable space is sitting in the temp folder."""
    total, _used, free = shutil.disk_usage(_DISK_ROOT)
    temp_size = _dir_size(tempfile.gettempdir())
    label = "drive C" if IS_WINDOWS else "the main drive"
    return (f"You have {_human_size(free)} free out of {_human_size(total)} on {label} sir. "
            f"Your temp folder alone is holding {_human_size(temp_size)} of reclaimable space.")


def _clear_temp_folder(path):
    for name in os.listdir(path):
        full = os.path.join(path, name)
        try:
            if os.path.isdir(full) and not os.path.islink(full):
                shutil.rmtree(full, ignore_errors=True)
            else:
                os.remove(full)
        except Exception:
            pass


def _do_clean_disk():
    """Shared by clean_disk and check_system_health: clear the OS temp
    folder and empty the Recycle Bin, returning the bytes freed. Never
    touches user files or documents."""
    _, _, free_before = shutil.disk_usage(_DISK_ROOT)
    _clear_temp_folder(tempfile.gettempdir())
    try:
        if IS_WINDOWS:
            subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command", "Clear-RecycleBin -Force -ErrorAction SilentlyContinue"],
                timeout=30, capture_output=True,
            )
        elif shutil.which("gio"):
            subprocess.run(["gio", "trash", "--empty"], timeout=30, capture_output=True)
        else:
            for sub in ("files", "info"):
                _clear_temp_folder(os.path.join(HOME, ".local", "share", "Trash", sub))
    except Exception:
        pass
    _, _, free_after = shutil.disk_usage(_DISK_ROOT)
    return max(0, free_after - free_before)


def clean_disk() -> str:
    """Clear the OS temp folder and empty the Recycle Bin. Never touches user
    files or documents."""
    freed = _do_clean_disk()
    return f"Done sir. Cleared temporary files and the recycle bin, freeing up {_human_size(freed)}."


# ================================================================ SYSTEM HEALTH
HEALTH_LOG_PATH = os.path.join(JARVIS_DIR, "health_log.jsonl")

# Priority order for picking the single "headline" temperature to report when
# several sensors are readable -- prefer the actual CPU package/die sensor
# over ambient/chipset ones.
_TEMP_ZONE_PRIORITY = ("x86_pkg_temp", "cpu_thermal", "k10temp", "acpitz", "pch_skylake")
_TEMP_WARN_C = 85.0
_TEMP_CRITICAL_C = 95.0
_DISK_LOW_FREE_GB = 10.0
_DISK_CRITICAL_FREE_GB = 3.0

# Structured result of the most recent check_system_health() call -- lets
# callers (e.g. jarvis.py's health-watcher thread) branch on whether it was
# critical without re-parsing the human-readable reply string.
LAST_HEALTH_RESULT = {}


def _read_linux_temps():
    """Read every plausible thermal zone under /sys/class/thermal, in
    Celsius. No extra dependency needed -- the kernel exposes these directly."""
    temps = {}
    base = "/sys/class/thermal"
    if not os.path.isdir(base):
        return temps
    for entry in os.listdir(base):
        if not entry.startswith("thermal_zone"):
            continue
        zdir = os.path.join(base, entry)
        try:
            with open(os.path.join(zdir, "type"), "r", encoding="utf-8") as f:
                zone_type = f.read().strip()
            with open(os.path.join(zdir, "temp"), "r", encoding="utf-8") as f:
                raw = int(f.read().strip())
        except Exception:
            continue
        c = raw / 1000.0
        # Disabled/unpopulated sensors commonly read as -273 (absolute
        # zero) or other nonsense -- skip anything outside a plausible range.
        if -20.0 <= c <= 150.0:
            temps[zone_type] = c
    return temps


def _read_windows_temps():
    """Best-effort CPU temp via WMI's MSAcpi_ThermalZoneTemperature. Often
    needs the `wmi` package and admin rights; returns empty if unavailable
    rather than failing the whole health check."""
    try:
        import wmi
        w = wmi.WMI(namespace="root\\wmi")
        temps = {}
        for i, zone in enumerate(w.MSAcpi_ThermalZoneTemperature()):
            c = (zone.CurrentTemperature / 10.0) - 273.15
            if -20.0 <= c <= 150.0:
                temps[f"zone{i}"] = c
        return temps
    except Exception:
        return {}


def _hottest_reading(temps):
    if not temps:
        return None, None
    for key in _TEMP_ZONE_PRIORITY:
        if key in temps:
            return key, temps[key]
    hottest_zone = max(temps, key=temps.get)
    return hottest_zone, temps[hottest_zone]


def _log_health_event(entry):
    _ensure_jarvis_dir()
    entry = dict(entry, ts=time.time())
    try:
        with open(HEALTH_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def check_system_health() -> str:
    """Run a PC stabilization/health check: read CPU/system thermal sensors
    and flag any overheating risk, and check free disk space -- if it's
    running low, automatically clear temp files and empty the Recycle Bin.
    Every run is appended to ~/.jarvis/health_log.jsonl. Safe to call anytime
    on demand; also runs automatically in the background every 2 hours."""
    temps = _read_windows_temps() if IS_WINDOWS else _read_linux_temps()
    zone, hottest = _hottest_reading(temps)
    zone_label = zone.replace("_", " ") if zone else ""

    if hottest is None:
        thermal_note = "I couldn't read a temperature sensor on this machine sir."
        thermal_state = "unknown"
    elif hottest >= _TEMP_CRITICAL_C:
        thermal_note = (f"Your {zone_label} is at {hottest:.0f}°C sir -- that's a real "
                         f"overheating risk, you should let it cool down.")
        thermal_state = "critical"
    elif hottest >= _TEMP_WARN_C:
        thermal_note = f"Your {zone_label} is running warm at {hottest:.0f}°C sir, worth keeping an eye on."
        thermal_state = "warning"
    else:
        thermal_note = f"Temperatures look fine sir, {zone_label} is at {hottest:.0f}°C."
        thermal_state = "ok"

    _total, _used, free = shutil.disk_usage(_DISK_ROOT)
    free_gb = free / (1024 ** 3)
    cleaned = free_gb < _DISK_LOW_FREE_GB
    freed_bytes = _do_clean_disk() if cleaned else 0
    if cleaned:
        # Re-measure after cleanup -- if it's still critically low, temp
        # files/recycle bin weren't the problem and the user needs to know.
        _total, _used, free = shutil.disk_usage(_DISK_ROOT)
        free_gb = free / (1024 ** 3)
    disk_critical = free_gb < _DISK_CRITICAL_FREE_GB
    disk_note = (
        f"Disk space is critically low ({free_gb:.1f} GB free) even after clearing temp files "
        f"and the recycle bin sir -- you'll need to free up space manually." if disk_critical else
        f"Disk space was low ({free_gb:.1f} GB free) so I cleared temp files and the recycle "
        f"bin, freeing {_human_size(freed_bytes)} sir." if cleaned else
        f"Disk space is fine sir, {free_gb:.1f} GB free."
    )

    critical = thermal_state == "critical" or disk_critical

    _log_health_event({
        "thermal_state": thermal_state, "hottest_zone": zone, "hottest_c": hottest,
        "free_gb": round(free_gb, 1), "cleaned": cleaned, "freed_bytes": freed_bytes,
        "disk_critical": disk_critical, "critical": critical,
    })

    global LAST_HEALTH_RESULT
    LAST_HEALTH_RESULT = {
        "thermal_state": thermal_state, "disk_critical": disk_critical, "critical": critical,
        "free_gb": round(free_gb, 1), "hottest_c": hottest,
    }

    return f"{thermal_note} {disk_note}"


def system_power(action: str, delay_minutes: float = 1) -> str:
    """Shut down, restart, or cancel a pending shutdown/restart of THIS
    computer -- the one Jarvis is running on, not a remote machine. action
    must be one of: shutdown, restart, cancel. Defaults to a 1-minute delay
    so a wall message warns whoever's on the machine and it can still be
    cancelled (call this again with action="cancel") -- only pass
    delay_minutes=0 if the user explicitly says "now"/"immediately". Note
    this will also stop Jarvis's own backend, since it runs on this same
    machine. If JARVIS_ADMIN_BOT_TOKEN is configured, shutdown/restart (not
    cancel) first waits for an explicit yes/no approval over the dedicated
    JarvisAdmin Telegram bot -- this call blocks until that's answered or
    times out, so it can take a few minutes to return."""
    action = (action or "").strip().lower()
    if action not in ("shutdown", "restart", "cancel"):
        return f"I don't recognize '{action}' sir -- action must be shutdown, restart, or cancel."

    if action in ("shutdown", "restart") and jarvis_admin.JARVIS_ADMIN_AVAILABLE:
        when = "now" if delay_minutes == 0 else f"in {max(0.0, float(delay_minutes)):g} minute(s)"
        approved, status = jarvis_admin.request_approval(f"{action} this PC {when}")
        if not approved:
            if status == "timeout":
                return "No response on JarvisAdmin in time sir -- treating that as a no, for safety. Not going ahead."
            return "Denied on JarvisAdmin sir -- I won't go ahead with that."

    shutdown_bin = "shutdown.exe" if IS_WINDOWS else "shutdown"
    # On Linux, Jarvis's backend runs as an ordinary desktop user, and whether
    # that user's session is considered "active" by polkit (and so gets a
    # passwordless power-off/reboot) depends on exactly how/where the backend
    # process was started -- unreliable to depend on, and a remote Telegram
    # command has no way to answer a graphical polkit prompt if one appears.
    # So this always goes through `sudo -n` on Linux instead: deterministic
    # regardless of session context, and -n (non-interactive) means it fails
    # fast with a clear stderr message rather than ever hanging on a prompt
    # nobody can see. Requires a one-time NOPASSWD sudoers rule scoped to
    # exactly this one binary -- see README's "Homelab" / shutdown setup.
    _prefix = [] if IS_WINDOWS else ["sudo", "-n"]

    def _detail(result):
        detail = (result.stderr or result.stdout or "").strip()
        low = detail.lower()
        if "password is required" in low or "a terminal is required" in low or "authentication is required" in low:
            return ("passwordless sudo for shutdown isn't set up yet -- see README's "
                     "Homelab section for the one-time setup command.")
        return detail or "unknown error"

    try:
        if action == "cancel":
            cmd = [shutdown_bin, "/a"] if IS_WINDOWS else _prefix + [shutdown_bin, "-c"]
            result = subprocess.run(cmd, capture_output=True, timeout=10, text=True)
            if IS_WINDOWS and result.returncode != 0:
                return "There wasn't a shutdown or restart scheduled to cancel, sir."
            if not IS_WINDOWS and result.returncode != 0:
                return f"I couldn't cancel that sir: {_detail(result)}"
            return "Cancelled the pending shutdown, sir."

        delay_minutes = max(0.0, float(delay_minutes))

        if IS_WINDOWS:
            flag = "/s" if action == "shutdown" else "/r"
            cmd = [shutdown_bin, flag, "/t", str(int(delay_minutes * 60))]
        else:
            when = "now" if delay_minutes == 0 else f"+{max(1, round(delay_minutes))}"
            flag = "-h" if action == "shutdown" else "-r"
            cmd = _prefix + [shutdown_bin, flag, when]

        result = subprocess.run(cmd, capture_output=True, timeout=10, text=True)
        if result.returncode != 0:
            return f"I couldn't {action} the PC sir: {_detail(result)}"

        verb = "Shutting down" if action == "shutdown" else "Restarting"
        if delay_minutes == 0:
            return f"{verb} now, sir."
        return f"{verb} in {delay_minutes:g} minute(s), sir. Say 'cancel shutdown' if you change your mind."
    except Exception as e:
        return f"I couldn't {action} the PC sir: {e}"


def run_admin_action(description: str, command: str) -> str:
    """Run ONE real system command that needs actual shell access -- an
    install (`sudo apt install ffmpeg`, an npm/pip package), a config
    change, an update/upgrade, or anything else run_diagnostic_command's
    fixed read-only list can't cover. Use this whenever the user asks
    Jarvis to install, run, upgrade, or otherwise DO something concrete on
    this machine -- not just describe what could be done.

    Every call first asks for explicit yes/no approval on the dedicated
    JarvisAdmin Telegram bot, exactly like system_power's shutdown/restart
    gate, and only runs if approved. This is the one deliberate exception
    to this module's no-shell-exec rule (see the file header) -- the
    safety property isn't a fixed command list here, it's that nothing
    ever runs without an explicit human approval of the exact command,
    every single time, on a separate channel from whatever asked for it.
    Fails closed (refuses outright) if JarvisAdmin isn't configured --
    never falls back to running unapproved.

    `description` should read naturally after "Jarvis wants to " (e.g.
    "install ffmpeg via apt"). `command` is the literal shell command that
    will run -- shown verbatim in the approval prompt, so make it the real
    command, not a paraphrase, since that's what's actually being
    approved."""
    command = (command or "").strip()
    if not command:
        return "What command should I run sir?"
    if not jarvis_admin.JARVIS_ADMIN_AVAILABLE:
        return ("JarvisAdmin isn't configured sir -- I can't run a real system action "
                 "without a dedicated approval channel. See README's JarvisAdmin setup section.")

    ask = (description or "").strip() or "run a command"
    approved, status = jarvis_admin.request_approval(f"{ask}\n\nCommand: {command}")
    if not approved:
        if status == "timeout":
            return "No response on JarvisAdmin in time sir -- treating that as a no, for safety. Not running that."
        return "Denied on JarvisAdmin sir -- I won't run that."

    try:
        result = subprocess.run(command, shell=True, capture_output=True, text=True,
                                 timeout=600, cwd=os.path.expanduser("~"))
        output = ((result.stdout or "") + (result.stderr or "")).strip()
        if len(output) > 1500:
            output = output[-1500:]
        status_line = (f"Done sir (exit {result.returncode})." if result.returncode == 0
                        else f"That failed sir (exit {result.returncode}).")
        return f"{status_line}\n{output}" if output else status_line
    except subprocess.TimeoutExpired:
        return "That command took too long sir, I stopped waiting -- it may still be running in the background."
    except Exception as e:
        return f"Something went wrong running that sir: {e}"


# Fixed, narrow allowlist of hardening tools JarSecurity/the brain can
# propose installing -- never free text, never a new sudo path. Each entry
# is the literal, complete command that will run; propose_hardening_install
# below only ever hands one of these exact strings to run_admin_action,
# which is what actually gates it behind JarvisAdmin's Telegram approval.
# aide's install bundles `aideinit` so its file-integrity baseline is set
# immediately -- otherwise its first real check would flag everything.
_HARDENING_INSTALLS = {
    "fail2ban": "sudo apt-get install -y fail2ban",
    "chkrootkit": "sudo apt-get install -y chkrootkit",
    "aide": "sudo apt-get install -y aide && sudo aideinit",
}


def propose_hardening_install(tool_name: str) -> str:
    """Propose installing one vetted homelab-security tool -- fail2ban
    (bans IPs hammering exposed services), chkrootkit (a second, independent
    rootkit scanner alongside rkhunter), or aide (file-integrity
    monitoring). `tool_name` must be one of those three; anything else is
    refused outright, never passed through. Routes through the existing
    run_admin_action() -- same JarvisAdmin Telegram yes/no gate as any other
    real system action, fails closed if that bot isn't configured. This
    function never constructs a command itself, only looks one up from the
    fixed _HARDENING_INSTALLS dict above, so there is no way for this to
    become a general install-anything tool."""
    tool_name = (tool_name or "").strip().lower()
    command = _HARDENING_INSTALLS.get(tool_name)
    if not command:
        return (f"I don't have '{tool_name}' in my hardening-tool list sir -- I can propose: "
                 + ", ".join(_HARDENING_INSTALLS))
    if shutil.which(tool_name):
        return f"{tool_name} is already installed sir."
    return run_admin_action(f"install {tool_name} to improve homelab security", command)


# ================================================================ TERMINAL / SYSTEM DIAGNOSTICS
# The "basic PowerShell/terminal commands" capability -- deliberately NOT a
# generic shell-exec tool (that stays forbidden, see the module docstring at
# the top of this file). The brain can only pick a *name* from the fixed
# list below; the underlying argv for each name is hardcoded, subprocess.run
# is always called with a list (never shell=True or a joined string, so
# there's no shell-metacharacter injection surface), and every command here
# is read-only/non-destructive. The one command that takes a free-text
# argument (ping's hostname) is strictly validated against _HOSTNAME_RE
# before it's appended to the fixed argv, so nothing else can ever reach it.
_DIAGNOSTIC_COMMANDS_WIN = {
    "disk_usage": (["powershell", "-NoProfile", "-Command",
                     "Get-PSDrive -PSProvider FileSystem | Format-Table -AutoSize"], False),
    "memory_usage": (["powershell", "-NoProfile", "-Command",
                       "Get-CimInstance Win32_OperatingSystem | "
                       "Select-Object FreePhysicalMemory,TotalVisibleMemorySize | Format-List"], False),
    "processes": (["powershell", "-NoProfile", "-Command",
                    "Get-Process | Sort-Object CPU -Descending | "
                    "Select-Object -First 15 Name,CPU,Id | Format-Table -AutoSize"], False),
    "network_info": (["ipconfig", "/all"], False),
    "uptime": (["powershell", "-NoProfile", "-Command",
                "(Get-CimInstance Win32_OperatingSystem).LastBootUpTime"], False),
    "whoami": (["whoami"], False),
    "listening_ports": (["netstat", "-an"], False),
    "ping": (["ping", "-n", "4"], True),
    "system_info": (["powershell", "-NoProfile", "-Command",
                      "Get-ComputerInfo | Select-Object WindowsProductName,OsVersion,"
                      "CsName,OsHardwareAbstractionLayer | Format-List"], False),
    "disk_layout": (["powershell", "-NoProfile", "-Command",
                      "Get-Disk | Format-Table -AutoSize"], False),
    "dns_lookup": (["powershell", "-NoProfile", "-Command", "Resolve-DnsName"], True),
    "public_ip": (["powershell", "-NoProfile", "-Command",
                    "(Invoke-RestMethod -Uri https://ifconfig.me)"], False),
}
_DIAGNOSTIC_COMMANDS_LINUX = {
    "disk_usage": (["df", "-h"], False),
    "memory_usage": (["free", "-h"], False),
    "processes": (["bash", "-c", "ps aux --sort=-%cpu | head -16"], False),
    "network_info": (["ip", "addr"], False),
    "uptime": (["uptime"], False),
    "whoami": (["whoami"], False),
    "listening_ports": (["ss", "-tulpn"], False),
    "ping": (["ping", "-c", "4"], True),
    "system_info": (["hostnamectl"], False),
    # -e7 excludes loop devices (major number 7) -- on a machine with a lot
    # of snaps installed, those otherwise bury the real physical disks/
    # partitions under dozens of irrelevant loop-mounted squashfs entries.
    "disk_layout": (["lsblk", "-e7"], False),
    "dns_lookup": (["dig", "+short"], True),
    "public_ip": (["curl", "-s", "-4", "--max-time", "6", "https://ifconfig.me"], False),
}
_DIAGNOSTIC_COMMANDS = _DIAGNOSTIC_COMMANDS_WIN if IS_WINDOWS else _DIAGNOSTIC_COMMANDS_LINUX
_HOSTNAME_RE = re.compile(r'^[A-Za-z0-9](?:[A-Za-z0-9\-.]{0,252})$')


def run_diagnostic_command(command: str, target: str = "") -> str:
    """Run one basic, safe, read-only system/network diagnostic -- Jarvis's
    equivalent of a basic PowerShell/terminal command, restricted to a fixed
    named list rather than free-text (never a generic shell-exec -- see this
    file's module docstring). command must be one of: disk_usage,
    memory_usage, processes, network_info, uptime, whoami, listening_ports,
    ping, system_info, disk_layout, dns_lookup, public_ip. `target` is only
    used by ping and dns_lookup and must be a plain hostname or IP (e.g.
    "google.com", "8.8.8.8") -- nothing else is accepted."""
    key = (command or "").strip().lower()
    entry = _DIAGNOSTIC_COMMANDS.get(key)
    if not entry:
        return (f"I don't have '{command}' in my diagnostic command list sir -- I can run: "
                 + ", ".join(sorted(_DIAGNOSTIC_COMMANDS)) + ".")
    base_cmd, needs_target = entry
    cmd = list(base_cmd)
    if needs_target:
        target = (target or "").strip()
        if not target or not _HOSTNAME_RE.match(target):
            return "I need a plain hostname or IP address sir -- letters, numbers, dots, and dashes only."
        cmd.append(target)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        output = (result.stdout or result.stderr or "").strip()
        if not output:
            return f"Ran {key} sir -- no output."
        if len(output) > 1500:
            output = output[:1500] + "\n... (truncated)"
        return f"{key} sir:\n{output}"
    except FileNotFoundError:
        return f"That diagnostic isn't available on this system sir ({cmd[0]} not found)."
    except subprocess.TimeoutExpired:
        return "That diagnostic took too long sir, I stopped waiting."
    except Exception as e:
        return f"I couldn't run that diagnostic sir: {e}"


# ================================================================ HOMELAB (NAS + network, thin wrappers)
def get_nas_status() -> str:
    """Report your Synology NAS's current status -- CPU load, memory use,
    and storage volume usage. Read-only; there is no NAS power-control
    capability. Requires SYNOLOGY_HOST/USER/PASSWORD to be set in .env
    first."""
    if not synology_service.SYNOLOGY_AVAILABLE:
        return "Your NAS isn't configured sir -- set SYNOLOGY_HOST/SYNOLOGY_USER/SYNOLOGY_PASSWORD in .env first."
    try:
        status = synology_service.get_status()
    except Exception as e:
        return f"I couldn't reach your NAS sir: {e}"
    vol_note = ""
    if status["volumes"]:
        v = status["volumes"][0]
        if v["used_pct"] is not None:
            vol_note = f", storage is {v['used_pct']:.0f} percent full"
    mem_note = f", memory at {status['mem_used_pct']} percent" if status.get("mem_used_pct") is not None else ""
    return f"Your NAS is at {status['cpu_pct']:.0f} percent CPU{mem_note}{vol_note} sir."


def export_folder_to_nas(name: str, location: str = "desktop") -> str:
    """Upload a folder to your Synology NAS. `name` is the folder's name
    inside one of the safe local directories (desktop, documents,
    downloads, pictures, music, videos, creations); it's copied to the NAS
    under a folder of the same name inside SYNOLOGY_BASE_PATH (a jailed
    sync folder, never an arbitrary NAS path), overwriting any files
    already there with the same name. Use this for "export/send/push/back
    up the <folder> folder to the NAS"."""
    if not synology_service.SYNOLOGY_AVAILABLE:
        return "Your NAS isn't configured sir -- set SYNOLOGY_HOST/SYNOLOGY_USER/SYNOLOGY_PASSWORD in .env first."
    target, err = _resolve_safe_path(name, location)
    if err:
        return err
    if not os.path.isdir(target):
        return f"I couldn't find a folder called {name} in {location} sir."
    try:
        result = synology_service.upload_folder(target, os.path.basename(target))
    except Exception as e:
        return f"I couldn't upload that to the NAS sir: {e}"
    return f"Uploaded {result['files_uploaded']} file(s) to {result['folder_path']} on the NAS sir."


def import_folder_from_nas(name: str, location: str = "desktop") -> str:
    """Download a folder from your Synology NAS into one of the safe local
    directories (desktop, documents, downloads, pictures, music, videos,
    creations). `name` is looked up inside SYNOLOGY_BASE_PATH on the NAS
    and copied into a same-named folder locally, overwriting any local
    files with matching names. Use this for "grab/pull/import/sync the
    <folder> folder from the NAS"."""
    if not synology_service.SYNOLOGY_AVAILABLE:
        return "Your NAS isn't configured sir -- set SYNOLOGY_HOST/SYNOLOGY_USER/SYNOLOGY_PASSWORD in .env first."
    target, err = _resolve_safe_path(name, location)
    if err:
        return err
    os.makedirs(target, exist_ok=True)
    try:
        result = synology_service.download_folder(_sanitize_name(name), target)
    except Exception as e:
        return f"I couldn't pull that from the NAS sir: {e}"
    if result["files_downloaded"] == 0:
        return f"There's nothing in {result['folder_path']} on the NAS sir -- nothing to pull."
    return f"Pulled {result['files_downloaded']} file(s) from the NAS into {location} sir."


def list_nas_folder(name: str = "") -> str:
    """List what's inside a folder on your Synology NAS, relative to
    SYNOLOGY_BASE_PATH (leave `name` blank for the top-level sync folder
    itself). Use this for "what's in the NAS <folder> folder" type
    questions, before deciding whether to pull it down."""
    if not synology_service.SYNOLOGY_AVAILABLE:
        return "Your NAS isn't configured sir -- set SYNOLOGY_HOST/SYNOLOGY_USER/SYNOLOGY_PASSWORD in .env first."
    try:
        entries = synology_service.list_folder(_sanitize_name(name))
    except Exception as e:
        return f"I couldn't reach your NAS sir: {e}"
    if not entries:
        return "That folder's empty, or doesn't exist yet, sir."
    names = ", ".join(f"{e['name']}/" if e["is_dir"] else e["name"] for e in entries[:50])
    return f"On the NAS: {names}"


def check_internet_speed() -> str:
    """Run a real internet speed test right now (takes roughly 15-30
    seconds) and report download/upload speed and ping. Use this for "how's
    my internet"/"run a speed test"/"check my wifi speed" requests."""
    try:
        result = speedtest_service.run_speed_test()
    except Exception as e:
        return f"I couldn't run a speed test sir: {e}"
    ping_note = f"{result['ping_ms']:.0f} ms ping" if result["ping_ms"] is not None else "ping unavailable"
    return (f"{result['download_mbps']:.0f} down, {result['upload_mbps']:.0f} up, "
            f"{ping_note} sir -- tested against {result['server']}.")


def scan_network() -> str:
    """Scan the local network for connected devices (router-agnostic --
    works by pinging the local subnet directly from this PC, not by talking
    to the router). Reports the total device count and flags anything new
    since the last scan; new devices also trigger a Telegram alert
    automatically in the background. Requires nmap installed."""
    result = network_watch.scan()
    if "error" in result:
        return result["error"]
    if result["new_count"]:
        new_ips = ", ".join(d["ip"] for d in result["devices"] if d["new"])
        return (f"{len(result['devices'])} devices on your network sir -- "
                f"{result['new_count']} new: {new_ips}.")
    return f"{len(result['devices'])} devices on your network sir, nothing new."


def deep_scan_device(ip: str) -> str:
    """Real port scan with service/version detection (nmap -sV) on ONE
    device already seen on your LAN -- richer than scan_network's plain
    ping sweep, telling you WHAT'S actually running on each open port
    (e.g. "OpenSSH 8.9p1"), not just that the port is open. Works without
    root. `ip` must already be a real address (from scan_network's device
    list) -- validated as a literal IP before it ever reaches nmap."""
    try:
        ipaddress.ip_address((ip or "").strip())
    except ValueError:
        return "I need a real IP address sir, from a device scan_network already found."
    nmap = shutil.which("nmap")
    if not nmap:
        return "nmap isn't installed sir -- run: sudo apt install nmap."
    try:
        result = subprocess.run(
            [nmap, "-sV", "--version-intensity", "0", "-T4", "--top-ports", "100", "-Pn", ip.strip()],
            capture_output=True, text=True, timeout=90,
        )
    except subprocess.TimeoutExpired:
        return "That scan took too long sir, I stopped waiting."
    open_lines = [l for l in result.stdout.splitlines() if "/tcp" in l and "open" in l]
    if not open_lines:
        return f"No open ports found on {ip} sir (top 100, TCP)."
    return f"{ip} sir:\n" + "\n".join(open_lines[:20])


def scan_file_for_malware(name: str, location: str = "downloads") -> str:
    """Run a REAL ClamAV virus/malware scan (not a placeholder) on one file
    or folder under a safe named location -- same locations as
    create_folder/read_text_file: desktop, documents, downloads, pictures,
    music, videos, creations. Uses whatever signature database is
    currently installed (clamav-freshclam keeps it updated automatically
    in the background if that service is running). Can take a while for a
    large folder."""
    target, err = _resolve_safe_path(name, location)
    if err:
        return err
    if not os.path.exists(target):
        return f"I can't find {name} in {location} sir."
    clamscan = shutil.which("clamscan")
    if not clamscan:
        return "ClamAV isn't installed sir -- run: sudo apt install clamav."
    try:
        result = subprocess.run([clamscan, "-r", "--no-summary", target],
                                 capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return "That scan took too long sir, I stopped waiting."
    infected = [l for l in (result.stdout or "").splitlines() if l.rstrip().endswith("FOUND")]
    if infected:
        return f"Found {len(infected)} infected file(s) sir:\n" + "\n".join(infected[:10])
    return f"Clean sir -- no threats found in {name}."


def _sudo_n(argv, timeout):
    """Try a command with non-interactive sudo (-n) -- fails INSTANTLY
    rather than ever hanging on a password prompt nobody can answer, which
    is exactly what happens if passwordless sudo isn't configured for that
    exact command. Returns (CompletedProcess, had_password_prompt) so
    callers can fall back to a plain non-root invocation specifically when
    sudo itself refused for lack of a NOPASSWD rule -- not when the
    command's own real error is something else. See the README's
    "Real actions from your phone" section for how to add a scoped
    NOPASSWD rule for a specific diagnostic command."""
    try:
        r = subprocess.run(["sudo", "-n"] + argv, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return None, True
    err = (r.stderr or "").lower()
    # Verified live on this machine: a NOPASSWD-less `sudo -n` here says
    # "interactive authentication is required", not the more commonly-
    # documented "a password is required" -- checking for both (and the
    # no-tty variant some sudo builds use) rather than trusting one exact
    # string across every sudo version/build.
    needs_password = r.returncode == 1 and (
        "password is required" in err or "no tty present" in err or "authentication is required" in err
    )
    return r, needs_password


def get_disk_health(device: str = "") -> str:
    """Real S.M.A.R.T. health data (overall health, temperature, power-on
    hours) for a drive physically attached to THIS machine -- not the NAS
    (see get_nas_status for that), and not any spare/unmounted drive that
    isn't actually connected here (there's no way to read SMART data over
    the network for a drive that isn't plugged into some machine Jarvis is
    running on). `device` is the short name from `smartctl --scan` (e.g.
    "nvme0", "sda") -- leave blank to auto-detect and report on every
    attached drive. Tries passwordless sudo first (works once the scoped
    NOPASSWD rule from the README is set up), falls back to a plain
    permission-denied message otherwise -- never hangs on a password
    prompt either way."""
    smartctl = shutil.which("smartctl")
    if not smartctl:
        return "smartmontools isn't installed sir -- run: sudo apt install smartmontools."

    devices = [device.strip()] if (device or "").strip() else []
    if not devices:
        try:
            scan = subprocess.run([smartctl, "--scan"], capture_output=True, text=True, timeout=10)
            devices = [line.split()[0].replace("/dev/", "") for line in scan.stdout.splitlines() if line.strip()]
        except Exception as e:
            return f"Couldn't list drives sir: {e}"
    if not devices:
        return "No drives found attached to this machine sir."

    results = []
    for dev in devices:
        if not re.match(r"^[A-Za-z0-9]+$", dev):
            results.append(f"{dev}: skipped, not a plain device name")
            continue
        try:
            r, needs_password = _sudo_n([smartctl, "-a", f"/dev/{dev}"], 15)
            out = (r.stdout or r.stderr or "") if r else ""
            if needs_password or r is None:
                r2 = subprocess.run([smartctl, "-a", f"/dev/{dev}"], capture_output=True, text=True, timeout=15)
                out = r2.stdout or r2.stderr or ""
        except Exception as e:
            results.append(f"{dev}: error ({e})")
            continue
        if "Permission denied" in out:
            results.append(f"{dev}: permission denied -- see README's NOPASSWD setup for real SMART data")
            continue
        health_m = re.search(r"(?:SMART overall-health self-assessment test result|SMART Health Status):\s*(\S+)", out)
        temp_m = re.search(r"Temperature.*?:\s*(\d+)", out)
        hours_m = re.search(r"Power_On_Hours.*?\s(\d+)\s*$", out, re.MULTILINE)
        entry = dev + ": " + (health_m.group(1) if health_m else "unknown")
        if temp_m:
            entry += f", {temp_m.group(1)}°C"
        if hours_m:
            entry += f", {hours_m.group(1)} hours on"
        results.append(entry)
    return "; ".join(results) + " sir."


def test_lan_throughput(target_host: str) -> str:
    """Real LAN throughput test (iperf3 client) between this PC and another
    device on your network -- distinct from check_internet_speed, which
    only measures internet bandwidth; this measures your actual local
    network speed. The OTHER device needs iperf3 installed and its server
    already running there first (`iperf3 -s`) -- this only ever connects
    as a client, it never starts a server itself. Fails with a clear
    connection error if nothing's listening at target_host."""
    iperf3 = shutil.which("iperf3")
    if not iperf3:
        return "iperf3 isn't installed sir -- run: sudo apt install iperf3."
    target_host = (target_host or "").strip()
    if not target_host or not _HOSTNAME_RE.match(target_host):
        return "I need a plain hostname or IP address sir."
    try:
        result = subprocess.run([iperf3, "-c", target_host, "-t", "5", "-J"],
                                 capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        return "That test took too long sir, I stopped waiting."
    try:
        data = json.loads(result.stdout)
    except Exception:
        err = (result.stderr or result.stdout or "unknown error").strip()
        return f"Couldn't reach an iperf3 server on {target_host} sir -- make sure `iperf3 -s` is running there. ({err[:200]})"
    if "error" in data:
        return f"iperf3 error sir: {data['error']}"
    mbps = data.get("end", {}).get("sum_received", {}).get("bits_per_second", 0) / 1_000_000
    return f"{mbps:.0f} Mbps sir, between this PC and {target_host}."


def get_live_system_snapshot() -> str:
    """Real, detailed live snapshot of THIS machine right now -- per-core
    CPU load, memory breakdown, 1/5/15-minute load averages, per-network-
    interface throughput, and root filesystem usage -- richer and more
    real-time than check_system_health's periodic thermal/disk check.
    Backed by glances' one-shot JSON export; not a placeholder."""
    glances = shutil.which("glances")
    if not glances:
        return "glances isn't installed sir -- run: sudo apt install glances."
    try:
        result = subprocess.run(
            [glances, "--stdout-json", "now,cpu,mem,load,network,fs", "--stop-after", "1", "-t", "1"],
            capture_output=True, text=True, timeout=15,
        )
    except subprocess.TimeoutExpired:
        return "That took too long sir, I stopped waiting."
    try:
        data = json.loads(result.stdout.strip())
    except Exception:
        return f"Couldn't read glances output sir: {(result.stderr or '')[:300]}"
    cpu, mem, load = data.get("cpu", {}), data.get("mem", {}), data.get("load", {})
    fs = data.get("fs", []) or []
    root_fs = next((f for f in fs if f.get("mnt_point") == "/"), fs[0] if fs else {})
    return (
        f"CPU {cpu.get('total', 0):.0f}% across {cpu.get('cpucore', '?')} cores, "
        f"load {load.get('min1', 0):.2f}/{load.get('min5', 0):.2f}/{load.get('min15', 0):.2f}, "
        f"memory {mem.get('percent', 0):.0f}% used, "
        f"disk {root_fs.get('percent', 0):.0f}% used on {root_fs.get('mnt_point', '/')} sir."
    )


def run_security_audit() -> str:
    """Real system hardening audit (lynis) -- a hardening index score plus
    concrete warnings and suggestions, read from lynis's own machine-
    readable report after a real scan. Tries passwordless sudo first for
    the FULL root-level audit (works once the scoped NOPASSWD rule from
    the README is set up); falls back to a non-root run otherwise, where
    root-only checks are simply skipped rather than failing. A full
    root-level --quick audit on a machine with a lot installed (many
    Docker containers, services, etc.) has been observed taking several
    minutes -- can call this on demand, but it's mainly meant to run
    unattended (see jarvis.py's daily _deep_security_watcher_thread),
    where that's not a concern."""
    lynis = shutil.which("lynis")
    if not lynis:
        return "lynis isn't installed sir -- run: sudo apt install lynis."
    # Fixed filename (not randomized) so the sudoers rules below that read
    # and clean it up can name it exactly, with no wildcard -- this sudo
    # build rejects mid-argument wildcards entirely (see get_disk_health's
    # smartctl rule). Lives in ~/.jarvis, which only tyler-kennedy can
    # write into, so a fixed path here can't be pre-planted by another
    # local user the way a fixed path in world-writable /tmp could.
    report_path = os.path.join(JARVIS_DIR, "lynis_report.dat")
    lynis_argv = [lynis, "audit", "system", "--quick", "--no-colors", "--no-log", "--report-file", report_path]
    try:
        _r, needs_password = _sudo_n(lynis_argv, 600)
        if needs_password:
            subprocess.run(lynis_argv, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        return "That audit took too long sir, I stopped waiting -- it may have made partial progress."
    if not os.path.exists(report_path):
        return "Lynis didn't produce a report sir -- something went wrong running it."

    # A root-run report file is owned by root -- read it back through sudo
    # too if a plain open() can't (still non-interactive, still never hangs
    # on a password prompt; if that ALSO fails there's genuinely no way to
    # read it back, and we say so rather than crash). Needs its own
    # jarvis-diagnostics sudoers line (README) since running lynis as root
    # doesn't by itself grant reading its root-owned output back.
    warnings, suggestions, hardening_index = [], [], None
    try:
        with open(report_path, "r", encoding="utf-8", errors="replace") as f:
            report_lines = f.readlines()
    except PermissionError:
        cat_r, cat_needs_password = _sudo_n(["cat", report_path], 15)
        if cat_needs_password or cat_r is None:
            return "Lynis's report was written as root and I can't read it back sir -- see the README's NOPASSWD setup."
        report_lines = (cat_r.stdout or "").splitlines(keepends=True)
    try:
        for line in report_lines:
            line = line.strip()
            if line.startswith("warning[]="):
                parts = line.split("=", 1)[1].split("|")
                warnings.append(parts[1] if len(parts) > 1 else parts[0])
            elif line.startswith("suggestion[]="):
                parts = line.split("=", 1)[1].split("|")
                suggestions.append(parts[1] if len(parts) > 1 else parts[0])
            elif line.startswith("hardening_index="):
                hardening_index = line.split("=", 1)[1]
    finally:
        try:
            os.remove(report_path)
        except OSError:
            _sudo_n(["rm", "-f", report_path], 5)  # best-effort if it's root-owned

    global LAST_AUDIT_RESULT
    LAST_AUDIT_RESULT = {
        "hardening_index": hardening_index, "warnings": warnings, "suggestions": suggestions,
    }

    parts = [f"hardening index {hardening_index or '–'}/100"]
    parts.append(f"{len(warnings)} warning(s)" + (": " + "; ".join(warnings[:5]) if warnings else ""))
    parts.append(f"{len(suggestions)} suggestion(s), top ones: " + "; ".join(suggestions[:5]) if suggestions else "0 suggestions")
    return "; ".join(parts) + " sir."


def run_rootkit_scan() -> str:
    """Real rootkit/backdoor scan (rkhunter --check) -- checks system
    binaries and known locations against rkhunter's rootkit signature
    database. Needs root for almost everything it does: tries passwordless
    sudo first (works once the scoped NOPASSWD rule from the README is set
    up), and returns a clear message -- never a hang -- if that isn't
    configured yet. Can take a couple of minutes; safe to call anytime on
    demand."""
    rkhunter = shutil.which("rkhunter")
    if not rkhunter:
        return "rkhunter isn't installed sir -- run: sudo apt install rkhunter."
    argv = [rkhunter, "--check", "--sk", "--nocolors"]
    try:
        r, needs_password = _sudo_n(argv, 180)
    except subprocess.TimeoutExpired:
        return "That scan took too long sir, I stopped waiting."
    if needs_password or r is None:
        return ("I need root for a real rootkit scan sir, and passwordless sudo isn't set up for "
                "rkhunter yet -- see the README's NOPASSWD setup, or ask me to run_admin_action the sudo version once.")
    out = (r.stdout or "") + (r.stderr or "")
    warnings = [l.strip() for l in out.splitlines() if "warning" in l.lower() and ":" in l]

    global LAST_ROOTKIT_RESULT
    LAST_ROOTKIT_RESULT = {"warnings": warnings}

    if warnings:
        return f"{len(warnings)} warning(s) sir:\n" + "\n".join(warnings[:10])
    return "Clean sir -- no rootkit warnings found."


def log_deep_scan_result() -> dict:
    """Logs the most recent run_security_audit()/run_rootkit_scan() results
    (from LAST_AUDIT_RESULT/LAST_ROOTKIT_RESULT) as one deep_scan entry in
    security_log.jsonl, distinct from run_security_check()'s regular
    4-hour sweep entries (security_status_report() filters on the
    "deep_scan" flag to report them separately). Called once daily by
    jarvis.py's deep-scan watcher thread, after both scans have run.
    Returns {"critical", "summary"} so the caller can decide whether to
    alert -- critical means a REAL lynis warning (not a suggestion) or any
    rkhunter warning, not just "the scan ran successfully"."""
    lynis_warnings = LAST_AUDIT_RESULT.get("warnings") or []
    lynis_suggestions = LAST_AUDIT_RESULT.get("suggestions") or []
    hardening_index = LAST_AUDIT_RESULT.get("hardening_index")
    rkhunter_warnings = LAST_ROOTKIT_RESULT.get("warnings") or []

    critical = bool(lynis_warnings) or bool(rkhunter_warnings)
    _log_security_event({
        "deep_scan": True,
        "lynis_hardening_index": hardening_index,
        "lynis_warnings": len(lynis_warnings), "lynis_warning_list": lynis_warnings[:10],
        "lynis_suggestions": len(lynis_suggestions),
        "rkhunter_warnings": len(rkhunter_warnings), "rkhunter_warning_list": rkhunter_warnings[:10],
        "critical": critical,
    })
    summary = (f"deep scan: hardening index {hardening_index or '–'}/100, "
               f"{len(lynis_warnings)} lynis warning(s), {len(rkhunter_warnings)} rkhunter warning(s)")
    if lynis_warnings:
        summary += "\nlynis: " + "; ".join(lynis_warnings[:5])
    if rkhunter_warnings:
        summary += "\nrkhunter: " + "; ".join(rkhunter_warnings[:5])
    return {"critical": critical, "summary": summary}


# ================================================================ LOCAL AI/MONITORING SERVICES WATCHDOG (Ollama, Prometheus)
# Detects whether the two local services this project's own AI/monitoring
# stack depends on -- Ollama (the local-fallback chat/vision/passive-memory
# model host, see OLLAMA_URL above) and Prometheus (homelab metrics
# scraping) -- are actually up, and restarts them if not. Deliberately
# narrow, same philosophy as the rest of this file (see module docstring):
# _AI_SERVICES below is a fixed dict, so only these two exact named
# services are ever restartable, never an arbitrary systemctl/docker unit.
# restart_ai_service() tries, in order: an already-running Docker container
# of that name (docker restart -- no root needed if this user is in the
# docker group), then a systemd unit (`systemctl --user` first, then
# non-interactive sudo via the same _sudo_n() helper run_rootkit_scan/
# run_security_audit use -- fails fast with a clear message rather than
# ever hanging on a password prompt, and needs the same kind of scoped
# NOPASSWD rule documented in the README's Homelab section if the unit is
# system-level).
_AI_SERVICES = {
    "ollama": {"label": "Ollama", "url": "http://localhost:11434/api/tags",
               "unit": "ollama.service", "container": "ollama"},
    "prometheus": {"label": "Prometheus", "url": "http://localhost:9090/-/healthy",
                   "unit": "prometheus.service", "container": "prometheus"},
}

# Cheap read of the most recent check_ai_services() call -- same pattern as
# LAST_HEALTH_RESULT/LAST_SECURITY_RESULT above, lets plaid_service.py's
# Homelab widget route and agent_status() show the last known state without
# triggering a fresh network round-trip on every page load.
LAST_AI_SERVICES_RESULT = {}


def _ai_service_up(entry) -> bool:
    """One fast, lightweight HTTP GET against a service's own health
    endpoint -- never more than a few seconds, so this is cheap enough to
    call on every watchdog cycle and every dashboard refresh."""
    try:
        r = requests.get(entry["url"], timeout=4)
        return r.status_code < 500
    except requests.RequestException:
        return False


def check_ai_services() -> str:
    """Read-only health check of the two local AI/monitoring services this
    project relies on -- Ollama (local-fallback chat/vision/passive-memory
    model host) and Prometheus (homelab metrics) -- by hitting each one's
    own lightweight HTTP health endpoint. Never restarts anything itself;
    use restart_ai_service for that. Use this for "is Ollama up"/"is
    Prometheus down"/"check my monitoring services" requests -- it's also
    what the background watchdog thread and the Homelab HUD widget call to
    decide whether a service needs restarting."""
    global LAST_AI_SERVICES_RESULT
    statuses = {key: {"label": entry["label"], "up": _ai_service_up(entry)}
                for key, entry in _AI_SERVICES.items()}
    LAST_AI_SERVICES_RESULT = statuses
    down = [s["label"] for s in statuses.values() if not s["up"]]
    if not down:
        return "Ollama and Prometheus are both up sir."
    return f"{' and '.join(down)} {'is' if len(down) == 1 else 'are'} down sir."


def restart_ai_service(service: str) -> str:
    """Restart ONE down local service. `service` must be exactly "ollama"
    or "prometheus" -- nothing else is accepted (see _AI_SERVICES above;
    this can never become an arbitrary systemctl/docker target). Tries a
    running Docker container of that name first, then a systemd unit
    (user-level, then system-level via non-interactive sudo -- same
    fails-fast pattern this file's other root-needing diagnostics use).
    Use this when check_ai_services (or the background watchdog) reports
    one of these two as down and it needs to be brought back up."""
    key = (service or "").strip().lower()
    entry = _AI_SERVICES.get(key)
    if not entry:
        return f"'{service}' isn't a service I can restart sir -- only: {', '.join(_AI_SERVICES)}."

    def _post_restart_note(verb):
        time.sleep(2)
        ok = _ai_service_up(entry)
        return f"{verb} {entry['label']} sir -- {'back up' if ok else 'still not answering yet, give it a moment'}."

    docker = shutil.which("docker")
    if docker:
        try:
            names = subprocess.run([docker, "ps", "-a", "--format", "{{.Names}}"],
                                    capture_output=True, text=True, timeout=10).stdout.splitlines()
        except Exception:
            names = []
        if entry["container"] in names:
            try:
                r = subprocess.run([docker, "restart", entry["container"]],
                                    capture_output=True, text=True, timeout=30)
            except Exception as e:
                return f"Docker restart of {entry['label']} failed sir: {e}"
            if r.returncode != 0:
                return f"Docker restart of {entry['label']} failed sir: {(r.stderr or r.stdout).strip()}"
            return _post_restart_note("Restarted (Docker container)")

    if IS_WINDOWS:
        return (f"I don't have a way to restart {entry['label']} on Windows sir -- "
                "it isn't running as a Docker container here, and there's no systemd on this OS.")

    if not shutil.which("systemctl"):
        return f"I couldn't find Docker or systemctl on this machine sir -- can't restart {entry['label']}."

    try:
        r = subprocess.run(["systemctl", "--user", "restart", entry["unit"]],
                            capture_output=True, text=True, timeout=20)
    except Exception as e:
        return f"I couldn't restart {entry['label']} sir: {e}"
    if r.returncode == 0:
        return _post_restart_note("Restarted (user service)")

    r2, needs_password = _sudo_n(["systemctl", "restart", entry["unit"]], 20)
    if needs_password or r2 is None:
        return (f"{entry['label']} isn't running as a user service, and passwordless sudo for "
                "systemctl isn't set up sir -- see the README's Homelab section for the NOPASSWD "
                "rule, or ask me to run_admin_action the sudo restart once.")
    if r2.returncode != 0:
        return f"I couldn't restart {entry['label']} sir: {(r2.stderr or r2.stdout).strip()}"
    return _post_restart_note("Restarted")


# ================================================================ SECURITY (JarSecurity)
SECURITY_LOG_PATH = os.path.join(JARVIS_DIR, "security_log.jsonl")

# Curated, named substrings of real-world Linux cryptominer/backdoor process
# names (XMRig family, Kinsing, perfctl, ...) -- a heuristic, not exhaustive,
# but enough to catch the loud, common cases without a generic "flag
# anything that looks weird" rule that would just be noisy false positives.
_SUSPICIOUS_PROCESS_NAMES = (
    "xmrig", "kinsing", "kdevtmpfsi", "kthreaddi", "kthreaddk", "minerd",
    "moneroocean", "perfctl", "ddgs", "cryptonight", "sysrv", "diicot", "skidmap",
)
# Directories a legitimate long-running process should never be executing
# out of -- classic dropper/persistence locations for malware that writes
# itself somewhere world-writable and self-launches.
_SUSPICIOUS_EXEC_DIRS = ("/tmp/", "/dev/shm/", "/var/tmp/")

# Structured result of the most recent run_security_check() call -- lets
# callers (e.g. jarvis.py's security-watcher thread) branch on whether it
# was critical without re-parsing the human-readable reply string.
LAST_SECURITY_RESULT = {}

# Structured results of the most recent run_security_audit()/
# run_rootkit_scan() calls -- same pattern as LAST_SECURITY_RESULT above,
# so jarvis.py's deep-scan watcher thread can log real numbers instead of
# re-parsing the human-readable return string.
LAST_AUDIT_RESULT = {}
LAST_ROOTKIT_RESULT = {}


def _log_security_event(entry):
    _ensure_jarvis_dir()
    entry = dict(entry, ts=time.time())
    try:
        with open(SECURITY_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def _list_processes_linux():
    """[(pid, comm, exe_path_or_none)] read straight from /proc -- no extra
    dependency needed, same read-only style as _read_linux_temps()."""
    procs = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/comm", "r", encoding="utf-8", errors="replace") as f:
                comm = f.read().strip()
        except Exception:
            continue
        try:
            exe = os.readlink(f"/proc/{entry}/exe")
        except Exception:
            exe = None
        procs.append((entry, comm, exe))
    return procs


def _find_suspicious_processes():
    """Flag any running process matching the curated cryptominer/backdoor
    name list, or executing out of a world-writable temp directory. Never
    kills or touches anything -- report-only, same as the rest of
    run_security_check()."""
    if IS_WINDOWS:
        entry = _DIAGNOSTIC_COMMANDS_WIN["processes"]
        try:
            out = subprocess.run(entry[0], capture_output=True, text=True, timeout=15).stdout
        except Exception:
            out = ""
        return [line.strip() for line in out.splitlines()
                if any(name in line.lower() for name in _SUSPICIOUS_PROCESS_NAMES)]
    hits = []
    for pid, comm, exe in _list_processes_linux():
        lname = comm.lower()
        if any(name in lname for name in _SUSPICIOUS_PROCESS_NAMES):
            hits.append(f"{comm} (pid {pid})")
        elif exe and any(exe.startswith(d) for d in _SUSPICIOUS_EXEC_DIRS):
            hits.append(f"{comm} (pid {pid}) running from {exe}")
    return hits


def _classify_bind_scope(addr, tailscale_ip=None):
    """Classifies a "host:port"-style bind address (as ss -tulpn/netstat
    prints it) into "loopback" / "tailscale" / "exposed". "exposed" means
    reachable from the LAN or WAN -- anything that isn't explicitly
    loopback or this host's own Tailscale address. Never guesses: an
    unrecognized or malformed address is treated as exposed rather than
    silently trusted -- this is what lets run_security_check() catch a
    service accidentally bound to 0.0.0.0 the same way the manual port
    audit did (see the Grafana/Ollama/uptime-kuma findings)."""
    host = addr
    if host.startswith("["):
        host = host.split("]", 1)[0][1:]
    else:
        host = host.rsplit(":", 1)[0]
    host = host.split("%", 1)[0]  # drop a %iface suffix, e.g. 127.0.0.53%lo

    if host in ("*", "0.0.0.0", "::", ""):
        return "exposed"
    if host == "127.0.0.1" or host.startswith("127.") or host == "::1":
        return "loopback"
    if tailscale_ip and host == tailscale_ip:
        return "tailscale"
    if host.startswith("fd7a:115c:a1e0:"):  # this project's tailnet's IPv6 ULA prefix
        return "tailscale"
    if re.match(r"^100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.", host):  # 100.64.0.0/10, Tailscale's CGNAT range
        return "tailscale"
    return "exposed"


def _open_listening_ports():
    """Structured list of {addr, port, proto, scope, process} for every
    currently-LISTENing local socket, via the same read-only diagnostic
    commands run_diagnostic_command already exposes (ss -tulpn /
    netstat -an) -- report-only, never touches or closes anything. `scope`
    (see _classify_bind_scope above) is what lets run_security_check()
    and JarSecurity's alert tell a normal loopback/Tailscale-bound service
    apart from one actually reachable off this machine."""
    entry = _DIAGNOSTIC_COMMANDS.get("listening_ports")
    if not entry:
        return []
    try:
        out = subprocess.run(entry[0], capture_output=True, text=True, timeout=15).stdout
    except Exception:
        return []

    tailscale_ip = None
    if tailscale_service.TAILSCALE_AVAILABLE:
        try:
            tailscale_ip = tailscale_service.get_status().get("self_ip")
        except Exception:
            tailscale_ip = None

    ports, seen = [], set()
    for line in out.splitlines():
        parts = line.split()
        if IS_WINDOWS:
            if len(parts) >= 4 and parts[0] in ("TCP", "UDP") and "LISTENING" in line.upper():
                proto, addr, process = parts[0].lower(), parts[1], ""
            else:
                continue
        else:
            if len(parts) >= 5 and parts[1] == "LISTEN":
                proto, addr = parts[0], parts[4]
                m = re.search(r'users:\(\("([^"]+)"', line)
                process = m.group(1) if m else ""
            else:
                continue
        if addr in seen:
            continue
        seen.add(addr)
        host_port = addr.rsplit(":", 1)
        port = host_port[1] if len(host_port) == 2 else ""
        ports.append({
            "addr": addr, "port": port, "proto": proto,
            "scope": _classify_bind_scope(addr, tailscale_ip),
            "process": process,
        })
    return ports


def _format_port_detail(ports):
    """Human-readable port table -- one source of truth shared by the
    Telegram alert text (run_security_check) and JarSecurity's grounded
    chat report (security_status_report), so "what ports are open" gets
    the same real detail either way instead of two different summaries."""
    if not ports:
        return "no listening ports found"
    exposed = [p for p in ports if p["scope"] == "exposed"]
    lines = []
    for p in sorted(ports, key=lambda p: (p["scope"] != "exposed", p["addr"])):
        proc = f" ({p['process']})" if p["process"] else ""
        lines.append(f"  {p['addr']}/{p['proto']} -- {p['scope']}{proc}")
    header = f"{len(ports)} listening port(s), {len(exposed)} exposed (LAN/WAN-reachable)"
    return header + ":\n" + "\n".join(lines)


def _check_firmware_and_drivers():
    """Read-only, best-effort firmware/driver freshness check -- report-only,
    same philosophy as the rest of run_security_check(): never installs or
    applies an update itself, just flags what's outdated. Linux: uses
    fwupdmgr (the standard LVFS firmware-update tool) to list devices with an
    available firmware update. Windows: uses pnputil to list devices
    currently reporting a driver problem (Windows has no built-in,
    scriptable "check for newer driver" query short of the paid Windows
    Update APIs, so a problem-device count is the honest, achievable signal
    here). Returns (summary_text, flagged)."""
    if IS_WINDOWS:
        if not shutil.which("pnputil"):
            return "unavailable -- pnputil not found", False
        try:
            out = subprocess.run(["pnputil", "/enum-devices", "/problem"],
                                  capture_output=True, text=True, timeout=20).stdout
        except Exception as e:
            return f"unavailable ({e})", False
        problem_count = out.count("Instance ID:")
        if problem_count:
            return f"{problem_count} device(s) with driver problems", True
        return "no driver problems detected", False

    if not shutil.which("fwupdmgr"):
        return "unavailable -- fwupdmgr not installed", False
    try:
        proc = subprocess.run(["fwupdmgr", "get-updates", "--json"],
                               capture_output=True, text=True, timeout=45)
        data = json.loads(proc.stdout) if proc.stdout.strip() else {}
        devices = data.get("Devices", [])
    except Exception as e:
        return f"unavailable ({e})", False
    if devices:
        names = sorted({d.get("Name") or d.get("Vendor") or "device" for d in devices})[:5]
        return f"{len(devices)} firmware update(s) available ({', '.join(names)})", True
    return "firmware up to date", False


def check_firmware_drivers() -> str:
    """Read-only check for outdated device firmware (via fwupdmgr on Linux)
    or devices reporting driver problems (via pnputil on Windows). Never
    installs or applies anything itself -- also runs automatically as part
    of JarSecurity's scheduled sweep. Use this whenever the user asks to
    check their firmware/drivers."""
    note, flagged = _check_firmware_and_drivers()
    if flagged:
        return f"Firmware/driver check sir: {note} -- worth a look."
    return f"Firmware/driver check sir: {note}."


def _check_dependency_vulnerabilities():
    """Read-only dependency vulnerability check -- report-only, same
    philosophy as _check_firmware_and_drivers: never upgrades anything
    itself, just flags what's known-vulnerable against the public
    advisory databases (pip-audit uses the Python Packaging Advisory
    Database; npm audit uses the npm registry's own advisory data).
    Checks this project's actual installed Python packages (via
    `sys.executable -m pip_audit`, so it's always the same interpreter/venv
    Jarvis itself runs in) and its Node dependencies (`npm audit`, in this
    project's own directory). Returns (summary_text, flagged)."""
    parts = []
    flagged = False

    try:
        proc = subprocess.run([sys.executable, "-m", "pip_audit", "--format", "json"],
                               capture_output=True, text=True, timeout=90)
        if proc.stdout.strip():
            data = json.loads(proc.stdout)
            vulns = [v for pkg in data.get("dependencies", []) for v in (pkg.get("vulns") or [])]
            if vulns:
                flagged = True
                parts.append(f"pip: {len(vulns)} known vulnerabilit{'y' if len(vulns) == 1 else 'ies'}")
            else:
                parts.append("pip: clean")
        else:
            parts.append(f"pip: unavailable ({(proc.stderr or 'no output').strip()[:80]})")
    except FileNotFoundError:
        parts.append("pip: pip-audit not installed (pip install pip-audit)")
    except Exception as e:
        parts.append(f"pip: unavailable ({e})")

    npm_bin = shutil.which("npm")
    project_dir = os.path.dirname(os.path.abspath(__file__))
    if npm_bin:
        try:
            proc = subprocess.run([npm_bin, "audit", "--omit=dev", "--json"],
                                   cwd=project_dir, capture_output=True, text=True, timeout=60)
            data = json.loads(proc.stdout) if proc.stdout.strip() else {}
            total = (data.get("metadata", {}) or {}).get("vulnerabilities", {}).get("total", 0)
            if total:
                flagged = True
                parts.append(f"npm: {total} known vulnerabilit{'y' if total == 1 else 'ies'}")
            else:
                parts.append("npm: clean")
        except Exception as e:
            parts.append(f"npm: unavailable ({e})")
    else:
        parts.append("npm: unavailable -- npm not found")

    return "; ".join(parts), flagged


def run_security_check() -> str:
    """JarSecurity sub-agent's core sweep: lists listening TCP/UDP ports,
    flags any running process matching a curated list of known
    cryptominer/backdoor names or executing out of a world-writable temp
    directory, re-runs the same LAN scan scan_network() already does
    (flagging brand-new devices as possible intruders), flags outdated
    device firmware/drivers (see check_firmware_drivers), flags known
    vulnerabilities in this project's own Python/Node dependencies (pip-audit
    + npm audit), refreshes the local malicious-link blocklist used by
    open_website/search_web/scan_url_safety, and verifies -- installing if
    missing -- each vetted browser security/privacy extension (uBlock Origin
    Lite, DuckDuckGo Privacy Essentials) in the dedicated Jarvis browser
    window. Every run is logged to ~/.jarvis/security_log.jsonl. Safe to
    call anytime on demand; also runs automatically in the background on a
    fixed schedule."""
    global LAST_SECURITY_RESULT
    ports = _open_listening_ports()
    suspicious = _find_suspicious_processes()

    net = network_watch.scan()
    net_error = net.get("error", "")
    new_count = net.get("new_count", 0) if not net_error else 0
    device_count = len(net.get("devices", [])) if not net_error else 0
    new_device_list = [d for d in net.get("devices", []) if d.get("new")] if not net_error else []

    firmware_note, firmware_flagged = _check_firmware_and_drivers()
    dependency_note, dependency_flagged = _check_dependency_vulnerabilities()

    blocklist_result = refresh_threat_blocklist()
    if blocklist_result.get("ok"):
        blocklist_hosts = len(_load_threat_blocklist_hosts())
        blocklist_note = f"{blocklist_hosts} known-malicious hosts loaded"
    else:
        blocklist_note = f"refresh failed ({blocklist_result.get('error', 'unknown error')})"

    def _sweep_extension(label, verify_fn, install_fn):
        status = verify_fn()
        if status.get("installed"):
            return True, f"{label} verified"
        installed = install_fn()
        ok = bool(installed.get("ok"))
        note = "installed just now" if ok else f"install failed ({installed.get('error', 'unknown error')})"
        return ok, f"{label} {note}"

    if BROWSER_CONTROL_AVAILABLE:
        ublock_ok, ublock_summary = _sweep_extension(
            "uBlock Origin Lite", browser_control.verify_ublock_origin, browser_control.install_ublock_origin)
        ddg_ok, ddg_summary = _sweep_extension(
            "DuckDuckGo Privacy Essentials", browser_control.verify_duckduckgo_privacy, browser_control.install_duckduckgo_privacy)
    else:
        ublock_ok = ddg_ok = False
        ublock_summary = "uBlock Origin Lite unavailable -- browser control module isn't available"
        ddg_summary = "DuckDuckGo Privacy Essentials unavailable -- browser control module isn't available"

    exposed_ports = [p for p in ports if p["scope"] == "exposed"]
    critical = bool(suspicious) or new_count > 0 or dependency_flagged or bool(exposed_ports)

    summary_parts = [f"{len(ports)} listening port{'s' if len(ports) != 1 else ''}"
                      + (f", {len(exposed_ports)} EXPOSED (LAN/WAN-reachable)" if exposed_ports else ", none exposed")]
    if suspicious:
        summary_parts.append(
            f"{len(suspicious)} suspicious process{'es' if len(suspicious) != 1 else ''} flagged: "
            + "; ".join(suspicious)
        )
    else:
        summary_parts.append("no suspicious processes")
    if net_error:
        summary_parts.append(f"network scan unavailable ({net_error})")
    elif new_count:
        summary_parts.append(f"{new_count} new device{'s' if new_count != 1 else ''} on the LAN")
    else:
        summary_parts.append(f"{device_count} known devices on the LAN, nothing new")
    summary_parts.append(f"firmware/drivers: {firmware_note}")
    summary_parts.append(f"dependencies: {dependency_note}")
    summary_parts.append(f"threat blocklist: {blocklist_note}")
    summary_parts.append(ublock_summary)
    summary_parts.append(ddg_summary)
    result_text = "; ".join(summary_parts) + "."
    if exposed_ports:
        result_text += "\n" + _format_port_detail(exposed_ports)

    LAST_SECURITY_RESULT = {
        "critical": critical, "open_ports": len(ports), "ports": ports, "exposed_ports": len(exposed_ports),
        "suspicious": suspicious,
        "new_devices": new_count, "new_device_list": new_device_list,
        "firmware_flagged": firmware_flagged,
        "dependency_flagged": dependency_flagged,
        "ublock_ok": ublock_ok, "ddg_ok": ddg_ok,
    }
    _log_security_event({
        "open_ports": len(ports), "ports": ports, "exposed_ports": len(exposed_ports),
        "suspicious": suspicious, "new_devices": new_count,
        "firmware_flagged": firmware_flagged, "dependency_flagged": dependency_flagged,
        "ublock_ok": ublock_ok, "ddg_ok": ddg_ok,
        "critical": critical,
    })
    return result_text


def verify_ublock_origin() -> str:
    """Read-only check: is uBlock Origin Lite (ad/tracker blocker) already
    installed and verified in the dedicated Jarvis browser window? Never
    downloads anything itself -- use install_ublock_origin for that. Use
    this whenever the user asks whether uBlock/an ad blocker is set up in
    the Jarvis browser."""
    if not BROWSER_CONTROL_AVAILABLE:
        return "The browser control module isn't available sir, so I can't check that."
    status = browser_control.verify_ublock_origin()
    if status.get("installed"):
        return "uBlock Origin Lite is installed and verified in your browser sir."
    return "uBlock Origin Lite isn't installed in your browser yet sir -- say \"install ublock\" and I'll fetch it."


def install_ublock_origin() -> str:
    """Download the latest official uBlock Origin Lite release straight
    from its own GitHub repo and install it (unpacked) into the dedicated
    Jarvis browser window -- a no-op if a verified copy is already there.
    Use this whenever the user asks to install/set up an ad blocker in the
    Jarvis browser. Close and reopen the browser window afterward for a
    freshly installed extension to actually load."""
    if not BROWSER_CONTROL_AVAILABLE:
        return "The browser control module isn't available sir, so I can't install that."
    result = browser_control.install_ublock_origin()
    if not result.get("ok"):
        return f"I couldn't install uBlock Origin Lite sir: {result.get('error', 'unknown error')}."
    if result.get("already_installed"):
        return "uBlock Origin Lite is already installed and verified sir."
    return "uBlock Origin Lite installed and verified sir -- close and reopen the browser window for it to take effect."


def verify_duckduckgo_privacy() -> str:
    """Read-only check: is DuckDuckGo Privacy Essentials (tracker blocking,
    HTTPS upgrade, Fire button) already installed and verified in the
    dedicated Jarvis browser window? Never downloads anything itself -- use
    install_duckduckgo_privacy for that."""
    if not BROWSER_CONTROL_AVAILABLE:
        return "The browser control module isn't available sir, so I can't check that."
    status = browser_control.verify_duckduckgo_privacy()
    if status.get("installed"):
        return "DuckDuckGo Privacy Essentials is installed and verified in your browser sir."
    return ("DuckDuckGo Privacy Essentials isn't installed in your browser yet sir -- "
            "say \"install duckduckgo privacy\" and I'll fetch it.")


def install_duckduckgo_privacy() -> str:
    """Download the latest official DuckDuckGo Privacy Essentials release
    straight from its own GitHub repo and install it (unpacked) into the
    dedicated Jarvis browser window -- a no-op if a verified copy is already
    there. Use this whenever the user asks to install/set up DuckDuckGo's
    tracker-blocking/privacy extension in the Jarvis browser. Close and
    reopen the browser window afterward for a freshly installed extension to
    actually load."""
    if not BROWSER_CONTROL_AVAILABLE:
        return "The browser control module isn't available sir, so I can't install that."
    result = browser_control.install_duckduckgo_privacy()
    if not result.get("ok"):
        return f"I couldn't install DuckDuckGo Privacy Essentials sir: {result.get('error', 'unknown error')}."
    if result.get("already_installed"):
        return "DuckDuckGo Privacy Essentials is already installed and verified sir."
    return ("DuckDuckGo Privacy Essentials installed and verified sir -- "
            "close and reopen the browser window for it to take effect.")


# ================================================================ TAILSCALE
def get_tailscale_status() -> str:
    """Report the full tailnet picture -- this machine's own Tailscale IP
    and connection state, plus every other device on the tailnet (name,
    IP, OS, online/offline). Use this for "what's on my tailnet"/"is my
    NAS reachable over tailscale"/"what's my tailscale IP" requests."""
    if not tailscale_service.TAILSCALE_AVAILABLE:
        return "Tailscale isn't installed on this machine sir."
    try:
        status = tailscale_service.get_status()
    except Exception as e:
        return f"I couldn't reach Tailscale sir: {e}"
    online = [p for p in status["peers"] if p["online"]]
    offline = [p for p in status["peers"] if not p["online"]]
    parts = [f"I'm {status['self_name']} at {status['self_ip']} sir, {status['backend_state'].lower()}."]
    if online:
        parts.append(f"Online: {', '.join(p['name'] for p in online)}.")
    if offline:
        parts.append(f"Offline: {', '.join(p['name'] for p in offline)}.")
    if status["using_exit_node"]:
        parts.append("Routing traffic through an exit node right now.")
    return " ".join(parts)


def tailscale_ping(device: str) -> str:
    """Check real connectivity to a named device on your tailnet (phone,
    NAS, other PCs) -- a real Tailscale ping, not just checking last-seen
    status. Use for "can you reach my NAS over tailscale"/"ping my phone
    on tailscale" requests."""
    if not tailscale_service.TAILSCALE_AVAILABLE:
        return "Tailscale isn't installed on this machine sir."
    if not device or not device.strip():
        return "Which device sir?"
    try:
        result = tailscale_service.ping(device)
    except Exception as e:
        return f"I couldn't ping that sir: {e}"
    if result["ok"]:
        return f"{device} responded sir -- {result['detail'].splitlines()[0] if result['detail'] else 'reachable'}."
    return f"I couldn't reach {device} over Tailscale sir."


def set_tailscale_exit_node(device: str) -> str:
    """Route this machine's internet traffic through a named tailnet
    device that offers exit-node routing, or turn it off (device="off").
    Use for "route my traffic through my NAS"/"use [device] as an exit
    node"/"turn off the exit node" requests. Needs a one-time permission
    grant on this machine the first time (message will say exactly what to
    run if that's missing)."""
    if not tailscale_service.TAILSCALE_AVAILABLE:
        return "Tailscale isn't installed on this machine sir."
    try:
        result = tailscale_service.set_exit_node(device)
    except Exception as e:
        return f"I couldn't change that sir: {e}"
    return f"{result['message'].rstrip('.')} sir."


def tailscale_connect() -> str:
    """Bring this machine's Tailscale connection up. Needs a one-time
    permission grant the first time (message will say exactly what to run
    if that's missing)."""
    if not tailscale_service.TAILSCALE_AVAILABLE:
        return "Tailscale isn't installed on this machine sir."
    try:
        tailscale_service.connect()
    except Exception as e:
        return f"I couldn't connect sir: {e}"
    return "Tailscale's up sir."


def tailscale_disconnect() -> str:
    """Take this machine off the tailnet. Note this also cuts off any
    remote access to Jarvis that goes over Tailscale specifically (LAN
    access is unaffected). Needs a one-time permission grant the first
    time (message will say exactly what to run if that's missing)."""
    if not tailscale_service.TAILSCALE_AVAILABLE:
        return "Tailscale isn't installed on this machine sir."
    try:
        tailscale_service.disconnect()
    except Exception as e:
        return f"I couldn't disconnect sir: {e}"
    return "Tailscale's down sir."


# ================================================================ FINANCE (unchanged services, thin wrappers)
def sync_bank_data() -> str:
    """Sync the latest bank transactions into Jarvis's records. Call this on
    any "sync statements", "sync my bank data", "grab my bank statements
    from the NAS", or "import my statements" request. Pulls in new files
    from up to two sources before parsing: (1) if the NAS is configured,
    whatever's in the "Bank Statements" folder under SYNOLOGY_BASE_PATH on
    the Synology NAS -- drop CSVs there from any device and this picks them
    up automatically; (2) whatever file(s) the user most recently sent (as
    a document or a screenshot/photo) to the Telegram bot. Then parses
    every statement file found in JarvisStatements (including both of the
    above, plus anything dropped there directly) into the local
    transaction store that get_spending_summary reads from -- whatever the
    format (CSV, TXT, PDF, XLSX/XLS, OFX/QFX, a screenshot, or a ZIP
    bundling any of those) and whatever it's named."""
    if not STATEMENTS_AVAILABLE:
        return "Statement syncing isn't set up sir."
    nas_pulled = 0
    if synology_service.SYNOLOGY_AVAILABLE:
        try:
            nas_result = synology_service.download_folder("Bank Statements", statements_service.STATEMENTS_DIR)
            nas_pulled = nas_result["files_downloaded"]
        except Exception as e:
            print(f"  [Statements] NAS pull error: {e}")
    imported = []
    if TELEGRAM_AVAILABLE:
        try:
            imported = telegram_bridge.import_latest_to(statements_service.LATEST_STATEMENTS_DIR)
        except Exception as e:
            print(f"  [Statements] Telegram import error: {e}")
    try:
        result = statements_service.sync()
    except Exception as e:
        return f"I couldn't sync your statements sir: {e}"
    if result["files_seen"] == 0:
        return ("I didn't find any statement files to sync sir. Drop one in the NAS's "
                 "Bank Statements folder, send it to the Telegram bot (CSV, TXT, PDF, XLSX, "
                 "OFX/QFX, a screenshot, or a ZIP), or drop it in the JarvisStatements folder "
                 "directly.")
    sources = []
    if nas_pulled:
        sources.append(f"{nas_pulled} file(s) from the NAS")
    if imported:
        sources.append(f"{len(imported)} file(s) from Telegram")
    prefix = f"Pulled {' and '.join(sources)} and synced sir. " if sources else "Synced sir. "
    return f"{prefix}Found {result['new_transactions']} new transactions across {result['files_seen']} files."


def get_spending_summary() -> str:
    """Answer any financial question about spending, budgets, categories, or
    trends -- summarizes the last 30 days by category and merchant, how that
    compares to the prior 30 days, from synced bank statements (including
    ones imported via Telegram) or a linked Plaid account."""
    summary = None
    if STATEMENTS_AVAILABLE and statements_service.has_data():
        try:
            summary = statements_service.get_spending_summary(days=30)
        except Exception:
            pass
    if summary is None and PLAID_AVAILABLE and plaid_service.is_linked():
        try:
            summary = plaid_service.get_spending_summary(days=30)
        except Exception as e:
            return f"I couldn't reach Plaid just now sir: {e}"
    if summary is None:
        return ("I don't have any spending data yet sir. Send me a bank statement over Telegram "
                 "and say sync statements, drop CSVs or PDFs in the JarvisStatements folder, or "
                 "connect a bank in the Finance widget.")
    if not summary["by_category"]:
        return "No transactions found for the last thirty days sir."
    top = summary["by_category"][0]
    trend_note = ""
    change_pct = summary.get("change_pct")
    if change_pct is not None:
        direction = "up" if change_pct > 0 else "down"
        trend_note = f" That's {direction} {abs(change_pct):.0f} percent from the prior thirty days."
    return (f"Over the last thirty days you've spent {summary['total_spent']:.0f} dollars sir, "
            f"most of it on {top['name']}, about {top['amount']:.0f} dollars.{trend_note}")


def get_financial_insights() -> str:
    """Give a deeper financial read than get_spending_summary -- concrete,
    actionable tips grounded in the actual synced data: whether one category
    is eating an outsized share of spending, a real spike or drop versus the
    prior period, and merchants that look like recurring subscriptions (same
    name and amount, seen repeatedly). Use this specifically for "give me
    financial tips" / "how am I doing with money" / "any insights on my
    spending" requests; use get_spending_summary for a plain numbers recap
    instead."""
    if not (STATEMENTS_AVAILABLE and statements_service.has_data()):
        return ("I don't have enough spending data yet sir -- send me a bank statement over "
                 "Telegram and say sync statements first.")
    try:
        summary = statements_service.get_spending_summary(days=30)
        recurring = statements_service.get_recurring_charges()
    except Exception as e:
        return f"I couldn't pull your spending data sir: {e}"
    if not summary or not summary["by_category"]:
        return "No transactions found for the last thirty days sir, nothing to give tips on yet."

    tips = []
    total = summary["total_spent"] or 0.0
    top = summary["by_category"][0]
    if total > 0 and top["amount"] / total >= 0.4:
        tips.append(f"{top['name']} alone is {top['amount'] / total * 100:.0f} percent of your "
                     f"spending this month, about {top['amount']:.0f} dollars -- that's a lot "
                     f"concentrated in one category.")

    change_pct = summary.get("change_pct")
    if change_pct is not None and change_pct >= 20:
        tips.append(f"You're spending {change_pct:.0f} percent more than the prior thirty days -- "
                     "worth a look at what changed.")
    elif change_pct is not None and change_pct <= -20:
        tips.append(f"You've cut spending {abs(change_pct):.0f} percent versus the prior thirty "
                     "days -- nicely done sir.")

    if recurring:
        lines = "; ".join(f"{r['name']} at {r['amount']:.2f} dollars, seen {r['count']} times"
                           for r in recurring[:3])
        tips.append(f"These look like recurring charges or subscriptions: {lines}.")

    if not tips:
        tips.append("Nothing stands out sir -- your spending looks steady and spread out.")

    return "Financial insights sir: " + " ".join(tips)


def build_finance_dashboard() -> str:
    """Build and show a real financial dashboard grounded in the user's
    actual synced spending data -- category/merchant breakdown, the trend vs
    the prior period, and detected recurring charges -- not a guess. Use this
    specifically for "build me a dashboard" / "show me a spending dashboard" /
    "make me a website of my finances" requests; falls back to asking the
    user to sync statements first if nothing's synced yet."""
    if not (STATEMENTS_AVAILABLE and statements_service.has_data()):
        return ("I don't have any spending data synced yet sir -- send me a bank statement "
                 "over Telegram and say sync statements first.")
    try:
        summary = statements_service.get_spending_summary(days=30)
        recurring = statements_service.get_recurring_charges()
    except Exception as e:
        return f"I couldn't pull your spending data sir: {e}"
    if not summary or not summary["by_category"]:
        return "No transactions found for the last thirty days sir, nothing to build a dashboard from yet."
    data_blob = json.dumps({"summary": summary, "recurring_charges": recurring})
    description = (
        "A personal finance dashboard built from this real spending data (JSON -- use exactly "
        f"these figures, never invent numbers): {data_blob}. Show total spent, a category "
        "breakdown (a bar or donut chart drawn with inline SVG/canvas, no external chart "
        "library), the monthly trend, top merchants, and a short callout list of the detected "
        "recurring charges/subscriptions."
    )
    return build_creation(description, kind="dashboard")


def _finance_watcher_check() -> list:
    """Not an MCP tool -- called directly by jarvis.py's _finance_watcher_thread
    (same relationship check_system_health has to _health_watcher_thread).
    Syncs whatever new bank data is waiting (NAS + Telegram inbox, same as a
    user-initiated "sync my bank data") and returns
    statements_service.check_spending_anomalies()'s result -- empty if
    nothing's new/notable, or if statement syncing isn't set up at all."""
    if not STATEMENTS_AVAILABLE:
        return []
    try:
        sync_bank_data()
    except Exception as e:
        print(f"  [FinanceWatch] Sync error: {e}")
    try:
        return statements_service.check_spending_anomalies()
    except Exception as e:
        print(f"  [FinanceWatch] Anomaly check error: {e}")
        return []


# ================================================================ EYES (screen vision, off by default, on-demand only)
def enable_eyes() -> str:
    """Turn on screen vision. Even when enabled, a screenshot is only ever
    taken at the exact moment describe_screen is called -- never in the
    background or on a timer."""
    _set_state("eyes_enabled", True)
    return "Eyes open sir. I'll only look when you ask me to."


def disable_eyes() -> str:
    """Turn off screen vision."""
    _set_state("eyes_enabled", False)
    return "Eyes closed sir."


def describe_screen() -> str:
    """Take a single screenshot right now and describe what's on it. Only
    works if enable_eyes has been called first."""
    if not _get_state().get("eyes_enabled"):
        return "My eyes are closed sir. Enable them first if you want me to look."
    try:
        from PIL import ImageGrab
        import base64, io
        img = ImageGrab.grab()
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode()
        r = requests.post(OLLAMA_URL, json={
            "model": VISION_MODEL,
            "prompt": "Describe what's currently visible on this screen in a couple of sentences, focusing on the main content.",
            "images": [b64], "stream": False,
        }, timeout=60)
        if r.status_code == 200:
            return r.json().get("response", "I couldn't make sense of that sir.")
        return "I couldn't analyze the screen sir."
    except requests.ConnectionError:
        return "I can't reach Ollama sir, is it running?"
    except Exception as e:
        return f"I had trouble looking at your screen sir: {e}"


# ================================================================ REMINDERS & NOTES
def set_reminder(text: str, minutes: float) -> str:
    """Set a reminder that will be spoken aloud after the given number of
    minutes from now, even across separate conversations (Jarvis keeps
    running in the background)."""
    if not text or minutes is None or minutes < 0:
        return "I need what to remind you about and how many minutes from now sir."
    reminders = _load_json(REMINDERS_PATH, [])
    reminders.append({"text": text, "due": time.time() + float(minutes) * 60})
    _save_json(REMINDERS_PATH, reminders)
    return f"I'll remind you in {minutes:g} minutes sir."


def list_reminders() -> str:
    """List every reminder that hasn't fired yet, soonest first, with how
    much time is left on each."""
    reminders = _load_json(REMINDERS_PATH, [])
    if not reminders:
        return "You don't have any reminders set sir."
    now = time.time()
    lines = []
    for r in sorted(reminders, key=lambda r: r["due"]):
        remaining_min = max(0.0, (r["due"] - now) / 60)
        if remaining_min < 1:
            when = "due any moment"
        elif remaining_min < 60:
            when = f"in {remaining_min:.0f} min"
        else:
            when = f"in {remaining_min / 60:.1f} hr"
        lines.append(f"{r['text']} ({when})")
    return "Your reminders sir: " + "; ".join(lines)


def cancel_reminder(text: str) -> str:
    """Cancel a pending reminder that hasn't fired yet. Matches by a
    substring of the reminder's text -- doesn't need to be exact. If more
    than one reminder matches, none are cancelled and the matches are
    listed so the user can be more specific."""
    if not text or not text.strip():
        return "Which reminder sir?"
    reminders = _load_json(REMINDERS_PATH, [])
    needle = text.strip().lower()
    matches = [r for r in reminders if needle in r["text"].lower()]
    if not matches:
        return f"I couldn't find a reminder matching \"{text}\" sir."
    if len(matches) > 1:
        listed = "; ".join(r["text"] for r in matches)
        return f"That matches more than one reminder sir: {listed}. Be more specific."
    remaining = [r for r in reminders if r is not matches[0]]
    _save_json(REMINDERS_PATH, remaining)
    return f"Cancelled the reminder: {matches[0]['text']} sir."


def add_note(text: str) -> str:
    """Save a short note for later, retrievable with list_notes."""
    if not text:
        return "What should the note say sir?"
    notes = _load_json(NOTES_PATH, [])
    notes.append({"text": text, "ts": time.time()})
    _save_json(NOTES_PATH, notes)
    return "Noted sir."


def list_notes() -> str:
    """Read back the user's saved notes."""
    notes = _load_json(NOTES_PATH, [])
    if not notes:
        return "You don't have any notes saved sir."
    lines = [n["text"] for n in notes[-10:]]
    return "Your notes: " + "; ".join(lines)


def delete_note(text: str) -> str:
    """Delete a saved note. Matches by a substring of the note's text --
    doesn't need to be exact. If more than one note matches, none are
    deleted and the matches are listed so the user can be more specific."""
    if not text or not text.strip():
        return "Which note sir?"
    notes = _load_json(NOTES_PATH, [])
    needle = text.strip().lower()
    matches = [n for n in notes if needle in n["text"].lower()]
    if not matches:
        return f"I couldn't find a note matching \"{text}\" sir."
    if len(matches) > 1:
        listed = "; ".join(n["text"] for n in matches)
        return f"That matches more than one note sir: {listed}. Be more specific."
    remaining = [n for n in notes if n is not matches[0]]
    _save_json(NOTES_PATH, remaining)
    return f"Deleted the note: {matches[0]['text']} sir."


# ================================================================ LONG-TERM MEMORY
# Distinct from notes above: notes are a plain explicit list the user
# manages directly; memory is searchable (memory_store.recall's FTS5
# ranking) and partly self-maintaining -- most of what ends up here comes
# from brain.py's passive per-turn capture (memory_store.maybe_capture),
# not just what the user explicitly asks to remember.
def remember_this(text: str) -> str:
    """Explicitly save something to Jarvis's long-term memory -- a
    preference, a fact, a standing instruction -- so it can be recalled in
    future conversations, potentially days or weeks later (this is real
    persistent memory, not the short rolling conversation-recap window).
    Use this whenever the user says "remember that...", "keep in mind
    that...", or clearly states something they want you to retain going
    forward. Jarvis also passively captures durable facts from ordinary
    conversation on its own -- this tool is for when the user explicitly
    asks."""
    if not text or not text.strip():
        return "What should I remember sir?"
    memory_store.remember(text.strip(), category="fact")
    return f"I'll remember that sir: {text.strip()}"


def recall_memory(query: str) -> str:
    """Search Jarvis's long-term memory for anything relevant to a topic --
    use this whenever answering the user might benefit from something
    remembered from a past conversation (a stated preference, a fact about
    them, a prior decision) that isn't already in the current
    conversation's recap. Most everyday commands don't need this; reach
    for it when the user references something from "before" or asks what
    you know/remember about a topic."""
    if not query or not query.strip():
        return "What should I search for sir?"
    hits = memory_store.recall(query.strip(), limit=5)
    if not hits:
        return "I don't have anything remembered on that sir."
    return "Here's what I remember sir: " + " / ".join(h["text"] for h in hits)


def list_recent_memories() -> str:
    """List the most recent things Jarvis has remembered, newest first --
    use this for "what do you remember about me" / "what have you learned
    about me" type requests."""
    hits = memory_store.recent(10)
    if not hits:
        return "I don't have anything in long-term memory yet sir."
    return "Recent memory sir: " + "; ".join(h["text"] for h in hits)


def due_reminders():
    """Not an MCP tool -- called directly by jarvis.py's background watcher
    thread. Returns and clears any reminders whose time has come."""
    reminders = _load_json(REMINDERS_PATH, [])
    now = time.time()
    due = [r for r in reminders if r["due"] <= now]
    remaining = [r for r in reminders if r["due"] > now]
    if due:
        _save_json(REMINDERS_PATH, remaining)
    return due


# ================================================================ CREATOR MODE
WEBSITES_DIR = os.path.join(HOME, "JarvisWebsites")
CREATIONS_DIR = os.path.join(HOME, "JarvisCreations")


def _slugify(text):
    slug = re.sub(r'[^a-z0-9]+', '-', (text or "").lower()).strip('-')
    return slug[:40] or "creation"


def build_creation(description: str, kind: str = "dashboard") -> str:
    """Build something and show it to the user -- a single self-contained
    HTML page. kind="dashboard" for something styled to match the HUD (a
    widget, a visualization, a small tool) -- dark/glass/cyan monospace.
    kind="webpage" for a full site with clean, modern, professional styling
    instead. Either way, the moment it's ready it pops into the HUD's own
    creation panel AND is opened front-and-center in the user's real browser
    (Brave if installed) -- like a Claude artifact appearing, not something
    built silently in the background. This can take up to several minutes
    for anything nontrivial; the user will see a live indicator while it
    builds."""
    if not os.path.exists(CLAUDE_CLI):
        return json.dumps({"ok": False, "message": "I can't find Claude Code on this system sir."})

    kind = "webpage" if kind == "webpage" else "dashboard"
    base_dir = WEBSITES_DIR if kind == "webpage" else CREATIONS_DIR
    slug = _slugify(description)
    target_dir = os.path.join(base_dir, slug)
    os.makedirs(target_dir, exist_ok=True)

    style_note = (
        "Style it to match a dark sci-fi HUD: near-black background, cyan "
        "(#00f0ff) glow and borders, translucent glass panels (backdrop-filter "
        "blur), monospace uppercase labels with letter-spacing -- it will be "
        "shown inside a floating panel on top of a Jarvis-style interface."
        if kind == "dashboard" else
        "Give it clean, modern, professional styling appropriate to the subject."
    )
    prompt = (
        "Build a single self-contained static HTML page (inline CSS and JS "
        "only -- no build step, no package installs, no external servers, no "
        "fetch/XHR/WebSocket network calls, no localStorage) based on this "
        f"description: {description}. {style_note} Save it directly as "
        "index.html in the current directory."
    )
    try:
        subprocess.run(
            [CLAUDE_CLI, "-p", prompt,
             "--permission-mode", "acceptEdits",
             "--tools", "Write Edit Read Glob",
             "--allowedTools", "Write Edit Read Glob"],
            cwd=target_dir, capture_output=True, text=True, timeout=600,
        )
    except subprocess.TimeoutExpired:
        return json.dumps({"ok": False, "message": "That took too long sir, I stopped waiting."})
    except Exception as e:
        return json.dumps({"ok": False, "message": f"Something went wrong building that sir: {e}"})

    index_path = os.path.join(target_dir, "index.html")
    if not os.path.exists(index_path):
        return json.dumps({"ok": False, "message": "I finished but couldn't find the result sir."})

    # slug is included so callers never have to re-derive it (it's already
    # exactly the folder name -- see _slugify above); notify_creation_ready
    # below is what actually works out the phone/tailnet link and notifies
    # the user, called separately by whichever caller is running in
    # jarvis.py's own long-lived process (see that function's docstring for
    # why it can't safely happen in here).
    return json.dumps({
        "ok": True, "kind": kind, "title": description, "slug": slug,
        "path": index_path.replace("\\", "/"),
    })


def _verify_creation_url(url: str) -> bool:
    """A quick real GET against a just-built creation's own phone/tailnet
    URL -- confirms the page actually loads (200) before notify_creation_
    ready trusts it enough to text/record it, rather than trusting that
    dashboard_server._server_info being populated means the server is
    actually up and serving right now. Short timeout since this sits in
    the "immediately after it's built" notification path -- a slow/dead
    server should fail fast, not delay the notice."""
    try:
        resp = requests.get(url, timeout=5)
        return resp.status_code == 200
    except Exception:
        return False


def notify_creation_ready(payload: dict):
    """Not an MCP tool -- called once per successful build_creation() (or
    build_finance_dashboard(), which returns the same payload shape) by
    whichever caller is running inside jarvis.py's own long-lived process:
    jarvis.py's _on_brain_creation for the interactive voice/chat path, and
    employees.py's _run_job for the designer employee's background jobs.

    Deliberately NOT called from inside build_creation() itself: build_
    creation() is also invoked from within the short-lived, separate `claude
    -p --mcp-config jarvis_mcp_server.py` subprocess brain.py spawns for
    every interactive command (see brain.py's module docstring) -- that
    subprocess never calls dashboard_server.start_server(), so
    dashboard_server._server_info would always be empty there and
    creation_url() would always (wrongly) report no phone link available,
    even when the real phone dashboard in the main process is up. Calling
    this from the main process instead is what makes the link determination
    actually reliable.

    Persists the creation (with or without a link -- see record_creation)
    and always sends a Telegram notice, with the link if one's available or
    an explicit note that it isn't, so the user is never left hearing
    nothing. Returns the phone/tailnet URL (or None).

    Before trusting the URL creation_url() hands back, it's actually
    fetched (see _verify_creation_url below) -- creation_url() only ever
    formats a string from _server_info, it can't tell if the Flask thread
    it's describing is still alive (a crashed/never-bound server, a stale
    Tailscale IP after a reconnect, an expired cert) versus just still
    running. Sending/recording a link that LOOKS right but 404s or refuses
    to connect would be worse than being upfront that there isn't one --
    this is what makes the link genuinely reliable, not just present."""
    kind = payload.get("kind", "dashboard")
    slug = payload.get("slug", "")
    title = payload.get("title", "") or "Your creation"

    lan_url = None
    try:
        import dashboard_server
        candidate = dashboard_server.creation_url(kind, slug)
        if candidate and _verify_creation_url(candidate):
            lan_url = candidate
        elif candidate:
            print(f"  [Creation] Phone/tailnet link didn't respond, not sending it: {candidate}")
    except Exception as e:
        print(f"  [Creation] Couldn't determine phone/tailnet link: {e}")

    record_creation(title, kind, slug, lan_url)

    try:
        import telegram_bridge
        if lan_url:
            telegram_bridge.send_message(
                f"{title} is ready sir -- open it on your phone: {lan_url}")
        else:
            telegram_bridge.send_message(
                f"{title} is ready sir -- but I couldn't get you a phone link this "
                "time (the phone dashboard isn't reachable). Ask me to list your recent "
                "creations once it's back up.")
    except Exception as e:
        print(f"  [Creation] Couldn't send Telegram notice: {e}")

    return lan_url


def record_creation(title, kind, slug, url):
    """Not an MCP tool -- called directly by jarvis.py's _on_brain_creation
    right after every build_creation() finishes, success or not, so every
    creation the user has ever been shown stays retrievable afterward (via
    list_creations below) even after this session's in-memory creation
    panel is gone. `url` is whatever creation_url() returned at the time --
    None is stored as-is (rather than skipped) so list_creations can say
    plainly that a given creation currently has no phone link, instead of
    silently omitting it."""
    log = _load_json(CREATIONS_LOG_PATH, [])
    log.append({
        "title": title, "kind": kind, "slug": slug, "url": url,
        "ts": time.time(),
    })
    _save_json(CREATIONS_LOG_PATH, log[-_CREATIONS_LOG_MAX:])


def list_creations(count: int = 10) -> str:
    """List the most recent things Jarvis has built with build_creation
    (dashboards and webpages), newest first, each with its phone/tailnet
    link if one was available when it was built -- use this when the user
    asks for the link to something built earlier, not just the one on
    screen right now."""
    log = _load_json(CREATIONS_LOG_PATH, [])
    if not log:
        return "I haven't built anything yet sir."
    recent = list(reversed(log))[:max(1, int(count))]
    lines = []
    for c in recent:
        when = time.strftime("%b %d %H:%M", time.localtime(c.get("ts", 0)))
        link = c.get("url") or "no phone link available for this one"
        lines.append(f"\"{c.get('title', 'Untitled')}\" ({when}): {link}")
    return "Here's what I've built recently sir: " + " | ".join(lines)


# ================================================================ WEB RESEARCH (nested, separately-scoped Claude call)
def ask_claude_web(question: str) -> str:
    """Answer a question that needs real reasoning and/or live web search --
    for anything Jarvis wouldn't already know (current events, specific facts,
    documentation lookups)."""
    if not os.path.exists(CLAUDE_CLI):
        return "I can't find Claude Code on this system sir."
    try:
        result = subprocess.run(
            [CLAUDE_CLI, "-p", question,
             "--permission-mode", "dontAsk",
             "--tools", "WebSearch WebFetch",
             "--allowedTools", "WebSearch WebFetch"],
            capture_output=True, text=True, timeout=90,
        )
        answer = (result.stdout or "").strip()
        return answer if answer else "I didn't get a clear answer sir."
    except subprocess.TimeoutExpired:
        return "That took too long sir."
    except Exception as e:
        return f"I couldn't reach my other brain sir: {e}"


# ================================================================ ON-DEMAND SELF-IMPROVEMENT
# "Jarvis, improve your ability to X" -- triggers a real, verified, nested
# Claude Code pass that actually changes and commits code, not just a
# description of what could be done. Same hard constraints as the nightly
# self_improve.md pass; keep the two in sync if either changes.
_IMPROVE_CONSTRAINTS = (
    "Hard constraints, never violate: never add a general shell-exec tool to "
    "jarvis_mcp_server.py's exposed tool set -- every capability must stay a "
    "specific, narrow, named function in tools.py; never widen the _SAFE_DIRS "
    "filesystem scope beyond a clearly-reasonable named folder; never make "
    "delete_item bypass the Recycle Bin; never let close_app target anything "
    "outside the curated _APP_ALIASES/_APP_IMAGE_NAMES list; never make "
    "draft_email send automatically, on any provider; never add gmail.send, "
    "Mail.Send, or any other real-send OAuth scope to gmail_service.py/ "
    "outlook_service.py -- drafts only, always; never weaken CreationPanel.tsx's "
    "iframe sandboxing or build_creation's network/localStorage restrictions; never "
    "give _consult_admin_on_new_devices (jarvis.py) or any future security-response "
    "logic a real effectful action (e.g. actually blocking/quarantining a device) -- "
    "it may only ever result in a read-only research job through JarvisAdmin's "
    "approval gate; that's a separate, bigger, router-specific ask if ever wanted; "
    "never touch files outside this project folder. Before committing: run a Python "
    "syntax check on every changed .py file, and `npm run build` if any "
    "frontend file changed; if either fails, fix it or revert rather than "
    "leaving the repo broken. Commit with git and a clear message describing "
    "what changed and why. If the requested improvement genuinely can't be "
    "done safely within these constraints, don't force it -- explain why not "
    "instead."
)


def self_improve(focus: str) -> str:
    """Trigger a real, verified, autonomous improvement to one of Jarvis's own
    capabilities. Use this whenever the user asks Jarvis to improve, get
    better at, upgrade, work on, or fix something about ITSELF -- e.g.
    "improve your ability to run PowerShell commands" or "improve on playing
    YouTube videos without the window closing abruptly". This makes a real
    code change to this project, verifies it builds/compiles, and commits it
    to git -- not just a description of what could be done. Can take several
    minutes; the user will see a live indicator while it works."""
    if not focus or not focus.strip():
        return json.dumps({"ok": False, "message": "What should I improve sir?"})
    if not os.path.exists(CLAUDE_CLI):
        _report_ondemand_improve(focus, False, "Claude Code not found on this system.")
        return json.dumps({"ok": False, "message": "I can't find Claude Code on this system sir."})

    project_dir = os.path.dirname(os.path.abspath(__file__))
    prompt = (
        "The user just asked you, live, to improve one specific thing about "
        f"yourself (Jarvis): \"{focus.strip()}\"\n\n"
        "Investigate the relevant code in this project, make a real, working "
        "improvement, and verify it. " + _IMPROVE_CONSTRAINTS
    )
    try:
        result = subprocess.run(
            [CLAUDE_CLI, "-p", prompt,
             "--permission-mode", "bypassPermissions",
             "--tools", "Read Write Edit Glob Grep Bash",
             "--allowedTools", "Read Write Edit Glob Grep Bash"],
            cwd=project_dir, capture_output=True, text=True, timeout=600,
        )
    except subprocess.TimeoutExpired:
        _report_ondemand_improve(focus, False, "hit the time box before finishing -- may have made partial progress.")
        return json.dumps({"ok": False, "message": "That took too long sir, I stopped waiting -- but I may have made partial progress, worth checking git log."})
    except Exception as e:
        _report_ondemand_improve(focus, False, str(e))
        return json.dumps({"ok": False, "message": f"Something went wrong sir: {e}"})

    try:
        log = subprocess.run(["git", "-C", project_dir, "log", "-1", "--format=%h %s"],
                              capture_output=True, text=True, timeout=10)
        latest_commit = log.stdout.strip()
    except Exception:
        latest_commit = ""

    _report_ondemand_improve(focus, True, latest_commit=latest_commit)
    summary = (result.stdout or "").strip()
    return json.dumps({
        "ok": True, "focus": focus.strip(),
        "summary": summary[-800:] if summary else "No summary returned.",
        "latest_commit": latest_commit,
    })


def _report_ondemand_improve(focus: str, ok: bool, summary: str = "", latest_commit: str = "") -> None:
    """Best-effort push of a JarvisImprovement Telegram update after every
    on-demand self_improve() run -- a delivery failure here (bot not
    configured, network hiccup) must never surface as a self_improve
    failure to the user, so any error is swallowed."""
    try:
        jarvis_improvement.send_ondemand_report(focus, ok, summary=summary, latest_commit=latest_commit)
    except Exception:
        pass


# ================================================================ SCHEDULED DAILY SELF-IMPROVEMENT
# Same self_improve.md-driven mechanism as the on-demand self_improve(focus)
# above, but unfocused (it picks its own candidate improvements, same as
# the nightly pass this project was designed around) and time-boxed to a
# fixed daily window. Never exposed to the brain as a tool -- only called
# from jarvis.py's _improvement_watcher_thread (JarvisImprovement
# sub-agent), which schedules it once every 24 hours starting at 6 AM
# local time. NOT in jarvis_mcp_server.py's _TOOL_FUNCS on purpose: this
# takes no user input and must never be voice/text-triggerable, only
# scheduler-triggerable.
_DAILY_IMPROVE_TIMEOUT_SECONDS = 2 * 60 * 60  # up to 2 hours, per the 6 AM window


def run_daily_self_improve() -> dict:
    """Runs self_improve.md's autonomous pass (find + apply safe, verified
    improvements, same hard constraints and build/syntax gate as
    self_improve(focus)) for up to 2 hours, then reports which commits it
    actually made. Returns {"ok", "added" (list of commit-subject lines
    made during this run, empty if nothing safe was found), "timed_out"}."""
    project_dir = os.path.dirname(os.path.abspath(__file__))
    if not os.path.exists(CLAUDE_CLI):
        return {"ok": False, "added": [], "timed_out": False, "message": "Claude Code not found."}

    try:
        with open(os.path.join(project_dir, "self_improve.md"), "r", encoding="utf-8") as f:
            prompt = f.read()
    except Exception as e:
        return {"ok": False, "added": [], "timed_out": False, "message": f"Couldn't read self_improve.md: {e}"}

    try:
        start = subprocess.run(["git", "-C", project_dir, "rev-parse", "HEAD"],
                                capture_output=True, text=True, timeout=10)
        start_commit = start.stdout.strip()
    except Exception as e:
        return {"ok": False, "added": [], "timed_out": False, "message": f"git rev-parse failed: {e}"}

    timed_out = False
    try:
        subprocess.run(
            [CLAUDE_CLI, "-p", prompt,
             "--permission-mode", "bypassPermissions",
             "--tools", "Read Write Edit Glob Grep Bash",
             "--allowedTools", "Read Write Edit Glob Grep Bash"],
            cwd=project_dir, capture_output=True, text=True,
            timeout=_DAILY_IMPROVE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        timed_out = True
    except Exception as e:
        return {"ok": False, "added": [], "timed_out": False, "message": f"Run failed: {e}"}

    # Safety net: if the pass made changes but didn't commit them (crashed
    # mid-run, or got cut off right at the time-box), capture them anyway
    # rather than losing the work or leaving the working tree dirty for
    # tomorrow's run -- same net run_self_improve.ps1 uses.
    try:
        subprocess.run(["git", "-C", project_dir, "add", "-A"],
                        capture_output=True, text=True, timeout=30)
        status = subprocess.run(["git", "-C", project_dir, "status", "--porcelain"],
                                 capture_output=True, text=True, timeout=10)
        if status.stdout.strip():
            subprocess.run(
                ["git", "-C", project_dir, "commit", "-m",
                 "auto-wrapper: uncommitted changes from JarvisImprovement daily run"],
                capture_output=True, text=True, timeout=30)
    except Exception:
        pass

    try:
        log = subprocess.run(
            ["git", "-C", project_dir, "log", f"{start_commit}..HEAD", "--format=%s"],
            capture_output=True, text=True, timeout=10)
        added = [line.strip() for line in log.stdout.splitlines() if line.strip()]
    except Exception:
        added = []

    return {"ok": True, "added": added, "timed_out": timed_out}


# ================================================================ AGENT STATUS / MANUAL TELEGRAM TEST
# On-demand visibility into the two background sub-agents (JarvisCPU_Alerts,
# JarvisImprovement) that otherwise only speak up on their own schedules --
# lets the user check whether each is configured and actually delivering,
# and force a one-off test send instead of waiting for the next scheduled
# health check or 6 AM report.
_AGENT_ALIASES = {
    "cpu_alerts": "JarvisCPU_Alerts", "cpu": "JarvisCPU_Alerts", "health": "JarvisCPU_Alerts",
    "watchdog": "JarvisCPU_Alerts", "jarviscpu_alerts": "JarvisCPU_Alerts",
    "improvement": "JarvisImprovement", "self_improvement": "JarvisImprovement",
    "self improve": "JarvisImprovement", "jarvisimprovement": "JarvisImprovement",
    "security": "JarSecurity", "jarsecurity": "JarSecurity", "cybersecurity": "JarSecurity",
    "security_check": "JarSecurity",
}


def _resolve_agent(agent: str):
    key = re.sub(r"[\s\-]+", "_", (agent or "").strip().lower())
    name = _AGENT_ALIASES.get(key) or _AGENT_ALIASES.get(key.replace("_", " "))
    if name == "JarvisCPU_Alerts":
        return name, jarvis_cpu_alerts
    if name == "JarvisImprovement":
        return name, jarvis_improvement
    if name == "JarSecurity":
        return name, jarvis_security
    return None, None


def _read_jsonl_tail(path, max_lines=500):
    """Best-effort JSONL reader -- skips any line that fails to parse rather
    than failing the whole read, since these are append-only supervision
    logs, not critical state."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()[-max_lines:]
    except Exception:
        return []
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def _last_delivery_for(agent_name):
    last = None
    for entry in _read_jsonl_tail(telegram_common.DELIVERY_LOG_PATH):
        if entry.get("agent") == agent_name:
            last = entry
    return last


def _fmt_ago(ts):
    mins = (time.time() - ts) / 60
    if mins < 1:
        return "just now"
    if mins < 60:
        return f"{mins:.0f} min ago"
    hours = mins / 60
    if hours < 48:
        return f"{hours:.1f} hr ago"
    return f"{hours / 24:.1f} days ago"


def agent_status() -> str:
    """Report whether the JarvisCPU_Alerts (PC health watchdog),
    JarvisImprovement (daily self-improvement), and JarSecurity
    (cybersecurity watchdog) Telegram sub-agents are configured, and when
    each last actually delivered a message (from the shared
    confirmed-delivery log), plus the most recent health check and security
    sweep results on file. Use this whenever the user asks how the
    watchdog/CPU alerts, self-improvement, or security agent is doing,
    whether it's still running, or when it last sent something."""
    lines = []

    for label, name, mod in (
        ("JarvisCPU_Alerts (PC health watchdog)", "JarvisCPU_Alerts", jarvis_cpu_alerts),
        ("JarvisImprovement (daily self-improvement)", "JarvisImprovement", jarvis_improvement),
        ("JarSecurity (cybersecurity watchdog)", "JarSecurity", jarvis_security),
    ):
        if not mod.AVAILABLE:
            lines.append(f"{label}: not configured -- missing bot token or chat ID.")
            continue
        last = _last_delivery_for(name)
        if last:
            preview = last.get("preview", "").replace("\n", " ")
            lines.append(f"{label}: configured, last delivered {_fmt_ago(last['ts'])} -- \"{preview}\"")
        else:
            lines.append(f"{label}: configured, but no confirmed delivery on record yet.")

    health_entries = _read_jsonl_tail(HEALTH_LOG_PATH, max_lines=1)
    if health_entries:
        h = health_entries[-1]
        state = h.get("thermal_state", "unknown")
        free_gb = h.get("free_gb")
        lines.append(
            f"Last health check ({_fmt_ago(h['ts'])}): thermal {state}, "
            f"{free_gb:.1f} GB free, critical={h.get('critical', False)}."
        )

    security_entries = _read_jsonl_tail(SECURITY_LOG_PATH, max_lines=1)
    if security_entries:
        s = security_entries[-1]
        lines.append(
            f"Last security sweep ({_fmt_ago(s['ts'])}): {s.get('open_ports', 0)} open ports, "
            f"{len(s.get('suspicious', []))} suspicious processes, "
            f"{s.get('new_devices', 0)} new LAN devices, critical={s.get('critical', False)}."
        )

    return "Agent status sir:\n" + "\n".join(lines)


def _daily_briefing_content() -> str:
    """Not an MCP tool -- called by jarvis.py's _daily_briefing_thread.
    Gathers a plain-text snapshot across every domain Jarvis tracks (PC
    health, security, finances, the employee team, long-term memory) for
    telegram_common.ask_grounded to synthesize into one cohesive morning
    message, instead of the four separate watchdog messages the user
    would otherwise get piecemeal across the morning. Read-only, no side
    effects -- never triggers a fresh check itself, just reports whatever
    the existing watchers/ledger/memory already have on file."""
    parts = []

    if LAST_HEALTH_RESULT:
        h = LAST_HEALTH_RESULT
        parts.append(f"PC health: thermal {h.get('thermal_state', 'unknown')}, "
                      f"{h.get('free_gb', '?')} GB free, critical={h.get('critical', False)}.")

    if LAST_SECURITY_RESULT:
        s = LAST_SECURITY_RESULT
        parts.append(f"Security: {s.get('open_ports', '?')} open ports, "
                      f"{len(s.get('suspicious', []))} suspicious processes, "
                      f"{s.get('new_devices', 0)} new LAN devices, critical={s.get('critical', False)}.")

    if STATEMENTS_AVAILABLE and statements_service.has_data():
        summary = statements_service.get_spending_summary(days=30)
        if summary:
            chg = summary.get("change_pct")
            chg_txt = f", {chg:+.0f}% vs last month" if chg is not None else ""
            parts.append(f"Finance: ${summary['total_spent']:.0f} spent in the last 30 days{chg_txt}.")
        anomalies = statements_service.recent_anomalies(3)
        if anomalies:
            parts.append("Recent spending flags: " + "; ".join(a["detail"] for a in anomalies))

    recent_jobs = [
        j for j in employees.list_jobs(limit=10)
        if j.get("status") in ("done", "failed") and j.get("finished_at")
        and (time.time() - j["finished_at"]) < 86400
    ]
    if recent_jobs:
        parts.append("Team activity in the last day: "
                      + "; ".join(f"{j['name']} ({j['role']}) {j['status']}" for j in recent_jobs))

    mem_hits = memory_store.recent(5)
    if mem_hits:
        parts.append("Recently remembered: " + "; ".join(m["text"] for m in mem_hits))

    return "\n".join(parts) if parts else "Nothing notable to report -- quiet across the board."


def send_agent_test_message(agent: str) -> str:
    """Force a one-off manual test Telegram message from either background
    sub-agent right now, instead of waiting for its next scheduled send --
    use this whenever the user asks to test, ping, or verify the CPU
    alerts/watchdog or self-improvement Telegram bot. `agent` must identify
    which one: e.g. "cpu_alerts"/"watchdog"/"health" for JarvisCPU_Alerts,
    "improvement"/"self_improvement" for JarvisImprovement, or
    "security"/"jarsecurity" for JarSecurity. Returns whether Telegram
    confirmed the send."""
    name, mod = _resolve_agent(agent)
    if not name:
        return json.dumps({"ok": False, "message": f"I don't recognize the agent \"{agent}\" sir -- try \"cpu alerts\" or \"improvement\"."})
    if not mod.AVAILABLE:
        return json.dumps({"ok": False, "agent": name, "message": f"{name} isn't configured sir -- its bot token or chat ID is missing."})
    ok = mod.send_test_message()
    message = (f"Test message sent to {name} sir -- Telegram confirmed delivery." if ok else
               f"Test send to {name} failed sir -- check the bot token, chat ID, and network.")
    return json.dumps({"ok": ok, "agent": name, "message": message})


def security_status_report() -> str:
    """Read-only summary of JarSecurity's recent activity: the last few
    logged sweeps from ~/.jarvis/security_log.jsonl (open ports, suspicious
    processes, new LAN devices, firmware/driver and browser-extension
    findings) plus the last confirmed JarSecurity Telegram delivery. Never
    triggers a fresh run_security_check() itself, so it replies immediately
    rather than waiting on a network/browser sweep. Called by
    jarvis_security.py's poll_thread() when the user texts JarSecurity's own
    bot directly, asking what it's been finding lately. Not exposed to the
    brain as a voice/text-command tool -- agent_status() already covers that
    combined, cross-agent view; this one is specific to JarSecurity's own
    two-way channel."""
    entries = _read_jsonl_tail(SECURITY_LOG_PATH, max_lines=20)
    sweeps = [e for e in entries if not e.get("deep_scan")]
    deep_scans = [e for e in entries if e.get("deep_scan")]
    if not sweeps:
        lines = ["No sweeps logged yet sir -- the first one runs shortly after startup."]
    else:
        recent = sweeps[-5:]
        lines = [f"Last {len(recent)} sweep{'s' if len(recent) != 1 else ''}:"]
        for e in reversed(recent):
            state = "CRITICAL" if e.get("critical") else "clear"
            lines.append(
                f"- {_fmt_ago(e['ts'])}: {state} -- {e.get('open_ports', 0)} open ports "
                f"({e.get('exposed_ports', 0)} exposed), "
                f"{len(e.get('suspicious', []))} suspicious process(es), "
                f"{e.get('new_devices', 0)} new device(s), "
                f"firmware flagged={e.get('firmware_flagged', False)}, "
                f"uBlock={'ok' if e.get('ublock_ok') else 'issue'}, "
                f"DuckDuckGo={'ok' if e.get('ddg_ok') else 'issue'}"
            )
        # Real per-port detail from the MOST RECENT sweep only -- this is
        # what actually answers "what ports are open"/"which are exposed"
        # instead of just a count, grounding ask_grounded() in real data.
        newest_ports = sweeps[-1].get("ports")
        if newest_ports:
            lines.append(_format_port_detail(newest_ports))

    if deep_scans:
        d = deep_scans[-1]
        lines.append(
            f"Last deep scan ({_fmt_ago(d['ts'])}): lynis hardening index "
            f"{d.get('lynis_hardening_index', '–')}/100, {d.get('lynis_warnings', 0)} lynis warning(s), "
            f"{d.get('rkhunter_warnings', 0)} rkhunter warning(s)"
        )
    else:
        lines.append("No deep scan (lynis/rkhunter) logged yet -- runs once daily.")

    installed = {name: bool(shutil.which(name)) for name in _HARDENING_INSTALLS}
    missing = [name for name, present in installed.items() if not present]
    if missing:
        lines.append(f"Not installed: {', '.join(missing)} -- can propose installing via propose_hardening_install.")

    last = _last_delivery_for("JarSecurity")
    if last:
        preview = last.get("preview", "").replace("\n", " ")
        lines.append(f"Last job sent {_fmt_ago(last['ts'])}: \"{preview}\"")
    return "JarSecurity status sir:\n" + "\n".join(lines)


# ================================================================ MANAGER / EMPLOYEES
# Jarvis-as-manager framing over the narrow capabilities already defined
# above -- "hiring an employee" never grants any new capability, it just
# gives a job a name and routes it to whichever existing scoped function
# already does that kind of work:
#   developer  -> self_improve()       (real, verified, committed code changes)
#   designer   -> build_creation()     (a dashboard or webpage)
#   researcher -> ask_claude_web()     (live web research/Q&A)
#   analyst    -> get_financial_insights() (spending tips from synced data)
# Every one of those already runs inside its own tightly-scoped nested
# Claude Code call (fixed --tools allowlist, no shell-exec surface beyond
# what those functions already had) -- hire_employee adds no new subprocess
# call of its own, only a name, a queue slot, and a background thread
# (employees.py's worker_loop, run from jarvis.py) to actually run in. See
# employees.py for the queue/worker implementation -- this used to run
# jobs inline and block whoever asked (up to 10 minutes for self_improve/
# build_creation); now it enqueues and returns immediately.
def hire_employee(role: str, job: str) -> str:
    """Act as the manager: "hire" a named employee to run one specific job
    in the background, then report back once it's done -- ask "who's on my
    team" / "what has X been working on" / "is X still working" later to
    check progress, don't expect this call itself to wait for the result.
    role must be one of: developer (code/self-improvement changes to Jarvis
    itself), designer (build a dashboard or webpage), researcher (answer
    something needing live web search), analyst (financial insights/
    spending tips from synced statements). Use this whenever the user
    explicitly asks Jarvis to "hire"/"get someone"/"put someone on"/"bring
    on" a job -- for a direct request ("build me a dashboard", "improve X")
    just call that specific tool instead. This is a friendly framing over
    Jarvis's existing narrow tools; hiring never grants any new capability,
    only names, queues, and backgrounds the job."""
    record, error = employees.enqueue_job(role, job)
    if error:
        return error
    return f"Putting {record['name']} on it sir: {job}"


def list_employees() -> str:
    """Report on the manager's team: everyone Jarvis has "hired" recently,
    what they're on, and whether it's queued, still running, or finished --
    most recent first. Use this for "who's on my team" / "what has my team
    been working on" / "employee status" / "is X still working" requests."""
    jobs = employees.list_jobs(limit=10)
    if not jobs:
        return "No one's been hired yet sir -- ask me to hire a developer, designer, researcher, or analyst for a job."
    lines = []
    for j in reversed(jobs):
        if j["status"] == "queued":
            status = "queued, hasn't started yet"
        elif j["status"] == "running":
            status = "still working on it"
        elif j["status"] == "done":
            status = f"done, {_fmt_ago(j['finished_at'])}"
        else:
            status = f"hit a snag, {_fmt_ago(j['finished_at'])}"
        auto_tag = " (self-hired)" if j.get("auto") else ""
        lines.append(f"{j['name']} ({j['role']}){auto_tag} -- \"{j['job']}\" -- {status}")
    return "Your team sir:\n" + "\n".join(lines)
