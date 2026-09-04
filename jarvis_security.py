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
#   processes, a LAN scan for new/unknown devices, outdated device
#   firmware/drivers, a refresh of the malicious-link blocklist that gates
#   browser navigation, and verify/install for each vetted browser
#   security/privacy extension -- uBlock Origin Lite, DuckDuckGo Privacy
#   Essentials -- in the dedicated Jarvis browser). send_summary() fires
#   once after every check, send_alert() fires immediately the moment
#   run_security_check() flags something critical (a suspicious process or
#   a brand-new device on the LAN) instead of waiting for the next
#   scheduled summary.
#
#   Two-way for exactly one thing: any incoming text message from the
#   authorized chat triggers an immediate reply, grounded (via
#   telegram_common.ask_grounded()) in the same read-only summary of recent
#   security events, sweeps, and jobs this bot has been running -- so
#   "what's my sweep history" and "any new devices on the LAN" get
#   different, actually-relevant answers instead of the same fixed dump --
#   see poll_thread(). That underlying summary is read-only, built from the
#   on-disk sweep log and the shared delivery log, never a fresh sweep
#   itself (a full sweep touches the network and the browser and can take a
#   while; it still runs on its own 4-hour schedule). Grounding only ever
#   sees that summary, so it still can't invent a finding or accept a
#   command; a leaked token can only be used to ask "what have you found
#   lately", never to control anything.
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


def respond(text: str) -> str:
    """Answer one message the same way this bot's Telegram two-way channel
    does: pulls tools.security_status_report() -- a read-only summary of
    recent sweeps and jobs from ~/.jarvis/security_log.jsonl and the shared
    delivery log -- and grounds a reply in it via telegram_common.
    ask_grounded() so the reply actually answers what was asked instead of
    always sending the same fixed dump. Shared by poll_thread() (Telegram)
    and the phone dashboard's chat. Deliberately doesn't trigger a fresh
    run_security_check() itself (that sweep touches the network and the
    browser and can take a while); it just reports what the scheduled
    4-hour sweep has already found, so the reply is fast. tools is imported
    locally, not at module load time, because tools.py imports this module
    -- importing it back up top would be a circular import."""
    import tools
    try:
        report = tools.security_status_report()
    except Exception as e:
        return f"Couldn't pull a security status report sir: {e}"
    return telegram_common.ask_grounded(
        _AGENT_NAME, "monitoring this PC's cybersecurity status and reporting sweep findings",
        report, text)


def poll_thread(pipeline_stop) -> None:
    """This bot's two-way half: long-polls its own token/chat (isolated from
    every other bot's inbox) and replies to any incoming text via
    respond()."""
    def on_message(text):
        _send(respond(text))
    telegram_common.poll_thread(BOT_TOKEN, CHAT_ID, on_message, pipeline_stop, agent=_AGENT_NAME)
