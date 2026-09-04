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
#
#   Also provides ask_grounded() -- turns a fixed report into a natural,
#   cohesive reply to whatever the user actually asked (the read-only
#   sub-agent bots -- JarvisCPU_Alerts, JarSecurity, JarvisImprovement's
#   question path -- all use it) without the sub-agent ever gaining a new
#   capability: it only ever sees data the caller already collected through
#   its existing, narrow tool (a health check, a security sweep log, agent
#   status), and never runs a tool itself.
#
#   Primary path is the same Claude Code CLI brain.py uses for the main
#   command bot, but locked down with --tools "" and no --mcp-config at
#   all -- pure chat, structurally incapable of running a command or
#   touching a file, same jail as before, just a real model instead of a
#   small local one. That's a real behavior change worth knowing: unlike
#   the old local-only Ollama call, this path sends `context_text`/
#   `user_text` to Anthropic's API (same place voice/text commands already
#   go via brain.py) rather than staying fully offline. Falls back to the
#   local Ollama model (same offline safety model as jarvis.py's own
#   ask_ollama) if the Claude CLI isn't installed or the call fails, and
#   falls back to returning context_text unchanged if neither is
#   available, so a two-way bot still replies with something true even in
#   the worst case.
# ================================================================
import json
import os
import shutil
import subprocess
import time

import requests

_JARVIS_DIR = os.path.join(os.path.expanduser("~"), ".jarvis")
DELIVERY_LOG_PATH = os.path.join(_JARVIS_DIR, "telegram_delivery_log.jsonl")

_OLLAMA_URL = "http://localhost:11434/api/generate"
_OLLAMA_MODEL = "llama3.2"

_CLAUDE_CLI = shutil.which("claude")
_CLAUDE_CLI_TIMEOUT_SECS = 45


def _grounded_system_prompt(agent_name: str, role_description: str) -> str:
    return (
        f"You are {agent_name}, a narrow Jarvis sub-agent whose only job is {role_description}. "
        "You have no tools and cannot run commands, browse the web, or take any action -- you can "
        "only read the data given to you below and answer the user's message using it. Never invent "
        "facts, numbers, or events that aren't in that data. If the data doesn't answer what they "
        "asked, say so plainly rather than guessing. Give a real, cohesive answer to what they "
        "actually asked -- don't just restate the raw data as a list of numbers or field names. "
        "Speak with calm, blunt confidence, address the user as sir occasionally (not every "
        "sentence), keep it under 60 words, no markdown, no bullet points, no emoji."
    )


def _ask_claude_cli(system: str, user_prompt: str) -> str:
    """Pure-chat call through the already-installed Claude Code CLI: --tools
    "" disables every built-in tool and no --mcp-config is passed at all, so
    this is structurally incapable of running a command, reading a file, or
    reaching anything Jarvis can do -- it can only read the prompt and
    answer in text. Returns "" (never raises) on any failure -- missing
    CLI, timeout, non-zero exit, empty output -- so the caller can fall
    through to the Ollama path."""
    if not _CLAUDE_CLI:
        return ""
    try:
        r = subprocess.run(
            [_CLAUDE_CLI, "-p", user_prompt, "--tools", "", "--system-prompt", system],
            capture_output=True, text=True, timeout=_CLAUDE_CLI_TIMEOUT_SECS,
        )
        if r.returncode == 0:
            return (r.stdout or "").strip()
    except Exception as e:
        print(f"  [Telegram] ask_grounded Claude CLI error: {e}")
    return ""


def _ask_ollama(system: str, agent_name: str, context_text: str, user_text: str) -> str:
    prompt = f"{system}\n\nData you know:\n{context_text}\n\nUser just said: {user_text}\n{agent_name}:"
    try:
        r = requests.post(_OLLAMA_URL, json={
            "model": _OLLAMA_MODEL, "prompt": prompt, "stream": False,
        }, timeout=30)
        if r.status_code == 200:
            return (r.json().get("response") or "").strip()
    except Exception as e:
        print(f"  [{agent_name}] ask_grounded Ollama fallback error: {e}")
    return ""


def ask_grounded(agent_name: str, role_description: str, context_text: str, user_text: str) -> str:
    """Turn `context_text` (a report the caller already generated through
    its own narrow, existing tool -- never fetched here) into a natural,
    cohesive reply to `user_text`, the message the user actually sent.
    Tries the real Claude CLI first (see _ask_claude_cli), falls back to
    the local Ollama model if that's unavailable, and falls back to
    context_text unchanged if both fail -- so a two-way bot always replies
    with something true."""
    system = _grounded_system_prompt(agent_name, role_description)
    user_prompt = f"Data you know:\n{context_text}\n\nUser just said: {user_text}\nReply as {agent_name}, in character, with just the reply text:"
    reply = _ask_claude_cli(system, user_prompt)
    if reply:
        return reply
    reply = _ask_ollama(system, agent_name, context_text, user_text)
    if reply:
        return reply
    return context_text


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
