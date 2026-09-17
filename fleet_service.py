# ================================================================
#   J.A.R.V.I.S — Fleet (remote Tailscale device control)
#
#   Lets Jarvis reach INTO the other machines on the tailnet, not just
#   report their online/offline status (see tailscale_service.py). Same
#   curated-list philosophy as run_diagnostic_command in tools.py: every
#   device exposes a fixed, named set of actions -- never a free-text
#   remote shell, never an arbitrary command string from the caller.
#   File operations follow the same rule in spirit: browsing/reading is
#   unrestricted (read-only, can't hurt anything), but every WRITE
#   operation (delete/rename/mkdir/upload) is a separate, explicit,
#   narrow function here -- and every one of them requires a JarvisAdmin
#   Telegram approval before it runs, wired in tools.py's wrappers, not
#   here (this module stays purely mechanical, same split as
#   system_power/run_admin_action for THIS machine). Directory deletion
#   is deliberately NOT recursive -- only an already-empty directory can
#   be removed, so one wrong call can delete at most one file.
#
#   One transport for every device: "openssh" -- a normal OpenSSH server
#   reached over the Tailscale network, authenticated with a dedicated
#   keypair (~/.jarvis/ssh/fleet_key). A Synology NAS can't use Tailscale's
#   own zero-config SSH server for this -- Synology's own Tailscale package
#   explicitly disables it ("The Tailscale SSH server does not run on
#   Synology") -- so the "nas" slot below also uses a normal OpenSSH
#   server, same as every other slot.
#
#   There are four fixed device slots -- "nas" (Linux, e.g. a Synology NAS
#   via DSM) and "pc1"/"pc2"/"pc3" (Windows) -- each fully optional and
#   configured entirely through .env; see .env.example. Leave a slot's
#   *_HOST unset and it's simply never offered.
#
#   One-time setup per device:
#
#     nas (DSM):
#       1. DSM > Control Panel > Terminal & SNMP > Enable SSH service.
#       2. Add ~/.jarvis/ssh/fleet_key.pub to that DSM user's
#          ~/.ssh/authorized_keys (chmod 700 ~/.ssh, 600 the file).
#       3. Set FLEET_NAS_HOST (its tailnet hostname, e.g.
#          "mynas.tailxxxxx.ts.net") and FLEET_NAS_USER (its DSM username)
#          in .env.
#
#     Each Windows box (pc1, pc2, pc3):
#       1. Settings > Apps > Optional Features > Add > "OpenSSH Server"
#          (or: Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0)
#       2. Start-Service sshd ; Set-Service -Name sshd -StartupType Automatic
#       3. Add ~/.jarvis/ssh/fleet_key.pub to that Windows user's
#          C:\Users\<user>\.ssh\authorized_keys (or, for an admin
#          account, C:\ProgramData\ssh\administrators_authorized_keys --
#          Windows ignores the per-user file for admins).
#       4. Set FLEET_<SLOT>_HOST and FLEET_<SLOT>_USER in .env to that
#          machine's tailnet hostname and Windows username.
#
#   Every action call is a real subprocess/SFTP call with a timeout --
#   nothing here ever hangs Jarvis's brain waiting on a dead machine.
#
#   Wake-on-LAN (wake(), below) is the one action that deliberately does
#   NOT go over SSH -- the whole point is reaching a device that's fully
#   powered off, which has no SSH server to answer. Instead it broadcasts
#   a magic packet on this machine's own local network, which only
#   reaches a device that's on the same LAN (Tailscale can't deliver a
#   broadcast to a powered-off machine, so this needs Jarvis's host and
#   the target physically on the same network -- true for this homelab).
#
#   One-time setup per device, in addition to the SSH setup above:
#     1. BIOS/UEFI: enable "Wake on LAN" (may be named "Power On By PCI-E"/
#        "Deep Sleep Control" -- set Deep Sleep to disabled if present).
#        This is the one step that can only be done locally, in the BIOS
#        setup screen, not remotely.
#     2. Windows: Device Manager > the wired Ethernet adapter > Power
#        Management tab > check "Allow this device to wake the computer";
#        Advanced tab > "Wake on Magic Packet" = Enabled. Also worth
#        turning off Fast Startup (Control Panel > Power Options > Choose
#        what the power buttons do > uncheck "Turn on fast startup") --
#        it can prevent the NIC from listening for a magic packet after a
#        full shutdown on some systems.
#     3. Linux: `sudo ethtool -s <iface> wol g` (persist across reboots
#        with a NetworkManager connection setting or a systemd/udev rule,
#        since a plain ethtool call resets on the next boot).
#     4. Set FLEET_<SLOT>_MAC in .env to that NIC's MAC address (Windows:
#        `Get-NetAdapter`; Linux: `ip link`) -- must be the wired Ethernet
#        adapter's address, not Wi-Fi. Wi-Fi Wake-on-LAN support is rare
#        and unreliable (most USB/many built-in Wi-Fi adapters don't
#        implement it at all), so this is only expected to work reliably
#        over Ethernet.
# ================================================================
import ipaddress
import os
import re
import socket
import stat
import subprocess

