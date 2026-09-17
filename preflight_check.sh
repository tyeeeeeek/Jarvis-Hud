#!/usr/bin/env bash
# Run this FIRST on the target Ubuntu machine, before any setup steps, so
# gaps are known up front instead of discovered one error at a time.
# Usage: bash preflight_check.sh

echo "=== J.A.R.V.I.S preflight check ==="
ok=1
check() { command -v "$1" >/dev/null 2>&1 && echo "  [OK] $1 found" || { echo "  [MISSING] $1"; ok=0; }; }

echo "--- Core runtimes ---"
check python3
check node
check npm
check git
if command -v python3 >/dev/null 2>&1; then
  echo "  python3 version: $(python3 --version)"
fi
if command -v node >/dev/null 2>&1; then
  node_major=$(node --version | sed 's/v\([0-9]*\).*/\1/')
  echo "  node version: $(node --version)"
  if [ "$node_major" -lt 18 ] 2>/dev/null; then
    echo "  [WARNING] Node is older than 18 -- this project needs 18+. Ubuntu's default"
    echo "            apt nodejs package is often way too old; install via NodeSource:"
    echo "            curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash - && sudo apt install -y nodejs"
  fi
fi

echo "--- Audio ---"
if command -v arecord >/dev/null 2>&1; then
  echo "  Input devices (arecord -l):"
  arecord -l 2>&1 | sed 's/^/    /'
else
  echo "  [MISSING] arecord (alsa-utils) -- can't even check for a mic. sudo apt install alsa-utils"
fi
check espeak-ng || check espeak
python3 -c "import ctypes.util; print('  [OK] portaudio found' if ctypes.util.find_library('portaudio') else '  [MISSING] portaudio -- sudo apt install portaudio19-dev')" 2>/dev/null

echo "--- Display (needed for real, visible YouTube playback via Playwright) ---"
if [ -n "$DISPLAY" ] || [ -n "$WAYLAND_DISPLAY" ]; then
  echo "  [OK] A display session is active (\$DISPLAY=$DISPLAY \$WAYLAND_DISPLAY=$WAYLAND_DISPLAY)"
else
  echo "  [WARNING] No \$DISPLAY or \$WAYLAND_DISPLAY set -- Playwright's non-headless"
  echo "            Chromium (used for real YouTube playback control) needs a real"
  echo "            desktop session. Fine if you're running this in a terminal on the"
  echo "            actual desktop; a problem if this is a pure SSH/headless session."
fi

echo "--- Browsers (for webbrowser.open() / default-browser links) ---"
check xdg-open
check google-chrome || check firefox || check chromium-browser

echo "--- Claude Code CLI ---"
check claude
if command -v claude >/dev/null 2>&1; then
  claude --version
  echo "  Run 'claude auth status' (or just 'claude' interactively) to confirm this"
  echo "  install is authenticated."
fi

echo ""
if [ "$ok" -eq 1 ]; then
  echo "=== Core tools present. Review any [WARNING] lines above, then continue with README.md's setup steps. ==="
else
  echo "=== Missing core tools listed above -- install those first. ==="
fi
