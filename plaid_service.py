# ================================================================
#   Plaid integration — local-only, read-only spending insight.
#
#   Your access token never leaves this machine: it's encrypted at
#   rest (plaid_store.enc) with a key generated locally (.jarvis_key,
#   never committed), and only ever sent back out to Plaid's own API
#   over HTTPS. The HUD frontend and the Ollama model only ever see
#   aggregated summaries (category totals, merchant names) — never
#   the access token, account numbers, or raw Plaid credentials.
#
#   Only the "transactions" product is requested — this integration
#   can read statement history but has no ability to move money.
# ================================================================
import os
import json
import uuid
import threading
from datetime import datetime, timedelta

from dotenv import load_dotenv
from cryptography.fernet import Fernet
from flask import Flask, request, jsonify
from flask_cors import CORS

import statements_service
import network_watch
import speedtest_service
import synology_service

import plaid
from plaid.api import plaid_api
from plaid.model.products import Products
from plaid.model.country_code import CountryCode
from plaid.model.link_token_create_request import LinkTokenCreateRequest
from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest
from plaid.model.transactions_sync_request import TransactionsSyncRequest

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
KEY_PATH = os.path.join(BASE_DIR, ".jarvis_key")
STORE_PATH = os.path.join(BASE_DIR, "plaid_store.enc")
PORT = 8766

PLAID_CLIENT_ID = os.environ.get("PLAID_CLIENT_ID", "")
PLAID_SECRET = os.environ.get("PLAID_SECRET", "")
PLAID_ENV = os.environ.get("PLAID_ENV", "sandbox").strip().lower()

_ENV_MAP = {
    "sandbox": plaid.Environment.Sandbox,
    "development": getattr(plaid.Environment, "Development", plaid.Environment.Production),
    "production": plaid.Environment.Production,
}


def _is_configured():
    return bool(PLAID_CLIENT_ID and PLAID_SECRET)


_client = None


def _get_client():
    global _client
    if _client is None:
        configuration = plaid.Configuration(
            host=_ENV_MAP.get(PLAID_ENV, plaid.Environment.Sandbox),
            api_key={"clientId": PLAID_CLIENT_ID, "secret": PLAID_SECRET},
        )
        _client = plaid_api.PlaidApi(plaid.ApiClient(configuration))
    return _client


# ---------------------------------------------------------------- encrypted local storage
def _get_fernet():
    if not os.path.exists(KEY_PATH):
        with open(KEY_PATH, "wb") as f:
            f.write(Fernet.generate_key())
    with open(KEY_PATH, "rb") as f:
        return Fernet(f.read())


def _save_item(access_token, item_id):
    data = json.dumps({"access_token": access_token, "item_id": item_id}).encode()
    with open(STORE_PATH, "wb") as f:
        f.write(_get_fernet().encrypt(data))


def _load_item():
    if not os.path.exists(STORE_PATH):
        return None
    try:
        with open(STORE_PATH, "rb") as f:
            data = _get_fernet().decrypt(f.read())
        return json.loads(data)
    except Exception as e:
        print(f"  [Plaid] Could not read stored item: {e}")
        return None


def is_linked():
    return _load_item() is not None


def unlink():
    if os.path.exists(STORE_PATH):
        os.remove(STORE_PATH)


# ---------------------------------------------------------------- Plaid calls
def create_link_token():
    req = LinkTokenCreateRequest(
        products=[Products("transactions")],
        client_name="Jarvis HUD",
        country_codes=[CountryCode("US")],
        language="en",
        user=LinkTokenCreateRequestUser(client_user_id=str(uuid.uuid4())),
    )
    response = _get_client().link_token_create(req)
    return response["link_token"]


def exchange_public_token(public_token):
    req = ItemPublicTokenExchangeRequest(public_token=public_token)
    response = _get_client().item_public_token_exchange(req)
    _save_item(response["access_token"], response["item_id"])


def _fetch_all_transactions(access_token):
    transactions = []
    cursor = None
    while True:
        req = TransactionsSyncRequest(access_token=access_token, cursor=cursor) if cursor \
            else TransactionsSyncRequest(access_token=access_token)
        response = _get_client().transactions_sync(req)
        transactions.extend(response["added"])
        cursor = response["next_cursor"]
        if not response["has_more"]:
            break
    return transactions


