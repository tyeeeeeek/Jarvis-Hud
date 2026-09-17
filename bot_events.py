# ================================================================
#   J.A.R.V.I.S — shared event bus
#
#   Minimal in-process pub/sub used to bridge a voice-triggered tool call
#   (e.g. show_map/show_weather_radar in tools.py) into jarvis.py's own
#   WebSocket feed to the desktop HUD, without tools.py needing to import
#   jarvis.py itself (which would be a circular import). Deliberately NOT
#   a network channel -- just in-process pub/sub. If you add your own
#   background watchers/integrations, this is a ready-made place for them
#   to publish updates the HUD (or anything else you subscribe) should
#   react to, instead of wiring a new one-off callback per pair.
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
