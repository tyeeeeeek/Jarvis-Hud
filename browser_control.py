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


def _submit(fn, timeout=45):
    """Run fn(page) on the worker thread, block for the result."""
    _ensure_worker()
    fut: concurrent.futures.Future = concurrent.futures.Future()
    _request_q.put((fn, fut))
    return fut.result(timeout=timeout)


def _worker_thread():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        os.makedirs(PROFILE_DIR, exist_ok=True)
        context = p.chromium.launch_persistent_context(
            PROFILE_DIR, headless=False,
            args=["--window-size=900,700", "--window-position=100,100"],
        )
        page = context.pages[0] if context.pages else context.new_page()

        while True:
            fn, fut = _request_q.get()
            if fn is None:  # shutdown sentinel
                break
            try:
                result = fn(page)
                if not fut.done():
                    fut.set_result(result)
            except Exception as e:
                if not fut.done():
                    fut.set_exception(e)

        try:
            context.close()
        except Exception:
            pass


def shutdown():
    """Best-effort graceful close, called from jarvis.py on exit."""
    if _worker_started:
        _request_q.put((None, concurrent.futures.Future()))


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
        _submit(lambda page: _do_control(action, page), timeout=15)
        return {"play_pause": "Toggling playback sir.", "next": "Restarting the video sir.",
                "mute": "Muting sir."}[action]
    except Exception as e:
        return f"I couldn't control the YouTube tab sir: {e}"