import paramiko

import network_watch

FLEET_KEY_PATH = os.path.expanduser("~/.jarvis/ssh/fleet_key")

# Per-slot tailnet hostnames -- e.g. "mynas.tailxxxxx.ts.net" (Tailscale
# admin console > Machines, or `tailscale status` on the target). Blank
# until set in .env; a slot with no host is simply not offered.
_NAS_HOST = os.environ.get("FLEET_NAS_HOST", "").strip()
_PC1_HOST = os.environ.get("FLEET_PC1_HOST", "").strip()
_PC2_HOST = os.environ.get("FLEET_PC2_HOST", "").strip()
_PC3_HOST = os.environ.get("FLEET_PC3_HOST", "").strip()

# SSH usernames -- device-specific since they can differ. Blank until set
# in .env; devices needing OpenSSH just report "not configured" until
# then rather than guessing a username.
_NAS_USER = os.environ.get("FLEET_NAS_USER", "").strip()
_PC1_USER = os.environ.get("FLEET_PC1_USER", "").strip()
_PC2_USER = os.environ.get("FLEET_PC2_USER", "").strip()
_PC3_USER = os.environ.get("FLEET_PC3_USER", "").strip()

# Wake-on-LAN target MACs -- independent of the SSH username above (wake()
# never uses SSH), and independent per device since not every device needs
# it set up. Blank until set in .env; _normalize_mac rejects anything that
# isn't a real MAC address rather than silently sending a broken packet.
_MAC_RE = re.compile(r'^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$')


def _normalize_mac(raw: str):
    """Validates and lowercases a MAC address string; returns None (never
    raises) for blank/malformed input, so a typo'd .env value degrades to
    "not configured" instead of crashing at import time."""
    raw = (raw or "").strip()
    if not _MAC_RE.match(raw):
        return None
    return raw.replace("-", ":").lower()


_NAS_MAC = _normalize_mac(os.environ.get("FLEET_NAS_MAC", ""))
_PC1_MAC = _normalize_mac(os.environ.get("FLEET_PC1_MAC", ""))
_PC2_MAC = _normalize_mac(os.environ.get("FLEET_PC2_MAC", ""))
_PC3_MAC = _normalize_mac(os.environ.get("FLEET_PC3_MAC", ""))

FLEET_DEVICES = {
    "nas": {"host": _NAS_HOST, "os": "linux", "user": _NAS_USER, "mac": _NAS_MAC},
    "pc1": {"host": _PC1_HOST, "os": "windows", "user": _PC1_USER, "mac": _PC1_MAC},
    "pc2": {"host": _PC2_HOST, "os": "windows", "user": _PC2_USER, "mac": _PC2_MAC},
    "pc3": {"host": _PC3_HOST, "os": "windows", "user": _PC3_USER, "mac": _PC3_MAC},
}

# The only containers docker_restart will ever touch -- matches a typical
# self-hosted media stack. Never accepts an arbitrary container name; edit
# this list to match what actually runs on your "nas" slot.
DOCKER_CONTAINERS = ["radarr", "sonarr", "prowlarr", "jellyfin"]