def get_spending_summary(days=30):
    item = _load_item()
    if not item:
        return None

    transactions = _fetch_all_transactions(item["access_token"])
    cutoff = datetime.now().date() - timedelta(days=days)

    by_category = {}
    by_merchant = {}
    total_spent = 0.0

    for t in transactions:
        t_date = t["date"] if isinstance(t["date"], str) else t["date"].isoformat()
        if datetime.fromisoformat(t_date).date() < cutoff:
            continue
        amount = t["amount"]
        if amount <= 0:
            continue  # negative/zero = income or refund, not spending

        pfc = t.get("personal_finance_category")
        category = pfc["primary"].replace("_", " ").title() if pfc else "Other"
        merchant = t.get("merchant_name") or t.get("name") or "Unknown"

        by_category[category] = by_category.get(category, 0.0) + amount
        by_merchant[merchant] = by_merchant.get(merchant, 0.0) + amount
        total_spent += amount

    top_categories = sorted(by_category.items(), key=lambda x: -x[1])[:8]
    top_merchants = sorted(by_merchant.items(), key=lambda x: -x[1])[:8]

    return {
        "period_days": days,
        "total_spent": round(total_spent, 2),
        "by_category": [{"name": n, "amount": round(a, 2)} for n, a in top_categories],
        "top_merchants": [{"name": n, "amount": round(a, 2)} for n, a in top_merchants],
    }


# ---------------------------------------------------------------- local HTTP API (loopback only)
app = Flask(__name__)
CORS(app)


@app.route("/plaid/status")
def _status():
    return jsonify({"configured": _is_configured(), "linked": is_linked()})


