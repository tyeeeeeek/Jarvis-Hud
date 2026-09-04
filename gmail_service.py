# ================================================================
#   J.A.R.V.I.S — Gmail + Google Calendar service
#
#   One shared Google OAuth identity (readonly mail, compose-only mail,
#   and calendar events) used by email_watcher.py (background "important
#   mail" polling) and tools.py (draft_email/create_calendar_event). Kept
#   as its own module, separate from any Gmail connection this chat
#   session uses, so Jarvis can keep watching mail and creating drafts/
#   events even when no Claude Code session is open.
#
#   Scopes are deliberately the minimum needed for what Jarvis actually
#   does, and NEVER include gmail.send or any full-mailbox-modify scope:
#     - gmail.readonly    -- read subject/snippet/body for triage & alerts
#     - gmail.compose     -- create/edit DRAFTS only; this scope cannot
#                            send mail, delete mail, or touch anything
#                            outside drafts it created
#     - calendar.events   -- create/read/update/delete events only, not
#                            calendar settings or other calendars' ACLs
#   See tools.draft_email's docstring for the "never send automatically"
#   hard constraint this scope choice backs up structurally, not just by
#   convention -- even a compromised token literally cannot call the send
#   endpoint.
#
#   First run opens a real browser window for the standard Google OAuth
#   consent screen, then caches the result in token.json (gitignored via
#   the existing `token*.json` pattern) so it never asks again. See
#   README's "Connect email + calendar" section for the one-time Google
#   Cloud Console setup (enable Gmail API + Calendar API, OAuth consent
#   screen, Desktop app credentials.json).
# ================================================================
import base64
import os
from email.mime.text import MIMEText

HOME = os.path.expanduser("~")

CREDENTIALS_PATH = os.environ.get("GMAIL_CREDENTIALS_PATH", os.path.join(os.path.dirname(__file__), "credentials.json"))
TOKEN_PATH = os.environ.get("GMAIL_TOKEN_PATH", os.path.join(os.path.dirname(__file__), "token.json"))

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/calendar.events",
]

try:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    GMAIL_LIB_AVAILABLE = True
except ImportError:
    GMAIL_LIB_AVAILABLE = False

GMAIL_AVAILABLE = bool(GMAIL_LIB_AVAILABLE and os.path.exists(CREDENTIALS_PATH))

_gmail_service = None
_calendar_service = None


def _get_credentials():
    creds = None
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    if not creds or not creds.valid or not set(SCOPES).issubset(set(creds.scopes or [])):
        if creds and creds.expired and creds.refresh_token and set(SCOPES).issubset(set(creds.scopes or [])):
            creds.refresh(Request())
        else:
            # Either first-run, or the cached token predates one of these
            # scopes (e.g. it was granted back when only gmail.readonly was
            # requested) -- either way, a fresh consent covering every scope
            # above is the only correct fix, never a silent partial-scope
            # fallback that would make draft/calendar calls fail mysteriously.
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_PATH, "w", encoding="utf-8") as f:
            f.write(creds.to_json())
    return creds


def _gmail():
    global _gmail_service
    if _gmail_service is None:
        _gmail_service = build("gmail", "v1", credentials=_get_credentials())
    return _gmail_service


def _calendar():
    global _calendar_service
    if _calendar_service is None:
        _calendar_service = build("calendar", "v3", credentials=_get_credentials())
    return _calendar_service


def list_recent_messages(query: str = "newer_than:2d", max_results: int = 15):
    """Return [{"id", "subject", "from", "snippet"}, ...] for recent inbox
    messages matching `query` (Gmail search syntax). Metadata + snippet
    only -- never the full body -- keeps triage cheap and avoids pulling
    full message content for mail that turns out not to matter."""
    resp = _gmail().users().messages().list(userId="me", q=query, maxResults=max_results).execute()
    out = []
    for m in resp.get("messages", []):
        msg = _gmail().users().messages().get(
            userId="me", id=m["id"], format="metadata",
            metadataHeaders=["Subject", "From"],
        ).execute()
        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        out.append({
            "id": m["id"],
            "subject": headers.get("Subject", "(no subject)"),
            "from": headers.get("From", ""),
            "snippet": msg.get("snippet", ""),
        })
    return out


def _html_to_text(html: str) -> str:
    """Dependency-free HTML->text for the (common) case where a message has
    no plain-text part -- strips tags/scripts/styles and decodes the small
    set of entities real marketing/notification email actually uses,
    rather than pulling in a full HTML parser for this one narrow need."""
    import re as _re
    import html as _htmllib
    text = _re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    text = _re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>", "\n", text)
    text = _re.sub(r"(?s)<[^>]+>", "", text)
    text = _htmllib.unescape(text)
    return _re.sub(r"[ \t]+", " ", _re.sub(r"\n{3,}", "\n\n", text)).strip()


