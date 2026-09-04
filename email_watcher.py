# ================================================================
#   J.A.R.V.I.S — Email watcher (Gmail + Outlook)
#
#   Polls whichever of Gmail (gmail_service.py) / Outlook
#   (outlook_service.py) is configured for new mail and flags anything
#   that looks like a bill due, a job offer, a delivery/order update, or
#   a task-finished notification -- then hands a short spoken/text
#   summary back to jarvis.py to announce (voice + HUD + Telegram).
#   Either, both, or neither provider can be configured; each is fully
#   independent, so setting up one doesn't require the other.
#
#   Classification prefers the real Claude CLI (same locked-down, no-
#   tools chat-only call telegram_common.ask_grounded uses) so triage
#   reads like a real judgment call instead of a rigid keyword match,
#   and falls back to a local Ollama model if the CLI is unavailable --
#   this polls every couple of minutes across two mailboxes, so a free/
#   local fallback matters for cost, but quality shouldn't have to
#   suffer when the better option is right there.
# ================================================================
import json
import os
import subprocess
import shutil
import time
from datetime import datetime, timedelta, timezone

import requests

try:
    import gmail_service
    GMAIL_LIB_AVAILABLE = True
except ImportError:
    GMAIL_LIB_AVAILABLE = False

try:
    import outlook_service
    OUTLOOK_LIB_AVAILABLE = True
except ImportError:
    OUTLOOK_LIB_AVAILABLE = False

HOME = os.path.expanduser("~")
JARVIS_DIR = os.path.join(HOME, ".jarvis")
SEEN_PATH = os.path.join(JARVIS_DIR, "email_seen.json")

OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "llama3.2"
_CLAUDE_CLI = shutil.which("claude")

GMAIL_WATCH_AVAILABLE = bool(GMAIL_LIB_AVAILABLE and gmail_service.GMAIL_AVAILABLE)
OUTLOOK_WATCH_AVAILABLE = bool(OUTLOOK_LIB_AVAILABLE and outlook_service.OUTLOOK_AVAILABLE)
EMAIL_WATCH_AVAILABLE = GMAIL_WATCH_AVAILABLE or OUTLOOK_WATCH_AVAILABLE


def _ensure_dir():
    os.makedirs(JARVIS_DIR, exist_ok=True)


def _load_seen():
    try:
        with open(SEEN_PATH, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def _save_seen(seen):
    _ensure_dir()
    trimmed = list(seen)[-4000:]  # doubled vs. single-provider days, since ids from two mailboxes share this file
    tmp = SEEN_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(trimmed, f)
    os.replace(tmp, SEEN_PATH)


_CLASSIFY_SYSTEM = (
    "You triage email for a personal assistant. Given a subject and a short snippet, decide "
    "if this is personally important: a bill or payment due, a job offer or interview request, "
    "an order/package delivery update, or a notification that some task/build/job finished. "
    "Ignore newsletters, marketing, social notifications, and anything not in those categories. "
    "Reply with EXACTLY one line: either the word NONE, or CATEGORY | a one-sentence spoken-style "
    "summary (no markdown). CATEGORY must be one of: bill, job, delivery, task, other."
)


def _classify_claude(subject, snippet):
    if not _CLAUDE_CLI:
        return None
    prompt = f"Subject: {subject[:200]}\nSnippet: {snippet[:400]}\n"
    try:
        r = subprocess.run(
            [_CLAUDE_CLI, "-p", prompt, "--tools", "", "--system-prompt", _CLASSIFY_SYSTEM],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            return None
        return (r.stdout or "").strip()
    except Exception as e:
        print(f"  [EmailWatch] Claude CLI classify error: {e}")
        return None


def _classify_ollama(subject, snippet):
    try:
        r = requests.post(OLLAMA_URL, json={
            "model": OLLAMA_MODEL,
            "prompt": f"{_CLASSIFY_SYSTEM}\n\nSubject: {subject[:200]}\nSnippet: {snippet[:400]}\n",
            "stream": False,
        }, timeout=20)
        if r.status_code != 200:
            return None
        return r.json().get("response", "").strip()
    except Exception as e:
        print(f"  [EmailWatch] Ollama classify error: {e}")
        return None


def _classify(subject, snippet):
    """Returns (category, summary) or None. Tries the real Claude CLI
    first, falls back to local Ollama, matching telegram_common.
    ask_grounded's upgrade path -- see module docstring."""
    text = _classify_claude(subject, snippet) or _classify_ollama(subject, snippet)
    if not text or text.upper().startswith("NONE"):
        return None
    if "|" not in text:
        return None
    category, summary = text.split("|", 1)
    return category.strip().lower(), summary.strip()


def _poll_gmail(seen, on_important):
    try:
        for m in gmail_service.list_recent_messages("newer_than:2d", 15):
            key = f"gmail:{m['id']}"
            if key in seen:
                continue
            seen.add(key)
            result = _classify(m["subject"], m["snippet"])
            if result:
                category, summary = result
                on_important("Gmail", category, summary)
    except Exception as e:
        print(f"  [EmailWatch] Gmail poll error: {e}")


def _poll_outlook(seen, on_important):
    try:
        since_iso = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        for m in outlook_service.list_recent_messages(since_iso, 15):
            key = f"outlook:{m['id']}"
            if key in seen:
                continue
            seen.add(key)
            result = _classify(m["subject"], m["snippet"])
            if result:
                category, summary = result
                on_important("Outlook", category, summary)
    except Exception as e:
        print(f"  [EmailWatch] Outlook poll error: {e}")


def poll_thread(on_important, pipeline_stop, poll_secs=120):
    """Background loop: checks whichever of Gmail/Outlook is configured
    for new mail and calls on_important(provider, category, summary) for
    anything that looks important. A provider that isn't set up (no
    credentials.json / no OUTLOOK_CLIENT_ID+device-code login yet) is
    silently skipped, not treated as an error -- see README."""
    if not EMAIL_WATCH_AVAILABLE:
        print("  [EmailWatch] Neither Gmail nor Outlook is configured -- email watching is disabled. See README.")
        return

    which = ", ".join(p for p, ok in (("Gmail", GMAIL_WATCH_AVAILABLE), ("Outlook", OUTLOOK_WATCH_AVAILABLE)) if ok)
    print(f"  [EmailWatch] Watching for important mail ({which}).")

    seen = _load_seen()
    while not pipeline_stop.is_set():
        if GMAIL_WATCH_AVAILABLE:
            _poll_gmail(seen, on_important)
        if OUTLOOK_WATCH_AVAILABLE:
            _poll_outlook(seen, on_important)
        _save_seen(seen)
        time.sleep(poll_secs)