@app.route("/plaid/link_token")
def _link_token():
    if not _is_configured():
        return jsonify({"error": "PLAID_CLIENT_ID/PLAID_SECRET not set in .env"}), 400
    try:
        return jsonify({"link_token": create_link_token()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/plaid/exchange", methods=["POST"])
def _exchange():
    public_token = (request.get_json(silent=True) or {}).get("public_token")
    if not public_token:
        return jsonify({"error": "missing public_token"}), 400
    try:
        exchange_public_token(public_token)
        return jsonify({"linked": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/plaid/summary")
def _summary():
    days = int(request.args.get("days", 30))
    try:
        summary = get_spending_summary(days=days)
        if summary is None:
            return jsonify({"error": "not linked"}), 400
        return jsonify(summary)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/plaid/unlink", methods=["POST"])
def _unlink():
    unlink()
    return jsonify({"linked": False})


@app.route("/statements/sync", methods=["POST"])
def _statements_sync():
    return jsonify(statements_service.sync())


@app.route("/statements/summary")
def _statements_summary():
    days = int(request.args.get("days", 30))
    summary = statements_service.get_spending_summary(days=days)
    if summary is None:
        return jsonify({"error": "no statements imported yet"}), 400
    return jsonify(summary)


@app.route("/homelab/nas")
def _homelab_nas():
    if not synology_service.SYNOLOGY_AVAILABLE:
        return jsonify({"configured": False})
    try:
        status = synology_service.get_status()
        return jsonify({"configured": True, **status})
    except Exception as e:
        return jsonify({"configured": True, "error": str(e)}), 502


@app.route("/homelab/speedtest")
def _homelab_speedtest_last():
    return jsonify({
        "available": speedtest_service.SPEEDTEST_AVAILABLE,
        "last_result": speedtest_service.get_last_result(),
    })


@app.route("/homelab/speedtest/run", methods=["POST"])
def _homelab_speedtest_run():
    if not speedtest_service.SPEEDTEST_AVAILABLE:
        return jsonify({"error": "speedtest-cli isn't installed"}), 400
    try:
        return jsonify(speedtest_service.run_speed_test())
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/homelab/network")
def _homelab_network():
    return jsonify(network_watch.last_scan_summary())


@app.route("/homelab/network/scan", methods=["POST"])
def _homelab_network_scan():
    return jsonify(network_watch.scan())


@app.route("/homelab/network/ports")
def _homelab_network_ports():
    """On-demand port scan of ONE already-discovered device -- see
    network_watch.scan_ports's docstring for why this is a separate,
    explicit, single-host action rather than folded into the ping sweep."""
    return jsonify(network_watch.scan_ports(request.args.get("ip", "")))


@app.route("/homelab/network/gateway")
def _homelab_network_gateway():
    return jsonify({"gateway": network_watch.detect_gateway()})


@app.route("/homelab/network/hostname")
def _homelab_network_hostname():
    return jsonify({"hostname": network_watch.hostname_for(request.args.get("ip", ""))})


@app.route("/homelab/network/probe")
def _homelab_network_probe():
    return jsonify(network_watch.probe_http(request.args.get("ip", "")))


@app.route("/homelab/health")
def _homelab_health():
    """Read-only: the PC Jarvis itself runs on -- CPU thermal state and
    disk headroom from the last periodic check_system_health() run (the
    watcher thread that already runs every 2 hours in jarvis.py), never a
    fresh check triggered by this route. Local import: tools.py imports
    plaid_service (see PLAID_AVAILABLE at its top), so importing tools back
    at this module's top level would be circular."""
    import tools
    return jsonify(tools.LAST_HEALTH_RESULT or {})


@app.route("/homelab/ai_services")
def _homelab_ai_services():
    """Read-only: Ollama/Prometheus status from the last periodic
    check_ai_services() run (the watcher thread that runs every 10 minutes
    in jarvis.py), same "never a fresh check from this route" pattern as
    _homelab_health above."""
    import tools
    return jsonify(tools.LAST_AI_SERVICES_RESULT or {})


@app.route("/homelab/ai_services/check", methods=["POST"])
def _homelab_ai_services_check():
    """On-demand, live re-check (unlike the GET route above) -- same
    "manual button triggers a real call" pattern as /homelab/network/scan
    and /homelab/speedtest/run."""
    import tools
    tools.check_ai_services()
    return jsonify(tools.LAST_AI_SERVICES_RESULT or {})


@app.route("/homelab/ai_services/restart", methods=["POST"])
def _homelab_ai_services_restart():
    """Manual restart button for one down service. `service` must be
    exactly "ollama" or "prometheus" in the request body -- tools.
    restart_ai_service() itself refuses anything else, so there's no way
    for this route to become a general restart-anything endpoint."""
    import tools
    service = (request.get_json(silent=True) or {}).get("service", "")
    result = tools.restart_ai_service(service)
    tools.check_ai_services()
    return jsonify({"result": result, "services": tools.LAST_AI_SERVICES_RESULT or {}})


@app.route("/homelab/fleet")
def _homelab_fleet():
    """Every fleet device Jarvis can reach into, what it can run there,
    whether it's actually configured yet (see fleet_service.list_devices's
    docstring), merged with that device's live Tailscale info (IP, online/
    last-seen, direct-vs-relayed) -- read-only, no SSH call made here, just
    one already-cheap `tailscale status` lookup."""
    import fleet_service
    import tailscale_service
    devices = fleet_service.list_devices()
    if tailscale_service.TAILSCALE_AVAILABLE:
        try:
            peers_by_name = {p["name"].lower(): p for p in tailscale_service.get_status()["peers"]}
        except Exception:
            peers_by_name = {}
        for d in devices:
            peer = peers_by_name.get(d["name"])
            d["tailscale"] = peer if peer else None
    return jsonify({"devices": devices})


@app.route("/homelab/fleet/status", methods=["POST"])
def _homelab_fleet_status():
    """On-demand status check on one fleet device -- makes a real SSH
    call, can take a few seconds, and fails with a specific reason (not
    configured yet / unreachable / non-zero exit) rather than a generic
    error if something's wrong."""
    import fleet_service
    device = (request.get_json(silent=True) or {}).get("device", "")
    try:
        result = fleet_service.run_action(device, "status")
        return jsonify({"ok": True, "output": result["output"]})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502


@app.route("/homelab/fleet/restart", methods=["POST"])
def _homelab_fleet_restart():
    """Restart a Windows fleet device. This route IS tools.fleet_restart
    -- same JarvisAdmin Telegram approval gate as system_power on this
    machine, so the call can block for a while waiting on that answer."""
    import tools
    device = (request.get_json(silent=True) or {}).get("device", "")
    return jsonify({"result": tools.fleet_restart(device)})


@app.route("/homelab/fleet/cancel_restart", methods=["POST"])
def _homelab_fleet_cancel_restart():
    import tools
    device = (request.get_json(silent=True) or {}).get("device", "")
    return jsonify({"result": tools.fleet_cancel_restart(device)})


def start_server():
    statements_service.ensure_statements_dir()

    def _run():
        app.run(host="127.0.0.1", port=PORT, use_reloader=False)
    threading.Thread(target=_run, daemon=True, name="PlaidServer").start()
    print(f"  [Plaid] Local API -> http://localhost:{PORT} (configured: {_is_configured()})")
    print(f"  [Statements] Drop bank exports (CSV/TXT/PDF/XLSX/OFX/QFX/images/ZIP) in {statements_service.STATEMENTS_DIR}")
    print(f"  [Homelab] NAS configured: {synology_service.SYNOLOGY_AVAILABLE}, "
          f"nmap available: {network_watch.NMAP_AVAILABLE}, speedtest-cli available: {speedtest_service.SPEEDTEST_AVAILABLE}")
