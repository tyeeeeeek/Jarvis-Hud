# ================================================================
#   J.A.R.V.I.S — JarvisImprovement sub-agent
#
#   Dedicated, send-only Telegram bot for the daily self-improvement
#   report. Kept as its own bot/module (separate from telegram_bridge.py's
#   two-way command channel and jarvis_cpu_alerts.py's PC health alerts)
#   so a leaked token here can only be used to read one status message a
#   day -- never to control anything, see command traffic, or see health
#   data.
#
#   Wired into jarvis.py's _improvement_watcher_thread: once every 24
#   hours, starting at 6 AM local time, that thread calls
#   tools.run_daily_self_improve() (the same self_improve.md-driven
#   mechanism as the nightly/on-demand self-improvement pass, capped at up
#   to 2 hours) and then send_report() fires exactly once, right after the
#   pass completes -- whether it made changes, found nothing safe to
#   improve, or hit the time-box.
#
#   Never polls for incoming messages or accepts commands -- a leaked
#   token can only be used to spam this one chat, never to control
#   anything on the PC.
#
#   Fully inert until JARVIS_IMPROVEMENT_BOT_TOKEN is set in .env; reuses
#   TELEGRAM_CHAT_ID (same user, same phone) unless
#   JARVIS_IMPROVEMENT_CHAT_ID overrides it.
# ================================================================
import os

import telegram_common

BOT_TOKEN = os.environ.get("JARVIS_IMPROVEMENT_BOT_TOKEN", "")
CHAT_ID = os.environ.get("JARVIS_IMPROVEMENT_CHAT_ID", "") or os.environ.get("TELEGRAM_CHAT_ID", "")

AVAILABLE = bool(BOT_TOKEN and CHAT_ID)


def send_report(added, timed_out=False) -> bool:
    """Sent once after every daily self-improvement pass completes.
    `added` is the list of commit-subject lines (one per safe, verified
    change actually committed during the run) -- an empty list means
    nothing safe to improve was found today. `timed_out` marks a run that
    hit the 2-hour cap rather than finishing on its own."""
    if not added:
        text = "Daily self-improvement pass sir: nothing safe to improve found today."
    else:
        bullets = "\n".join(f"• {line}" for line in added)
        text = f"Daily self-improvement pass sir, here's what I added today:\n{bullets}"
    if timed_out:
        text += "\n\n(Hit the 2-hour time box -- may have more queued for tomorrow.)"
    return telegram_common.send(BOT_TOKEN, CHAT_ID, text)
