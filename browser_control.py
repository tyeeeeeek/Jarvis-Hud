# ================================================================
#   J.A.R.V.I.S — Browser control (general browsing + YouTube playback)
#
#   Owns one persistent, visible Chromium instance driven by
#   Playwright's SYNC API, confined to a single dedicated worker
#   thread (Playwright's sync API is not thread-safe and must never
#   share a thread with an asyncio loop). Every call from tools.py
#   goes through a small thread-safe request queue.
#
#   Routing general web opens through this same dedicated,
#   Jarvis-owned window (instead of tools.py firing webbrowser.open()
#   at whatever the OS's default-browser association happens to be)
#   is deliberate: it's the one browser Jarvis actually holds a
#   handle to, so it's the one Jarvis can reliably navigate, re-use,
#   and bring to the front on demand -- an OS-launched default
#   browser process is fire-and-forget with no such guarantee.
#
#   Still deliberately narrow: every exposed action is a specific,
#   named operation (open a URL, read the page back, search YouTube
#   and click play, basic playback control) -- no generic "click
#   anything on any page" / "run this JS" tool.
# ================================================================
import os
import re
import json
import shutil
import queue
import tempfile
import zipfile
import threading
import urllib.parse
import concurrent.futures

import requests

HOME = os.path.expanduser("~")
PROFILE_DIR = os.path.join(HOME, ".jarvis-browser-profile")
# Used only when the worker thread itself has to be abandoned because a
# Playwright call hung instead of raising (see _submit's TimeoutError
# handling below). Alternating between two fixed directories guarantees a
# freshly-started worker never launches Chromium on the same profile
# directory the just-abandoned (possibly still-alive, just unresponsive)
# worker was using -- reusing that directory would mean deleting its
# SingletonLock out from under a live process, which corrupts the profile
# and was a likely source of repeat crashes.
_PROFILE_DIRS = [PROFILE_DIR, PROFILE_DIR + "-alt"]

# Chromium stability/resource flags. `--window-size`/`--window-position` are
# cosmetic; the rest exist to stop the automated renderer from crashing or
# hanging over long unattended playback sessions:
#  - background timer/backgrounding throttling is meant for real user tabs
#    that get covered/minimized -- for an unattended automation window it
#    just adds another way playback can silently stall or wedge.
#  - a persistent profile's disk cache grows without bound over a
#    long-running session; capping it avoids the browser slowly starving
#    itself of disk/memory headroom until the renderer gets OOM-killed.
#  - --disable-dev-shm-usage avoids renderer crashes from small /dev/shm on
#    constrained environments (Linux containers/VMs); harmless on Windows.
_CHROMIUM_ARGS = [
    "--window-size=900,700", "--window-position=100,100",
    "--disable-features=CalculateNativeWinOcclusion",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-dev-shm-usage",
    "--disk-cache-size=104857600",
]

# ---- uBlock Origin Lite (JarSecurity) ---------------------------------
# A dedicated folder under the user's home directory, entirely separate
# from tools.py's _SAFE_DIRS file-management jail -- nothing else reads or
# writes here except the two functions below. Playwright's persistent
# Chromium context can only load an *unpacked* MV3 extension via
# --load-extension (there's no Web-Store-install API for an automated
# profile), so the official open-source release is fetched straight from
# its own GitHub repo (uBlockOrigin/uBOL-home -- the same project
# distributed on the Chrome Web Store) and unpacked here.
_EXTENSIONS_DIR = os.path.join(HOME, ".jarvis-browser-extensions")
_UBLOCK_DIR = os.path.join(_EXTENSIONS_DIR, "ublock-origin-lite")
_UBLOCK_RELEASES_API = "https://api.github.com/repos/uBlockOrigin/uBOL-home/releases/latest"


def _ublock_manifest_ok(ext_dir):
    """True only if ext_dir holds a real, structurally-verified uBlock
    Origin Lite (MV3) unpacked extension -- checked by manifest_version==3
    plus the English display name from its own locale file, not just "a
    file exists", so a half-written or unrelated directory is never
    trusted (and never silently loaded into the browser)."""
    try:
        with open(os.path.join(ext_dir, "manifest.json"), "r", encoding="utf-8") as f:
            manifest = json.load(f)
        if manifest.get("manifest_version") != 3:
            return False
        with open(os.path.join(ext_dir, "_locales", "en", "messages.json"), "r", encoding="utf-8") as f:
            messages = json.load(f)
        return messages.get("extName", {}).get("message") == "uBlock Origin Lite"
    except Exception:
        return False


def verify_ublock_origin() -> dict:
    """Read-only check: is a valid, verified uBlock Origin Lite already
    unpacked at _UBLOCK_DIR (and so already wired into the next browser
    launch's args, see _chromium_args below)? Never downloads anything --
    see install_ublock_origin() for that."""
    return {"installed": _ublock_manifest_ok(_UBLOCK_DIR), "path": _UBLOCK_DIR}


