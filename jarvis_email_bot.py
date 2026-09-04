# ================================================================
#   J.A.R.V.I.S — JarvisEmail bot
#
#   A dedicated Telegram bot for real, open-ended Gmail/Outlook mail and
#   calendar Q&A and actions -- "what's in my inbox", "read that Grubhub
#   email", "what's on my calendar this week", "reschedule my 3pm to 4pm".
#   Routes every incoming message through the SAME full-brain
#   handle_command() pipeline voice/the main Telegram bridge/SMS use --
#   this bot isn't a separate, narrower brain, it's just another channel
#   into the one brain, which already has search_email/read_email/
#   list_calendar_events/update_calendar_event/delete_calendar_event (see
#   tools.py) alongside everything else Jarvis can do.
#
#   Same safety model as every other integration here: gmail_service.py/
#   outlook_service.py are never granted a send-mail scope, so nothing
#   this bot triggers can ever send an email -- draft_email always lands
#   in Drafts for the user to review and send themselves.
#
#   Locked to a single chat_id (same reasoning as telegram_bridge.py) so a
#   leaked token can't hand a stranger access. Reuses TELEGRAM_CHAT_ID by
#   default (same person, same phone) unless JARVIS_EMAIL_CHAT_ID
#   overrides it.
#
#   Fully inert until JARVIS_EMAIL_BOT_TOKEN is set in .env.
# ================================================================
import os
import time

import requests

import telegram_common

BOT_TOKEN = os.environ.get("JARVIS_EMAIL_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("JARVIS_EMAIL_CHAT_ID", "").strip() or os.environ.get("TELEGRAM_CHAT_ID", "").strip()
API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}" if BOT_TOKEN else ""

JARVIS_EMAIL_AVAILABLE = bool(BOT_TOKEN and CHAT_ID)

_AGENT_NAME = "JarvisEmail"


def send_message(text: str) -> bool:
    """Message the configured chat. Best-effort -- returns False on
    failure rather than raising, so a network hiccup never crashes a
    command."""
    return telegram_common.send(BOT_TOKEN, CHAT_ID, text, agent=_AGENT_NAME)


def poll_thread(on_command, pipeline_stop):
    """Background loop using long polling (same shape as
    telegram_bridge.py's): each getUpdates call blocks server-side for up
    to ~25s waiting for a new message. Calls on_command(text) for every
    new text message from the authorized chat_id; silently ignores
    anyone else and non-text messages (no bank-statement-style file
    handling here -- that's the main bridge's job, not this one's)."""
    if not JARVIS_EMAIL_AVAILABLE:
        print("  [JarvisEmail] Not configured -- texting the email bot is disabled. Set JARVIS_EMAIL_BOT_TOKEN in .env.")
        return

    print(f"  [JarvisEmail] Watching for messages from chat {CHAT_ID}")

    offset = None
    try:
        r = requests.get(f"{API_BASE}/getUpdates", params={"timeout": 0}, timeout=15)
        updates = r.json().get("result", [])
        if updates:
            offset = updates[-1]["update_id"] + 1
    except Exception as e:
        print(f"  [JarvisEmail] Startup sync error: {e}")

    while not pipeline_stop.is_set():
        try:
            params = {"timeout": 25}
            if offset is not None:
                params["offset"] = offset
            r = requests.get(f"{API_BASE}/getUpdates", params=params, timeout=35)
            updates = r.json().get("result", [])
            for update in updates:
                offset = update["update_id"] + 1
                message = update.get("message") or {}
                chat_id = str(message.get("chat", {}).get("id", ""))
                if chat_id != CHAT_ID:
                    continue
                text = (message.get("text") or "").strip()
                if not text:
                    continue
                print(f"  [JarvisEmail] Received: {text!r}")
                try:
                    on_command(text)
                except Exception as e:
                    print(f"  [JarvisEmail] Command handling error: {e}")
                    send_message("Something went wrong handling that sir.")
        except requests.Timeout:
            continue
        except Exception as e:
            print(f"  [JarvisEmail] Poll error: {e}")
            time.sleep(5)