def read_message_body(message_id: str) -> dict:
    """Return {"ok": True, "subject", "from", "date", "body"} with the full
    plain-text body of one message (not just the snippet list_recent_
    messages gives) -- for when a search result needs reading in full.
    Falls back to the HTML part decoded to text if no plain-text part
    exists. Returns {"ok": False, "error"} on failure (e.g. bad id)."""
    import base64 as _b64
    try:
        msg = _gmail().users().messages().get(userId="me", id=message_id, format="full").execute()
        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}

        def _walk(part, mime_type):
            if part.get("mimeType") == mime_type and part.get("body", {}).get("data"):
                return _b64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="replace")
            for sub in part.get("parts", []) or []:
                found = _walk(sub, mime_type)
                if found:
                    return found
            return None

        payload = msg.get("payload", {})
        body_text = _walk(payload, "text/plain")
        if not body_text:
            html_body = _walk(payload, "text/html")
            body_text = _html_to_text(html_body) if html_body else msg.get("snippet", "")
        return {
            "ok": True,
            "subject": headers.get("Subject", "(no subject)"),
            "from": headers.get("From", ""),
            "date": headers.get("Date", ""),
            "body": body_text[:4000],
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def list_upcoming_events(days_ahead: int = 7) -> list:
    """Return [{"id","summary","start","end","location"}, ...] for events
    on the primary calendar starting between now and `days_ahead` days from
    now, soonest first."""
    import datetime as _dt
    now = _dt.datetime.utcnow().isoformat() + "Z"
    until = (_dt.datetime.utcnow() + _dt.timedelta(days=days_ahead)).isoformat() + "Z"
    resp = _calendar().events().list(
        calendarId="primary", timeMin=now, timeMax=until,
        singleEvents=True, orderBy="startTime", maxResults=50,
    ).execute()
    out = []
    for e in resp.get("items", []):
        start = e.get("start", {})
        end = e.get("end", {})
        out.append({
            "id": e.get("id"),
            "summary": e.get("summary", "(untitled)"),
            "start": start.get("dateTime") or start.get("date", ""),
            "end": end.get("dateTime") or end.get("date", ""),
            "location": e.get("location", ""),
        })
    return out


def update_calendar_event(event_id: str, summary: str = "", start_iso: str = "",
                           end_iso: str = "", description: str = "") -> dict:
    """Patch an existing primary-calendar event -- only the fields passed
    (non-empty) are changed, everything else on the event is left as-is.
    Returns {"ok": True, "event_id"} or {"ok": False, "error"}."""
    try:
        body = {}
        if summary:
            body["summary"] = summary
        if start_iso:
            body["start"] = {"dateTime": start_iso}
        if end_iso:
            body["end"] = {"dateTime": end_iso}
        if description:
            body["description"] = description
        if not body:
            return {"ok": False, "error": "nothing to change"}
        updated = _calendar().events().patch(calendarId="primary", eventId=event_id, body=body).execute()
        return {"ok": True, "event_id": updated.get("id")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def delete_calendar_event(event_id: str) -> dict:
    """Delete an event from the primary calendar. Returns {"ok": True} or
    {"ok": False, "error"}."""
    try:
        _calendar().events().delete(calendarId="primary", eventId=event_id).execute()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def create_draft(to: str, subject: str, body: str, cc: str = "") -> dict:
    """Create a real Gmail draft (visible in the Drafts folder, editable/
    sendable by the user from there) via the gmail.compose scope -- this
    scope cannot send mail, so there is no code path here that could ever
    send one automatically. Returns {"ok": True, "draft_id"} or
    {"ok": False, "error"}."""
    try:
        message = MIMEText(body or "")
        if to:
            message["to"] = to
        if cc:
            message["cc"] = cc
        message["subject"] = subject or ""
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        draft = _gmail().users().drafts().create(
            userId="me", body={"message": {"raw": raw}},
        ).execute()
        return {"ok": True, "draft_id": draft.get("id")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def create_calendar_event(summary: str, start_iso: str, end_iso: str,
                           description: str = "", attendees: str = "",
                           timezone: str = "") -> dict:
    """Create a real Google Calendar event on the primary calendar.
    start_iso/end_iso must be ISO 8601 datetimes (e.g.
    "2026-08-29T15:00:00"). `attendees` is a comma-separated list of email
    addresses (optional). Returns {"ok": True, "event_id", "html_link"} or
    {"ok": False, "error"}."""
    try:
        event = {
            "summary": summary or "(untitled event)",
            "description": description or "",
            "start": {"dateTime": start_iso, **({"timeZone": timezone} if timezone else {})},
            "end": {"dateTime": end_iso, **({"timeZone": timezone} if timezone else {})},
        }
        emails = [a.strip() for a in (attendees or "").split(",") if a.strip()]
        if emails:
            event["attendees"] = [{"email": e} for e in emails]
        created = _calendar().events().insert(calendarId="primary", body=event).execute()
        return {"ok": True, "event_id": created.get("id"), "html_link": created.get("htmlLink")}
    except Exception as e:
        return {"ok": False, "error": str(e)}