# Curated actions, per OS -- the only commands that will ever run on a
# fleet device. Add to these tables (never accept a caller-supplied
# command string) to extend what Jarvis can do out there. "docker_restart"
# is handled specially in run_action (it's the one parameterized action,
# validated against DOCKER_CONTAINERS above).
_ACTIONS_LINUX = {
    "status": "uptime && echo --- && df -h / && echo --- && free -h",
    "processes": "ps aux --sort=-%cpu | head -11",
    # Full path, not bare "docker" -- non-interactive SSH sessions on Synology
    # DSM don't inherit the interactive shell's PATH, which is where the
    # ContainerManager package adds /usr/local/bin. "sudo -n" (no password
    # prompt) requires a matching NOPASSWD sudoers entry for the SSH user --
    # see PORT_TO_LINUX.md / setup notes for the exact line to add on your NAS.
    "docker_status": "sudo -n /usr/local/bin/docker ps --format 'table {{.Names}}\\t{{.Status}}\\t{{.Image}}'",
    "docker_restart": None,  # placeholder so it appears in the action list; real command built in run_action
    # Emergency network isolation for tools.trigger_lockdown() -- takes this
    # device off the tailnet entirely. Disruptive (Jarvis loses the ability
    # to reach it again over Tailscale until someone re-runs `tailscale up`
    # locally on the device) -- gated behind JarvisAdmin approval in
    # tools.py's wrapper, same as docker_restart. Requires a one-time
    # NOPASSWD sudoers rule for this exact command on that device (same
    # pattern as docker_status above) since "tailscale down" needs root.
    "isolate": "sudo -n tailscale down",
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
    "processes": (
        'powershell -NoProfile -Command '
        '"Get-Process | Sort-Object CPU -Descending | Select-Object -First 10 '
        'Name,Id,CPU,@{N=\'MemMB\';E={[math]::Round($_.WorkingSet/1MB)}} | Format-Table -AutoSize"'
    ),
    # Disruptive -- gated behind JarvisAdmin approval in tools.py's wrapper,
    # not here.
    "restart": "shutdown /r /t 60 /c \"Restart requested via Jarvis\"",
    "cancel_restart": "shutdown /a",
    # Emergency network isolation for tools.trigger_lockdown() -- see the
    # matching Linux entry above for the full rationale. Assumes the
    # Tailscale CLI is on PATH, which its Windows installer does by default.
    "isolate": 'powershell -NoProfile -Command "tailscale down"',
    # Software management via winget (built into Windows 10/11) -- read-only
    # list/search need no approval; install/uninstall/upgrade are disruptive
    # and gated behind JarvisAdmin approval in tools.py's wrapper, same as
    # docker_restart/restart above. All four are handled specially in
    # run_action (they take a caller-supplied package id/query, validated
    # against _WINGET_ID_RE/_WINGET_QUERY_RE below before it ever touches a
    # command string -- never accepted raw).
    "apps_installed": (
        'powershell -NoProfile -Command '
        '"winget list --accept-source-agreements | Out-String -Width 300"'
    ),
    "apps_search": None,
    "app_install": None,
    "app_uninstall": None,
    "app_upgrade": None,
}

_ACTIONS_BY_OS = {"linux": _ACTIONS_LINUX, "windows": _ACTIONS_WINDOWS}

# winget package IDs look like "Publisher.AppName" (e.g. "Mozilla.Firefox") --
# letters/digits/./_/+/- only, never anything a shell would treat specially.
# Search queries are free text but still locked to a narrow, safe charset --
# both are validated BEFORE being interpolated into a remote command string
# (fleet devices execute this over SSH as a single string, not an argv list,
# so this validation is what actually prevents command injection here, not
# just cosmetic).
_WINGET_ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.+-]{0,127}$')
_WINGET_QUERY_RE = re.compile(r'^[A-Za-z0-9 ._+-]{1,80}$')


