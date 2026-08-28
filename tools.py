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
import os, re, sys, json, time, shutil, tempfile, ipaddress, threading, subprocess, urllib.parse, webbrowser

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
import jarvis_security

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


def _open_listening_ports():
    """Parsed list of unique local address:port entries currently
    LISTENing, via the same read-only diagnostic commands
    run_diagnostic_command already exposes (ss -tulpn / netstat -an) --
    report-only, never touches or closes anything."""
    entry = _DIAGNOSTIC_COMMANDS.get("listening_ports")
    if not entry:
        return []
    try:
        out = subprocess.run(entry[0], capture_output=True, text=True, timeout=15).stdout
    except Exception:
        return []
    ports, seen = [], set()
    for line in out.splitlines():
        parts = line.split()
        if IS_WINDOWS:
            if len(parts) >= 4 and parts[0] in ("TCP", "UDP") and "LISTENING" in line.upper():
                addr = parts[1]
            else:
                continue
        else:
            if len(parts) >= 5 and parts[1] == "LISTEN":
                addr = parts[4]
            else:
                continue
        if addr not in seen:
            seen.add(addr)
            ports.append(addr)
    return ports


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


def run_security_check() -> str:
    """JarSecurity sub-agent's core sweep: lists listening TCP/UDP ports,
    flags any running process matching a curated list of known
    cryptominer/backdoor names or executing out of a world-writable temp
    directory, re-runs the same LAN scan scan_network() already does
    (flagging brand-new devices as possible intruders), flags outdated
    device firmware/drivers (see check_firmware_drivers), refreshes the
    local malicious-link blocklist used by open_website/search_web/
    scan_url_safety, and verifies -- installing if missing -- each vetted
    browser security/privacy extension (uBlock Origin Lite, DuckDuckGo
    Privacy Essentials) in the dedicated Jarvis browser window. Every run is
    logged to ~/.jarvis/security_log.jsonl. Safe to call anytime on demand;
    also runs automatically in the background on a fixed schedule."""
    global LAST_SECURITY_RESULT
    ports = _open_listening_ports()
    suspicious = _find_suspicious_processes()

    net = network_watch.scan()
    net_error = net.get("error", "")
    new_count = net.get("new_count", 0) if not net_error else 0
    device_count = len(net.get("devices", [])) if not net_error else 0

    firmware_note, firmware_flagged = _check_firmware_and_drivers()

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

    critical = bool(suspicious) or new_count > 0

    summary_parts = [f"{len(ports)} listening port{'s' if len(ports) != 1 else ''}"]
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
    summary_parts.append(f"threat blocklist: {blocklist_note}")
    summary_parts.append(ublock_summary)
    summary_parts.append(ddg_summary)
    result_text = "; ".join(summary_parts) + "."

    LAST_SECURITY_RESULT = {
        "critical": critical, "open_ports": len(ports), "suspicious": suspicious,
        "new_devices": new_count, "firmware_flagged": firmware_flagged,
        "ublock_ok": ublock_ok, "ddg_ok": ddg_ok,
    }
    _log_security_event({
        "open_ports": len(ports), "suspicious": suspicious, "new_devices": new_count,
        "firmware_flagged": firmware_flagged, "ublock_ok": ublock_ok, "ddg_ok": ddg_ok,
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
# call of its own, only a name, a routing decision, and a roster log so the
# user can ask "who's on my team" / "what has my team done" later.
EMPLOYEE_ROSTER_PATH = os.path.join(JARVIS_DIR, "employee_roster.jsonl")

_EMPLOYEE_ROLES = {
    "developer": "code/self-improvement work on Jarvis itself",
    "designer": "building a dashboard or webpage",
    "researcher": "answering something that needs live web research",
    "analyst": "financial insights and spending tips from synced statements",
}
_ROLE_TITLES = {"developer": "Dev", "designer": "Design", "researcher": "Research", "analyst": "Finance"}


def _next_employee_name(role):
    """Sequential per-role name (Dev-1, Dev-2, Design-1, ...), counted from
    the roster log itself so names stay stable across restarts without a
    separate counter file to keep in sync."""
    title = _ROLE_TITLES.get(role, role.title())
    count = sum(1 for e in _read_jsonl_tail(EMPLOYEE_ROSTER_PATH, max_lines=10000) if e.get("role") == role)
    return f"{title}-{count + 1}"


def _log_employee_job(name, role, job, ok, result_summary):
    _ensure_jarvis_dir()
    entry = {"ts": time.time(), "name": name, "role": role, "job": job,
              "ok": ok, "summary": (result_summary or "")[:500]}
    try:
        with open(EMPLOYEE_ROSTER_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def hire_employee(role: str, job: str) -> str:
    """Act as the manager: "hire" a named employee (bot) to run one specific
    job, then report back what got done. role must be one of: developer
    (code/self-improvement changes to Jarvis itself), designer (build a
    dashboard or webpage), researcher (answer something needing live web
    search), analyst (financial insights/spending tips from synced
    statements). Use this whenever the user explicitly asks Jarvis to
    "hire"/"get someone"/"put someone on"/"bring on" a job -- for a direct
    request ("build me a dashboard", "improve X") just call that specific
    tool instead. This is a friendly framing over Jarvis's existing narrow
    tools; hiring never grants any new capability, only names and routes the
    job and logs it so list_employees can report on past hires."""
    role = (role or "").strip().lower()
    job = (job or "").strip()
    if role not in _EMPLOYEE_ROLES:
        return (f"I don't have a '{role}' role sir -- I can hire a: "
                 + ", ".join(f"{r} ({d})" for r, d in _EMPLOYEE_ROLES.items()))
    if not job:
        return "What should they work on sir?"

    name = _next_employee_name(role)
    intro = f"Putting {name} on it sir: {job}\n\n"

    try:
        if role == "developer":
            data = json.loads(self_improve(job))
            ok = bool(data.get("ok"))
            result = data.get("message") or data.get("summary") or ""
        elif role == "designer":
            kind = "webpage" if re.search(r'\bwebsite\b|\bwebpage\b|\bsite\b', job.lower()) else "dashboard"
            data = json.loads(build_creation(job, kind=kind))
            ok = bool(data.get("ok"))
            result = data.get("message") or (f"Built and saved it sir: {data.get('path', '')}" if ok else "")
        elif role == "researcher":
            result = ask_claude_web(job)
            ok = not result.startswith("I couldn't") and not result.startswith("That took too long")
        else:  # analyst
            result = get_financial_insights()
            ok = "I don't have enough spending data" not in result
    except Exception as e:
        ok, result = False, f"ran into an error: {e}"

    _log_employee_job(name, role, job, ok, result)
    return intro + (result or ("Done sir." if ok else "Couldn't get that done sir."))


def list_employees() -> str:
    """Report on the manager's team: everyone Jarvis has "hired" recently and
    what they were put on, most recent first. Use this for "who's on my
    team" / "what has my team been working on" / "employee status"
    requests."""
    entries = _read_jsonl_tail(EMPLOYEE_ROSTER_PATH, max_lines=10)
    if not entries:
        return "No one's been hired yet sir -- ask me to hire a developer, designer, researcher, or analyst for a job."
    lines = []
    for e in reversed(entries):
        status = "done" if e.get("ok") else "hit a snag"
        lines.append(f"{e.get('name')} ({e.get('role')}) -- \"{e.get('job')}\" -- {status}, {_fmt_ago(e['ts'])}")
    return "Your team sir:\n" + "\n".join(lines)
