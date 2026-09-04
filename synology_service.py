# ================================================================
#   J.A.R.V.I.S — Synology NAS status + file sync
#
#   Talks to your NAS's own DSM Web API directly over HTTPS: a session
#   login (SYNO.API.Auth), then either the read-only status calls
#   (SYNO.Core.System.Utilization, SYNO.Storage.CGI.Storage) or the
#   FileStation file-transfer calls below, then logout. There is
#   deliberately no NAS power-control tool, and no tool that can reboot,
#   shut down, or reconfigure DSM.
#
#   The file-transfer half (list_folder/upload_folder/download_folder) is
#   the one place in this module that *writes* to the NAS. It's still kept
#   narrow, matching the "narrow, named capability" philosophy of every
#   other integration in this project: every operation is jailed under a
#   single configured NAS folder (SYNOLOGY_BASE_PATH) via
#   _resolve_nas_subpath, exactly like tools.py's _SAFE_DIRS jails local
#   filesystem tools to a fixed set of folders -- there is no way to reach
#   an arbitrary path on the NAS from here.
#
#   NOTE ON RELIABILITY: get_status() and the FileStation calls
#   (list_folder/upload_folder/download_folder) have all been verified live
#   against a real DS720+ over Tailscale (upload_folder needed one fix:
#   _sid must be a URL query param on the multipart POST, not a form field,
#   or DSM returns error 119 "SID not found"). If something still errors
#   with a DSM error code or an unexpected-shape message, the raw JSON is
#   included in the error -- that's enough for a quick one-line fix, ask
#   Jarvis to self_improve("fix the Synology X, here's the error: ...") or
#   paste it back for a fix.
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
import re
import requests

HOST = os.environ.get("SYNOLOGY_HOST", "").strip()
PORT = os.environ.get("SYNOLOGY_PORT", "5001").strip() or "5001"
USER = os.environ.get("SYNOLOGY_USER", "").strip()
PASSWORD = os.environ.get("SYNOLOGY_PASSWORD", "")
# The NAS-side jail root for every file-transfer call below -- see the
# module docstring. Defaults to a dedicated shared folder so this never
# needs to touch anything else already on the NAS.
BASE_PATH = (os.environ.get("SYNOLOGY_BASE_PATH", "").strip() or "/JarvisSync").rstrip("/")

SYNOLOGY_AVAILABLE = bool(HOST and USER and PASSWORD)

_BASE = f"https://{HOST}:{PORT}/webapi" if HOST else ""
# DSM's default cert is self-signed -- verify=False is deliberate here (this
# only ever talks to a host you explicitly configured yourself in .env),
# not an oversight.
_VERIFY_TLS = False
_TIMEOUT = 10


