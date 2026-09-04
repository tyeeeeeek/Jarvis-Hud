# ================================================================
#   J.A.R.V.I.S — Outlook (Microsoft Graph) mail + calendar service
#
#   Mirrors gmail_service.py's shape and safety model, for a personal or
#   work Microsoft account instead of Google. Uses MSAL's device-code
#   flow (no client secret, no local web server/port needed -- the app is
#   registered as a "public client") so it works the same whether Jarvis
#   is headless or has a display: the one-time setup prints a URL and a
#   short code, you enter them on any device (phone included), and the
#   result is cached in token_outlook.json (gitignored via the existing
#   `token*.json` pattern) so it never asks again until the refresh token
#   itself expires.
#
#   Scopes are deliberately the minimum needed and NEVER include
#   Mail.Send:
#     - Mail.ReadWrite     -- read mail for triage/alerts, and create
#                             DRAFTS (Graph creates a message as a draft
#                             by default; sending is a *separate* action,
#                             POST .../send, that this module never calls)
#     - Calendars.ReadWrite -- create/read/update/delete events only
#     - offline_access      -- refresh token, so the one-time device-code
#                              login doesn't need repeating
#   See tools.draft_email's docstring for the "never send automatically"
#   hard constraint this scope choice backs up structurally.
#
#   One-time setup (see README's "Connect email + calendar" section):
#   register a free Azure AD app (App registrations -> New registration,
#   "Accounts in any organizational directory and personal Microsoft
#   accounts", enable "Allow public client flows"), put its Application
#   (client) ID in OUTLOOK_CLIENT_ID in .env, then run
#   `python3 -c "import outlook_service; outlook_service.ensure_authenticated()"`
#   once from an interactive terminal to complete the device-code login.
# ================================================================
import os

import requests

HOME = os.path.expanduser("~")

CLIENT_ID = os.environ.get("OUTLOOK_CLIENT_ID", "").strip()
TENANT = os.environ.get("OUTLOOK_TENANT_ID", "common").strip() or "common"
TOKEN_PATH = os.environ.get("OUTLOOK_TOKEN_PATH", os.path.join(os.path.dirname(__file__), "token_outlook.json"))

AUTHORITY = f"https://login.microsoftonline.com/{TENANT}"
SCOPES = ["Mail.ReadWrite", "Calendars.ReadWrite"]  # offline_access is implicit/always granted by MSAL

GRAPH_BASE = "https://graph.microsoft.com/v1.0"

try:
    import msal
    MSAL_AVAILABLE = True
except ImportError:
    MSAL_AVAILABLE = False

OUTLOOK_AVAILABLE = bool(MSAL_AVAILABLE and CLIENT_ID)

_cache = None
_app = None


def _load_cache():
    global _cache
    if _cache is None:
        _cache = msal.SerializableTokenCache()
        if os.path.exists(TOKEN_PATH):
            try:
                with open(TOKEN_PATH, "r", encoding="utf-8") as f:
                    _cache.deserialize(f.read())
            except Exception:
                pass
    return _cache


def _save_cache():
    if _cache is not None and _cache.has_state_changed:
        with open(TOKEN_PATH, "w", encoding="utf-8") as f:
            f.write(_cache.serialize())


def _client():
    global _app
    if _app is None:
        _app = msal.PublicClientApplication(CLIENT_ID, authority=AUTHORITY, token_cache=_load_cache())
    return _app


def _get_token(interactive: bool = False):
    """Returns a valid access token, or None if not authenticated and
    `interactive` is False -- callers on a background thread (the mail
    watcher) must never block waiting on a device code nobody will see;
    only ensure_authenticated() (the one-time manual setup step) passes
    interactive=True."""
    if not OUTLOOK_AVAILABLE:
        return None
    app = _client()
    accounts = app.get_accounts()
    result = app.acquire_token_silent(SCOPES, account=accounts[0]) if accounts else None
    if not result:
        if not interactive:
            return None
        flow = app.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            raise RuntimeError(f"Couldn't start device login: {flow.get('error_description', flow)}")
        print(flow["message"])
        result = app.acquire_token_by_device_flow(flow)  # blocks until the user finishes or it times out
    _save_cache()
    if result and "access_token" in result:
        return result["access_token"]
    return None


def ensure_authenticated() -> bool:
    """One-time interactive setup: prints the device-login URL/code and
    blocks until you complete it (or it times out), then caches the
    result so nothing needs this again until the refresh token itself
    expires. Run manually from an interactive terminal -- never called
    from the background watcher thread."""
    token = _get_token(interactive=True)
    return bool(token)


def _headers(interactive=False):
    token = _get_token(interactive=interactive)
    if not token:
        raise RuntimeError("Outlook isn't connected yet -- run the one-time device-code setup (see README).")
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def list_recent_messages(since_iso: str, max_results: int = 15):
    """Return [{"id", "subject", "from", "snippet"}, ...] for inbox
    messages received at/after `since_iso` (ISO 8601 UTC datetime),
    newest first."""
    params = {
        "$filter": f"receivedDateTime ge {since_iso}",
        "$orderby": "receivedDateTime desc",
        "$top": max_results,
        "$select": "id,subject,from,bodyPreview",
    }
    r = requests.get(f"{GRAPH_BASE}/me/mailFolders/inbox/messages",
                      headers=_headers(), params=params, timeout=20)
    r.raise_for_status()
    out = []
    for m in r.json().get("value", []):
        sender = ((m.get("from") or {}).get("emailAddress") or {}).get("address", "")
        out.append({
            "id": m["id"],
            "subject": m.get("subject") or "(no subject)",
            "from": sender,
            "snippet": (m.get("bodyPreview") or "")[:300],
        })
    return out


