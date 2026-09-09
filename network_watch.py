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
import socket
import ipaddress
import subprocess
import concurrent.futures

import requests

JARVIS_DIR = os.path.join(os.path.expanduser("~"), ".jarvis")
KNOWN_DEVICES_PATH = os.path.join(JARVIS_DIR, "known_devices.json")

NMAP_BIN = shutil.which("nmap")
NMAP_AVAILABLE = bool(NMAP_BIN)

# nmap ships its own offline OUI (MAC vendor prefix) database -- reusing it
# means vendor lookup is a local file read, never a network call to some
# third-party MAC-lookup API. Path is Debian/Ubuntu's default; harmless if
# missing (vendor just comes back None).
_OUI_PATHS = [
    "/usr/share/nmap/nmap-mac-prefixes",
    "/usr/local/share/nmap/nmap-mac-prefixes",
]
_oui_table = None

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


def _load_oui_table():
    """Lazy, process-lifetime cache of nmap's bundled MAC-prefix -> vendor
    database. Returns {} (not an error) if the file isn't found, since
    vendor is always best-effort."""
    global _oui_table
    if _oui_table is not None:
        return _oui_table
    table = {}
    for path in _OUI_PATHS:
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if not line or line.startswith("#"):
                        continue
                    parts = line.rstrip("\n").split(None, 1)
                    if len(parts) == 2 and len(parts[0]) == 6:
                        table[parts[0].upper()] = parts[1].strip()
            break
        except Exception:
            continue
    _oui_table = table
    return table


def _vendor_for(mac):
    """Best-effort vendor name for a MAC's OUI (first 3 octets), from the
    local nmap prefix database -- None if the table or the prefix isn't
    available."""
    table = _load_oui_table()
    if not table:
        return None
    prefix = mac.replace(":", "").replace("-", "").upper()[:6]
    return table.get(prefix)


def _resolve_hostnames(ips):
    """Best-effort reverse-DNS for many devices at once, in parallel --
    hostname_for's own 1.5s timeout is per-call, so resolving a whole LAN's
    worth of devices sequentially could take N times as long; running them
    concurrently keeps the total added time close to that same ~1.5s cap
    regardless of device count. Returns {ip: hostname_or_None}."""
    if not ips:
        return {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(ips), 32)) as pool:
        results = list(pool.map(hostname_for, ips))
    return dict(zip(ips, results))


def scan():
    """Run a fresh LAN scan, update the known-devices store, and best-effort
    Telegram-alert on any MAC seen for the first time ever. Returns
    {"subnet", "devices": [{"mac","ip","vendor","hostname","first_seen",
    "new"}], "new_count", "scanned_at"} or {"error": "..."} if nmap/subnet
    detection isn't available -- never raises. `vendor` is a local, offline
    OUI lookup; `hostname` is a best-effort parallel reverse-DNS lookup --
    both are None when nothing resolves, never a hard failure."""
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

    live_ips = [info["ip"] for mac, info in known.items() if mac in seen]
    hostnames = _resolve_hostnames(live_ips)

    devices = [
        {
            "mac": mac,
            "ip": info["ip"],
            "vendor": _vendor_for(mac),
            "hostname": hostnames.get(info["ip"]),
            "first_seen": info["first_seen"],
            "new": mac in new_macs,
        }
        for mac, info in known.items() if mac in seen
    ]
    devices.sort(key=lambda d: [int(o) for o in d["ip"].split(".")])
    return {"subnet": subnet, "devices": devices, "new_count": len(new_macs), "scanned_at": now}