def list_devices():
    """Every known fleet device with its OS and the actions available on
    it -- used to build helpful "I can run: ..." error messages and for
    the HUD/phone Fleet views."""
    out = []
    for name, dev in FLEET_DEVICES.items():
        out.append({
            "name": name,
            "os": dev["os"],
            "configured": bool(dev["user"] and dev["host"]),
            "actions": sorted(_ACTIONS_BY_OS[dev["os"]]),
        })
    return out


def _require_configured(device: str):
    """Shared preflight for every action below -- known device, has a
    username set, and the shared key actually exists. Returns the device
    entry. Raises RuntimeError with a specific, friendly reason otherwise."""
    device = (device or "").strip().lower()
    dev = FLEET_DEVICES.get(device)
    if not dev:
        raise RuntimeError(f"'{device}' isn't a known fleet device. Known devices: {', '.join(FLEET_DEVICES)}.")
    if not dev["user"] or not dev["host"]:
        raise RuntimeError(
            f"{device} isn't configured yet -- set FLEET_{device.upper()}_HOST and "
            f"FLEET_{device.upper()}_USER in .env (its tailnet hostname and SSH "
            f"username), once its OpenSSH server is enabled and "
            f"~/.jarvis/ssh/fleet_key.pub is in its authorized_keys."
        )
    if not os.path.isfile(FLEET_KEY_PATH):
        raise RuntimeError("Jarvis's fleet SSH key is missing (expected ~/.jarvis/ssh/fleet_key).")
    return dev


def _magic_packet(mac: str) -> bytes:
    """Builds a standard 102-byte Wake-on-LAN magic packet for one MAC
    address: 6 bytes of 0xFF, then the 6-byte MAC repeated 16 times."""
    mac_bytes = bytes.fromhex(mac.replace(":", ""))
    return b"\xff" * 6 + mac_bytes * 16


def wake(device: str):
    """Broadcasts a Wake-on-LAN magic packet to a named fleet device's
    configured MAC address, over UDP on this machine's local network --
    deliberately not SSH (see the module docstring: the target has no SSH
    server to answer while it's off). Fire-and-forget: sending the packet
    can't be confirmed to have actually woken the machine, only that it
    went out. Raises RuntimeError with a specific, friendly reason if the
    device is unknown or its MAC isn't configured."""
    device = (device or "").strip().lower()
    dev = FLEET_DEVICES.get(device)
    if not dev:
        raise RuntimeError(f"'{device}' isn't a known fleet device. Known devices: {', '.join(FLEET_DEVICES)}.")
    mac = dev.get("mac")
    if not mac:
        raise RuntimeError(
            f"{device}'s MAC address isn't configured for Wake-on-LAN -- set FLEET_{device.upper()}_MAC "
            f"in .env to its wired Ethernet adapter's MAC address, and make sure Wake-on-LAN is enabled "
            f"in its BIOS and NIC settings (see fleet_service.py's module docstring)."
        )

    targets = {"255.255.255.255"}
    subnet = network_watch._detect_subnet()
    if subnet:
        try:
            targets.add(str(ipaddress.ip_network(subnet).broadcast_address))
        except Exception:
            pass

    packet = _magic_packet(mac)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        for target in targets:
            try:
                sock.sendto(packet, (target, 9))
            except OSError:
                pass
    finally:
        sock.close()
    return {"ok": True, "mac": mac, "targets": sorted(targets)}


