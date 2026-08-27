# ================================================================
#   J.A.R.V.I.S — LAN device discovery / new-device alerts
#
#   Router-agnostic by design: rather than depend on a particular
#   router's local API (many home routers, including ISP-provided
#   gateways like Xfinity's, expose none), this scans the LAN directly
#   from this PC -- an nmap ping sweep of the local subnet (auto-
#   detected from this machine's default route) populates the kernel's
#   neighbor/ARP table, which is then read back for IP+MAC pairs. Works
#   identically no matter what router/gateway/switch sits in front of it,
#   as long as this PC is on the same broadcast domain as the devices
#   being discovered (true for a plain switch too -- it doesn't segment
#   the LAN).
#
#   Known devices persist in ~/.jarvis/known_devices.json, keyed by MAC
#   (stable across DHCP renewals, unlike IP). Any MAC seen for the first
#   time triggers a best-effort Telegram alert through the same bridge
#   "Text Jarvis" already uses -- no new bot/token to configure.
#
#   Fully inert (NMAP_AVAILABLE=False) if nmap isn't installed --
#   `sudo apt install nmap` on this machine. Never touches the router
#   itself, never anything but a read-only ping sweep + reading the local
#   kernel's own neighbor table.
# ================================================================
import os
import re
import json
import time
import shutil
import ipaddress
import subprocess

JARVIS_DIR = os.path.join(os.path.expanduser("~"), ".jarvis")
KNOWN_DEVICES_PATH = os.path.join(JARVIS_DIR, "known_devices.json")

NMAP_BIN = shutil.which("nmap")
NMAP_AVAILABLE = bool(NMAP_BIN)

# Override if auto-detection guesses the wrong subnet (e.g. not a /24) --
# see .env.example / README "Homelab" section.
_SUBNET_OVERRIDE = os.environ.get("LAN_SUBNET_OVERRIDE", "").strip()

_MAC_RE = re.compile(r'^([0-9a-f]{2}:){5}[0-9a-f]{2}$')


def _ensure_jarvis_dir():
    os.makedirs(JARVIS_DIR, exist_ok=True)


def _detect_subnet():
    """Best-effort local subnet (as a CIDR string) from this machine's
    default-route interface -- works regardless of router brand since it
    reads this PC's own network config, not the router's. Returns None if
    detection fails for any reason (no default route, unexpected `ip`
    output, etc); callers fall back to asking for LAN_SUBNET_OVERRIDE."""
    if _SUBNET_OVERRIDE:
        return _SUBNET_OVERRIDE
    try:
        route = subprocess.run(["ip", "-o", "-4", "route", "show", "default"],
                                capture_output=True, text=True, timeout=5)
        m = re.search(r"dev (\S+)", route.stdout)
        if not m:
            return None
        iface = m.group(1)
        addr = subprocess.run(["ip", "-o", "-4", "addr", "show", iface],
                               capture_output=True, text=True, timeout=5)
        m2 = re.search(r"inet (\d+\.\d+\.\d+\.\d+/\d+)", addr.stdout)
        if not m2:
            return None
        return str(ipaddress.ip_interface(m2.group(1)).network)
    except Exception:
        return None


def _load_known():
    try:
        with open(KNOWN_DEVICES_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_known(data):
    _ensure_jarvis_dir()
    try:
        with open(KNOWN_DEVICES_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def _ping_sweep(subnet):
    """nmap -sn: a ping sweep only, never a port scan -- just enough to
    populate the kernel's neighbor table for every host that responds."""
    try:
        subprocess.run([NMAP_BIN, "-sn", "-T4", subnet],
                        capture_output=True, text=True, timeout=90)
    except Exception:
        pass


def _read_neighbors():
    """Read the kernel's neighbor/ARP table (just populated by the ping
    sweep above) for IPv4->MAC pairs. Entries nmap couldn't resolve a MAC
    for (state FAILED/INCOMPLETE, no `lladdr` field) are skipped
    automatically, as are IPv6 neighbor-discovery entries (`ip neigh show`
    lists both address families in one table; only IPv4 devices are tracked
    here, since the whole point is discovering LAN hosts by the IPv4
    subnet just swept)."""
    devices = {}
    try:
        result = subprocess.run(["ip", "neigh", "show"], capture_output=True, text=True, timeout=10)
        for line in result.stdout.splitlines():
            parts = line.split()
            if "lladdr" not in parts:
                continue
            ip = parts[0]
            mac = parts[parts.index("lladdr") + 1].lower()
            if _MAC_RE.match(mac) and re.match(r'^\d+\.\d+\.\d+\.\d+$', ip):
                devices[mac] = ip
    except Exception:
        pass
    return devices


def scan():
    """Run a fresh LAN scan, update the known-devices store, and best-effort
    Telegram-alert on any MAC seen for the first time ever. Returns
    {"subnet", "devices": [{"mac","ip","first_seen","new"}], "new_count",
    "scanned_at"} or {"error": "..."} if nmap/subnet detection isn't
    available -- never raises."""
    if not NMAP_AVAILABLE:
        return {"error": "nmap isn't installed sir -- run: sudo apt install nmap"}
    subnet = _detect_subnet()
    if not subnet:
        return {"error": "I couldn't detect your LAN subnet sir -- set LAN_SUBNET_OVERRIDE in .env (e.g. 10.0.0.0/24)."}

    _ping_sweep(subnet)
    seen = _read_neighbors()
    known = _load_known()
    now = time.time()
    new_macs = []

    for mac, ip in seen.items():
        if mac not in known:
            known[mac] = {"ip": ip, "first_seen": now, "last_seen": now}
            new_macs.append(mac)
        else:
            known[mac]["ip"] = ip
            known[mac]["last_seen"] = now
    _save_known(known)

    if new_macs:
        try:
            import telegram_bridge
            if telegram_bridge.TELEGRAM_AVAILABLE:
                lines = "\n".join(f"- {mac} ({known[mac]['ip']})" for mac in new_macs)
                telegram_bridge.send_message(
                    f"New device{'s' if len(new_macs) != 1 else ''} joined your network sir:\n{lines}"
                )
        except Exception:
            pass

    devices = [
        {"mac": mac, "ip": info["ip"], "first_seen": info["first_seen"], "new": mac in new_macs}
        for mac, info in known.items() if mac in seen
    ]
    devices.sort(key=lambda d: [int(o) for o in d["ip"].split(".")])
    return {"subnet": subnet, "devices": devices, "new_count": len(new_macs), "scanned_at": now}


def last_scan_summary():
    """Read back the known-devices store without running a new scan --
    used by the local HTTP API / HUD widget for a cheap initial load."""
    known = _load_known()
    devices = [{"mac": mac, "ip": info["ip"], "first_seen": info["first_seen"]}
               for mac, info in known.items()]
    devices.sort(key=lambda d: [int(o) for o in d["ip"].split(".")])
    return {"devices": devices, "device_count": len(devices)}
