# ================================================================
#   J.A.R.V.I.S — shared bot event bus
#
#   Minimal in-process pub/sub shared by every sub-agent thread (the
#   watchdogs, the employee worker, Telegram command handling) running
#   inside jarvis.py's one long-lived process. This is deliberately NOT a
#   network channel and grants no new capability -- every Telegram bot
#   still has its own isolated token/chat (see telegram_common.py's module
#   docstring), a leaked one still can't see another's traffic. This bus
#   only carries already-public-to-the-user information (the same alerts/
#   status each bot already sends over Telegram) so "bots consulting each
#   other" -- and the future phone dashboard's live feed -- have one
#   shared place to plug into instead of a new transport per pair.
# ================================================================
import threading
import time

_lock = threading.Lock()
_subscribers = []

# Small in-memory ring buffer of the last N events, so a subscriber that
# starts listening late (e.g. a dashboard page that just loaded) can show
# recent history instead of starting on a blank feed. Lost on restart --
# this is a live activity feed, not a durable log (the watchers already
# keep their own JSONL logs for that: health_log.jsonl, security_log.jsonl,
# etc.).
_HISTORY_MAX = 200
_history = []


def publish(event_type: str, payload: dict) -> None:
    """Announce one event to every current subscriber. Never raises -- a
    bad subscriber callback must never take down the publisher (a watcher
    thread, the employee worker, ...)."""
    event = {"type": event_type, "payload": payload, "ts": time.time()}
    with _lock:
        _history.append(event)
        if len(_history) > _HISTORY_MAX:
            del _history[: len(_history) - _HISTORY_MAX]
        subs = list(_subscribers)
    for cb in subs:
        try:
            cb(event)
        except Exception:
            pass


def subscribe(callback):
    """Registers callback(event) for every future publish(). Returns an
    unsubscribe function."""
    with _lock:
        _subscribers.append(callback)

    def _unsubscribe():
        with _lock:
            if callback in _subscribers:
                _subscribers.remove(callback)

    return _unsubscribe


def recent(limit: int = 50):
    with _lock:
        return list(_history[-limit:])
