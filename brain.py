# ================================================================
#   J.A.R.V.I.S — Brain (Claude Code CLI tool-calling orchestration)
#
#   Runs one voice command through the Claude Code CLI, restricted
#   to exactly the tools in jarvis_mcp_server.py and nothing else
#   (verified empirically -- see notes below), streams the event
#   log so the HUD gets a live "what Jarvis is doing" caption, and
#   returns the final spoken reply.
#
#   Safety note: --allowedTools alone is NOT a hard restriction --
#   it only affects permission prompting, and this machine's normal
#   Claude Code settings already broadly allow tools like Bash. The
#   actual lockdown is `--tools ""` (disables every built-in tool)
#   plus `--mcp-config ... --strict-mcp-config` (only our own MCP
#   server, ignoring any other MCP servers configured globally)
#   plus `--allowedTools "mcp__jarvis__*"` (pre-authorizes just our
#   tools so they don't need an interactive prompt that can never
#   be answered in headless mode). All three together were verified
#   live: Bash is completely unavailable to this call, and only the
#   jarvis__* tools exist.
# ================================================================
import os, sys, json, shutil, tempfile, subprocess, threading
from datetime import datetime

import tools
import memory_store

HOME = os.path.expanduser("~")
CLAUDE_CLI = shutil.which("claude") or os.path.join(
    HOME, ".local", "bin", "claude.exe" if sys.platform == "win32" else "claude")
_HERE = os.path.dirname(os.path.abspath(__file__))
VENV_PYTHON = os.path.join(
    _HERE, "venv", "Scripts" if sys.platform == "win32" else "bin",
    "python.exe" if sys.platform == "win32" else "python")
MCP_SERVER_SCRIPT = os.path.join(_HERE, "jarvis_mcp_server.py")
MCP_CONFIG_PATH = os.path.join(tempfile.gettempdir(), "jarvis_mcp_config.json")

TIMEOUT_SECS = 900  # generous -- build_creation/self_improve can each take up to 600s

PERSONA = (
    "You are J.A.R.V.I.S, a capable voice assistant with real tools to "
    "actually do things on the user's computer -- not just describe them. "
    "Always prefer using a tool over saying you can't do something the tools "
    "clearly cover. Address the user as sir occasionally, not every sentence. "
    "Speak with calm confidence and quiet, dry wit. Your final reply is read "
    "aloud by a text-to-speech engine: keep it under 40 words, short natural "
    "sentences, no markdown, no bullet points, no emoji, never say your own "
    "name. If a tool result already gave the key information, confirm it "
    "briefly rather than repeating it verbatim. You may be given a short "
    "recap of recent conversation (possibly from an earlier session) before "
    "the current request -- use it naturally to stay consistent and resolve "
    "references like \"it\" or \"that\", but never read the recap back to "
    "the user or mention that you were given one. You're also given the "
    "current real date and time before each request -- use it to resolve "
    "relative dates (\"tomorrow\", \"next Friday\", \"in an hour\") into "
    "real ISO 8601 datetimes for tools like create_calendar_event, but "
    "never read it back to the user unless they actually asked the time."
)


