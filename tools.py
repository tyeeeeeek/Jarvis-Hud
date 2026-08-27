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
import os, re, json, time, shutil, tempfile, threading, subprocess, urllib.parse, webbrowser

import requests

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
    import browser_control; BROWSER_CONTROL_AVAILABLE = True
except ImportError:
    BROWSER_CONTROL_AVAILABLE = False


HOME = os.path.expanduser("~")
CLAUDE_CLI = os.path.join(HOME, ".local", "bin", "claude.exe")
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
_APP_ALIASES = {
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
}
# image names used by taskkill, for apps whose process name differs from the alias
_APP_IMAGE_NAMES = {
    "notepad": "notepad.exe", "calc": "CalculatorApp.exe", "explorer": None,  # never kill explorer
    "taskmgr": "Taskmgr.exe", "mspaint": "mspaint.exe", "spotify": "Spotify.exe",
    "code": "Code.exe", "winword": "WINWORD.EXE", "excel": "EXCEL.EXE",
    "chrome": "chrome.exe", "cmd": "cmd.exe", "wt": "WindowsTerminal.exe",
    "firefox": "firefox.exe", "msedge": "msedge.exe", "discord": "Discord.exe",
    "steam": "steam.exe", "slack": "slack.exe", "zoom": "Zoom.exe",
    "snippingtool": "SnippingTool.exe",
}


def launch_app(name: str) -> str:
    """Launch a known desktop application by common name (e.g. notepad,
    calculator, file explorer, chrome, spotify, discord, vs code)."""
    key = re.sub(r'^(the|a|an)\s+', '', (name or "").strip().lower())
    exe = _APP_ALIASES.get(key)
    if not exe:
        return f"I don't have {name} in my known app list sir. Try opening it by hand once and I'll remember it next time you ask me to add it."
    try:
        os.startfile(exe)
        return f"Opening {name} sir."
    except Exception:
        return f"{name} doesn't appear to be installed sir."


def close_app(name: str) -> str:
    """Close a known desktop application by common name, gracefully (never a
    forced kill). Restricted to the same curated app list as launch_app."""
    key = re.sub(r'^(the|a|an)\s+', '', (name or "").strip().lower())
    exe = _APP_ALIASES.get(key)
    image = _APP_IMAGE_NAMES.get(exe) if exe else None
    if not image:
        return f"I won't close {name} sir -- it's not in my curated list of apps I'm allowed to close."
    try:
        subprocess.run(["taskkill", "/IM", image], capture_output=True, timeout=10)
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
    """Open a website in the user's default browser -- a known site name
    (youtube, gmail, github, netflix, ...), a raw domain, or free text (which
    becomes a Google search). For actually searching-and-playing something on
    YouTube, use play_youtube instead."""
    try:
        webbrowser.open(_resolve_url(target))
        label = target if target in _SITE_ALIASES or "." in (target or "") else f"a search for {target}"
        return f"Opening {label} sir."
    except Exception as e:
        return f"I couldn't open that sir: {e}"


def search_web(query: str) -> str:
    """Open a Google search results page for the given query in the default
    browser."""
    try:
        webbrowser.open("https://www.google.com/search?q=" + urllib.parse.quote_plus(query or ""))
        return f"Searching for {query} sir."
    except Exception as e:
        return f"I couldn't search for that sir: {e}"


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
        webbrowser.open("https://mail.google.com/mail/?" + urllib.parse.urlencode(params))
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


def check_disk_space() -> str:
    """Report free vs total disk space on the C: drive, and how much
    reclaimable space is sitting in the temp folder."""
    total, _used, free = shutil.disk_usage("C:\\")
    temp_size = _dir_size(tempfile.gettempdir())
    return (f"You have {_human_size(free)} free out of {_human_size(total)} on drive C sir. "
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


def clean_disk() -> str:
    """Clear the OS temp folder and empty the Recycle Bin. Never touches user
    files or documents."""
    _, _, free_before = shutil.disk_usage("C:\\")
    _clear_temp_folder(tempfile.gettempdir())
    try:
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", "Clear-RecycleBin -Force -ErrorAction SilentlyContinue"],
            timeout=30, capture_output=True,
        )
    except Exception:
        pass
    _, _, free_after = shutil.disk_usage("C:\\")
    freed = max(0, free_after - free_before)
    return f"Done sir. Cleared temporary files and the recycle bin, freeing up {_human_size(freed)}."


# ================================================================ FINANCE (unchanged services, thin wrappers)
def sync_bank_data() -> str:
    """Sync the latest bank transactions from CSV statements dropped in the
    JarvisStatements folder."""
    if not STATEMENTS_AVAILABLE:
        return "Statement syncing isn't set up sir."
    try:
        result = statements_service.sync()
    except Exception as e:
        return f"I couldn't sync your statements sir: {e}"
    if result["files_seen"] == 0:
        return "I didn't find any statement files to sync sir. Drop your CSVs in the JarvisStatements folder first."
    return f"Synced sir. Found {result['new_transactions']} new transactions across {result['files_seen']} files."


def get_spending_summary() -> str:
    """Summarize the user's spending over the last 30 days by category, from
    synced bank statements or a linked Plaid account."""
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
        return "I don't have any spending data yet sir. Drop bank statements in the JarvisStatements folder and sync, or connect a bank in the Finance widget."
    if not summary["by_category"]:
        return "No transactions found for the last thirty days sir."
    top = summary["by_category"][0]
    return (f"Over the last thirty days you've spent {summary['total_spent']:.0f} dollars sir, "
            f"most of it on {top['name']}, about {top['amount']:.0f} dollars.")


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