def install_ublock_origin() -> dict:
    """Download the latest official uBlock Origin Lite (MV3) release and
    unpack it into _UBLOCK_DIR. A no-op if a verified copy is already
    there. The result is verified structurally (same check as
    verify_ublock_origin) before it's ever moved into place, and the
    download is extracted into a throwaway temp directory first so a
    corrupt/partial download can never leave a half-written extension
    directory behind. A later browser (re)launch (see _launch_context)
    picks it up automatically -- an already-open browser window must be
    closed and reopened for a freshly installed extension to load."""
    if _ublock_manifest_ok(_UBLOCK_DIR):
        return {"ok": True, "already_installed": True, "path": _UBLOCK_DIR}
    try:
        r = requests.get(_UBLOCK_RELEASES_API, timeout=20)
        r.raise_for_status()
        assets = r.json().get("assets", [])
        asset = next((a for a in assets if a.get("name", "").endswith(".chromium.zip")), None)
        if not asset:
            return {"ok": False, "error": "couldn't find a chromium release asset on GitHub"}
        zr = requests.get(asset["browser_download_url"], timeout=60)
        zr.raise_for_status()
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = os.path.join(tmp, "ubol.zip")
            with open(zip_path, "wb") as f:
                f.write(zr.content)
            extract_dir = os.path.join(tmp, "extracted")
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(extract_dir)
            if not _ublock_manifest_ok(extract_dir):
                return {"ok": False, "error": "downloaded extension failed verification"}
            os.makedirs(_EXTENSIONS_DIR, exist_ok=True)
            if os.path.isdir(_UBLOCK_DIR):
                shutil.rmtree(_UBLOCK_DIR, ignore_errors=True)
            shutil.move(extract_dir, _UBLOCK_DIR)
        return {"ok": True, "already_installed": False, "path": _UBLOCK_DIR}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _chromium_args():
    """Base Chromium args, plus --load-extension for uBlock Origin Lite
    when (and only when) a verified copy is on disk -- re-checked on every
    launch so a manually deleted/corrupted extension directory just falls
    back to no extension instead of a broken launch."""
    args = list(_CHROMIUM_ARGS)
    if _ublock_manifest_ok(_UBLOCK_DIR):
        args += [f"--disable-extensions-except={_UBLOCK_DIR}", f"--load-extension={_UBLOCK_DIR}"]
    return args


_state_lock = threading.Lock()
_current_queue = None
_worker_alive = False
_next_generation = 0

# Raised on the worker thread (relaunch=False) when there's no live page to
# act on; matched by string in control()/read_page() to give a friendlier
# reply than the raw RuntimeError.
_NOT_OPEN_ERR = "no browser window is currently open"


def _start_worker_locked():
    """Caller must hold _state_lock. Starts a fresh worker thread bound to
    its own request queue and its own alternating profile directory."""
    global _current_queue, _worker_alive, _next_generation
    q: "queue.Queue" = queue.Queue()
    profile_dir = _PROFILE_DIRS[_next_generation % len(_PROFILE_DIRS)]
    _next_generation += 1
    _current_queue = q
    _worker_alive = True
    t = threading.Thread(target=_worker_thread, args=(q, profile_dir),
                          name="BrowserControl", daemon=True)
    t.start()


def _submit(fn, timeout=45, relaunch=True):
    """Run fn(page) on the worker thread, block for the result.

    relaunch=False is used for playback controls (play/pause, mute, ...): if
    the window has been closed or crashed there is nothing to control, so we
    raise a clear error instead of popping open a fresh blank browser window.
    """
    global _worker_alive
    with _state_lock:
        if not _worker_alive:
            _start_worker_locked()
        q = _current_queue
    fut: concurrent.futures.Future = concurrent.futures.Future()
    q.put((fn, fut, relaunch))
    try:
        return fut.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        # Every Playwright call the worker makes on our behalf already has
        # its own, shorter internal timeout (goto=20s, wait_for_selector=
        # 15s, ...), so a request that still doesn't come back within our
        # own timeout means the worker thread itself is wedged inside a
        # Chromium call that hung instead of raising -- e.g. the browser
        # process is a zombie. There is no reliable way to force-kill a
        # blocked Python thread, so abandon it (it stays a daemon and can't
        # block process exit) and let the *next* call spin up a brand-new
        # worker + browser instead of leaving playback permanently dead.
        with _state_lock:
            if _current_queue is q:
                _worker_alive = False
        raise RuntimeError(
            "the browser window stopped responding, sir -- restarting it, please try again"
        ) from None


