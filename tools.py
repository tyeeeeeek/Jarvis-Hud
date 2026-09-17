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
#   This is the starter set -- desktop app/media control, web
#   browsing, weather, the map widget, and local conversational
#   memory, none of which need any personal account or token. Add
#   your own integrations the same way: a plain function here,
#   registered in jarvis_mcp_server.py's _TOOL_FUNCS.
#
#   IMPORTANT: this module intentionally exposes NO *unsupervised*
#   shell-exec / run-arbitrary-command tool. Every capability is a
#   specific, named, scoped function. If a new capability is needed,
#   add a new narrow function here -- never widen one of these into
#   a general-purpose executor.
# ================================================================
import os, re, sys, json, time, shutil, signal, ipaddress, subprocess, urllib.parse, webbrowser

import requests

IS_WINDOWS = sys.platform == "win32"

try:
    import win32api; WIN32_AVAILABLE = True
except ImportError:
    WIN32_AVAILABLE = False

import bot_events
import memory_store

try:
    import browser_control; BROWSER_CONTROL_AVAILABLE = True
except ImportError:
    BROWSER_CONTROL_AVAILABLE = False

HOME = os.path.expanduser("~")

JARVIS_DIR = os.path.join(HOME, ".jarvis")

STATE_PATH = os.path.join(JARVIS_DIR, "state.json")

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

_THREAT_BLOCKLIST_PATH = os.path.join(JARVIS_DIR, "security_blocklist.txt")

_THREAT_BLOCKLIST_URL = "https://urlhaus.abuse.ch/downloads/hostfile/"

_THREAT_BLOCKLIST_MAX_AGE = 6 * 3600

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

SECURITY_LOG_PATH = os.path.join(JARVIS_DIR, "security_log.jsonl")

def _log_security_event(entry):
    _ensure_jarvis_dir()
    entry = dict(entry, ts=time.time())
    try:
        with open(SECURITY_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass

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
