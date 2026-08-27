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

_request_q: "queue.Queue" = queue.Queue()
_worker_started = False
_worker_lock = threading.Lock()


def _ensure_worker():
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        t = threading.Thread(target=_worker_thread, name="BrowserControl", daemon=True)
        t.start()
        _worker_started = True


def _submit(fn, timeout=45, relaunch=True):
    """Run fn(page) on the worker thread, block for the result.

    relaunch=False is used for playback controls (play/pause, mute, ...): if
    the window has been closed or crashed there is nothing to control, so we
    raise a clear error instead of popping open a fresh blank browser window.
    """
    _ensure_worker()
    fut: concurrent.futures.Future = concurrent.futures.Future()
    _request_q.put((fn, fut, relaunch))
    return fut.result(timeout=timeout)


def _clear_stale_singleton_files():
    """If Chromium was killed or crashed instead of exiting cleanly, it can
    leave SingletonLock/SingletonCookie/SingletonSocket behind in the profile
    dir. A leftover lock makes the *next* launch_persistent_context fail
    outright, which used to permanently break playback until the whole app
    was restarted -- clear it before every (re)launch."""
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        path = os.path.join(PROFILE_DIR, name)
        try:
            if os.path.exists(path) or os.path.islink(path):
                os.remove(path)
        except OSError:
            pass


_page_crashed = {"flag": False}


def _watch_for_crash(page):
    """Playwright's page.is_closed() only reflects an explicit close -- a
    renderer *crash* (tab dies but the window/context stays open) leaves
    is_closed() False forever, which used to make every future request retry
    the same dead page and fail in a loop. Track crashes explicitly so
    _is_usable() can tell the two apart."""
    _page_crashed["flag"] = False
    page.on("crash", lambda _page: _page_crashed.update(flag=True))


def _launch_context(p):
    os.makedirs(PROFILE_DIR, exist_ok=True)
    _clear_stale_singleton_files()
    context = p.chromium.launch_persistent_context(
        PROFILE_DIR, headless=False,
        args=[
            "--window-size=900,700", "--window-position=100,100",
            # Windows suspends/throttles occluded (covered or minimized)
            # Chromium windows by default, which can make an unattended
            # playback window look stalled or dead -- keep it running.
            "--disable-features=CalculateNativeWinOcclusion",
        ],
    )
    page = context.pages[0] if context.pages else context.new_page()
    _watch_for_crash(page)
    return context, page


def _is_usable(page):
    if page is None or _page_crashed["flag"]:
        return False
    try:
        return not page.is_closed()
    except Exception:
        return False


def _recover(old_page, context, p):
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
            _watch_for_crash(new_page)
            return context, new_page
        try:
            context.close()
        except Exception:
            pass
    return _launch_context(p)


def _worker_thread():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        context = page = None

        while True:
            fn, fut, relaunch = _request_q.get()
            if fn is None:  # shutdown sentinel
                break
            try:
                if not _is_usable(page):
                    if not relaunch:
                        raise RuntimeError("no YouTube window is currently open")
                    # The tab crashed, the window was closed, or Chromium
                    # crashed entirely -- transparently heal instead of
                    # leaving playback dead.
                    context, page = _recover(page, context, p)
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
    if _worker_started:
        _request_q.put((None, concurrent.futures.Future(), True))


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
        title = _submit(lambda page: _do_play_youtube(query, page), timeout=45)
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
