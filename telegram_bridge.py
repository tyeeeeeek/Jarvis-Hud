# ================================================================
#   J.A.R.V.I.S — Telegram bridge
#
#   Free, no-card-ever alternative to the Twilio SMS bridge: same
#   idea (message Jarvis, it runs the command, replies back), but
#   over Telegram's official Bot API instead of SMS. Uses long
#   polling (getUpdates with a server-side wait) -- no public
#   webhook/inbound networking needed, and no per-message cost ever.
#
#   Locked to a single chat_id so a bot token leak or a guessed
#   username can't hand a stranger command access to your PC.
#
#   Fully inert until TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are set
#   in .env -- see README's "Text Jarvis (Telegram)" section.
# ================================================================
import os
import time

import requests

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"

TELEGRAM_AVAILABLE = bool(BOT_TOKEN and CHAT_ID)


def send_message(text: str) -> bool:
    """Message the configured chat. Best-effort -- returns False on failure
    rather than raising, so a network hiccup never crashes a command."""
    if not TELEGRAM_AVAILABLE or not text:
        return False
    try:
        r = requests.post(f"{API_BASE}/sendMessage", json={
            "chat_id": CHAT_ID, "text": text[:4000],
        }, timeout=15)
        return r.status_code == 200
    except Exception as e:
        print(f"  [Telegram] Send error: {e}")
        return False


def poll_thread(on_command, pipeline_stop):
    """Background loop using long polling: each getUpdates call blocks
    server-side for up to ~25s waiting for a new message, so this is near-
    instant without hammering the API. Calls on_command(text) for every new
    message from the authorized chat_id; silently ignores anyone else."""
    if not TELEGRAM_AVAILABLE:
        print("  [Telegram] Not configured -- texting Jarvis is disabled. See README.")
        return

    print(f"  [Telegram] Watching for messages from chat {CHAT_ID}")

    # Consume any backlog without acting on it -- only react to messages
    # sent after Jarvis started watching.
    offset = None
    try:
        r = requests.get(f"{API_BASE}/getUpdates", params={"timeout": 0}, timeout=15)
        updates = r.json().get("result", [])
        if updates:
            offset = updates[-1]["update_id"] + 1
    except Exception as e:
        print(f"  [Telegram] Startup sync error: {e}")

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
                text = (message.get("text") or "").strip()
                if chat_id != CHAT_ID or not text:
                    continue
                print(f"  [Telegram] Received: {text!r}")
                try:
                    on_command(text)
                except Exception as e:
                    print(f"  [Telegram] Command handling error: {e}")
                    send_message("Something went wrong handling that sir.")
        except requests.Timeout:
            continue
        except Exception as e:
            print(f"  [Telegram] Poll error: {e}")
            time.sleep(5)
