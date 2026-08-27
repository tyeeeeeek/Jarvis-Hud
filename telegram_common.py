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
