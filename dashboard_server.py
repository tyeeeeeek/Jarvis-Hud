# ================================================================
#   J.A.R.V.I.S — creations LAN/Tailscale server
#
#   Serves a build_creation() output (tools.py) over a real page reachable
#   from your phone over Tailscale, in addition to the existing local-file/
#   Brave open -- same construction style as plaid_service.py's existing
#   local API.
#
#   Binds to this machine's actual Tailscale IP (not 0.0.0.0, not
#   localhost) -- reachable only from other devices on the same tailnet,
#   never the public internet or even the plain LAN. Requires a bearer/
#   query token on every request -- network-level reachability from
#   Tailscale isn't the same as authentication.
#   Skips starting entirely (rather than falling back to some looser bind)
#   if Tailscale isn't installed/connected at startup -- logged, not fatal.
# ================================================================
import os
import re
import secrets
import threading

from flask import Flask, request, jsonify, Response

import tailscale_service
import tools

PORT = 8767

# Same two directories tools.build_creation() already writes into -- reused
# here, not duplicated, so this server and the tool that produces the files
# can never drift apart on where they live.
CREATIONS_KIND_DIRS = {
    "dashboard": tools.CREATIONS_DIR,
    "webpage": tools.WEBSITES_DIR,
}

# Filled in by start_server() once Tailscale connectivity is confirmed;
# stays all-None if the server never starts, so creation_url() below can
# tell callers (jarvis.py's _on_brain_creation) "no LAN URL available"
# instead of handing back a broken one.
_server_info = {"host": None, "port": None, "https": False, "dns_name": None}

JARVIS_DIR = os.path.join(os.path.expanduser("~"), ".jarvis")
TOKEN_PATH = os.path.join(JARVIS_DIR, "dashboard_token.txt")


def _load_or_create_token():
    """DASHBOARD_TOKEN in .env wins if set. Otherwise a token is generated
    once and cached in ~/.jarvis/dashboard_token.txt (already gitignored
    via the existing `.jarvis/` entry, same as every other local secret/
    state file this project keeps there) so it survives restarts without
    the user needing to set anything up."""
    env_token = os.environ.get("DASHBOARD_TOKEN", "").strip()
    if env_token:
        return env_token
    try:
        with open(TOKEN_PATH, "r", encoding="utf-8") as f:
            existing = f.read().strip()
            if existing:
                return existing
    except FileNotFoundError:
        pass
    token = secrets.token_urlsafe(32)
    os.makedirs(JARVIS_DIR, exist_ok=True)
    with open(TOKEN_PATH, "w", encoding="utf-8") as f:
        f.write(token)
    return token


DASHBOARD_TOKEN = _load_or_create_token()

app = Flask(__name__)


def _check_token(token):
    return bool(token) and token == DASHBOARD_TOKEN


@app.before_request
def _require_token():
    # A creation is opened by navigating straight to a URL (no chance to
    # attach an Authorization header) -- the token travels in the query
    # string instead, same as EventSource-style endpoints elsewhere in
    # this project.
    token = request.args.get("token", "")
    if not _check_token(token):
        return jsonify({"error": "unauthorized"}), 401


@app.route("/creations/<kind>/<slug>/")
def _serve_creation(kind, slug):
    """Serves a build_creation() output over this server's own Tailscale-
    bound, token-gated address -- so a creation opened locally in Brave
    (see jarvis.py's _on_brain_creation) can ALSO be opened from a phone
    using this machine's LAN/Tailscale IP, not just as a local file. Read
    fresh from disk every request.

    Deliberately narrow: kind must be one of the two real directories
    build_creation() ever writes into, slug is stripped down to
    [a-z0-9-] (build_creation's own _slugify() never produces anything
    else) and re-checked with the same commonpath containment check
    tools._resolve_safe_path() uses elsewhere in this project -- no
    arbitrary filename is ever accepted, only that creation's own
    index.html, since build_creation() never writes any other file."""
    base = CREATIONS_KIND_DIRS.get(kind)
    if base is None:
        return "Not found sir.", 404
    clean_slug = re.sub(r"[^a-z0-9-]", "", (slug or "").lower())
    base_abs = os.path.abspath(base)
    target_dir = os.path.abspath(os.path.join(base, clean_slug))
    if not clean_slug or os.path.commonpath([target_dir, base_abs]) != base_abs:
        return "Not found sir.", 404
    index_path = os.path.join(target_dir, "index.html")
    if not os.path.isfile(index_path):
        return "That creation doesn't exist sir.", 404
    try:
        with open(index_path, "r", encoding="utf-8") as f:
            return Response(f.read(), mimetype="text/html")
    except Exception as e:
        return f"Couldn't read that creation sir: {e}", 500


