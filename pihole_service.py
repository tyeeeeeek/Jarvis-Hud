# ================================================================
#   J.A.R.V.I.S — Pi-hole integration
#
#   Talks to a Pi-hole's own v6 REST API directly over HTTP: a session
#   login (POST /api/auth with the app password), the read-only stats
#   calls below, then logout (DELETE /api/auth). Read-only only -- there
#   is deliberately no tool here that can disable blocking, edit
#   allow/deny lists, or otherwise change Pi-hole's configuration.
#
#   Pi-hole v6 replaced the old api.php?summary&auth=<key> shape with a
#   session-based REST API under /api -- see docs.pi-hole.net/api. Login
#   uses an "App Password" (Pi-hole admin UI: Settings -> API / Web
#   interface -> App Password), NOT the admin login password itself, so a
#   leaked PIHOLE_APP_PASSWORD only grants API access, never a browser
#   login to the admin UI.
#
#   Fully inert (PIHOLE_AVAILABLE=False) until PIHOLE_APP_PASSWORD is set
#   in .env. PIHOLE_URL defaults to Pi-hole's own standard hostname
#   (http://pi.hole, which Pi-hole answers for itself once it's your DNS
#   server) -- set it explicitly to a LAN/Tailscale IP if that doesn't
#   resolve for you.
# ================================================================
import os

import requests

URL = (os.environ.get("PIHOLE_URL", "").strip() or "http://pi.hole").rstrip("/")
APP_PASSWORD = os.environ.get("PIHOLE_APP_PASSWORD", "")

PIHOLE_AVAILABLE = bool(APP_PASSWORD)

_BASE = f"{URL}/api"
_TIMEOUT = 10


def _login():
    r = requests.post(f"{_BASE}/auth", json={"password": APP_PASSWORD}, timeout=_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    session = data.get("session") or {}
    if not session.get("valid"):
        raise RuntimeError(
            "Pi-hole login failed -- check PIHOLE_APP_PASSWORD in .env "
            "(generate one in Pi-hole: Settings -> API / Web interface -> App Password)."
        )
    return session["sid"]


def _logout(sid):
    try:
        requests.delete(f"{_BASE}/auth", headers={"sid": sid}, timeout=5)
    except Exception:
        pass  # best-effort -- the session times out on its own regardless


def get_status():
    """One-shot: log in, pull today's query/blocking stats + whether
    blocking is currently on, log out. Returns {"blocking_enabled",
    "domains_blocked", "queries_today", "blocked_today", "percent_blocked",
    "unique_clients"}. Raises RuntimeError (with Pi-hole's own response
    folded in where possible) on any failure -- tools.py turns that into a
    friendly spoken reply / FYI note rather than crashing the security
    sweep. Every field is read with .get() and defaults to None/0 rather
    than raising, so a Pi-hole version that renames or omits one just
    shows as unknown instead of breaking the whole call."""
    if not PIHOLE_AVAILABLE:
        raise RuntimeError("Pi-hole integration isn't configured (PIHOLE_APP_PASSWORD missing in .env).")

    sid = _login()
    try:
        summary_resp = requests.get(f"{_BASE}/stats/summary", headers={"sid": sid}, timeout=_TIMEOUT)
        blocking_resp = requests.get(f"{_BASE}/dns/blocking", headers={"sid": sid}, timeout=_TIMEOUT)
    finally:
        _logout(sid)

    summary_resp.raise_for_status()
    summary = summary_resp.json()
    queries = summary.get("queries", {}) or {}
    clients = summary.get("clients", {}) or {}
    gravity = summary.get("gravity", {}) or {}

    blocking_enabled = None
    if blocking_resp.ok:
        blocking_enabled = (blocking_resp.json() or {}).get("blocking") == "enabled"

    return {
        "blocking_enabled": blocking_enabled,
        "domains_blocked": gravity.get("domains_being_blocked"),
        "queries_today": queries.get("total"),
        "blocked_today": queries.get("blocked"),
        "percent_blocked": queries.get("percent_blocked"),
        "unique_clients": clients.get("active", clients.get("total")),
    }
