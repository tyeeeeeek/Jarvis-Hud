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
#   IMPORTANT: this module intentionally exposes NO shell-exec /
#   run-arbitrary-command tool. Every capability is a specific,
#   named, scoped function. If a new capability is needed, add a
#   new narrow function here -- never widen one of these into a
#   general-purpose executor.
# ================================================================
import os, re, sys, json, time, shutil, tempfile, threading, subprocess, urllib.parse, webbrowser

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
    import telegram_bridge; TELEGRAM_AVAILABLE = True
except ImportError:
    TELEGRAM_AVAILABLE = False

import telegram_common
import jarvis_cpu_alerts
import jarvis_improvement

try:
    import browser_control; BROWSER_CONTROL_AVAILABLE = True
except ImportError:
    BROWSER_CONTROL_AVAILABLE = False


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
    try:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/IM", image], capture_output=True, timeout=10)
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
            subprocess.run(["pkill", "-f", "-x", pattern], capture_output=True, timeout=10)
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


def open_website(target: str) -> str:
    """Open a website in the dedicated, visible Jarvis browser window and
    bring it to the front -- a known site name (youtube, gmail, github,
    netflix, ...), a raw domain, or free text (which becomes a Google
    search). Reuses the same persistent, Jarvis-controlled Chromium window as
    play_youtube so it can be reliably re-focused, falling back to the OS
    default browser only if that dedicated window is unavailable. For
    actually searching-and-playing something on YouTube, use play_youtube
    instead."""
    url = _resolve_url(target)
    if BROWSER_CONTROL_AVAILABLE:
        return browser_control.open_url(url)
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
        return browser_control.open_url(url)
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
    (play_pause, next -- meaning restart the current video, mute). For system-
    wide media keys (works on Spotify etc too) use media_control instead."""
    if not BROWSER_CONTROL_AVAILABLE:
        return "The YouTube browser isn't available sir."
    try:
        return browser_control.control(action or "")
    except Exception as e:
        return f"I couldn't do that sir: {e}"


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


# ================================================================ EMAIL (draft only, never sends)
def draft_email(recipient: str = "", subject: str = "") -> str:
    """Open a pre-filled Gmail compose draft in the browser. Never sends
    automatically -- the user must review and hit send themselves."""
    params = {"view": "cm", "fs": "1"}
    if subject:
        params["su"] = subject
    if recipient:
        params["to"] = recipient
    try:
        if not webbrowser.open("https://mail.google.com/mail/?" + urllib.parse.urlencode(params)):
            return "I couldn't find a browser to open that draft with sir."
        return f"Opening a draft{' to ' + recipient if recipient else ''}{' about ' + subject if subject else ''} sir. You'll need to hit send yourself."
    except Exception as e:
        return f"I couldn't open that draft sir: {e}"


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
    machine."""
    action = (action or "").strip().lower()
    if action not in ("shutdown", "restart", "cancel"):
        return f"I don't recognize '{action}' sir -- action must be shutdown, restart, or cancel."

    shutdown_bin = "shutdown.exe" if IS_WINDOWS else "shutdown"

    try:
        if action == "cancel":
            cmd = [shutdown_bin, "/a"] if IS_WINDOWS else [shutdown_bin, "-c"]
            result = subprocess.run(cmd, capture_output=True, timeout=10, text=True)
            if IS_WINDOWS and result.returncode != 0:
                return "There wasn't a shutdown or restart scheduled to cancel, sir."
            return "Cancelled the pending shutdown, sir."

        delay_minutes = max(0.0, float(delay_minutes))

        if IS_WINDOWS:
            flag = "/s" if action == "shutdown" else "/r"
            cmd = [shutdown_bin, flag, "/t", str(int(delay_minutes * 60))]
        else:
            when = "now" if delay_minutes == 0 else f"+{max(1, round(delay_minutes))}"
            flag = "-h" if action == "shutdown" else "-r"
            cmd = [shutdown_bin, flag, when]

        result = subprocess.run(cmd, capture_output=True, timeout=10, text=True)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            return f"I couldn't {action} the PC sir: {detail or 'unknown error'}"

        verb = "Shutting down" if action == "shutdown" else "Restarting"
        if delay_minutes == 0:
            return f"{verb} now, sir."
        return f"{verb} in {delay_minutes:g} minute(s), sir. Say 'cancel shutdown' if you change your mind."
    except Exception as e:
        return f"I couldn't {action} the PC sir: {e}"


