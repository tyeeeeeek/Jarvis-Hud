# Starts Jarvis's Python backend (voice, texting, email watching, reminders)
# as a persistent background process, independent of whether the HUD window
# is ever opened. Scheduled via Windows Task Scheduler (task name:
# JarvisBackend, trigger: at logon) -- see README's "Always-on backend"
# section to inspect/disable/change it.
#
# jarvis.py itself refuses to start a second time if it's already running
# (checks whether ws://localhost:8765 is taken), so it's always safe to
# re-run this -- at login, manually, or via the Electron app's own spawn --
# without ever double-processing a text command.

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonwExe = Join-Path $ProjectDir "venv\Scripts\pythonw.exe"
$env:PYTHONIOENCODING = "utf-8"

Start-Process -FilePath $PythonwExe -ArgumentList "jarvis.py" `
    -WorkingDirectory $ProjectDir -WindowStyle Hidden