def _clear_stale_singleton_files(profile_dir):
    """If Chromium was killed or crashed instead of exiting cleanly, it can
    leave SingletonLock/SingletonCookie/SingletonSocket behind in the profile
    dir. A leftover lock makes the *next* launch_persistent_context fail
    outright, which used to permanently break playback until the whole app
    was restarted -- clear it before every (re)launch."""
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        path = os.path.join(profile_dir, name)
        try:
            if os.path.exists(path) or os.path.islink(path):
                os.remove(path)
        except OSError:
            pass


def _watch_page(context, page, active):
    """Track this page as the one actively in use, and arm crash detection
    for it. Returns a mutable {"crashed": bool} flag: page.is_closed() only
    reflects an explicit close, not a renderer *crash* (tab dies but the
    window/context stays open), which used to make every future request
    retry the same dead page and fail in a loop forever."""
    active["page"] = page
    crash_flag = {"crashed": False}
    page.on("crash", lambda _page: crash_flag.update(crashed=True))
    return crash_flag


def _watch_context_for_stray_pages(context, active):
    """YouTube ads and "open in new tab" links can spawn extra pages in the
    same context. Left alone these pile up as extra live renderer processes
    over an unattended, long-running session -- a real source of the
    resource exhaustion that leads to eventual crashes. Close anything that
    isn't the one page we're actively tracking."""
    def _on_page(new_page):
        if new_page is not active.get("page"):
            try:
                new_page.close()
            except Exception:
                pass
    context.on("page", _on_page)


def _launch_context(p, profile_dir, active):
    os.makedirs(profile_dir, exist_ok=True)
    _clear_stale_singleton_files(profile_dir)
    context = p.chromium.launch_persistent_context(
        profile_dir, headless=False, args=_chromium_args(),
    )
    _watch_context_for_stray_pages(context, active)
    page = context.pages[0] if context.pages else context.new_page()
    crash_flag = _watch_page(context, page, active)
    return context, page, crash_flag


def _is_usable(page, crash_flag):
    if page is None or crash_flag is None or crash_flag["crashed"]:
        return False
    try:
        return not page.is_closed()
    except Exception:
        return False


def _recover(old_page, context, p, profile_dir, active):
    """Get a fresh, working page. Prefers opening a new tab in the existing
    browser window (context still alive, e.g. only the tab crashed) over
    closing and relaunching the whole window, so a single crashed tab
    doesn't make the automation window itself flash shut and reopen. Falls
    back to a full relaunch if the window/context itself is gone."""
    if context is not None:
        try:
            new_page = context.new_page()
        except Exception:
            new_page = None
        if new_page is not None:
            if old_page is not None:
                try:
                    old_page.close()
                except Exception:
                    pass
            crash_flag = _watch_page(context, new_page, active)
            return context, new_page, crash_flag
        try:
            context.close()
        except Exception:
            pass
    return _launch_context(p, profile_dir, active)


def _worker_thread(q: "queue.Queue", profile_dir):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        context = page = crash_flag = None
        active = {"page": None}

        while True:
            fn, fut, relaunch = q.get()
            if fn is None:  # shutdown sentinel
                break
            try:
                if not _is_usable(page, crash_flag):
                    if not relaunch:
                        raise RuntimeError(_NOT_OPEN_ERR)
                    # The tab crashed, the window was closed, or Chromium
                    # crashed entirely -- transparently heal instead of
                    # leaving playback dead.
                    context, page, crash_flag = _recover(page, context, p, profile_dir, active)
                try:
                    result = fn(page)
                except Exception:
                    # A whole-browser-process crash (as opposed to just one
                    # renderer/tab) doesn't always make context.new_page()
                    # fail immediately -- the freshly "recovered" page can
                    # still turn out to be dead on first real use. If that's
                    # what happened, recover once more and retry within this
                    # same request instead of surfacing a failure that would
                    # have healed itself on the very next call anyway.
                    if not relaunch or _is_usable(page, crash_flag):
                        raise
                    context, page, crash_flag = _recover(page, context, p, profile_dir, active)
                    result = fn(page)
                if not fut.done():
                    fut.set_result(result)
            except Exception as e:
                if not fut.done():
                    fut.set_exception(e)

        if context is not None:
            try:
                context.close()
            except Exception:
                pass


def shutdown():
    """Best-effort graceful close, called from jarvis.py on exit."""
    with _state_lock:
        alive, q = _worker_alive, _current_queue
    if alive and q is not None:
        q.put((None, concurrent.futures.Future(), True))


