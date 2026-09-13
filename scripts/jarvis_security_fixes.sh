#!/bin/bash
# ================================================================
#   J.A.R.V.I.S — JarSecurity curated remediation script
#
#   Root-level fixes for specific, known, real exposed-port findings
#   from tools._remediate_exposed_port(). Deliberately NOT a generic
#   "run whatever command" hook -- only the two fixed keyword actions
#   below exist, matching this project's curated-allowlist philosophy
#   used everywhere else (fleet_service.py's action tables, etc).
#
#   Invoked as: sudo -n jarvis_security_fixes.sh <fix_exposed_ollama|fix_exposed_iperf3>
#   Wired up via a scoped NOPASSWD sudoers rule -- see README.md.
# ================================================================
set -euo pipefail

case "${1:-}" in
  fix_exposed_ollama)
    # Ollama's systemd unit binds it to 0.0.0.0:11434 by default here, but
    # every caller in this project (jarvis.py, tools.py, telegram_common.py,
    # memory_store.py, email_watcher.py) only ever talks to it over
    # localhost -- so loopback-only is strictly safer and breaks nothing.
    sed -i 's/OLLAMA_HOST=0\.0\.0\.0:11434/OLLAMA_HOST=127.0.0.1:11434/' /etc/systemd/system/ollama.service
    systemctl daemon-reload
    systemctl restart ollama.service
    ;;
  fix_exposed_iperf3)
    # iperf3.service is meant to be started on demand for a one-off LAN
    # throughput test (see tools.test_lan_throughput), not run permanently
    # as an always-on, unauthenticated, LAN/WAN-reachable server.
    systemctl stop iperf3.service
    systemctl disable iperf3.service
    ;;
  *)
    echo "unknown fix: ${1:-<none>}" >&2
    exit 2
    ;;
esac
