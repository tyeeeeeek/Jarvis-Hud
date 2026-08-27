# ================================================================
#   J.A.R.V.I.S — Tailscale integration
#
#   Wraps the `tailscale` CLI already installed on this machine (and
#   on the rest of the tailnet: phone, NAS, other PCs) -- narrow, named
#   functions only, same as everything else in this project. Never a
#   generic "run any tailscale/tailscale-adjacent command" tool.
#
#   Read-only operations (status, ping) need no special permission --
#   verified live, they just work. State-changing ones (exit-node
#   switching, connect/disconnect) need Tailscale's own "operator"
#   grant first -- by default only root can change a node's state, and
#   this project never runs as root. This is Tailscale's supported,
#   documented mechanism for letting a normal user manage the local
#   node without sudo (NOT a workaround/hack): run once, on this
#   machine, in a real terminal:
#       sudo tailscale set --operator=<your-linux-username>
#   Every state-changing function below detects the "access denied"
#   case (rather than crashing) and returns that exact command in its
#   error message so the fix is always one copy-paste away.
#
#   set_exit_node only ever accepts a peer already reported by
#   `tailscale status` itself (or the sentinel to turn it off) --
#   never arbitrary text -- consistent with the curated-list pattern
#   used for launch_app/close_app elsewhere in this project.
# ================================================================
import json
import shutil
import subprocess

TAILSCALE_BIN = shutil.which("tailscale")
TAILSCALE_AVAILABLE = bool(TAILSCALE_BIN)

_OPERATOR_HINT = (
    "run `sudo tailscale set --operator=<your-username>` once in a terminal on this "
    "machine to let me manage Tailscale without sudo -- that's Tailscale's own supported "
    "mechanism, not a workaround."
)


def _run(args, timeout=15):
    return subprocess.run([TAILSCALE_BIN] + args, capture_output=True, text=True, timeout=timeout)


def _is_access_denied(result):
    return result.returncode != 0 and "access denied" in (result.stderr or result.stdout or "").lower()


def get_status():
    """Full tailnet picture: this device's own Tailscale IP/connection
    state, plus every other device on the tailnet (name, Tailscale IP, OS,
    online/offline, whether it's usable as an exit node). Read-only, no
    special permission needed. Raises RuntimeError with a friendly message
    on failure."""
    if not TAILSCALE_AVAILABLE:
        raise RuntimeError("Tailscale isn't installed on this machine.")
    result = _run(["status", "--json"])
    if result.returncode != 0:
        raise RuntimeError(f"Couldn't reach the Tailscale daemon: {result.stderr.strip() or result.stdout.strip()}")
    data = json.loads(result.stdout)

    self_node = data.get("Self", {}) or {}
    peers = []
    for p in (data.get("Peer") or {}).values():
        peers.append({
            "name": p.get("HostName", "unknown"),
            "ip": (p.get("TailscaleIPs") or [None])[0],
            "os": p.get("OS", "unknown"),
            "online": bool(p.get("Online")),
            "exit_node_option": bool(p.get("ExitNodeOption")),
            "is_exit_node": bool(p.get("ExitNode")),
        })
    peers.sort(key=lambda d: d["name"].lower())

    return {
        "backend_state": data.get("BackendState", "unknown"),
        "self_name": self_node.get("HostName", "unknown"),
        "self_ip": (self_node.get("TailscaleIPs") or [None])[0],
        "using_exit_node": bool(self_node.get("ExitNode")),
        "peers": peers,
    }


def ping(device: str):
    """Real connectivity check to a named tailnet device (one ICMP-ish
    Tailscale ping, ~1-3s). Read-only. Returns {"ok", "detail"} -- ok=False
    with the raw tailscale output as detail if the device is unreachable,
    never raises for that case (only for tailscale itself being
    unavailable)."""
    if not TAILSCALE_AVAILABLE:
        raise RuntimeError("Tailscale isn't installed on this machine.")
    device = (device or "").strip()
    if not device:
        return {"ok": False, "detail": "No device given."}
    try:
        result = _run(["ping", "--c", "1", device], timeout=10)
    except subprocess.TimeoutExpired:
        return {"ok": False, "detail": "Timed out."}
    output = (result.stdout or result.stderr or "").strip()
    return {"ok": result.returncode == 0 and "pong" in output.lower(), "detail": output}


def set_exit_node(device: str):
    """Route this machine's internet traffic through a named tailnet peer
    (must already be reported as exit_node_option=True by get_status), or
    turn exit-node routing off (device="" / "off" / "none"). Validates
    against the *current* peer list before running anything -- never
    passes arbitrary text to the tailscale CLI. Needs the one-time
    operator grant (see module docstring) or returns a friendly error
    naming the exact fix."""
    if not TAILSCALE_AVAILABLE:
        raise RuntimeError("Tailscale isn't installed on this machine.")
    device = (device or "").strip()

    if device.lower() in ("", "off", "none", "disable"):
        result = _run(["set", "--exit-node="])
        if result.returncode != 0:
            if _is_access_denied(result):
                raise RuntimeError(f"I need permission first -- {_OPERATOR_HINT}")
            raise RuntimeError((result.stderr or result.stdout).strip())
        return {"ok": True, "message": "Exit node turned off."}

    status = get_status()
    match = next((p for p in status["peers"] if p["name"].lower() == device.lower()), None)
    if not match:
        known = ", ".join(p["name"] for p in status["peers"]) or "none found"
        raise RuntimeError(f"'{device}' isn't a device I see on your tailnet. Known devices: {known}.")
    if not match["exit_node_option"]:
        raise RuntimeError(f"{match['name']} isn't advertised as an exit node, so I can't route through it.")

    result = _run(["set", f"--exit-node={match['name']}"])
    if result.returncode != 0:
        if _is_access_denied(result):
            raise RuntimeError(f"I need permission first -- {_OPERATOR_HINT}")
        raise RuntimeError((result.stderr or result.stdout).strip())
    return {"ok": True, "message": f"Routing through {match['name']} now."}


def connect():
    """Bring this machine's Tailscale connection up. Needs the one-time
    operator grant (see module docstring) or returns a friendly error
    naming the exact fix."""
    if not TAILSCALE_AVAILABLE:
        raise RuntimeError("Tailscale isn't installed on this machine.")
    result = _run(["up"], timeout=30)
    if result.returncode != 0:
        if _is_access_denied(result):
            raise RuntimeError(f"I need permission first -- {_OPERATOR_HINT}")
        raise RuntimeError((result.stderr or result.stdout).strip())
    return {"ok": True}


def disconnect():
    """Take this machine off the tailnet. Needs the one-time operator
    grant (see module docstring) or returns a friendly error naming the
    exact fix. Note: if you rely on Tailscale to reach this machine
    remotely, disconnecting cuts that off until you reconnect locally or
    ask Jarvis to reconnect (voice/Telegram both stop working over
    Tailscale specifically, though LAN access is unaffected)."""
    if not TAILSCALE_AVAILABLE:
        raise RuntimeError("Tailscale isn't installed on this machine.")
    result = _run(["down"], timeout=15)
    if result.returncode != 0:
        if _is_access_denied(result):
            raise RuntimeError(f"I need permission first -- {_OPERATOR_HINT}")
        raise RuntimeError((result.stderr or result.stdout).strip())
    return {"ok": True}