def creation_url(kind, slug):
    """The LAN/Tailscale URL a build_creation() output is reachable at
    right now, or None if this server never started (Tailscale not
    installed/connected -- see start_server() below). jarvis.py's
    _on_brain_creation calls this to hand the user a real phone-openable
    link alongside the existing local-file/Brave open."""
    if not _server_info["host"]:
        return None
    scheme = "https" if _server_info["https"] else "http"
    server_host = _server_info["dns_name"] or _server_info["host"]
    return f"{scheme}://{server_host}:{_server_info['port']}/creations/{kind}/{slug}/?token={DASHBOARD_TOKEN}"


def start_server():
    """Checks Tailscale connectivity right now and binds to this device's
    actual tailnet IP if it's up; logs and returns without starting
    otherwise (never falls back to a looser bind). Called unconditionally
    from jarvis.py's startup sequence, same as plaid_service.start_server --
    the runtime check here (not a static *_AVAILABLE flag) is what decides
    whether it actually starts, since Tailscale connectivity can change
    between restarts in a way an import-time constant can't reflect."""
    if not tailscale_service.TAILSCALE_AVAILABLE:
        print("  [Dashboard] Tailscale isn't installed -- creations LAN server disabled. See README.")
        return
    try:
        status = tailscale_service.get_status()
    except Exception as e:
        print(f"  [Dashboard] Couldn't read Tailscale status -- creations LAN server disabled: {e}")
        return
    if status.get("backend_state") != "Running" or not status.get("self_ip"):
        print("  [Dashboard] Tailscale isn't connected -- creations LAN server disabled. Run `tailscale up` and restart.")
        return

    host = status["self_ip"]

    # HTTPS is used when available (see tailscale_service.ensure_https_cert)
    # so a link copied to a phone loads over a trusted connection; falls
    # back to plain HTTP rather than failing to start.
    cert_info = tailscale_service.ensure_https_cert()

    _server_info["host"] = host
    _server_info["port"] = PORT
    _server_info["https"] = bool(cert_info)
    _server_info["dns_name"] = cert_info[2] if cert_info else None

    def _run():
        try:
            if cert_info:
                cert_path, key_path, _dns = cert_info
                app.run(host=host, port=PORT, use_reloader=False, ssl_context=(cert_path, key_path))
            else:
                app.run(host=host, port=PORT, use_reloader=False)
        except Exception as e:
            # app.run() blocks for as long as the server is actually up --
            # reaching here means it never bound (port already in use, bad
            # cert, etc.) or died mid-run. Reset _server_info so creation_
            # url() honestly reports "no link" afterward instead of going on
            # handing out a URL that looks fine but nothing is answering --
            # a stale _server_info here is exactly the kind of "wrong but
            # confident" link build_creation's Telegram notice must not send.
            print(f"  [Dashboard] Creations LAN server crashed -- disabling phone links: {e}")
            _server_info["host"] = None
            _server_info["port"] = None
            _server_info["https"] = False
            _server_info["dns_name"] = None

    threading.Thread(target=_run, daemon=True, name="Dashboard").start()
    if cert_info:
        _, _, dns_name = cert_info
        print(f"  [Dashboard] Creations reachable on your phone/tailnet -> https://{dns_name}:{PORT}/creations/... (token in {TOKEN_PATH})")
    else:
        print(f"  [Dashboard] Creations reachable on your phone/tailnet -> http://{host}:{PORT}/creations/... (token in {TOKEN_PATH})")