# ================================================================ ACTIONS
def _focus_window(page):
    """Raise the actual OS-level window, not just the in-browser tab.
    page.bring_to_front() alone only activates the tab -- it does nothing if
    the window itself was minimized or is sitting behind other windows,
    which is exactly the "browser doesn't visibly come to the front" failure
    mode. Best-effort: swallow errors, since a stray/half-closed window
    target can make the CDP call fail even though the page itself is fine."""
    page.bring_to_front()
    try:
        cdp = page.context.new_cdp_session(page)
        window_id = cdp.send("Browser.getWindowForTarget").get("windowId")
        if window_id is not None:
            cdp.send("Browser.setWindowBounds",
                     {"windowId": window_id, "bounds": {"windowState": "normal"}})
    except Exception:
        pass


def _do_open_url(url, page):
    page.goto(url, timeout=20000)
    try:
        page.wait_for_load_state("domcontentloaded", timeout=8000)
    except Exception:
        pass
    _focus_window(page)
    return (page.title() or "").strip()


def open_url(url: str) -> str:
    """Navigate the one dedicated, visible Jarvis browser window to any URL
    and bring it to the front -- reuses the same persistent window as
    YouTube playback instead of the OS default-browser association, so
    Jarvis actually holds a handle it can reliably re-focus."""
    url = (url or "").strip()
    if not url:
        return "What page should I open sir?"
    try:
        title = _submit(lambda page: _do_open_url(url, page), timeout=45)
        return f"Opened {title} sir." if title else f"Opened {url} sir."
    except Exception as e:
        return f"I couldn't open that sir: {e}"


def _do_read_page(page):
    title = (page.title() or "").strip()
    try:
        text = page.inner_text("body", timeout=5000)
    except Exception:
        text = ""
    text = re.sub(r'\s+', ' ', text).strip()
    return title, text[:4000]


def read_page() -> str:
    """Read back the title and visible text of whatever page is currently
    open in the dedicated Jarvis browser window -- lets Jarvis summarize or
    answer questions about a page it (or the user, in that same window) just
    opened, without a generic "run arbitrary JS" tool."""
    try:
        title, text = _submit(_do_read_page, timeout=20, relaunch=False)
    except Exception as e:
        if _NOT_OPEN_ERR in str(e):
            return "There's no page open in the browser right now sir."
        return f"I couldn't read that page sir: {e}"
    if not text:
        return f"There's no readable text on {title or 'this page'} sir."
    return f"{title}: {text}" if title else text


def _do_play_youtube(query, page):
    page.goto("https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus(query), timeout=20000)
    page.wait_for_selector("ytd-video-renderer a#video-title", timeout=15000)
    first = page.locator("ytd-video-renderer a#video-title").first
    title = (first.get_attribute("title") or "").strip()
    first.click()
    page.wait_for_selector("video", timeout=15000)
    _focus_window(page)
    return title


def play_youtube(query: str) -> str:
    if not query.strip():
        return "What should I play sir?"
    try:
        title = _submit(lambda page: _do_play_youtube(query, page), timeout=60)
        return f"Now playing {title} sir." if title else f"Playing {query} sir."
    except Exception as e:
        return f"I couldn't find that on YouTube sir: {e}"


def _do_control(action, page):
    if action == "play_pause":
        page.keyboard.press("k")
    elif action == "next":
        page.keyboard.press("Home"); page.keyboard.press("k"); page.keyboard.press("k")
    elif action == "mute":
        page.keyboard.press("m")
    elif action == "stop":
        # Unlike play_pause (a toggle via the 'k' key), "stop" is meant to be
        # idempotent -- always ends up paused, never accidentally resumes
        # playback if it was already paused. Reaches into the page directly
        # rather than a keypress for that reason.
        page.evaluate("() => { const v = document.querySelector('video'); if (v) v.pause(); }")
    return True


def control(action: str) -> str:
    action = (action or "").strip().lower()
    if action not in ("play_pause", "next", "mute", "stop"):
        return "I can play/pause, stop, restart, or mute the YouTube tab sir."
    try:
        _submit(lambda page: _do_control(action, page), timeout=15, relaunch=False)
        return {"play_pause": "Toggling playback sir.", "next": "Restarting the video sir.",
                "mute": "Muting sir.", "stop": "Stopping playback sir."}[action]
    except Exception as e:
        if _NOT_OPEN_ERR in str(e):
            return "There's no YouTube video playing right now sir."
        return f"I couldn't control the YouTube tab sir: {e}"


def close() -> str:
    """Close the dedicated Jarvis browser window if one is open. A later
    open_url/play_youtube/control call transparently relaunches a fresh
    window (same worker-restart mechanism a crashed/wedged browser already
    uses), so this is safe to call any time -- never a dead end."""
    global _worker_alive
    with _state_lock:
        alive, q = _worker_alive, _current_queue
        _worker_alive = False
    if not alive or q is None:
        return "There's no browser window open right now sir."
    q.put((None, concurrent.futures.Future(), True))
    return "Closing the browser sir."