# ================================================================ FINANCE (unchanged services, thin wrappers)
def sync_bank_data() -> str:
    """Sync the latest bank transactions into Jarvis's records. Call this on
    any "sync statements", "sync my bank data", or "import my statements"
    request. First imports whatever file(s) the user has most recently sent
    (as a document or a screenshot/photo) to the Telegram bot into the
    "latest bank statements" folder, then parses every statement file found
    there (plus anything dropped directly in JarvisStatements) into the
    local transaction store that get_spending_summary reads from --
    whatever the format (CSV, TXT, PDF, XLSX/XLS, OFX/QFX, a screenshot, or
    a ZIP bundling any of those) and whatever it's named."""
    if not STATEMENTS_AVAILABLE:
        return "Statement syncing isn't set up sir."
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
        return ("I didn't find any statement files to sync sir. Send one to the Telegram bot "
                 "(CSV, TXT, PDF, XLSX, OFX/QFX, a screenshot, or a ZIP) or drop it in the "
                 "JarvisStatements folder first.")
    prefix = f"Imported {len(imported)} file(s) you sent over Telegram and synced sir. " if imported else "Synced sir. "
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
    HTML page. kind="dashboard" for something meant to appear right inside
    the HUD (a widget, a visualization, a small tool) -- style it dark/glass/
    cyan monospace to match the HUD. kind="webpage" for a full site meant to
    open in the real browser instead. This can take up to several minutes for
    anything nontrivial; the user will see a live indicator while it builds."""
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

    return json.dumps({
        "ok": True, "kind": kind, "title": description,
        "path": index_path.replace("\\", "/"),
    })


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
    "draft_email send automatically; never weaken CreationPanel.tsx's iframe "
    "sandboxing or build_creation's network/localStorage restrictions; never "
    "touch files outside this project folder. Before committing: run a Python "
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
}


def _resolve_agent(agent: str):
    key = re.sub(r"[\s\-]+", "_", (agent or "").strip().lower())
    name = _AGENT_ALIASES.get(key) or _AGENT_ALIASES.get(key.replace("_", " "))
    if name == "JarvisCPU_Alerts":
        return name, jarvis_cpu_alerts
    if name == "JarvisImprovement":
        return name, jarvis_improvement
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
    """Report whether the JarvisCPU_Alerts (PC health watchdog) and
    JarvisImprovement (daily self-improvement) Telegram sub-agents are
    configured, and when each last actually delivered a message (from the
    shared confirmed-delivery log), plus the most recent health check
    result on file. Use this whenever the user asks how the watchdog/CPU
    alerts or self-improvement agent is doing, whether it's still running,
    or when it last sent something."""
    lines = []

    for label, name, mod in (
        ("JarvisCPU_Alerts (PC health watchdog)", "JarvisCPU_Alerts", jarvis_cpu_alerts),
        ("JarvisImprovement (daily self-improvement)", "JarvisImprovement", jarvis_improvement),
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

    return "Agent status sir:\n" + "\n".join(lines)


def send_agent_test_message(agent: str) -> str:
    """Force a one-off manual test Telegram message from either background
    sub-agent right now, instead of waiting for its next scheduled send --
    use this whenever the user asks to test, ping, or verify the CPU
    alerts/watchdog or self-improvement Telegram bot. `agent` must identify
    which one: e.g. "cpu_alerts"/"watchdog"/"health" for JarvisCPU_Alerts,
    or "improvement"/"self_improvement" for JarvisImprovement. Returns
    whether Telegram confirmed the send."""
    name, mod = _resolve_agent(agent)
    if not name:
        return json.dumps({"ok": False, "message": f"I don't recognize the agent \"{agent}\" sir -- try \"cpu alerts\" or \"improvement\"."})
    if not mod.AVAILABLE:
        return json.dumps({"ok": False, "agent": name, "message": f"{name} isn't configured sir -- its bot token or chat ID is missing."})
    ok = mod.send_test_message()
    message = (f"Test message sent to {name} sir -- Telegram confirmed delivery." if ok else
               f"Test send to {name} failed sir -- check the bot token, chat ID, and network.")
    return json.dumps({"ok": ok, "agent": name, "message": message})
