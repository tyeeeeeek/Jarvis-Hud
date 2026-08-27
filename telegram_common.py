# ================================================================
#   J.A.R.V.I.S — shared Telegram send helper
#
#   Single low-level "POST to a given bot's sendMessage endpoint" used by
#   every Telegram sub-agent (telegram_bridge.py's two-way command bot,
#   jarvis_cpu_alerts.py's health alerts, jarvis_improvement.py's daily
#   self-improvement report) so the actual HTTP call only has one
#   implementation to get right or fix. Each caller still owns its own bot
#   token/chat ID -- this never reads from the environment itself, so one
#   sub-agent's token can never accidentally leak into another's requests.
# ================================================================
import requests


def send(bot_token: str, chat_id: str, text: str) -> bool:
    """Send-only helper -- returns False on any failure (missing
    token/chat/text, network error, non-200 response) rather than raising,
    so a network hiccup never crashes the caller's loop."""
    if not (bot_token and chat_id and text):
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{bot_token}/sendMessage", json={
            "chat_id": chat_id, "text": text[:4000],
        }, timeout=15)
        return r.status_code == 200
    except Exception as e:
        print(f"  [Telegram] Send error: {e}")
        return False
