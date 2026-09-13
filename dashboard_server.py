# ================================================================
#   J.A.R.V.I.S — phone dashboard server
#
#   A real page served directly by this project (Flask, same construction
#   style as plaid_service.py's existing local API), reachable from your
#   phone over Tailscale -- full control of every named tool Jarvis has,
#   live employee status, and a live activity feed, all from one page.
#
#   Deliberately NOT a shell/arbitrary-command channel: /api/run only ever
#   calls a function already registered in jarvis_mcp_server._TOOL_FUNCS,
#   by exact name -- the literal same registry brain.py's Claude-driven
#   tool calls use, never an eval/exec path. Every tool's own existing
#   safety logic (jailed directories, curated app lists, JarvisAdmin's
#   approval gate inside system_power, draft-only email) applies exactly
#   as it does for voice/Telegram, since it's the same function.
#
#   Two things make this different from every other local server in this
#   project (plaid_service.py's Flask app and the HUD's WebSocket server
#   are both loopback-only with zero auth -- their whole security model is
#   "unreachable from outside"):
#     1. Binds to this machine's actual Tailscale IP (not 0.0.0.0, not
#        localhost) -- reachable only from other devices on the same
#        tailnet, never the public internet or even the plain LAN.
#     2. Requires a bearer token on every /api/* call -- network-level
#        reachability from Tailscale isn't the same as authentication, so
#        this is the first real auth check anywhere in the project.
#   Skips starting entirely (rather than falling back to some looser bind)
#   if Tailscale isn't installed/connected at startup -- logged, not fatal.
#
#   The page itself (dashboard/index.html) is read fresh from disk on
#   every request, not cached at import time -- so it can keep being
#   edited and re-served without restarting the backend, the same way any
#   other static asset would be.
# ================================================================
import inspect
import json
import os
import re
import secrets
import threading
import time
import uuid
from queue import Queue, Empty

from flask import Flask, request, jsonify, Response, send_file

import base64

import requests

import agent_registry
import bot_events
import employees
import fleet_service
import jarvis_admin
import jarvis_cpu_alerts
import jarvis_improvement
import jarvis_security
import jarvis_mcp_server
import tailscale_service
import tools
import tts_service
import vision_service

try:
    import statements_service
    STATEMENTS_AVAILABLE = True
except ImportError:
    STATEMENTS_AVAILABLE = False

try:
    import synology_service
    SYNOLOGY_LIB_AVAILABLE = True
except ImportError:
    SYNOLOGY_LIB_AVAILABLE = False

try:
    import telegram_bridge
    TELEGRAM_BRIDGE_AVAILABLE = True
except ImportError:
    TELEGRAM_BRIDGE_AVAILABLE = False

# The main Jarvis chat handler (jarvis.py's handle_command) is registered
# here at jarvis.py's own startup, rather than imported directly -- jarvis.py
# is normally run as the "__main__" script, not a module named "jarvis", so
# a plain `import jarvis` from here would load and re-execute the entire
# entrypoint a second time (duplicate WS server, duplicate voice threads,
# ...) instead of finding the already-running instance. This sidesteps that
# entirely: jarvis.py hands over a plain function reference once, after
# both modules are already loaded.
_command_handler = None


def register_command_handler(fn):
    global _command_handler
    _command_handler = fn


# Same pattern as _command_handler above, for a different reason: this one
# is deliberately NOT routed through bot_events like vision_qa is. The
# phone HUD's live camera mirror posts a frame ~5x/second while the
# camera is open, and bot_events fans every publish out to the main
# dashboard's "Live Activity" feed and its 200-event replay history --
# five images a second would spam that feed with unreadable JSON blobs
# and evict everything else (health/security/finance alerts) from replay
# within seconds. This is a direct, high-frequency, ephemeral-only path
# straight to the desktop HUD's WebSocket, bypassing that entirely -- see
# `_hud_stream` below and jarvis.py's registration of it.
_frame_handler = None


def register_frame_handler(fn):
    global _frame_handler
    _frame_handler = fn


# Same reasoning and pattern as _frame_handler above: the phone HUD's
# gesture tracking posts a hand position ~10-15x/second while the camera
# is open (see dashboard/jarvis_hud.html's MediaPipe Hands loop) -- far
# too frequent for bot_events, and this is purely ephemeral cursor state,
# never anything worth keeping in the activity history anyway.
_gesture_handler = None


