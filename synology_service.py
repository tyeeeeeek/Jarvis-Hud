# ================================================================
#   J.A.R.V.I.S — Synology NAS status (read-only)
#
#   Talks to your NAS's own DSM Web API directly over HTTPS: a session
#   login (SYNO.API.Auth), then SYNO.Core.System.Utilization for CPU/
#   memory load and SYNO.Storage.CGI.Storage for volume/disk usage, then
#   logout. Read-only by design -- only status/monitoring endpoints are
#   ever called here, never anything that reboots, shuts down, or
#   changes NAS configuration, matching the "narrow, named capability"
#   philosophy of every other integration in this project. There is
#   deliberately no NAS power-control tool.
#
#   NOTE ON RELIABILITY: this was written against Synology's documented
#   Web API shape, not verified against a live DS720+ (this environment
#   has no network path to your NAS). If get_status() raises with a DSM
#   error code or an unexpected-shape message, the raw JSON is included
#   in the error -- that's enough for a quick one-line field-name fix,
#   ask Jarvis to self_improve("fix the Synology status field parsing,
#   here's the error: ...") or paste it back for a fix.
#
#   Reachability is just a host:port -- point SYNOLOGY_HOST at either the
#   NAS's LAN IP (simplest, same-network only) or its Tailscale IP
#   (100.x.x.x) if this PC and the NAS are both on your tailnet and you'd
#   rather not expose DSM on the LAN, or want this to keep working from
#   off-network. No Tailscale-specific code needed either way -- this is
#   just an HTTPS request to whatever host is configured; Tailscale is
#   invisible at this layer as long as tailscaled is running.
#
#   Fully inert (SYNOLOGY_AVAILABLE=False) until SYNOLOGY_HOST/
#   SYNOLOGY_USER/SYNOLOGY_PASSWORD are set in .env -- see README's
#   "Homelab" section. Strongly recommended: create a DEDICATED,
#   LOW-PRIVILEGE DSM user for this (Control Panel > User & Group >
#   Create, no admin group membership needed for read-only status),
#   rather than using your admin account -- least privilege matters if
#   this .env ever leaks, even though Jarvis only ever calls read-only
#   APIs with it.
# ================================================================
import os
import requests

HOST = os.environ.get("SYNOLOGY_HOST", "").strip()
PORT = os.environ.get("SYNOLOGY_PORT", "5001").strip() or "5001"
USER = os.environ.get("SYNOLOGY_USER", "").strip()
PASSWORD = os.environ.get("SYNOLOGY_PASSWORD", "")

SYNOLOGY_AVAILABLE = bool(HOST and USER and PASSWORD)

_BASE = f"https://{HOST}:{PORT}/webapi" if HOST else ""
# DSM's default cert is self-signed -- verify=False is deliberate here (this
# only ever talks to a host you explicitly configured yourself in .env),
# not an oversight.
_VERIFY_TLS = False
_TIMEOUT = 10


def _login():
    r = requests.get(f"{_BASE}/auth.cgi", params={
        "api": "SYNO.API.Auth", "version": 6, "method": "login",
        "account": USER, "passwd": PASSWORD, "session": "jarvis", "format": "sid",
    }, timeout=_TIMEOUT, verify=_VERIFY_TLS)
    r.raise_for_status()
    data = r.json()
    if not data.get("success"):
        code = (data.get("error") or {}).get("code")
        raise RuntimeError(f"DSM login failed (error code {code}) -- check SYNOLOGY_USER/PASSWORD in .env.")
    return data["data"]["sid"]


def _logout(sid):
    try:
        requests.get(f"{_BASE}/auth.cgi", params={
            "api": "SYNO.API.Auth", "version": 6, "method": "logout",
            "session": "jarvis", "_sid": sid,
        }, timeout=5, verify=_VERIFY_TLS)
    except Exception:
        pass  # best-effort -- the session times out on its own regardless


def get_status():
    """One-shot: log in, pull CPU/memory utilization + volume storage, log
    out. Returns {"cpu_pct","mem_used_pct","mem_total_mb","volumes":
    [{"id","status","total_bytes","used_bytes","used_pct"}]}. Raises
    RuntimeError (with the raw DSM response folded in where possible) on
    any failure -- tools.py turns that into a friendly spoken reply, the
    local HTTP API turns it into a JSON error the widget can show."""
    if not SYNOLOGY_AVAILABLE:
        raise RuntimeError("Synology integration isn't configured (SYNOLOGY_HOST/USER/PASSWORD missing in .env).")

    sid = _login()
    try:
        util_resp = requests.get(f"{_BASE}/entry.cgi", params={
            "api": "SYNO.Core.System.Utilization", "version": 1, "method": "get", "_sid": sid,
        }, timeout=_TIMEOUT, verify=_VERIFY_TLS)
        storage_resp = requests.get(f"{_BASE}/entry.cgi", params={
            "api": "SYNO.Storage.CGI.Storage", "version": 1, "method": "load", "_sid": sid,
        }, timeout=_TIMEOUT, verify=_VERIFY_TLS)
    finally:
        _logout(sid)

    util = util_resp.json()
    if not util.get("success"):
        raise RuntimeError(f"Couldn't read NAS utilization -- DSM said: {util}")

    u = util.get("data", {}) or {}
    cpu = u.get("cpu", {}) or {}
    mem = u.get("memory", {}) or {}
    # Verified live against a real DS720+: user_load/system_load can sit at 0
    # while the NAS is genuinely busy (that load shows up in other_load
    # instead), so summing just those two badly underreports actual load.
    # 1min_load is DSM's own "current CPU load" figure (what Resource
    # Monitor's headline percentage tracks) -- use that directly instead.
    cpu_pct = cpu.get("1min_load", 0) or 0

    volumes = []
    storage = storage_resp.json()
    if storage.get("success"):
        for v in (storage.get("data", {}) or {}).get("volumes", []) or []:
            size = v.get("size", {}) or {}
            try:
                total = int(size.get("total", 0) or 0)
                used = int(size.get("used", 0) or 0)
            except (TypeError, ValueError):
                total = used = 0
            volumes.append({
                "id": v.get("id", "volume"),
                "status": v.get("status", "unknown"),
                "total_bytes": total, "used_bytes": used,
                "used_pct": round(used / total * 100, 1) if total else None,
            })
    # A storage-API failure doesn't invalidate the whole status -- CPU/
    # memory is still useful on its own -- so it's left as an empty list
    # rather than raising, with the raw response available for debugging.
    elif os.environ.get("JARVIS_DEBUG"):
        print(f"  [Synology] Storage API call failed: {storage}")

    # DSM reports memory_size in KB (verified live: 2097152 -> a DS720+'s
    # real 2GB base RAM), not MB -- convert here so callers get a sane unit.
    mem_size_kb = mem.get("memory_size")
    return {
        "cpu_pct": round(cpu_pct, 1),
        "mem_used_pct": mem.get("real_usage"),
        "mem_total_mb": round(mem_size_kb / 1024, 0) if mem_size_kb else None,
        "volumes": volumes,
    }