def _login():
    # POST with the credentials in the form body, not GET with them in the
    # query string -- DSM's Web API accepts either identically, but a GET
    # request's full URL (password included) ends up in a lot of places
    # that never should have seen it: requests/urllib3 exception messages
    # (e.g. a connection-timeout error embeds the full URL verbatim), any
    # log line that prints str(exception), proxy/firewall access logs
    # between here and the NAS, etc. A POST body isn't logged by any of
    # those. Caught live in this project: a real NAS connection timeout
    # put the account password in plaintext into this service's own error
    # message, which then flowed into the phone dashboard's /api/status
    # JSON response and the systemd journal.
    r = requests.post(f"{_BASE}/auth.cgi", data={
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
    # POST for the same reason as _login() -- sid is a live session token,
    # lower-stakes than the account password but still not something that
    # belongs in a URL that might end up in a log line.
    try:
        requests.post(f"{_BASE}/auth.cgi", data={
            "api": "SYNO.API.Auth", "version": 6, "method": "logout",
            "session": "jarvis", "_sid": sid,
        }, timeout=5, verify=_VERIFY_TLS)
    except Exception:
        pass  # best-effort -- the session times out on its own regardless


def get_status():
    """One-shot: log in, pull CPU/memory/network utilization + volume AND
    per-disk storage + system identity, log out. Returns {"cpu_pct",
    "mem_used_pct","mem_total_mb","volumes":[{"id","status","total_bytes",
    "used_bytes","used_pct"}],"disks":[{"id","model","status","temp_c",
    "smart_status"}],"network":[{"device","rx_bps","tx_bps"}],"model",
    "dsm_version","serial","temp_c","uptime"}. Raises RuntimeError (with the
    raw DSM response folded in where possible) on any failure -- tools.py
    turns that into a friendly spoken reply, the local HTTP API turns it
    into a JSON error the widget can show.

    The per-disk and network fields below read from the SAME two API
    responses get_status() already fetches (SYNO.Storage.CGI.Storage's
    response also includes a top-level "disks" array alongside "volumes";
    SYNO.Core.System.Utilization's response also includes "network"
    alongside "cpu"/"memory") -- no extra round-trip needed, this was
    already coming back and just wasn't being read. SYNO.Core.System is the
    one genuinely new call, for model/serial/version/uptime/temperature.
    Field names for disks/system are DSM's documented Web API shape but
    haven't been verified live against every DSM version/model the way
    cpu_pct/mem_used_pct/volumes have -- every field here is read with
    .get() and defaults to None rather than raising, so a DSM version that
    renames or omits one just shows "–" in the widget instead of breaking
    the whole call."""
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
        system_resp = requests.get(f"{_BASE}/entry.cgi", params={
            "api": "SYNO.Core.System", "version": 1, "method": "info", "_sid": sid,
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

    # Per-interface throughput, same response as cpu/memory above. DSM
    # includes a synthetic "total" aggregate row alongside real interfaces
    # (eth0, ...) -- skip it so callers only see actual physical NICs.
    network = []
    for n in u.get("network", []) or []:
        device = n.get("device", "")
        if not device or device == "total":
            continue
        try:
            network.append({
                "device": device,
                "rx_bps": int(n.get("rx", 0) or 0),
                "tx_bps": int(n.get("tx", 0) or 0),
            })
        except (TypeError, ValueError):
            pass

    volumes = []
    disks = []
    storage = storage_resp.json()
    if storage.get("success"):
        sdata = storage.get("data", {}) or {}
        for v in sdata.get("volumes", []) or []:
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
        # Physical disks -- same "load" response, a sibling array to
        # "volumes" above (a volume can span multiple disks in a RAID).
        for d in sdata.get("disks", []) or []:
            temp = d.get("temp")
            disks.append({
                "id": d.get("id", "disk"),
                "model": (d.get("model") or "").strip() or None,
                "status": d.get("status", "unknown"),
                "temp_c": temp if isinstance(temp, (int, float)) else None,
                "smart_status": d.get("smart_status") or d.get("health_status") or None,
            })
    # A storage-API failure doesn't invalidate the whole status -- CPU/
    # memory is still useful on its own -- so it's left as an empty list
    # rather than raising, with the raw response available for debugging.
    elif os.environ.get("JARVIS_DEBUG"):
        print(f"  [Synology] Storage API call failed: {storage}")

    system = {"model": None, "dsm_version": None, "serial": None, "temp_c": None, "uptime": None}
    sysinfo = system_resp.json()
    if sysinfo.get("success"):
        sd = sysinfo.get("data", {}) or {}
        temp = sd.get("temperature")
        system = {
            "model": sd.get("model"),
            "dsm_version": sd.get("version_string") or sd.get("firmware_ver"),
            "serial": sd.get("serial"),
            "temp_c": temp if isinstance(temp, (int, float)) else None,
            # up_time's exact shape (seconds vs. a formatted string) varies
            # by DSM version -- passed through as-is rather than guessing a
            # parse, so the widget just displays whatever DSM actually sent.
            "uptime": sd.get("up_time"),
        }
    elif os.environ.get("JARVIS_DEBUG"):
        print(f"  [Synology] System info API call failed: {sysinfo}")

    # DSM reports memory_size in KB (verified live: 2097152 -> a DS720+'s
    # real 2GB base RAM), not MB -- convert here so callers get a sane unit.
    mem_size_kb = mem.get("memory_size")
    return {
        "cpu_pct": round(cpu_pct, 1),
        "mem_used_pct": mem.get("real_usage"),
        "mem_total_mb": round(mem_size_kb / 1024, 0) if mem_size_kb else None,
        "volumes": volumes,
        "disks": disks,
        "network": network,
        **system,
    }


# ---- File sync (FileStation) -------------------------------------------
def _resolve_nas_subpath(subfolder):
    """Sanitize a user-supplied subfolder name into a path confined under
    BASE_PATH -- the NAS-side equivalent of tools.py's _resolve_safe_path.
    Strips any leading/trailing slashes and drops '..' components outright,
    so this can never resolve outside BASE_PATH regardless of input."""
    clean = (subfolder or "").strip().strip("/\\")
    parts = [p for p in re.split(r"[\\/]+", clean) if p and p not in (".", "..")]
    return BASE_PATH if not parts else BASE_PATH + "/" + "/".join(parts)


def _fs_list(sid, folder_path):
    """One FileStation List call. Returns [] if the folder doesn't exist
    yet on the NAS (DSM error 408), raises RuntimeError on any other
    failure."""
    r = requests.get(f"{_BASE}/entry.cgi", params={
        "api": "SYNO.FileStation.List", "version": 2, "method": "list",
        "folder_path": folder_path, "additional": '["size"]', "_sid": sid,
    }, timeout=_TIMEOUT, verify=_VERIFY_TLS)
    data = r.json()
    if not data.get("success"):
        code = (data.get("error") or {}).get("code")
        if code == 408:
            return []
        raise RuntimeError(f"Couldn't list {folder_path} on the NAS -- DSM said: {data}")
    return (data.get("data", {}) or {}).get("files", []) or []


def list_folder(subfolder=""):
    """List the immediate contents of a folder under BASE_PATH on the NAS.
    Returns [{"name","is_dir","size_bytes"}]. Raises RuntimeError if
    unreachable or not configured."""
    if not SYNOLOGY_AVAILABLE:
        raise RuntimeError("Synology integration isn't configured (SYNOLOGY_HOST/USER/PASSWORD missing in .env).")
    path = _resolve_nas_subpath(subfolder)
    sid = _login()
    try:
        items = _fs_list(sid, path)
    finally:
        _logout(sid)
    return [
        {"name": i.get("name"), "is_dir": bool(i.get("isdir")),
         "size_bytes": (i.get("additional") or {}).get("size")}
        for i in items
    ]


def upload_folder(local_dir, subfolder):
    """Recursively upload every file under local_dir to `subfolder` on the
    NAS (relative to BASE_PATH), preserving the local folder structure,
    creating destination folders as needed and overwriting files that
    already exist there. Returns {"files_uploaded", "folder_path"}."""
    if not SYNOLOGY_AVAILABLE:
        raise RuntimeError("Synology integration isn't configured (SYNOLOGY_HOST/USER/PASSWORD missing in .env).")
    if not os.path.isdir(local_dir):
        raise RuntimeError(f"Local folder doesn't exist: {local_dir}")
    dest_root = _resolve_nas_subpath(subfolder)
    sid = _login()
    uploaded = 0
    try:
        for root, _dirs, files in os.walk(local_dir):
            rel = os.path.relpath(root, local_dir)
            dest_dir = dest_root if rel == "." else dest_root + "/" + rel.replace(os.sep, "/")
            for name in files:
                local_path = os.path.join(root, name)
                with open(local_path, "rb") as fh:
                    # Verified live against a real DSM 7 instance: _sid must be a
                    # query param here, not a multipart form field -- passing it
                    # as `data` alongside the file part gets back error 119
                    # ("SID not found") even though the exact same session sid
                    # works fine as a `data`/`params` field on every other call.
                    r = requests.post(f"{_BASE}/entry.cgi", params={"_sid": sid}, data={
                        "api": "SYNO.FileStation.Upload", "version": 2, "method": "upload",
                        "path": dest_dir, "create_parents": "true", "overwrite": "true",
                    }, files={"file": (name, fh)}, timeout=60, verify=_VERIFY_TLS)
                data = r.json()
                if not data.get("success"):
                    raise RuntimeError(f"Upload failed for {name} -- DSM said: {data}")
                uploaded += 1
    finally:
        _logout(sid)
    return {"files_uploaded": uploaded, "folder_path": dest_root}


def download_folder(subfolder, local_dir):
    """Recursively download every file from `subfolder` on the NAS
    (relative to BASE_PATH) into local_dir, preserving the NAS folder
    structure and overwriting local files with the same name. Returns
    {"files_downloaded", "folder_path"}."""
    if not SYNOLOGY_AVAILABLE:
        raise RuntimeError("Synology integration isn't configured (SYNOLOGY_HOST/USER/PASSWORD missing in .env).")
    remote_root = _resolve_nas_subpath(subfolder)
    os.makedirs(local_dir, exist_ok=True)
    sid = _login()
    downloaded = 0
    try:
        stack = [(remote_root, local_dir)]
        while stack:
            remote_path, local_path = stack.pop()
            for item in _fs_list(sid, remote_path):
                name = item.get("name")
                # Defensive: a file name containing a path separator or ".."
                # could otherwise escape local_dir when joined below --
                # never trusted even though it's this user's own NAS.
                if not name or "/" in name or "\\" in name or name in (".", ".."):
                    continue
                item_remote = f"{remote_path}/{name}"
                item_local = os.path.join(local_path, name)
                if item.get("isdir"):
                    os.makedirs(item_local, exist_ok=True)
                    stack.append((item_remote, item_local))
                    continue
                dl = requests.get(f"{_BASE}/entry.cgi", params={
                    "api": "SYNO.FileStation.Download", "version": 2, "method": "download",
                    "path": item_remote, "mode": "download", "_sid": sid,
                }, timeout=60, verify=_VERIFY_TLS, stream=True)
                dl.raise_for_status()
                with open(item_local, "wb") as out:
                    for chunk in dl.iter_content(chunk_size=65536):
                        out.write(chunk)
                downloaded += 1
    finally:
        _logout(sid)
    return {"files_downloaded": downloaded, "folder_path": remote_root}