def run_action(device: str, action: str, arg: str = None, timeout: int = 20):
    """Runs one curated action (see _ACTIONS_LINUX/_ACTIONS_WINDOWS) on a
    named fleet device over SSH. `arg` is used by "docker_restart" (must be
    one of DOCKER_CONTAINERS), "apps_search" (a free-text query, validated
    against _WINGET_QUERY_RE), and "app_install"/"app_uninstall"/
    "app_upgrade" (a winget package id, validated against _WINGET_ID_RE) --
    ignored otherwise. Returns {"ok": True, "output": str} on success.
    Raises RuntimeError with a specific, friendly reason on any failure
    (unknown device, action not offered on that device's OS, not configured
    yet, unreachable, non-zero exit, invalid arg) -- never raises a bare
    subprocess/OSError up to the caller."""
    dev = _require_configured(device)
    device = (device or "").strip().lower()
    actions = _ACTIONS_BY_OS[dev["os"]]
    action = (action or "").strip().lower()
    if action not in actions:
        raise RuntimeError(f"'{action}' isn't offered on {device}. Available there: {', '.join(sorted(actions))}.")

    if action == "docker_restart":
        if dev["os"] != "linux":
            raise RuntimeError(f"docker_restart isn't offered on {device}.")
        arg = (arg or "").strip().lower()
        if arg not in DOCKER_CONTAINERS:
            raise RuntimeError(f"'{arg}' isn't a known container on {device}. Known: {', '.join(DOCKER_CONTAINERS)}.")
        cmd_str = f"sudo -n /usr/local/bin/docker restart {arg}"
    elif action == "apps_search":
        if dev["os"] != "windows":
            raise RuntimeError(f"apps_search isn't offered on {device}.")
        query = (arg or "").strip()
        if not _WINGET_QUERY_RE.match(query):
            raise RuntimeError("that search text isn't valid -- letters, numbers, spaces, and . _ + - only.")
        # winget's own output is plain text, one line per result -- capped
        # to the first 20 lines (header + ~18 results) so a broad query
        # (e.g. "firefox" matching every locale build) doesn't return a
        # multi-hundred-line dump that's unreadable on a phone. Captured
        # into $r first, THEN sliced -- piping Select-Object -First
        # directly onto winget's own output truncates its stdout mid-write,
        # which makes winget.exe itself exit non-zero (broken pipe) and
        # this whole call gets treated as a failure even though the output
        # was actually fine.
        cmd_str = (
            'powershell -NoProfile -Command '
            f"\"$r = winget search '{query}' --accept-source-agreements; "
            f'$r | Select-Object -First 20 | Out-String -Width 200"'
        )
    elif action in ("app_install", "app_uninstall", "app_upgrade"):
        if dev["os"] != "windows":
            raise RuntimeError(f"{action} isn't offered on {device}.")
        pkg_id = (arg or "").strip()
        if not _WINGET_ID_RE.match(pkg_id):
            raise RuntimeError("that isn't a valid winget package id -- letters, numbers, and . _ + - only, "
                                "e.g. 'Mozilla.Firefox' (use apps_search to find the exact id).")
        winget_verb = {"app_install": "install", "app_uninstall": "uninstall", "app_upgrade": "upgrade"}[action]
        agreements = " --accept-package-agreements --accept-source-agreements" if winget_verb != "uninstall" else ""
        cmd_str = (
            'powershell -NoProfile -Command '
            f'"winget {winget_verb} --id {pkg_id} -e --silent --disable-interactivity{agreements} '
            f'| Out-String -Width 300"'
        )
    else:
        cmd_str = actions[action]

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


def _parse_winget_top_match(search_output: str):
    """Picks the single best result out of a winget search's plain-text
    table (see run_action's "apps_search" -- winget has no machine-readable
    output for search, just this table). Columns are separated by 2+
    spaces; the Id column is always the second one and never contains a
    space (see _WINGET_ID_RE), which is what makes splitting on runs of
    spaces reliable even though the Name column can itself contain spaces.
    winget already sorts results by relevance, so the first data row (right
    after the header/divider) is the closest match. Returns (name, id) or
    None if the table couldn't be parsed / had no results."""
    lines = search_output.strip().splitlines()
    divider_idx = next((i for i, l in enumerate(lines) if l.strip(" -") == "" and "-" in l), None)
    if divider_idx is None or divider_idx + 1 >= len(lines):
        return None
    for line in lines[divider_idx + 1:]:
        parts = re.split(r"\s{2,}", line.strip())
        if len(parts) >= 2 and _WINGET_ID_RE.match(parts[1]):
            return parts[0], parts[1]
    return None


