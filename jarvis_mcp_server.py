# ================================================================
#   J.A.R.V.I.S — MCP tool server
#
#   Exposes every capability in tools.py to the Claude-powered brain
#   (brain.py) over stdio. Run with the venv's python.exe (NOT
#   pythonw.exe -- this needs real, working stdio pipes to speak
#   JSON-RPC; pythonw.exe has none).
#
#   The server key below ("jarvis") is what tool names get prefixed
#   with when Claude sees them (mcp__jarvis__<fn>) -- keep it in
#   sync with brain.py's generated --mcp-config and --allowedTools.
# ================================================================
import sys, io

# Same UTF-8 guard jarvis.py uses -- Windows defaults to cp1252 for piped
# output, which would crash on the first non-ASCII character.
try:
    if sys.stdout.encoding != "utf-8":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    if sys.stderr.encoding != "utf-8":
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
except Exception:
    pass

from mcp.server.mcpserver import MCPServer

import tools

mcp = MCPServer("jarvis")

_TOOL_FUNCS = [
    tools.create_folder, tools.create_file, tools.list_directory,
    tools.read_text_file, tools.delete_item,
    tools.launch_app, tools.close_app,
    tools.media_control,
    tools.open_website, tools.search_web, tools.play_youtube, tools.youtube_control,
    tools.get_weather,
    tools.draft_email,
    tools.check_disk_space, tools.clean_disk, tools.check_system_health,
    tools.system_power,
    tools.sync_bank_data, tools.get_spending_summary,
    tools.enable_eyes, tools.disable_eyes, tools.describe_screen,
    tools.set_reminder, tools.add_note, tools.list_notes,
    tools.build_creation,
    tools.ask_claude_web,
    tools.self_improve,
    tools.agent_status, tools.send_agent_test_message,
]

for _fn in _TOOL_FUNCS:
    mcp.add_tool(_fn)


if __name__ == "__main__":
    mcp.run()