def register_gesture_handler(fn):
    global _gesture_handler
    _gesture_handler = fn

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PAGE_PATH = os.path.join(BASE_DIR, "dashboard", "index.html")
HUD_PAGE_PATH = os.path.join(BASE_DIR, "dashboard", "jarvis_hud.html")
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

_TOOLS_BY_NAME = {fn.__name__: fn for fn in jarvis_mcp_server._TOOL_FUNCS}

app = Flask(__name__)


def _check_token(token):
    return bool(token) and token == DASHBOARD_TOKEN


@app.before_request
def _require_token():
    is_creation = request.path.startswith("/creations/")
    if not (request.path.startswith("/api/") or is_creation):
        return  # the page shell itself is ungated; its JS prompts for the token before calling any /api/ route
    if request.path in ("/api/stream", "/api/fleet/download") or is_creation:
        # EventSource can't set custom headers, and a creation/download is
        # opened by navigating straight to a URL (no chance to attach an
        # Authorization header either) -- all three pass the token in the
        # query string instead.
        token = request.args.get("token", "")
    else:
        auth = request.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
    if not _check_token(token):
        return jsonify({"error": "unauthorized"}), 401


@app.route("/")
def _page():
    try:
        with open(PAGE_PATH, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        return f"Dashboard page missing sir: {e}", 500


@app.route("/hud")
def _hud_page():
    """The Iron-Man-HUD-style camera/object-ID page -- a separate page from
    the main Teams-style dashboard on purpose (see jarvis_hud.html's own
    header comment), reachable from the same Tailscale-gated server and
    reusing the same access token. Read fresh from disk every request, same
    as `_page()` above."""
    try:
        with open(HUD_PAGE_PATH, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        return f"HUD page missing sir: {e}", 500


@app.route("/creations/<kind>/<slug>/")
def _serve_creation(kind, slug):
    """Serves a build_creation() output over the phone dashboard's own
    Tailscale-bound, token-gated server -- so a creation opened locally in
    Brave (see jarvis.py's _on_brain_creation) can ALSO be opened from a
    phone using this machine's LAN/Tailscale IP, not just as a local file.
    Read fresh from disk every request, same as _page()/_hud_page() above.

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
    right now, or None if the phone dashboard server never started
    (Tailscale not installed/connected -- see start_server() below).
    jarvis.py's _on_brain_creation calls this to hand the user a real
    phone-openable link alongside the existing local-file/Brave open."""
    if not _server_info["host"]:
        return None
    scheme = "https" if _server_info["https"] else "http"
    server_host = _server_info["dns_name"] or _server_info["host"]
    return f"{scheme}://{server_host}:{_server_info['port']}/creations/{kind}/{slug}/?token={DASHBOARD_TOKEN}"


@app.route("/api/hud/ask", methods=["POST"])
def _hud_ask():
    """Camera Q&A for the phone HUD -- routes through the REAL Jarvis
    brain (the same Claude tool-calling pipeline voice/Telegram/SMS/the
    main dashboard use), not a standalone vision-model call. One captured
    frame is described by Gemini (vision_service.describe_scene) and that
    description is handed to the brain as context ahead of the user's
    actual question, so "what am I looking at" and "what can you help me
    with" both get a real, capable, personality-consistent answer instead
    of a stateless vision-only reply that has no idea what Jarvis can
    actually do. If Gemini isn't configured or a particular request
    fails, the brain is told exactly that (never a silently invented
    description) and still answers everything that isn't about the live
    view -- see vision_service.py's module docstring.

    Async by design: the real answer can take a few seconds (an LLM call,
    possibly a tool call), so this returns immediately once the request
    is queued, and the actual reply arrives over /api/stream as a
    'chat_reply' event -- the same event dashboard/jarvis_hud.html's
    startStream() already listens for, and the same one the main
    dashboard's chat tab uses."""
    body = request.get_json(silent=True) or {}
    image_b64 = body.get("image", "")
    question = (body.get("question") or "What do you see?").strip()

    if not image_b64:
        return jsonify({"error": "no image captured"}), 400
    try:
        if "," in image_b64:
            image_b64 = image_b64.split(",", 1)[1]
        jpeg_bytes = base64.b64decode(image_b64)
    except Exception:
        return jsonify({"error": "bad image data"}), 400

    if not _command_handler:
        return jsonify({"error": "main command handler isn't ready yet"}), 503

    def _go():
        if vision_service.GEMINI_AVAILABLE:
            description = vision_service.describe_scene(jpeg_bytes)
            vision_note = (
                f"[Through my phone camera right now I can see: {description}] "
                if description else
                "[My camera vision request just failed for this frame -- the "
                "camera is on but I couldn't get a read on the scene this "
                "time. Let the user know briefly, then still help with "
                "whatever they actually asked.] "
            )
        else:
            vision_note = (
                "[Camera vision isn't configured (no GEMINI_API_KEY set) -- "
                "I can't see through the phone camera right now, but I can "
                "still help with anything else.] "
            )

        def _notify(reply_text):
            bot_events.publish("chat_reply", {"bot": "jarvis", "text": reply_text})
            # Shares this with the desktop HUD's Vision widget too (via
            # jarvis.py, which subscribes to this bus and forwards it over
            # its own WebSocket) -- one Jarvis, not two brains that don't
            # know what the other saw.
            bot_events.publish("vision_qa", {"question": question, "answer": reply_text})

        _command_handler(vision_note + question, acked=True, notify=_notify)

    threading.Thread(target=_go, daemon=True).start()
    return jsonify({"status": "sent"})


@app.route("/api/hud/stream", methods=["POST"])
def _hud_stream():
    """Live camera mirror: the phone HUD posts one downscaled JPEG frame
    here roughly 5x/second for as long as its camera is open, and each one
    is relayed straight to the desktop HUD (see register_frame_handler
    above for why this bypasses bot_events). `image: null` (or omitted)
    signals the phone camera just closed, so the desktop can clear its
    feed immediately instead of showing a stale last frame. Frames are
    never written to disk or kept anywhere beyond this single relay call
    -- same "never on a timer, never stored" spirit as the existing screen
    vision tool."""
    body = request.get_json(silent=True) or {}
    image_b64 = body.get("image")
    if image_b64 and "," in image_b64:
        image_b64 = image_b64.split(",", 1)[1]
    if _frame_handler:
        try:
            _frame_handler(image_b64)
        except Exception:
            pass
    return jsonify({"ok": True})


@app.route("/api/hud/gesture", methods=["POST"])
def _hud_gesture():
    """Hand-gesture cursor: the phone HUD posts a hand position (MediaPipe
    Hands, running locally in the phone's browser) roughly 10-15x/second
    while its camera is open, so the desktop HUD can show a live cursor
    ring and let a pinch-and-hold drag a widget -- see
    dashboard/jarvis_hud.html's gesture loop and ArcReactor.tsx's
    handling of the 'hand_gesture' WebSocket message. `x`/`y` are
    normalized 0-1 (already mirrored for a natural front-camera feel);
    `pinching` is a plain thumb-to-index-tip distance threshold, no
    training/calibration step. `active: false` (or no recent gesture)
    means no hand is currently visible -- the desktop hides the cursor
    rather than leaving it frozen in place."""
    body = request.get_json(silent=True) or {}
    if _gesture_handler:
        try:
            _gesture_handler(
                body.get("x"), body.get("y"),
                bool(body.get("pinching")), bool(body.get("active", True)),
            )
        except Exception:
            pass
    return jsonify({"ok": True})


@app.route("/api/tools")
def _tools():
    out = []
    for fn in jarvis_mcp_server._TOOL_FUNCS:
        doc = (fn.__doc__ or "").strip()
        first_line = doc.split("\n")[0].strip() if doc else ""
        params = []
        try:
            for name, p in inspect.signature(fn).parameters.items():
                params.append({
                    "name": name,
                    "default": None if p.default is inspect.Parameter.empty else p.default,
                    "required": p.default is inspect.Parameter.empty,
                })
        except (TypeError, ValueError):
            pass
        out.append({"name": fn.__name__, "description": first_line, "params": params})
    return jsonify(out)


@app.route("/api/run", methods=["POST"])
def _run_tool():
    body = request.get_json(silent=True) or {}
    tool_name = (body.get("tool") or "").strip()
    args = body.get("args") or {}
    fn = _TOOLS_BY_NAME.get(tool_name)
    if not fn:
        return jsonify({"error": f"unknown tool '{tool_name}'"}), 400

    run_id = uuid.uuid4().hex[:12]
    bot_events.publish("tool_run_started", {"id": run_id, "tool": tool_name, "args": args})

    def _go():
        try:
            result = fn(**args)
            # Runs in this same long-lived process (unlike the interactive
            # voice/chat path's separate MCP subprocess -- see tools.
            # notify_creation_ready's docstring), so it's both safe and
            # necessary to do this here too: without it, a creation built
            # from the phone dashboard's own Tools tab would never get a
            # link, a Telegram notice, or a spot in the retrievable log.
            if tool_name in ("build_creation", "build_finance_dashboard"):
                try:
                    parsed = json.loads(result)
                    if parsed.get("ok"):
                        tools.notify_creation_ready(parsed)
                except Exception as e:
                    print(f"  [Dashboard] Couldn't notify creation ready: {e}")
            bot_events.publish("tool_run_finished", {"id": run_id, "tool": tool_name, "ok": True, "result": str(result)[:2000]})
        except Exception as e:
            bot_events.publish("tool_run_finished", {"id": run_id, "tool": tool_name, "ok": False, "result": str(e)[:2000]})

    threading.Thread(target=_go, daemon=True).start()
    return jsonify({"id": run_id, "status": "started"})


@app.route("/api/employees")
def _employees():
    return jsonify(employees.list_jobs(limit=20))


@app.route("/api/hire", methods=["POST"])
def _hire():
    body = request.get_json(silent=True) or {}
    record, error = employees.enqueue_job(role=body.get("role", ""), job=body.get("job", ""))
    if error:
        return jsonify({"error": error}), 400
    return jsonify(record)


# ---------------- Agent registry (Org tab "onboarding") ----------------
# Persistent, optionally-scheduled agents -- see agent_registry.py's module
# docstring. Every route here just calls straight into that module; the
# @app.before_request token check above already covers all of /api/*.

@app.route("/api/registry")
def _registry_list():
    return jsonify(agent_registry.list_agents())


@app.route("/api/registry", methods=["POST"])
def _registry_create():
    body = request.get_json(silent=True) or {}
    record, error = agent_registry.create_agent(
        name=body.get("name", ""), kind=body.get("kind", ""),
        tool=body.get("tool"), args=body.get("args"),
        role=body.get("role"), job=body.get("job"),
        trigger=body.get("trigger"),
    )
    if error:
        return jsonify({"error": error}), 400
    return jsonify(record)


@app.route("/api/registry/<agent_id>/run", methods=["POST"])
def _registry_run(agent_id):
    record, error = agent_registry.run_agent_now(agent_id, auto=False)
    if error:
        return jsonify({"error": error}), 400
    return jsonify(record)


@app.route("/api/registry/<agent_id>/toggle", methods=["POST"])
def _registry_toggle(agent_id):
    body = request.get_json(silent=True) or {}
    updated = agent_registry.update_agent(agent_id, enabled=bool(body.get("enabled")))
    if not updated:
        return jsonify({"error": "No such agent"}), 404
    return jsonify(updated)


@app.route("/api/registry/<agent_id>", methods=["DELETE"])
def _registry_delete(agent_id):
    if not agent_registry.delete_agent(agent_id):
        return jsonify({"error": "No such agent"}), 404
    return jsonify({"ok": True})


# Static roster for the Teams-style contact list -- "chat": True means the
# dashboard shows a free-text message box for it (backed by that module's
# own respond(text) function, the same logic its Telegram channel already
# uses). JarvisAdmin deliberately gets chat: False -- it's not a general
# command channel by design (see jarvis_admin.py's module docstring); the
# dashboard shows a live Approve/Deny card for it instead, never a text box.
_BOT_ROSTER = [
    {"key": "jarvis", "name": "J.A.R.V.I.S", "role": "Main assistant -- full tool access", "chat": True},
    {"key": "cpu_alerts", "name": "JarvisCPU_Alerts", "role": "PC thermal & disk health watchdog", "chat": True},
    {"key": "improvement", "name": "JarvisImprovement", "role": "Daily self-improvement passes", "chat": True},
    {"key": "security", "name": "JarSecurity", "role": "Cybersecurity sweeps & LAN monitoring", "chat": True},
    {"key": "admin", "name": "JarvisAdmin", "role": "Approval gate for sudo-like actions", "chat": False},
]


def _bot_configured(key):
    return {
        "jarvis": TELEGRAM_BRIDGE_AVAILABLE and telegram_bridge.TELEGRAM_AVAILABLE,
        "cpu_alerts": jarvis_cpu_alerts.AVAILABLE,
        "improvement": jarvis_improvement.AVAILABLE,
        "security": jarvis_security.AVAILABLE,
        "admin": jarvis_admin.JARVIS_ADMIN_AVAILABLE,
    }.get(key, False)


@app.route("/api/agents")
def _agents():
    bots = []
    for bot in _BOT_ROSTER:
        entry = dict(bot)
        entry["configured"] = _bot_configured(bot["key"])
        bots.append(entry)
    return jsonify({
        "bots": bots,
        "admin_pending": jarvis_admin.get_pending(),
        "status_text": tools.agent_status(),
    })


_CHAT_RESPONDERS = {
    "cpu_alerts": jarvis_cpu_alerts.respond,
    "security": jarvis_security.respond,
    "improvement": jarvis_improvement.respond,
}


@app.route("/api/chat", methods=["POST"])
def _chat():
    body = request.get_json(silent=True) or {}
    bot = (body.get("bot") or "jarvis").strip()
    text = (body.get("message") or "").strip()
    if not text:
        return jsonify({"error": "empty message"}), 400

    bot_events.publish("chat_message", {"bot": bot, "text": text})

    if bot == "jarvis":
        if not _command_handler:
            return jsonify({"error": "main command handler isn't ready yet"}), 503

        def _go():
            _command_handler(text, acked=True,
                              notify=lambda r: bot_events.publish("chat_reply", {"bot": "jarvis", "text": r}))

        threading.Thread(target=_go, daemon=True).start()
        return jsonify({"status": "sent"})

    fn = _CHAT_RESPONDERS.get(bot)
    if not fn:
        return jsonify({"error": f"'{bot}' doesn't take chat messages"}), 400

    def _go2():
        try:
            reply = fn(text)
        except Exception as e:
            reply = f"Error: {e}"
        bot_events.publish("chat_reply", {"bot": bot, "text": reply})

    threading.Thread(target=_go2, daemon=True).start()
    return jsonify({"status": "sent"})


@app.route("/api/admin/decide", methods=["POST"])
def _admin_decide():
    body = request.get_json(silent=True) or {}
    ok = jarvis_admin.decide_from_dashboard(body.get("id", ""), bool(body.get("approved")))
    return jsonify({"ok": ok})


@app.route("/api/status")
def _status():
    """Cheap, read-only snapshot for the Home tab's status widgets. Never
    triggers a fresh check itself (run_security_check touches the network
    and browser, check_system_health can take a moment) -- reads whatever
    the existing 2-hour/4-hour watcher threads last found, same numbers
    agent_status() already reports from. NAS status is a live call (a
    single fast DSM API hit), and the finance snapshot is a live read of
    the ledger (no side effects, safe to call on every page load unlike
    check_spending_anomalies)."""
    nas = None
    if SYNOLOGY_LIB_AVAILABLE and synology_service.SYNOLOGY_AVAILABLE:
        try:
            nas = synology_service.get_status()
        except Exception as e:
            nas = {"error": str(e)}

    finance = None
    if STATEMENTS_AVAILABLE and statements_service.has_data():
        finance = statements_service.get_spending_summary(days=30)

    return jsonify({
        "health": tools.LAST_HEALTH_RESULT,
        "security": tools.LAST_SECURITY_RESULT,
        "nas": nas,
        "nas_configured": bool(SYNOLOGY_LIB_AVAILABLE and synology_service.SYNOLOGY_AVAILABLE),
        "finance": finance,
    })


@app.route("/api/finance")
def _finance():
    if not (STATEMENTS_AVAILABLE and statements_service.has_data()):
        return jsonify({"available": False})
    return jsonify({
        "available": True,
        "summary": statements_service.get_spending_summary(days=30),
        "recurring": statements_service.get_recurring_charges(),
        "anomalies": statements_service.recent_anomalies(10),
    })


@app.route("/api/ollama-status")
def _ollama_status():
    """Read-only proxy to this PC's own Ollama (http://127.0.0.1:11434/api/tags,
    same shape Ollama itself returns) -- exists because Ollama is deliberately
    bound to loopback-only (see JarSecurity's exposed-port remediation in
    tools.py), so nothing off this PC can reach it directly anymore, INCLUDING
    the homelab dashboard's Docker container (it's on its own bridge network,
    not host networking). This route is the one narrow, already-authenticated
    (same Bearer token as every other /api/ route here) exception -- it only
    ever forwards Ollama's own model-list response, nothing else, and only to
    someone who already has this dashboard's token. Point a homepage/Uptime-
    Kuma widget at this instead of Ollama's own port directly."""
    try:
        r = requests.get("http://127.0.0.1:11434/api/tags", timeout=5)
        return Response(r.content, status=r.status_code, mimetype="application/json")
    except Exception as e:
        return jsonify({"error": f"Ollama unreachable: {e}"}), 502


@app.route("/api/fleet/devices")
def _fleet_devices():
    """Every fleet device, its configured/OS/actions, merged with live
    Tailscale info (IP, online/last-seen, direct-vs-relayed) -- same
    shape the HUD's own Fleet tab uses (plaid_service.py's
    /homelab/fleet), just reachable from the phone over this
    Tailscale-gated HTTPS server instead of the HUD's local-only one."""
    devices = fleet_service.list_devices()
    if tailscale_service.TAILSCALE_AVAILABLE:
        try:
            peers_by_name = {p["name"].lower(): p for p in tailscale_service.get_status()["peers"]}
        except Exception:
            peers_by_name = {}
        for d in devices:
            d["tailscale"] = peers_by_name.get(d["name"])
    return jsonify({"devices": devices})


# Every fleet action reachable from the phone dashboard, by name -- always
# goes through the matching tools.py wrapper (never fleet_service
# directly) so restart/docker_restart get the exact same JarvisAdmin
# approval gate here as they do from voice/Telegram. Read-only actions
# (status/processes/docker_status) hit the same wrappers too, just with
# nothing to approve.
_FLEET_ACTIONS = {
    "status": lambda device, arg: tools.fleet_status(device),
    "processes": lambda device, arg: tools.fleet_processes(device),
    "docker_status": lambda device, arg: tools.fleet_docker_status(device),
    "docker_restart": lambda device, arg: tools.fleet_docker_restart(device, arg),
    "restart": lambda device, arg: tools.fleet_restart(device),
    "cancel_restart": lambda device, arg: tools.fleet_cancel_restart(device),
    "apps_installed": lambda device, arg: tools.fleet_apps_installed(device),
    "apps_search": lambda device, arg: tools.fleet_apps_search(device, arg),
    "apps_resolve": lambda device, arg: tools.fleet_resolve_app(device, arg),
    "app_install": lambda device, arg: tools.fleet_app_install(device, arg),
    "app_uninstall": lambda device, arg: tools.fleet_app_uninstall(device, arg),
    "app_upgrade": lambda device, arg: tools.fleet_app_upgrade(device, arg),
}


@app.route("/api/fleet/action", methods=["POST"])
def _fleet_action():
    """Runs one curated fleet action -- action must be a key in
    _FLEET_ACTIONS above (never a caller-supplied command). Disruptive
    actions (restart, docker_restart) block here until JarvisAdmin
    approval is answered or times out, exactly like every other path to
    them."""
    body = request.get_json(silent=True) or {}
    device = (body.get("device") or "").strip()
    action = (body.get("action") or "").strip()
    arg = body.get("arg")
    fn = _FLEET_ACTIONS.get(action)
    if not fn:
        return jsonify({"error": f"unknown fleet action '{action}'"}), 400
    return jsonify({"result": fn(device, arg)})


@app.route("/api/fleet/browse")
def _fleet_browse():
    """Lists one directory on a fleet device -- read-only, no approval
    needed. `path` blank means that device's root."""
    device = request.args.get("device", "")
    path = request.args.get("path", "")
    try:
        entries = fleet_service.list_dir(device, path)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 502
    return jsonify({"path": path, "entries": entries})


@app.route("/api/fleet/download")
def _fleet_download():
    """Downloads one file from a fleet device -- read-only, no approval
    needed (see the query-string-token exception for this path in
    _require_token above; a browser navigating straight to this URL has
    no chance to set an Authorization header)."""
    device = request.args.get("device", "")
    path = request.args.get("path", "")
    try:
        data = fleet_service.read_file(device, path)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 502
    filename = (path or "file").replace("\\", "/").rsplit("/", 1)[-1] or "file"
    return Response(
        data, mimetype="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.route("/api/fleet/mkdir", methods=["POST"])
def _fleet_mkdir():
    body = request.get_json(silent=True) or {}
    return jsonify({"result": tools.fleet_make_dir(body.get("device", ""), body.get("path", ""))})


@app.route("/api/fleet/delete", methods=["POST"])
def _fleet_delete():
    body = request.get_json(silent=True) or {}
    return jsonify({"result": tools.fleet_delete_path(body.get("device", ""), body.get("path", ""))})


@app.route("/api/fleet/rename", methods=["POST"])
def _fleet_rename():
    body = request.get_json(silent=True) or {}
    return jsonify({"result": tools.fleet_rename_path(body.get("device", ""), body.get("src", ""), body.get("dst", ""))})


@app.route("/api/fleet/upload", methods=["POST"])
def _fleet_upload():
    """Uploads one file to a fleet device -- multipart form upload
    (`file` field) plus `device` and `path` fields for the destination.
    Same JarvisAdmin approval gate as every other fleet write action, via
    tools.fleet_write_file."""
    device = request.form.get("device", "")
    path = request.form.get("path", "")
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "no file"}), 400
    data = f.read()
    return jsonify({"result": tools.fleet_write_file(device, path, data)})


@app.route("/api/stream")
def _stream():
    def gen():
        q = Queue()
        unsubscribe = bot_events.subscribe(q.put)
        try:
            for event in bot_events.recent(50):
                yield f"data: {json.dumps(event)}\n\n"
            while True:
                try:
                    event = q.get(timeout=15)
                    yield f"data: {json.dumps(event)}\n\n"
                except Empty:
                    yield ": keepalive\n\n"
        finally:
            unsubscribe()

    return Response(gen(), mimetype="text/event-stream")


def start_server():
    """Checks Tailscale connectivity right now and binds to this device's
    actual tailnet IP if it's up; logs and returns without starting
    otherwise (never falls back to a looser bind). Called unconditionally
    from jarvis.py's startup sequence, same as plaid_service.start_server --
    the runtime check here (not a static *_AVAILABLE flag) is what decides
    whether it actually starts, since Tailscale connectivity can change
    between restarts in a way an import-time constant can't reflect."""
    if not tailscale_service.TAILSCALE_AVAILABLE:
        print("  [Dashboard] Tailscale isn't installed -- phone dashboard disabled. See README.")
        return
    try:
        status = tailscale_service.get_status()
    except Exception as e:
        print(f"  [Dashboard] Couldn't read Tailscale status -- phone dashboard disabled: {e}")
        return
    if status.get("backend_state") != "Running" or not status.get("self_ip"):
        print("  [Dashboard] Tailscale isn't connected -- phone dashboard disabled. Run `tailscale up` and restart.")
        return

    host = status["self_ip"]

    # HTTPS is required for the /hud page's camera + speech-recognition
    # access to work at all (phone browsers refuse getUserMedia/Web Speech
    # on a plain-HTTP origin) -- see tailscale_service.ensure_https_cert's
    # docstring. Falls back to plain HTTP (main dashboard still works
    # fully; only /hud's camera/mic won't) rather than failing to start.
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
            print(f"  [Dashboard] Phone dashboard crashed -- disabling phone links: {e}")
            _server_info["host"] = None
            _server_info["port"] = None
            _server_info["https"] = False
            _server_info["dns_name"] = None

    threading.Thread(target=_run, daemon=True, name="Dashboard").start()
    if cert_info:
        _, _, dns_name = cert_info
        print(f"  [Dashboard] Phone dashboard -> https://{dns_name}:{PORT} (token in {TOKEN_PATH})")
        print(f"  [Dashboard] Phone HUD (camera) -> https://{dns_name}:{PORT}/hud")
    else:
        print(f"  [Dashboard] Phone dashboard -> http://{host}:{PORT} (token in {TOKEN_PATH})")
        print("  [Dashboard] HTTPS unavailable -- /hud will load but its camera/mic won't work "
              "on a phone browser without it. See README \"Phone HUD (Iron Man desk-view camera)\" "
              "-> \"Enable HTTPS\" to turn this on (one-time Tailscale admin console toggle).")
