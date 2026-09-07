# ================================================================
#   J.A.R.V.I.S — Fleet (remote Tailscale device control)
#
#   Lets Jarvis reach INTO the other machines on the tailnet, not just
#   report their online/offline status (see tailscale_service.py). Same
#   curated-list philosophy as run_diagnostic_command in tools.py: every
#   device exposes a fixed, named set of actions -- never a free-text
#   remote shell, never an arbitrary command string from the caller.
#
#   Two transports, one per platform:
#
#     - "tailscale-ssh": Tailscale's own zero-config SSH server (identity
#       based auth via the tailnet itself, no keys to manage) -- used for
#       TyeStore (Synology/Linux). One-time setup on that device:
#           tailscale set --ssh
#
#     - "openssh": a normal OpenSSH server reached over the Tailscale
#       network, authenticated with a dedicated keypair
#       (~/.jarvis/ssh/fleet_key) -- used for the Windows boxes, since
#       Tailscale's own SSH server isn't reliably available on Windows
#       yet. One-time setup on each Windows device:
#         1. Settings > Apps > Optional Features > Add > "OpenSSH Server"
#            (or: Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0)
#         2. Start the service: Start-Service sshd ; Set-Service -Name sshd -StartupType Automatic
#         3. Add ~/.jarvis/ssh/fleet_key.pub to that Windows user's
#            C:\Users\<user>\.ssh\authorized_keys (or, for an admin
#            account, C:\ProgramData\ssh\administrators_authorized_keys)
#         4. Set FLEET_<DEVICE>_USER in .env to the Windows username.
#
#   Every action call is a real subprocess with a timeout -- nothing here
#   ever hangs Jarvis's brain waiting on a dead machine.
# ================================================================
import os
import subprocess

FLEET_KEY_PATH = os.path.expanduser("~/.jarvis/ssh/fleet_key")

# Windows SSH usernames -- device-specific since they can differ. Blank
# until set in .env; devices needing OpenSSH just report "not configured"
# until then rather than guessing a username.
_TYEWINPC1_USER = os.environ.get("FLEET_TYEWINPC1_USER", "").strip()
_TYEPC_USER = os.environ.get("FLEET_TYEPC_USER", "").strip()
_TYEWINTABLET_USER = os.environ.get("FLEET_TYEWINTABLET_USER", "").strip()

FLEET_DEVICES = {
    "tyestore": {
        "host": "tyestore.tail8bdaa1.ts.net", "os": "linux",
        "transport": "tailscale-ssh", "user": None,
    },
    "tyewinpc1": {
        "host": "tyewinpc1.tail8bdaa1.ts.net", "os": "windows",
        "transport": "openssh", "user": _TYEWINPC1_USER,
    },
    "tyepc": {
        "host": "tyepc.tail8bdaa1.ts.net", "os": "windows",
        "transport": "openssh", "user": _TYEPC_USER,
    },
    "tyewintablet": {
        "host": "tyewintablet.tail8bdaa1.ts.net", "os": "windows",
        "transport": "openssh", "user": _TYEWINTABLET_USER,
    },
}

# Curated actions, per OS -- the only commands that will ever run on a
# fleet device. Add to these tables (never accept a caller-supplied
# command string) to extend what Jarvis can do out there.
_ACTIONS_LINUX = {
    "status": "uptime && echo --- && df -h / && echo --- && free -h",
}
_ACTIONS_WINDOWS = {
    "status": (
        'powershell -NoProfile -Command '
        '"Get-CimInstance Win32_OperatingSystem | '
        'Select-Object @{N=\'UptimeHours\';E={[math]::Round(((Get-Date)-$_.LastBootUpTime).TotalHours,1)}}, '
        '@{N=\'FreeMemMB\';E={[math]::Round($_.FreePhysicalMemory/1024)}}, '
        '@{N=\'TotalMemMB\';E={[math]::Round($_.TotalVisibleMemorySize/1024)}} | Format-List; '
        'Get-PSDrive C | Select-Object Used,Free | Format-List"'
    ),
    # Disruptive -- gated behind JarvisAdmin approval in tools.py's wrapper,
    # not here (this module stays purely mechanical, same split as
    # system_power/run_admin_action for THIS machine).
    "restart": "shutdown /r /t 60 /c \"Restart requested via Jarvis\"",
    "cancel_restart": "shutdown /a",
}

_ACTIONS_BY_OS = {"linux": _ACTIONS_LINUX, "windows": _ACTIONS_WINDOWS}


def list_devices():
    """Every known fleet device with its OS and the actions available on
    it -- used to build helpful "I can run: ..." error messages and for
    the HUD's Fleet view."""
    out = []
    for name, dev in FLEET_DEVICES.items():
        out.append({
            "name": name,
            "os": dev["os"],
            "transport": dev["transport"],
            "configured": dev["transport"] == "tailscale-ssh" or bool(dev["user"]),
            "actions": sorted(_ACTIONS_BY_OS[dev["os"]]),
        })
    return out


def run_action(device: str, action: str, timeout: int = 20):
    """Runs one curated action (see _ACTIONS_LINUX/_ACTIONS_WINDOWS) on a
    named fleet device over SSH. Returns {"ok": True, "output": str} on
    success. Raises RuntimeError with a specific, friendly reason on any
    failure (unknown device, action not offered on that device's OS, not
    configured yet, unreachable, non-zero exit) -- never raises a bare
    subprocess/OSError up to the caller."""
    device = (device or "").strip().lower()
    dev = FLEET_DEVICES.get(device)
    if not dev:
        raise RuntimeError(f"'{device}' isn't a known fleet device. Known devices: {', '.join(FLEET_DEVICES)}.")

    actions = _ACTIONS_BY_OS[dev["os"]]
    action = (action or "").strip().lower()
    cmd_str = actions.get(action)
    if not cmd_str:
        raise RuntimeError(f"'{action}' isn't offered on {device}. Available there: {', '.join(sorted(actions))}.")

    if dev["transport"] == "tailscale-ssh":
        cmd = ["tailscale", "ssh", dev["host"], cmd_str]
    else:
        if not dev["user"]:
            raise RuntimeError(
                f"{device} isn't configured yet -- set FLEET_{device.upper()}_USER in .env "
                f"to the Windows username, once OpenSSH Server is enabled on it."
            )
        if not os.path.isfile(FLEET_KEY_PATH):
            raise RuntimeError("Jarvis's fleet SSH key is missing (expected ~/.jarvis/ssh/fleet_key).")
        cmd = [
            "ssh", "-i", FLEET_KEY_PATH,
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=8",
            "-o", "BatchMode=yes",
            f"{dev['user']}@{dev['host']}", cmd_str,
        ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{device} didn't respond in time -- it may be offline or unreachable.")
    except FileNotFoundError:
        raise RuntimeError(f"'{cmd[0]}' isn't available on this machine.")

    output = (result.stdout or result.stderr or "").strip()
    if result.returncode != 0:
        raise RuntimeError(f"That failed on {device} (exit {result.returncode}): {output[:300] or 'no output'}")
    return {"ok": True, "output": output}
