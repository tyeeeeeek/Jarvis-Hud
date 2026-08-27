# ================================================================
#   J.A.R.V.I.S — Internet speed test
#
#   Wraps the `speedtest-cli` PyPI package (pure Python, no separate
#   binary or account needed) to measure real download/upload/ping to
#   the internet, on demand (check_internet_speed / the HUD widget's
#   "run test" button) or on a schedule (jarvis.py's
#   _speedtest_watcher_thread, every few hours). Every run is appended
#   to ~/.jarvis/speedtest_log.jsonl so get_last_result() can show the
#   most recent reading instantly instead of re-running a ~15-30s test
#   on every widget load.
# ================================================================
import os
import json
import time

try:
    import speedtest as _speedtest_lib
    SPEEDTEST_AVAILABLE = True
except ImportError:
    SPEEDTEST_AVAILABLE = False

JARVIS_DIR = os.path.join(os.path.expanduser("~"), ".jarvis")
LOG_PATH = os.path.join(JARVIS_DIR, "speedtest_log.jsonl")


def _log(entry):
    os.makedirs(JARVIS_DIR, exist_ok=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def run_speed_test():
    """Run a real internet speed test right now (takes roughly 15-30
    seconds). Returns {"ts","download_mbps","upload_mbps","ping_ms",
    "server"} and appends it to the log. Raises RuntimeError if
    speedtest-cli isn't installed, or whatever speedtest-cli itself raises
    if every test server is unreachable (no internet, etc) -- callers turn
    that into a friendly reply."""
    if not SPEEDTEST_AVAILABLE:
        raise RuntimeError("speedtest-cli isn't installed -- pip install speedtest-cli in the venv.")
    st = _speedtest_lib.Speedtest()
    st.get_best_server()
    download_bps = st.download()
    upload_bps = st.upload()
    result = st.results.dict()
    # speedtest-cli's ping figure (from its own server-selection latency
    # probe) has been observed to occasionally come back wildly wrong (six
    # figures, i.e. thousands of seconds) rather than raising -- clamp
    # anything above a real-world-impossible threshold to None/unknown
    # instead of reporting a number that would just be a lie.
    raw_ping = result.get("ping") or 0
    ping_ms = round(raw_ping, 1) if 0 < raw_ping <= 5000 else None
    entry = {
        "ts": time.time(),
        "download_mbps": round(download_bps / 1_000_000, 1),
        "upload_mbps": round(upload_bps / 1_000_000, 1),
        "ping_ms": ping_ms,
        "server": (result.get("server") or {}).get("sponsor", "unknown"),
    }
    _log(entry)
    return entry


def get_last_result():
    """Read the most recent logged speed test result, if any, without
    running a new one -- used for a fast initial widget load."""
    if not os.path.exists(LOG_PATH):
        return None
    try:
        with open(LOG_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
        for line in reversed(lines):
            line = line.strip()
            if line:
                return json.loads(line)
    except Exception:
        return None
    return None
