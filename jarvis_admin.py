# ================================================================
#   J.A.R.V.I.S — JarvisAdmin approval gate
#
#   A dedicated, separate Telegram bot whose only job is to ask permission
#   before Jarvis runs a sudo-like/high-consequence action -- right now,
#   system_power's shutdown/restart -- and wait for an explicit yes/no
#   reply. It is deliberately NOT a general two-way command channel like
#   telegram_bridge.py: it never accepts commands, never runs anything
#   itself, and grants no capability beyond answering "should this one
#   pending request go ahead". A leaked token here can, at worst, be used
#   to approve or deny a request it's told about while it's pending --
#   never to control Jarvis directly, see other traffic, or reach any
#   other tool.
#
#   Deliberately synchronous, unlike the other sub-agents' poll_thread()
#   background loops: request_approval() sends the prompt and polls this
#   bot's own getUpdates for a reply itself, blocking the calling tool for
#   up to the timeout. That means whatever command the user sent Jarvis to
#   trigger the gated action sits waiting (up to the timeout) until it's
#   answered on JarvisAdmin -- intentional, since the whole point is a
#   human in the loop before the action runs.
#
#   Fails CLOSED: if this bot isn't configured (JARVIS_ADMIN_BOT_TOKEN/
#   JARVIS_ADMIN_CHAT_ID missing), request_approval() always returns
#   (False, "unavailable") immediately -- a gated action must never run
#   just because there was nowhere to ask, and never falls back to asking
#   on the main Jarvis bot instead (that would defeat having a separate,
#   dedicated approval channel).
#
#   Fully inert until JARVIS_ADMIN_BOT_TOKEN is set in .env; reuses
#   TELEGRAM_CHAT_ID (same user, same phone) unless JARVIS_ADMIN_CHAT_ID
#   overrides it. See README's "Shut down / restart the PC" section.
#
#   The phone dashboard can also decide a pending request (an Approve/Deny
#   card instead of a free-text chat box -- see README's "Phone dashboard"
#   section for why this bot doesn't get a chat box like the others).
#   Pending/decision state is a FILE (~/.jarvis/admin_pending.json /
#   admin_decision.json), not an in-memory variable: request_approval()
#   itself usually runs inside a short-lived per-command MCP subprocess
#   (e.g. when "Jarvis, shut down" triggers tools.system_power), a
#   different OS process than the dashboard's own long-lived one -- an
#   in-memory flag set in one process would simply never be seen by the
#   other, the same cross-process reasoning employees.py's job queue
#   already has to account for.
# ================================================================
import json
import os
import re
import time
import uuid

import requests

import telegram_common

BOT_TOKEN = os.environ.get("JARVIS_ADMIN_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("JARVIS_ADMIN_CHAT_ID", "").strip() or os.environ.get("TELEGRAM_CHAT_ID", "").strip()
API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}" if BOT_TOKEN else ""
DEFAULT_TIMEOUT_SECONDS = int(os.environ.get("JARVIS_ADMIN_APPROVAL_TIMEOUT_SECONDS", "300") or "300")

JARVIS_ADMIN_AVAILABLE = bool(BOT_TOKEN and CHAT_ID)

_AGENT_NAME = "JarvisAdmin"
# Deliberately exact-match (not "contains") so a longer reply like "yes but
# wait" or "no idea what that means" is never mistaken for a decision --
# only an unambiguous, standalone yes/no counts.
_APPROVE_RE = re.compile(r"^(y|yes|approve|approved|ok|okay|go ahead|do it)[.!]?$", re.IGNORECASE)
_DENY_RE = re.compile(r"^(n|no|deny|denied|cancel|stop|don'?t)[.!]?$", re.IGNORECASE)

_JARVIS_DIR = os.path.join(os.path.expanduser("~"), ".jarvis")
_PENDING_PATH = os.path.join(_JARVIS_DIR, "admin_pending.json")
_DECISION_PATH = os.path.join(_JARVIS_DIR, "admin_decision.json")


