# ================================================================
#   J.A.R.V.I.S — Browser control (YouTube playback)
#
#   Owns one persistent, visible Chromium instance driven by
#   Playwright's SYNC API, confined to a single dedicated worker
#   thread (Playwright's sync API is not thread-safe and must never
#   share a thread with an asyncio loop). Every call from tools.py
#   goes through a small thread-safe request queue.
#
#   Deliberately narrow: the only exposed actions are "search
#   YouTube and click play" and basic playback control on that same
#   tab -- no generic "click anything on any page" tool.
# ================================================================
import os
import queue
import threading
import urllib.parse
import concurrent.futures

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

_state_lock = threading.Lock()
_current_queue = None
_worker_alive = False
_next_generation = 0


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
            "the YouTube browser stopped responding, sir -- restarting it, please try again"
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
        profile_dir, headless=False, args=_CHROMIUM_ARGS,
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
                        raise RuntimeError("no YouTube window is currently open")
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
def _do_play_youtube(query, page):
    page.goto("https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus(query), timeout=20000)
    page.wait_for_selector("ytd-video-renderer a#video-title", timeout=15000)
    first = page.locator("ytd-video-renderer a#video-title").first
    title = (first.get_attribute("title") or "").strip()
    first.click()
    page.wait_for_selector("video", timeout=15000)
    page.bring_to_front()
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
    return True


def control(action: str) -> str:
    action = (action or "").strip().lower()
    if action not in ("play_pause", "next", "mute"):
        return "I can play/pause, restart, or mute the YouTube tab sir."
    try:
        _submit(lambda page: _do_control(action, page), timeout=15, relaunch=False)
        return {"play_pause": "Toggling playback sir.", "next": "Restarting the video sir.",
                "mute": "Muting sir."}[action]
    except Exception as e:
        if "no YouTube window is currently open" in str(e):
            return "There's no YouTube video playing right now sir."
        return f"I couldn't control the YouTube tab sir: {e}"
