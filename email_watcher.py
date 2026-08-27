# ================================================================
#   J.A.R.V.I.S — Email watcher
#
#   Polls Gmail for new mail and flags anything that looks like a
#   bill due, a job offer, a delivery/order update, or a
#   task-finished notification -- then hands a short spoken summary
#   back to jarvis.py to announce (voice + HUD + optionally SMS).
#
#   Uses its OWN Gmail OAuth credentials, separate from any Gmail
#   connection this chat session has -- Jarvis needs to keep
#   watching mail even when no Claude Code session is open. See
#   README's "Watch my email" setup section for how to get
#   credentials.json from Google Cloud Console (one-time, free).
#
#   Classification runs on local Ollama, not Claude -- this polls
#   frequently and cheaply, so it uses the free/local model for
#   triage rather than spending on every single email.
# ================================================================
import os
import json
import time

import requests

HOME = os.path.expanduser("~")
JARVIS_DIR = os.path.join(HOME, ".jarvis")
SEEN_PATH = os.path.join(JARVIS_DIR, "email_seen.json")

CREDENTIALS_PATH = os.environ.get("GMAIL_CREDENTIALS_PATH", os.path.join(os.path.dirname(__file__), "credentials.json"))
TOKEN_PATH = os.environ.get("GMAIL_TOKEN_PATH", os.path.join(os.path.dirname(__file__), "token.json"))
OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "llama3.2"

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

try:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    GMAIL_LIB_AVAILABLE = True
except ImportError:
    GMAIL_LIB_AVAILABLE = False

EMAIL_WATCH_AVAILABLE = bool(GMAIL_LIB_AVAILABLE and os.path.exists(CREDENTIALS_PATH))


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
    # Cap it so this file doesn't grow forever.
    trimmed = list(seen)[-2000:]
    tmp = SEEN_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(trimmed, f)
    os.replace(tmp, SEEN_PATH)


def _get_service():
    creds = None
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            # First-run only: opens a browser for the standard Google OAuth
            # consent screen, then caches token.json so this never happens
            # again.
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
            creds = flow.run_local_server(port=0)
        _ensure_dir()
        with open(TOKEN_PATH, "w", encoding="utf-8") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


_CLASSIFY_PROMPT = (
    "You triage email for a personal assistant. Given the subject and a short "
    "snippet, decide if this is personally important: a bill or payment due, "
    "a job offer or interview request, an order/package delivery update, or a "
    "notification that some task/build/job finished. Ignore newsletters, "
    "marketing, social notifications, and anything not in those categories.\n\n"
    "Reply with EXACTLY one line: either the word NONE, or "
    "CATEGORY | a one-sentence spoken-style summary (no markdown).\n"
    "CATEGORY must be one of: bill, job, delivery, task, other.\n\n"
    "Subject: {subject}\nSnippet: {snippet}\n"
)


def _classify(subject, snippet):
    try:
        r = requests.post(OLLAMA_URL, json={
            "model": OLLAMA_MODEL,
            "prompt": _CLASSIFY_PROMPT.format(subject=subject[:200], snippet=snippet[:400]),
            "stream": False,
        }, timeout=20)
        if r.status_code != 200:
            return None
        text = r.json().get("response", "").strip()
        if not text or text.upper().startswith("NONE"):
            return None
        if "|" not in text:
            return None
        category, summary = text.split("|", 1)
        return category.strip().lower(), summary.strip()
    except Exception as e:
        print(f"  [EmailWatch] Classify error: {e}")
        return None


def poll_thread(on_important, pipeline_stop, poll_secs=120):
    """Background loop: checks Gmail for new mail and calls
    on_important(category, summary) for anything that looks important."""
    if not EMAIL_WATCH_AVAILABLE:
        print("  [EmailWatch] Not configured -- email watching is disabled. See README.")
        return

    try:
        service = _get_service()
    except Exception as e:
        print(f"  [EmailWatch] Auth error: {e}")
        return

    seen = _load_seen()
    print("  [EmailWatch] Watching for important mail.")

    while not pipeline_stop.is_set():
        try:
            resp = service.users().messages().list(
                userId="me", q="newer_than:2d", maxResults=15,
            ).execute()
            for m in resp.get("messages", []):
                mid = m["id"]
                if mid in seen:
                    continue
                seen.add(mid)

                msg = service.users().messages().get(
                    userId="me", id=mid, format="metadata",
                    metadataHeaders=["Subject", "From"],
                ).execute()
                headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
                subject = headers.get("Subject", "(no subject)")
                snippet = msg.get("snippet", "")

                result = _classify(subject, snippet)
                if result:
                    category, summary = result
                    try:
                        on_important(category, summary)
                    except Exception as e:
                        print(f"  [EmailWatch] Notify error: {e}")

            _save_seen(seen)
        except Exception as e:
            print(f"  [EmailWatch] Poll error: {e}")

        time.sleep(poll_secs)
