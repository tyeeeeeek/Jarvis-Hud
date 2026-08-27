# ================================================================
#   J.A.R.V.I.S — JarvisCPU_Alerts sub-agent
#
#   Dedicated, send-only Telegram bot for PC health alerts. Kept as its
#   own bot/module (separate from telegram_bridge.py's two-way command
#   channel) so health notifications have their own identity and never
#   get mixed up with command replies or bank-statement uploads.
#
#   Wired into jarvis.py's existing 2-hour health-check loop
#   (_health_watcher_thread): send_summary() fires once after every
#   check completes, send_critical_alert() fires immediately the moment
#   check_system_health() flags overheating risk or critically low disk
#   space, rather than waiting for the next scheduled summary.
#
#   Never polls for incoming messages or accepts commands -- a leaked
#   token can only be used to spam this one chat, never to control
#   anything on the PC.
#
#   Fully inert until JARVIS_CPU_ALERTS_BOT_TOKEN is set in .env; reuses
#   TELEGRAM_CHAT_ID (same user, same phone) unless
#   JARVIS_CPU_ALERTS_CHAT_ID overrides it.
# ================================================================
import os

import requests

BOT_TOKEN = os.environ.get("JARVIS_CPU_ALERTS_BOT_TOKEN", "")
CHAT_ID = os.environ.get("JARVIS_CPU_ALERTS_CHAT_ID", "") or os.environ.get("TELEGRAM_CHAT_ID", "")
API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"

AVAILABLE = bool(BOT_TOKEN and CHAT_ID)


def _send(text: str) -> bool:
    """Send-only Telegram helper for this bot. Best-effort -- returns False
    on failure rather than raising, so a network hiccup never crashes the
    health-check loop."""
    if not AVAILABLE or not text:
        return False
    try:
        r = requests.post(f"{API_BASE}/sendMessage", json={
            "chat_id": CHAT_ID, "text": text[:4000],
        }, timeout=15)
        return r.status_code == 200
    except Exception as e:
        print(f"  [JarvisCPU_Alerts] Send error: {e}")
        return False


def send_summary(result_text: str) -> bool:
    """Sent once after every scheduled health check completes, regardless of
    whether anything was wrong -- a routine PC health status update."""
    return _send(f"PC health check sir: {result_text}")


def send_critical_alert(result_text: str) -> bool:
    """Sent immediately the moment check_system_health() flags a critical
    issue (overheating risk or critically low disk space), instead of
    waiting for the next scheduled summary."""
    return _send(f"⚠️ Critical PC health alert sir: {result_text}")
