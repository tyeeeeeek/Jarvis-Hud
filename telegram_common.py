# ================================================================
#   J.A.R.V.I.S — shared Telegram send/poll helpers
#
#   Single low-level "POST to a given bot's sendMessage endpoint" (send())
#   and "long-poll a given bot's getUpdates endpoint" (poll_thread()) used
#   by every Telegram sub-agent (telegram_bridge.py's two-way command bot,
#   jarvis_cpu_alerts.py's health alerts, jarvis_improvement.py's daily
#   self-improvement report and on-demand requests, jarvis_security.py's
#   sweeps) so the actual HTTP calls only have one implementation each to
#   get right or fix. Each caller still owns its own bot token/chat ID --
#   this never reads from the environment itself, so one sub-agent's token
#   can never accidentally leak into another's requests or see another's
#   incoming messages.
#
#   When a caller passes `agent`, a confirmed (HTTP 200) send is also
#   appended to ~/.jarvis/telegram_delivery_log.jsonl -- a supervision
#   trail so Jarvis (or the user) can check that background sub-agents like
#   JarvisCPU_Alerts and JarvisImprovement are actually firing on their
#   schedules, not just running silently.
# ================================================================
import json
import os
import time

import requests

_JARVIS_DIR = os.path.join(os.path.expanduser("~"), ".jarvis")
DELIVERY_LOG_PATH = os.path.join(_JARVIS_DIR, "telegram_delivery_log.jsonl")


def _log_delivery(agent: str, text: str) -> None:
    """Best-effort confirmation log entry -- a logging failure must never
    take down the caller's send path, so any error here is swallowed."""
    try:
        os.makedirs(_JARVIS_DIR, exist_ok=True)
        with open(DELIVERY_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(), "agent": agent, "preview": text[:120]}) + "\n")
    except Exception:
        pass


def send(bot_token: str, chat_id: str, text: str, agent: str = "") -> bool:
    """Send-only helper -- returns False on any failure (missing
    token/chat/text, network error, non-200 response) rather than raising,
    so a network hiccup never crashes the caller's loop. Pass `agent` (a
    short name like "JarvisCPU_Alerts") to record a confirmation entry in
    the shared delivery log once Telegram confirms the send."""
    if not (bot_token and chat_id and text):
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{bot_token}/sendMessage", json={
            "chat_id": chat_id, "text": text[:4000],
        }, timeout=15)
        ok = r.status_code == 200
        if ok and agent:
            _log_delivery(agent, text)
        return ok
    except Exception as e:
        print(f"  [Telegram] Send error: {e}")
        return False


def poll_thread(bot_token: str, chat_id: str, on_message, pipeline_stop, agent: str = "") -> None:
    """Generic long-polling loop shared by every two-way sub-agent bot
    (JarvisCPU_Alerts, JarvisImprovement, JarSecurity) -- each caller passes
    its own bot_token/chat_id, so one sub-agent's inbox can never see
    another's messages, same isolation send() already gives outbound. Uses
    the same near-instant, no-public-webhook long-polling style as
    telegram_bridge.poll_thread. Calls on_message(text) for every new
    non-empty text message from the authorized chat_id; anything else (a
    message from a different chat, a non-text attachment) is silently
    ignored -- these bots are status/command channels only, not file
    inboxes. A no-op immediately if bot_token/chat_id aren't set, so this
    stays fully inert until the corresponding *_BOT_TOKEN env var is
    configured."""
    if not (bot_token and chat_id):
        return
    api_base = f"https://api.telegram.org/bot{bot_token}"
    label = f"[{agent}] " if agent else ""
    print(f"  [Telegram] {label}Watching for messages from chat {chat_id}")

    # Consume any backlog without acting on it -- only react to messages
    # sent after this bot started watching.
    offset = None
    try:
        r = requests.get(f"{api_base}/getUpdates", params={"timeout": 0}, timeout=15)
        updates = r.json().get("result", [])
        if updates:
            offset = updates[-1]["update_id"] + 1
    except Exception as e:
        print(f"  [Telegram] {label}Startup sync error: {e}")

    while not pipeline_stop.is_set():
        try:
            params = {"timeout": 25}
            if offset is not None:
                params["offset"] = offset
            r = requests.get(f"{api_base}/getUpdates", params=params, timeout=35)
            updates = r.json().get("result", [])
            for update in updates:
                offset = update["update_id"] + 1
                message = update.get("message") or {}
                msg_chat_id = str(message.get("chat", {}).get("id", ""))
                if msg_chat_id != chat_id:
                    continue
                text = (message.get("text") or "").strip()
                if not text:
                    continue
                print(f"  [Telegram] {label}Received: {text!r}")
                try:
                    on_message(text)
                except Exception as e:
                    print(f"  [Telegram] {label}Message handling error: {e}")
        except requests.Timeout:
            continue
        except Exception as e:
            print(f"  [Telegram] {label}Poll error: {e}")
            time.sleep(5)
