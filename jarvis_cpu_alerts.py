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
#   Two-way for exactly one thing: any incoming text message from the
#   authorized chat triggers an immediate, on-demand check_system_health()
#   run and a reply with the result -- see poll_thread(). It still never
#   accepts commands or forwards text anywhere else, so a leaked token can
#   only be used to ask "how's the PC doing", never to control anything.
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


def send_test_message() -> bool:
    """Manual, on-demand test send -- lets the user confirm this bot's
    token/chat ID still deliver right now, without waiting for the next
    scheduled health check. Called from tools.send_agent_test_message()
    when the user asks Jarvis to test/ping this agent's Telegram channel.
    A confirmed send lands in the shared delivery log same as any other
    send from this agent, so it also shows up in agent_status()."""
    return _send("WATCHDOG TEST — this is a manual test message, sir. If you're reading this, "
                  "the JarvisCPU_Alerts Telegram pipeline is working.")


def poll_thread(pipeline_stop) -> None:
    """This bot's two-way half: long-polls its own token/chat (isolated from
    every other bot's inbox) and, on any incoming text message, runs a fresh
    check_system_health() and replies with the result -- an on-demand PC
    health/status update without waiting for the next scheduled 2-hour
    check. tools is imported locally (not at module load time) because
    tools.py imports this module itself; importing it up top would be a
    circular import. check_system_health() is a fast, read-only-except-for-
    routine-cleanup call (sensors + disk usage), so it's run right here on
    the poll thread rather than spun off in the background."""
    def on_message(_text):
        import tools
        try:
            result = tools.check_system_health()
        except Exception as e:
            _send(f"Couldn't run a health check sir: {e}")
            return
        _send(f"WATCHDOG ON-DEMAND CHECK — {result}")
    telegram_common.poll_thread(BOT_TOKEN, CHAT_ID, on_message, pipeline_stop, agent=_AGENT_NAME)