def resolve_app(device: str, query: str):
    """Finds the single closest-matching real app for a free-text name
    (e.g. "7zip" or "firefox") by running the same search apps_search uses
    and picking winget's own top result -- lets install/uninstall/upgrade
    (and the standalone "what's the exact id for X" lookup) work from a
    loose name instead of requiring the caller to already know the exact
    winget package id. Returns {"name": str, "id": str}. Raises
    RuntimeError (same friendly-message style as run_action) if nothing
    matched or the device/query is invalid."""
    result = run_action(device, "apps_search", arg=query, timeout=30)
    match = _parse_winget_top_match(result["output"])
    if not match:
        raise RuntimeError(f"No app matching '{query}' found on {device}")
    name, pkg_id = match
    return {"name": name, "id": pkg_id}


# ---------------------------------------------------------------- FILES
# SFTP over the same fleet_key -- Windows OpenSSH Server bundles an SFTP
# subsystem by default, same as any Linux/DSM OpenSSH server, so this
# works identically across every device type with no extra setup beyond
# what run_action already needs.

# Same trust store the plain `ssh` calls in run_action already rely on
# (via their own default ~/.ssh/known_hosts + StrictHostKeyChecking=
# accept-new) -- pointing paramiko at the identical file means a host key
# either path has already pinned is honored by the other, instead of two
# independent, silently-diverging trust stores for the same devices.
_KNOWN_HOSTS_PATH = os.path.expanduser("~/.ssh/known_hosts")


def _sftp_connect(device: str):
    """Opens one SFTP session to a fleet device over a real paramiko
    SSHClient (not a bare, unauthenticated-of-the-server Transport) so
    the server's host key is actually checked -- same trust-on-first-use
    model as run_action's plain `ssh` calls: an unseen host is accepted
    and its key persisted to ~/.ssh/known_hosts, but a host whose key has
    since CHANGED is rejected outright (paramiko raises before any
    credentials are sent) -- catches a spoofed/MITM'd hostname instead of
    silently trusting whatever answers there. Caller must close both
    returned objects when done (or use _sftp_session as a context manager
    instead of calling this directly)."""
    dev = _require_configured(device)
    client = paramiko.SSHClient()
    try:
        client.load_host_keys(_KNOWN_HOSTS_PATH)
    except IOError:
        pass  # no known_hosts yet -- fine, this connection will start one
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(dev["host"], username=dev["user"], key_filename=FLEET_KEY_PATH,
                        allow_agent=False, look_for_keys=False, timeout=10)
    except paramiko.BadHostKeyException as e:
        client.close()
        raise RuntimeError(
            f"{device}'s SSH host key doesn't match the one on record -- refusing to connect "
            f"(this is exactly what should happen if something were impersonating {device} on "
            f"the network). If you knowingly reinstalled/replaced that device, remove its old "
            f"entry from ~/.ssh/known_hosts first."
        ) from e
    except Exception as e:
        client.close()
        raise RuntimeError(f"Couldn't connect to {device}: {e}")
    try:
        client.save_host_keys(_KNOWN_HOSTS_PATH)
    except Exception:
        pass  # best-effort persistence; an in-memory pin still protects this session
    try:
        sftp = client.open_sftp()
    except Exception as e:
        client.close()
        raise RuntimeError(f"Couldn't start SFTP on {device}: {e}")
    return client, sftp


class _sftp_session:
    """`with _sftp_session(device) as sftp:` -- opens the connection,
    always closes it (even on error), and turns paramiko's own exception
    types into the same friendly RuntimeError shape everything else in
    this module uses."""
    def __init__(self, device):
        self.device = device

    def __enter__(self):
        self.client, self.sftp = _sftp_connect(self.device)
        return self.sftp

    def __exit__(self, exc_type, exc, tb):
        try:
            self.sftp.close()
        finally:
            self.client.close()
        if exc_type in (FileNotFoundError, IOError, OSError):
            raise RuntimeError(f"That path doesn't exist or isn't accessible on {self.device}: {exc}") from exc
        return False  # let any other exception propagate as-is