def _write_json(path, data):
    os.makedirs(_JARVIS_DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def get_pending():
    """Read-only: the currently pending approval request, if any -- e.g.
    {"id", "description", "created_at"}. Used by the phone dashboard to
    show a live Approve/Deny card; polled rather than pushed since it has
    to work across the process boundary described in the module
    docstring."""
    return _read_json(_PENDING_PATH)


def decide_from_dashboard(request_id: str, approved: bool) -> bool:
    """Called by the phone dashboard's Approve/Deny buttons. Returns False
    (rather than raising) if there's no matching pending request -- e.g. it
    already resolved on Telegram or timed out in the moment between the
    dashboard loading it and the user tapping a button."""
    pending = get_pending()
    if not pending or pending.get("id") != request_id:
        return False
    _write_json(_DECISION_PATH, {"id": request_id, "approved": approved})
    return True


def _sync_offset():
    """Drain any backlog on this bot without acting on it, and return the
    offset for the next getUpdates call -- so a stale message sent before
    this request (e.g. a leftover "no" from a previous, already-resolved
    approval) can never be mistaken for a reply to THIS one."""
    try:
        r = requests.get(f"{API_BASE}/getUpdates", params={"timeout": 0}, timeout=15)
        updates = r.json().get("result", [])
        if updates:
            return updates[-1]["update_id"] + 1
    except Exception:
        pass
    return None


def request_approval(description: str, timeout_seconds: int = None):
    """Ask a human for explicit yes/no approval -- over the JarvisAdmin
    Telegram bot, or from the phone dashboard's Approve/Deny card, whichever
    answers first -- before a gated action runs, and block until they
    answer or the timeout elapses. `description` should read naturally
    after "Jarvis wants to ", e.g. "restart this PC in 1 minute". Returns
    (approved: bool, status) where status is one of "approved", "denied",
    "timeout", "unavailable"."""
    if not JARVIS_ADMIN_AVAILABLE:
        return False, "unavailable"

    timeout_seconds = DEFAULT_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
    minutes = max(1, round(timeout_seconds / 60))
    prompt = (f"\U0001F512 Approval needed: Jarvis wants to {description}.\n"
              f"Reply YES to approve or NO to deny -- I'll wait {minutes} minute(s).")
    offset = _sync_offset()
    if not telegram_common.send(BOT_TOKEN, CHAT_ID, prompt, agent=_AGENT_NAME):
        return False, "unavailable"

    request_id = uuid.uuid4().hex[:12]
    for path in (_PENDING_PATH, _DECISION_PATH):
        try:
            os.remove(path)  # clear any stale leftover from a previous request
        except OSError:
            pass
    _write_json(_PENDING_PATH, {"id": request_id, "description": description, "created_at": time.time()})

    try:
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            decision = _read_json(_DECISION_PATH)
            if decision and decision.get("id") == request_id:
                approved = bool(decision.get("approved"))
                telegram_common.send(
                    BOT_TOKEN, CHAT_ID,
                    "Approved from the dashboard -- running it now." if approved else
                    "Denied from the dashboard -- not running that.",
                    agent=_AGENT_NAME)
                return approved, ("approved" if approved else "denied")

            remaining = deadline - time.time()
            # Shortened from a plain 25s cap so a dashboard decision (checked
            # once per loop iteration, above) is noticed within a few
            # seconds rather than up to 25 -- Telegram's own long-poll still
            # covers up to this same window each call.
            poll_timeout = min(3, max(1, int(remaining)))
            try:
                params = {"timeout": poll_timeout}
                if offset is not None:
                    params["offset"] = offset
                r = requests.get(f"{API_BASE}/getUpdates", params=params, timeout=poll_timeout + 10)
                updates = r.json().get("result", [])
                for update in updates:
                    offset = update["update_id"] + 1
                    message = update.get("message") or {}
                    if str(message.get("chat", {}).get("id", "")) != CHAT_ID:
                        continue
                    text = (message.get("text") or "").strip()
                    if not text:
                        continue
                    if _APPROVE_RE.match(text):
                        telegram_common.send(BOT_TOKEN, CHAT_ID, "Approved -- running it now.", agent=_AGENT_NAME)
                        return True, "approved"
                    if _DENY_RE.match(text):
                        telegram_common.send(BOT_TOKEN, CHAT_ID, "Denied -- not running that.", agent=_AGENT_NAME)
                        return False, "denied"
                    # Anything else (a question, a stray message) is ignored --
                    # only an unambiguous yes/no resolves the request.
            except requests.Timeout:
                continue
            except Exception as e:
                print(f"  [JarvisAdmin] Poll error: {e}")
                time.sleep(2)

        telegram_common.send(BOT_TOKEN, CHAT_ID, "No response in time -- treating that as denied, for safety.", agent=_AGENT_NAME)
        return False, "timeout"
    finally:
        for path in (_PENDING_PATH, _DECISION_PATH):
            try:
                os.remove(path)
            except OSError:
                pass
