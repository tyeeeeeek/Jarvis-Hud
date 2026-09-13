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
    tools.close_browser,
    tools.read_webpage,
    tools.get_weather, tools.show_map, tools.show_weather_radar,
    tools.draft_email, tools.create_calendar_event,
    tools.search_email, tools.read_email,
    tools.list_calendar_events, tools.update_calendar_event, tools.delete_calendar_event,
    tools.check_disk_space, tools.clean_disk, tools.check_system_health,
    tools.system_power, tools.restart_jarvis, tools.run_diagnostic_command, tools.run_admin_action,
    tools.propose_hardening_install, tools.trigger_lockdown,
    tools.get_nas_status, tools.export_folder_to_nas, tools.import_folder_from_nas,
    tools.list_nas_folder,
    tools.check_internet_speed, tools.scan_network,
    tools.deep_scan_device, tools.scan_file_for_malware, tools.get_disk_health,
    tools.test_lan_throughput, tools.get_live_system_snapshot, tools.run_security_audit, tools.run_rootkit_scan,
    tools.run_chkrootkit_scan, tools.run_fail2ban_status, tools.run_aide_check,
    tools.run_security_check, tools.verify_ublock_origin, tools.install_ublock_origin,
    tools.verify_duckduckgo_privacy, tools.install_duckduckgo_privacy,
    tools.check_firmware_drivers, tools.check_pihole_status, tools.scan_url_safety,
    tools.check_ai_services, tools.restart_ai_service,
    tools.get_tailscale_status, tools.tailscale_ping, tools.set_tailscale_exit_node,
    tools.tailscale_connect, tools.tailscale_disconnect,
    tools.list_fleet_devices, tools.fleet_status, tools.fleet_restart, tools.fleet_cancel_restart,
    tools.fleet_processes, tools.fleet_docker_status, tools.fleet_docker_restart,
    tools.fleet_list_dir, tools.fleet_make_dir, tools.fleet_delete_path, tools.fleet_rename_path,
    tools.fleet_apps_installed, tools.fleet_apps_search, tools.fleet_resolve_app,
    tools.fleet_app_install, tools.fleet_app_uninstall, tools.fleet_app_upgrade,
    tools.fleet_wake,
    tools.sync_bank_data, tools.get_spending_summary, tools.get_financial_insights,
    tools.build_finance_dashboard,
    tools.enable_eyes, tools.disable_eyes, tools.describe_screen,
    tools.set_reminder, tools.list_reminders, tools.cancel_reminder,
    tools.add_note, tools.list_notes, tools.delete_note,
    tools.remember_this, tools.recall_memory, tools.list_recent_memories,
    tools.build_creation, tools.list_creations,
    tools.ask_claude_web,
    tools.self_improve,
    tools.agent_status, tools.send_agent_test_message,
    tools.hire_employee, tools.list_employees,
]

for _fn in _TOOL_FUNCS:
    mcp.add_tool(_fn)


if __name__ == "__main__":
    mcp.run()