def read_message_body(message_id: str) -> dict:
    """Return {"ok": True, "subject", "from", "date", "body"} with the full
    plain-text body of one message (not just the snippet
    list_recent_messages gives). Returns {"ok": False, "error"} on failure
    (e.g. bad id)."""
    try:
        params = {"$select": "subject,from,receivedDateTime,body"}
        # Prefer plain text over Graph's default HTML body -- avoids needing
        # an HTML parser here for what's otherwise just marketing-email tag
        # soup; Graph does this conversion server-side when asked.
        headers = {**_headers(), "Prefer": 'outlook.body-content-type="text"'}
        r = requests.get(f"{GRAPH_BASE}/me/messages/{message_id}", headers=headers, params=params, timeout=20)
        r.raise_for_status()
        m = r.json()
        sender = ((m.get("from") or {}).get("emailAddress") or {}).get("address", "")
        body_content = (m.get("body") or {}).get("content", "")
        return {
            "ok": True,
            "subject": m.get("subject") or "(no subject)",
            "from": sender,
            "date": m.get("receivedDateTime", ""),
            "body": body_content[:4000],
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def list_upcoming_events(days_ahead: int = 7) -> list:
    """Return [{"id","summary","start","end","location"}, ...] for events
    starting between now and `days_ahead` days from now, soonest first."""
    import datetime as _dt
    now = _dt.datetime.utcnow().isoformat() + "Z"
    until = (_dt.datetime.utcnow() + _dt.timedelta(days=days_ahead)).isoformat() + "Z"
    params = {
        "startDateTime": now, "endDateTime": until,
        "$orderby": "start/dateTime",
        "$select": "id,subject,start,end,location",
        "$top": 50,
    }
    r = requests.get(f"{GRAPH_BASE}/me/calendarView", headers=_headers(), params=params, timeout=20)
    r.raise_for_status()
    out = []
    for e in r.json().get("value", []):
        out.append({
            "id": e.get("id"),
            "summary": e.get("subject", "(untitled)"),
            "start": (e.get("start") or {}).get("dateTime", ""),
            "end": (e.get("end") or {}).get("dateTime", ""),
            "location": (e.get("location") or {}).get("displayName", ""),
        })
    return out


def update_calendar_event(event_id: str, summary: str = "", start_iso: str = "",
                           end_iso: str = "", description: str = "",
                           timezone: str = "UTC") -> dict:
    """Patch an existing event -- only the fields passed (non-empty) are
    changed. Returns {"ok": True, "event_id"} or {"ok": False, "error"}."""
    try:
        payload = {}
        if summary:
            payload["subject"] = summary
        if start_iso:
            payload["start"] = {"dateTime": start_iso, "timeZone": timezone}
        if end_iso:
            payload["end"] = {"dateTime": end_iso, "timeZone": timezone}
        if description:
            payload["body"] = {"contentType": "Text", "content": description}
        if not payload:
            return {"ok": False, "error": "nothing to change"}
        r = requests.patch(f"{GRAPH_BASE}/me/events/{event_id}", headers=_headers(), json=payload, timeout=20)
        r.raise_for_status()
        return {"ok": True, "event_id": r.json().get("id")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def delete_calendar_event(event_id: str) -> dict:
    """Delete an event. Returns {"ok": True} or {"ok": False, "error"}."""
    try:
        r = requests.delete(f"{GRAPH_BASE}/me/events/{event_id}", headers=_headers(), timeout=20)
        r.raise_for_status()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def create_draft(to: str, subject: str, body: str, cc: str = "") -> dict:
    """Create a real Outlook draft (visible in the Drafts folder, editable/
    sendable by the user from there). POSTing to /me/messages creates the
    message as a draft by default -- this module never calls the separate
    .../send action, so there is no code path here that could send one
    automatically. Returns {"ok": True, "message_id"} or
    {"ok": False, "error"}."""
    try:
        payload = {
            "subject": subject or "",
            "body": {"contentType": "Text", "content": body or ""},
        }
        if to:
            payload["toRecipients"] = [{"emailAddress": {"address": a.strip()}} for a in to.split(",") if a.strip()]
        if cc:
            payload["ccRecipients"] = [{"emailAddress": {"address": a.strip()}} for a in cc.split(",") if a.strip()]
        r = requests.post(f"{GRAPH_BASE}/me/messages", headers=_headers(), json=payload, timeout=20)
        r.raise_for_status()
        return {"ok": True, "message_id": r.json().get("id")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def create_calendar_event(subject: str, start_iso: str, end_iso: str,
                           description: str = "", attendees: str = "",
                           timezone: str = "UTC") -> dict:
    """Create a real Outlook Calendar event. start_iso/end_iso must be ISO
    8601 datetimes (e.g. "2026-08-29T15:00:00"). `attendees` is a
    comma-separated list of email addresses (optional). Returns
    {"ok": True, "event_id", "web_link"} or {"ok": False, "error"}."""
    try:
        payload = {
            "subject": subject or "(untitled event)",
            "body": {"contentType": "Text", "content": description or ""},
            "start": {"dateTime": start_iso, "timeZone": timezone},
            "end": {"dateTime": end_iso, "timeZone": timezone},
        }
        emails = [a.strip() for a in (attendees or "").split(",") if a.strip()]
        if emails:
            payload["attendees"] = [{"emailAddress": {"address": e}, "type": "required"} for e in emails]
        r = requests.post(f"{GRAPH_BASE}/me/events", headers=_headers(), json=payload, timeout=20)
        r.raise_for_status()
        data = r.json()
        return {"ok": True, "event_id": data.get("id"), "web_link": data.get("webLink")}
    except Exception as e:
        return {"ok": False, "error": str(e)}
