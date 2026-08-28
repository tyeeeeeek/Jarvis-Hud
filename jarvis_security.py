# ================================================================
#   J.A.R.V.I.S — JarSecurity sub-agent
#
#   Dedicated, send-only Telegram bot for cybersecurity status --
#   separate from telegram_bridge.py's two-way command channel and the
#   other watchdogs (jarvis_cpu_alerts.py, jarvis_improvement.py) so a
#   leaked token here can only be used to spam this one chat, never to
#   control anything, see command traffic, or see other agents' data.
#
#   Wired into jarvis.py's _security_watcher_thread: runs
#   tools.run_security_check() on a fixed cadence (open ports, suspicious
#   processes, a LAN scan for new/unknown devices, and uBlock Origin Lite
#   verify/install in the dedicated Jarvis browser). send_summary() fires
#   once after every check, send_alert() fires immediately the moment
#   run_security_check() flags something critical (a suspicious process or
#   a brand-new device on the LAN) instead of waiting for the next
#   scheduled summary.
#
#   Never polls for incoming messages or accepts commands -- a leaked
#   token can only be used to spam this one chat, never to control
#   anything on the PC.
#
#   Fully inert until JARVIS_SECURITY_BOT_TOKEN is set in .env; reuses
#   TELEGRAM_CHAT_ID (same user, same phone) unless
#   JARVIS_SECURITY_CHAT_ID overrides it.
# ================================================================
import os

import telegram_common

BOT_TOKEN = os.environ.get("JARVIS_SECURITY_BOT_TOKEN", "")
CHAT_ID = os.environ.get("JARVIS_SECURITY_CHAT_ID", "") or os.environ.get("TELEGRAM_CHAT_ID", "")

AVAILABLE = bool(BOT_TOKEN and CHAT_ID)


_AGENT_NAME = "JarSecurity"


def _send(text: str) -> bool:
    """Send-only Telegram helper for this bot. Best-effort -- returns False
    on failure rather than raising, so a network hiccup never crashes the
    security-check loop. Tags sends with this bot's agent name so a
    confirmed delivery lands in the shared supervision log."""
    return telegram_common.send(BOT_TOKEN, CHAT_ID, text, agent=_AGENT_NAME)


def send_summary(result_text: str) -> bool:
    """Sent once after every scheduled security sweep completes, regardless
    of whether anything was wrong -- a routine security status update. Tone
    is the blunt, no-nonsense watchdog: it reports the findings, not
    feelings."""
    return _send(f"JARSECURITY SWEEP — {result_text}")


def send_alert(result_text: str) -> bool:
    """Sent immediately the moment run_security_check() flags a suspicious
    process or a brand-new device on the LAN, instead of waiting for the
    next scheduled summary. Tone stays blunt -- no hedging on something that
    actually needs a look."""
    return _send(f"⚠️ JARSECURITY ALERT — look at this now: {result_text}")


def send_test_message() -> bool:
    """Manual, on-demand test send -- lets the user confirm this bot's
    token/chat ID still deliver right now, without waiting for the next
    scheduled sweep. Called from tools.send_agent_test_message() when the
    user asks Jarvis to test/ping this agent's Telegram channel. A confirmed
    send lands in the shared delivery log same as any other send from this
    agent, so it also shows up in agent_status()."""
    return _send("JARSECURITY TEST — this is a manual test message, sir. If you're reading this, "
                  "the JarSecurity Telegram pipeline is working.")