def scan_ports(ip):
    """On-demand, single-host port scan (nmap's default top-100 ports) --
    NOT a subnet-wide port sweep, and never run automatically; only from an
    explicit "scan ports" click on one already-discovered device in the
    Homelab widget. Kept separate from scan()'s ping sweep on purpose (see
    module docstring: that one is read-only-by-design and deliberately
    never touches ports) so this stays an opt-in, single-target action.
    Returns {"ip", "ports": [{"port","proto","service"}], "scanned_at"} for
    each OPEN port found, or {"error": ...}. `service` includes real
    detected version info (nmap -sV, light intensity so this stays fast --
    no root needed for this), not just a port-number guess -- e.g. "ssh
    OpenSSH 8.9p1" instead of just "ssh"."""
    if not NMAP_AVAILABLE:
        return {"error": "nmap isn't installed sir -- run: sudo apt install nmap"}
    try:
        ipaddress.ip_address(ip)  # reject anything that isn't a literal IP, not just for subprocess safety (a list arg is already shell-safe) but so this can never be pointed at a flag-like string smuggled in as "ip"
    except ValueError:
        return {"error": "invalid IP"}
    try:
        result = subprocess.run(
            [NMAP_BIN, "-sV", "--version-intensity", "0", "-T4", "--top-ports", "100", "-Pn", ip],
            capture_output=True, text=True, timeout=60,
        )
    except Exception as e:
        return {"error": str(e)}

    ports = []
    for line in result.stdout.splitlines():
        m = re.match(r"^(\d+)/(tcp|udp)\s+(\S+)\s+(.*)$", line.strip())
        if m and m.group(3) == "open":
            ports.append({"port": int(m.group(1)), "proto": m.group(2), "service": m.group(4).strip() or "unknown"})
    return {"ip": ip, "ports": ports, "scanned_at": time.time()}


def hostname_for(ip):
    """Best-effort reverse-DNS/NetBIOS lookup for one device. Called both
    on-demand (single IP, e.g. the HUD's per-device inspect) and by
    scan()'s _resolve_hostnames, which fans this out across every device
    in parallel so a resolver timeout per device doesn't turn an N-device
    scan into an N-times-as-long one. Returns the hostname string, or None
    if nothing resolves within the short timeout."""
    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(1.5)
    try:
        name, _aliases, _addrs = socket.gethostbyaddr(ip)
        return name
    except Exception:
        return None
    finally:
        socket.setdefaulttimeout(old_timeout)


def detect_gateway():
    """This machine's own default-route gateway IP -- the real router/AP
    hardware's LAN address, read from `ip route`, not guessed. Returns the
    IP string, or None if it can't be determined."""
    try:
        route = subprocess.run(["ip", "-o", "-4", "route", "show", "default"],
                                capture_output=True, text=True, timeout=5)
        m = re.search(r"via (\d+\.\d+\.\d+\.\d+)", route.stdout)
        return m.group(1) if m else None
    except Exception:
        return None


def probe_http(ip):
    """Best-effort HTTP banner grab against one device's web UI (port 80,
    then 443) -- no credentials, just a plain GET a browser on the LAN
    could make. Returns {"reachable","port","server","title"} -- server is
    whatever the Server: response header says (many router/NAS/IoT web UIs
    self-identify there), title is the page's <title> tag if present.
    Never raises; a device with no web UI just comes back
    {"reachable": False}."""
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        return {"reachable": False}
    for port, scheme in ((80, "http"), (443, "https")):
        try:
            r = requests.get(f"{scheme}://{ip}", timeout=2.5, verify=False)
            title_m = re.search(r"<title[^>]*>(.*?)</title>", r.text, re.IGNORECASE | re.DOTALL)
            return {
                "reachable": True, "port": port,
                "server": r.headers.get("Server"),
                "title": title_m.group(1).strip()[:80] if title_m else None,
            }
        except Exception:
            continue
    return {"reachable": False}


def last_scan_summary():
    """Read back the known-devices store without running a new scan --
    used by the local HTTP API / HUD widget for a cheap initial load."""
    known = _load_known()
    devices = [{"mac": mac, "ip": info["ip"], "first_seen": info["first_seen"]}
               for mac, info in known.items()]
    devices.sort(key=lambda d: [int(o) for o in d["ip"].split(".")])
    return {"devices": devices, "device_count": len(devices)}
