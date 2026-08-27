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

import telegram_common

BOT_TOKEN = os.environ.get("JARVIS_CPU_ALERTS_BOT_TOKEN", "")
CHAT_ID = os.environ.get("JARVIS_CPU_ALERTS_CHAT_ID", "") or os.environ.get("TELEGRAM_CHAT_ID", "")

AVAILABLE = bool(BOT_TOKEN and CHAT_ID)


_AGENT_NAME = "JarvisCPU_Alerts"


def _send(text: str) -> bool:
    """Send-only Telegram helper for this bot. Best-effort -- returns False
    on failure rather than raising, so a network hiccup never crashes the
    health-check loop. Tags sends with this bot's agent name so a
    confirmed delivery lands in the shared supervision log."""
    return telegram_common.send(BOT_TOKEN, CHAT_ID, text, agent=_AGENT_NAME)


def send_summary(result_text: str) -> bool:
    """Sent once after every scheduled health check completes, regardless of
    whether anything was wrong -- a routine PC health status update. Tone is
    the blunt, no-nonsense watchdog: it reports the numbers, not feelings."""
    return _send(f"WATCHDOG CHECK — {result_text}")


def send_critical_alert(result_text: str) -> bool:
    """Sent immediately the moment check_system_health() flags a critical
    issue (overheating risk or critically low disk space), instead of
    waiting for the next scheduled summary. Tone stays blunt -- no
    hedging on something that actually needs action."""
    return _send(f"⚠️ WATCHDOG ALERT — fix this now: {result_text}")