def list_dir(device: str, path: str):
    """Lists one directory's contents on a fleet device -- name, whether
    it's a directory, size in bytes, and last-modified time (unix
    seconds). Read-only. Raises RuntimeError on any failure (bad path,
    permission denied, device unreachable)."""
    path = (path or "").strip()
    if not path:
        dev = FLEET_DEVICES.get((device or "").strip().lower(), {})
        path = "/" if dev.get("os") == "linux" else "C:/"
    with _sftp_session(device) as sftp:
        entries = []
        for attr in sftp.listdir_attr(path):
            entries.append({
                "name": attr.filename,
                "is_dir": stat.S_ISDIR(attr.st_mode or 0),
                "size": attr.st_size or 0,
                "modified": attr.st_mtime or 0,
            })
        entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
        return entries


# Anything bigger than this refuses to download -- this goes straight
# into an HTTP response, not meant for multi-gigabyte files.
_MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024


def read_file(device: str, path: str):
    """Reads one whole file's bytes from a fleet device. Read-only.
    Refuses anything over 50MB (raises RuntimeError) rather than hanging
    a phone/HUD request on a huge transfer -- use the real SSH key
    yourself (~/.jarvis/ssh/fleet_key) for anything bigger."""
    path = (path or "").strip()
    if not path:
        raise RuntimeError("No path given.")
    with _sftp_session(device) as sftp:
        size = sftp.stat(path).st_size or 0
        if size > _MAX_DOWNLOAD_BYTES:
            raise RuntimeError(f"That file is {size // (1024*1024)}MB -- too big to download this way (50MB limit).")
        with sftp.open(path, "rb") as f:
            data = f.read()
        return data


def make_dir(device: str, path: str):
    """Creates one new (empty) directory on a fleet device. Fails if the
    parent doesn't exist or the directory already exists -- never creates
    intermediate parent directories, so this can't silently build out a
    whole tree from a typo'd path."""
    path = (path or "").strip()
    if not path:
        raise RuntimeError("No path given.")
    with _sftp_session(device) as sftp:
        sftp.mkdir(path)
    return {"ok": True}


def delete_path(device: str, path: str):
    """Deletes ONE file, or ONE already-empty directory, on a fleet
    device. Deliberately not recursive -- a non-empty directory fails to
    delete (with SFTP's own "directory not empty" error) rather than this
    function ever wiping out a whole tree in one call."""
    path = (path or "").strip()
    if not path:
        raise RuntimeError("No path given.")
    with _sftp_session(device) as sftp:
        is_dir = stat.S_ISDIR(sftp.stat(path).st_mode or 0)
        if is_dir:
            sftp.rmdir(path)
        else:
            sftp.remove(path)
    return {"ok": True}


def rename_path(device: str, src: str, dst: str):
    """Renames/moves one file or directory on a fleet device (same-device
    only -- this is a single SFTP rename, not a cross-device copy)."""
    src = (src or "").strip()
    dst = (dst or "").strip()
    if not src or not dst:
        raise RuntimeError("Need both a source and destination path.")
    with _sftp_session(device) as sftp:
        sftp.rename(src, dst)
    return {"ok": True}


# Anything bigger than this refuses to upload -- same reasoning as
# _MAX_DOWNLOAD_BYTES.
_MAX_UPLOAD_BYTES = 50 * 1024 * 1024


def write_file(device: str, path: str, data: bytes):
    """Writes bytes to a new or existing file on a fleet device (upload).
    Refuses anything over 50MB. Overwrites an existing file at that exact
    path -- callers should confirm that's intended before calling this
    (it's why tools.py's wrapper routes this through the same JarvisAdmin
    approval gate as delete/rename)."""
    path = (path or "").strip()
    if not path:
        raise RuntimeError("No path given.")
    if len(data) > _MAX_UPLOAD_BYTES:
        raise RuntimeError(f"That file is {len(data) // (1024*1024)}MB -- too big to upload this way (50MB limit).")
    with _sftp_session(device) as sftp:
        with sftp.open(path, "wb") as f:
            f.write(data)
    return {"ok": True}
