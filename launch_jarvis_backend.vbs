' Silently starts Jarvis's Python backend (voice, texting, email watching,
' reminders) with no visible window. Launched automatically at login via a
' shortcut in the Windows Startup folder -- see README's "Always-on backend"
' section. Safe to double-click manually too; jarvis.py refuses to start a
' second time if it's already running.
Set fso = CreateObject("Scripting.FileSystemObject")
Set WshShell = CreateObject("WScript.Shell")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
WshShell.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -File """ & scriptDir & "\run_jarvis_backend.ps1""", 0, False