def _ensure_mcp_config():
    if not os.path.exists(MCP_CONFIG_PATH):
        config = {"mcpServers": {"jarvis": {"command": VENV_PYTHON, "args": [MCP_SERVER_SCRIPT]}}}
        with open(MCP_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(config, f)
    return MCP_CONFIG_PATH


# Friendly present-tense captions for the HUD activity feed, keyed by the
# bare tool name (without the mcp__jarvis__ prefix). Falls back to the
# server-provided display_name, then a prettified function name.
_CAPTION_OVERRIDES = {
    "play_youtube": lambda a: f"Searching YouTube for {a.get('query', 'that')}",
    "youtube_control": lambda a: "Controlling YouTube playback",
    "build_creation": lambda a: f"Building {a.get('description', 'that')}",
    "self_improve": lambda a: f"Working on improving {a.get('focus', 'myself')} -- this may take a few minutes",
    "get_weather": lambda a: f"Checking the weather in {a.get('city', 'your area')}",
    "ask_claude_web": lambda a: "Researching that",
    "open_website": lambda a: f"Opening {a.get('target', 'that')}",
    "search_web": lambda a: f"Searching for {a.get('query', 'that')}",
    "launch_app": lambda a: f"Opening {a.get('name', 'that')}",
    "close_app": lambda a: f"Closing {a.get('name', 'that')}",
    "create_folder": lambda a: f"Creating a folder named {a.get('name', 'that')}",
    "create_file": lambda a: f"Creating {a.get('name', 'that')}",
    "delete_item": lambda a: f"Sending {a.get('name', 'that')} to the recycle bin",
    "list_directory": lambda a: f"Checking your {a.get('location', 'files')}",
    "read_text_file": lambda a: f"Reading {a.get('name', 'that file')}",
    "sync_bank_data": lambda a: "Syncing your bank data",
    "get_spending_summary": lambda a: "Pulling up your spending",
    "describe_screen": lambda a: "Taking a look",
    "set_reminder": lambda a: "Setting that reminder",
    "add_note": lambda a: "Saving that note",
    "remember_this": lambda a: "Remembering that",
    "recall_memory": lambda a: "Checking my memory",
    "list_notes": lambda a: "Pulling up your notes",
    "draft_email": lambda a: "Drafting that email",
    "create_calendar_event": lambda a: f"Adding {a.get('title', 'that')} to your calendar",
    "check_disk_space": lambda a: "Checking disk space",
    "clean_disk": lambda a: "Cleaning up disk space",
    "media_control": lambda a: "Adjusting playback",
    "close_browser": lambda a: "Closing the browser",
    "run_diagnostic_command": lambda a: f"Running {a.get('command', 'that')} diagnostic",
    "get_financial_insights": lambda a: "Pulling together financial insights",
    "get_nas_status": lambda a: "Checking the NAS",
    "check_internet_speed": lambda a: "Running a speed test -- this takes a bit",
    "scan_network": lambda a: "Scanning the network",
    "get_tailscale_status": lambda a: "Checking the tailnet",
    "tailscale_ping": lambda a: f"Pinging {a.get('device', 'that')} over Tailscale",
    "set_tailscale_exit_node": lambda a: "Changing the exit node",
    "tailscale_connect": lambda a: "Connecting Tailscale",
    "tailscale_disconnect": lambda a: "Disconnecting Tailscale",
    "build_finance_dashboard": lambda a: "Building your finance dashboard",
    "hire_employee": lambda a: f"Putting a {a.get('role', 'someone')} on it",
    "list_employees": lambda a: "Checking on the team",
    "system_power": lambda a: (
        "Cancelling the pending shutdown" if a.get("action") == "cancel"
        else f"Restarting the PC{'' if float(a.get('delay_minutes', 1) or 0) == 0 else ' shortly'}"
        if a.get("action") == "restart"
        else f"Shutting down the PC{'' if float(a.get('delay_minutes', 1) or 0) == 0 else ' shortly'}"
    ),
}


def _caption_for(name, args, meta_display_name):
    short = name.split("__")[-1]
    fn = _CAPTION_OVERRIDES.get(short)
    if fn:
        try:
            return fn(args or {})
        except Exception:
            pass
    if meta_display_name:
        return meta_display_name
    return short.replace("_", " ").capitalize()


def run_agent(command, on_activity=None, on_creation=None, on_process=None):
    """Run one voice command through the Claude tool-calling brain.

    on_activity(text): called as soon as each tool call starts (before it
        finishes), for live HUD feedback.
    on_creation(payload): called with the parsed build_creation tool result
        dict ({"ok", "kind", "title", "path"}) the moment it's available,
        without waiting for Claude's closing remark.
    on_process(proc): called with the Popen handle for this command's Claude
        CLI subprocess as soon as it's launched, so the caller can forcibly
        cancel a long-running command (e.g. self_improve/hire_employee) that
        would otherwise block until it finishes on its own. The subprocess is
        started in its own process group/session specifically so a cancel can
        take out its whole tree (it, the MCP server it spawns, and anything
        that server itself spawns like self_improve's inner Claude Code call)
        with one signal, not just the top-level process.

    Returns the final text to speak, or None if the brain couldn't be
    reached at all (caller should fall back to local chat-only Ollama).
    """
    if not os.path.exists(CLAUDE_CLI):
        return None

    config_path = _ensure_mcp_config()
    # Claude Code's own default environment context (which normally includes
    # today's date) is fully replaced by --system-prompt below, not appended
    # to -- so without this, "tomorrow at 3pm" (a reminder, a calendar event)
    # has nothing real to resolve against. Given directly in the prompt
    # rather than PERSONA so it's always fresh, never stale from an earlier
    # process start.
    now_line = f"Current date and time: {datetime.now().strftime('%A, %B %d, %Y, %I:%M %p')} (local)."
    history = tools.recent_conversation_context()

    # Ambient long-term memory recall -- distinct from `history` above
    # (a short rolling window of the last few turns): this can surface a
    # fact or preference from days/weeks ago. fallback_to_recent=False on
    # purpose -- unlike the explicit recall_memory tool, showing unrelated
    # recent memories on every single command would just be noise, not
    # useful context, so this only injects anything when there's a real
    # keyword match.
    memory_block = ""
    try:
        hits = memory_store.recall(command, limit=4, fallback_to_recent=False)
        if hits:
            memory_block = "Relevant things you remember about the user:\n" + "\n".join(f"- {h['text']}" for h in hits)
    except Exception:
        pass

    parts = [now_line]
    if memory_block:
        parts.append(memory_block)
    if history:
        parts.append(history)
    parts.append(f"Current request: {command}")
    prompt = "\n\n".join(parts)
    argv = [
        CLAUDE_CLI, "-p", prompt,
        "--mcp-config", config_path, "--strict-mcp-config",
        "--tools", "",
        "--allowedTools", "mcp__jarvis__*",
        "--system-prompt", PERSONA,
        "--output-format", "stream-json", "--verbose",
        "--no-session-persistence",
    ]

    # New process group/session (POSIX: start_new_session; Windows: its own
    # process group) so a cancel can reach the whole tree this spawns, not
    # just this top-level process -- see on_process docstring above.
    _group_kwargs = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32"
        else {"start_new_session": True}
    )
    try:
        proc = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", **_group_kwargs,
        )
    except Exception as e:
        print(f"  [Brain] Launch error: {e}")
        return None

    if on_process:
        try:
            on_process(proc)
        except Exception:
            pass

    killer = threading.Timer(TIMEOUT_SECS, lambda: proc.kill())
    killer.daemon = True
    killer.start()

    tool_calls = {}  # tool_use_id -> (name, input)
    final_text = None

    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            etype = event.get("type")

            if etype == "assistant":
                blocks = event.get("message", {}).get("content", []) or []
                meta_by_id = {m.get("id"): m.get("display_name") for m in (event.get("tool_use_meta") or [])}
                for block in blocks:
                    if block.get("type") != "tool_use":
                        continue
                    tid, tname, targs = block.get("id"), block.get("name", ""), block.get("input", {}) or {}
                    tool_calls[tid] = (tname, targs)
                    if on_activity:
                        try:
                            on_activity(_caption_for(tname, targs, meta_by_id.get(tid)))
                        except Exception:
                            pass

            elif etype == "user":
                for block in event.get("message", {}).get("content", []) or []:
                    if block.get("type") != "tool_result":
                        continue
                    tname, _targs = tool_calls.get(block.get("tool_use_id"), ("", {}))
                    # build_finance_dashboard returns build_creation's exact
                    # payload shape (it just calls build_creation internally
                    # with real spending data folded into the prompt), so the
                    # live HUD creation panel should pop for it too.
                    if tname.split("__")[-1] not in ("build_creation", "build_finance_dashboard") or not on_creation:
                        continue
                    structured = ((event.get("tool_use_result") or {}).get("structuredContent")) or {}
                    raw = structured.get("result")
                    try:
                        payload = json.loads(raw) if isinstance(raw, str) else raw
                    except Exception:
                        payload = None
                    if payload and payload.get("ok"):
                        try:
                            on_creation(payload)
                        except Exception:
                            pass

            elif etype == "result":
                final_text = event.get("result")

    finally:
        killer.cancel()
        try:
            proc.wait(timeout=5)
        except Exception:
            try: proc.kill()
            except Exception: pass

    if final_text:
        try:
            tools.record_conversation_turn(command, final_text)
        except Exception:
            pass
        # Passive long-term memory capture -- backgrounded so a slow/
        # unreachable local Ollama can never delay the actual reply the
        # user is waiting on; best-effort and silent by design (see
        # memory_store.maybe_capture's own docstring).
        threading.Thread(target=memory_store.maybe_capture, args=(command, final_text), daemon=True).start()

    return final_text
