# ================================================================
#   J.A.R.V.I.S — agent registry (dashboard "onboarding")
#
#   Persistent, named, optionally-scheduled agents -- the phone dashboard's
#   "+ Onboard Agent" flow. Each agent is either a hire-style role (same 4
#   roles employees.py already supports: developer/designer/researcher/
#   analyst) or a direct binding to one already-registered tool from
#   jarvis_mcp_server._TOOL_FUNCS, with an optional trigger (manual / once
#   / every N minutes).
#
#   IMPORTANT: an agent never runs anything itself. Creating/scheduling one
#   only ever ends up calling employees.enqueue_job(), the exact same queue
#   a "Hire someone" tap or hire_employee already uses -- so a tool-kind
#   agent can only ever name a function already in the tool catalog (the
#   same one a manual chat/dashboard command can already call), and
#   anything gated behind JarvisAdmin approval (system_power,
#   run_admin_action) stays gated no matter what enqueues it, since that
#   check lives inside the tool function itself in tools.py. Onboarding an
#   agent grants no capability beyond what's already reachable today --
#   only a name, a schedule, and a queue slot.
#
#   Same cross-process file-locking reasoning as employees.py: dashboard_
#   server.py's Flask routes and jarvis.py's own scheduler thread
#   (_agent_scheduler_thread) both run in the one long-lived jarvis.py
#   process, but this module mirrors employees.py's _locked() pattern
#   anyway for consistency and because it's cheap and dependency-free.
#
#   Completion tracking: rather than employees.py reaching back into this
#   module (which would create a real import cycle -- agent_registry
#   already imports employees to enqueue jobs), this module subscribes to
#   bot_events for "employee_updated" and watches for a job whose
#   agent_id matches one of ours reaching a terminal status. That's
#   exactly what bot_events.py's shared bus is for (see its own module
#   docstring): one place for a module to react to another's work without
#   a new direct dependency between them.
# ================================================================
import contextlib
import json
import os
import time
import uuid

import bot_events
import employees

HOME = os.path.expanduser("~")
JARVIS_DIR = os.path.join(HOME, ".jarvis")
AGENTS_PATH = os.path.join(JARVIS_DIR, "agents.json")
_LOCK_PATH = AGENTS_PATH + ".lock"
_LOCK_TIMEOUT_SECS = 5.0

_MIN_INTERVAL_MINUTES = 1
_TRIGGER_TYPES = ("manual", "once", "interval")


@contextlib.contextmanager
def _locked():
    """Same portable O_CREAT|O_EXCL sentinel-file lock as employees.py's
    _locked() -- see that module for the full reasoning. Duplicated rather
    than imported to keep the two registries independent (deleting/
    breaking one's lock file can never affect the other's)."""
    deadline = time.time() + _LOCK_TIMEOUT_SECS
    fd = None
    while fd is None:
        try:
            fd = os.open(_LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if time.time() > deadline:
                try:
                    os.remove(_LOCK_PATH)
                except OSError:
                    pass
                continue
            time.sleep(0.05)
    try:
        yield
    finally:
        os.close(fd)
        try:
            os.remove(_LOCK_PATH)
        except OSError:
            pass


def _load():
    try:
        with open(AGENTS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save(agents):
    os.makedirs(JARVIS_DIR, exist_ok=True)
    tmp = AGENTS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(agents, f)
    os.replace(tmp, AGENTS_PATH)


def _tool_names():
    # Same "each consumer builds its own small dict from the one canonical
    # list" pattern dashboard_server.py already uses for _TOOLS_BY_NAME --
    # jarvis_mcp_server._TOOL_FUNCS is the single source of truth.
    import jarvis_mcp_server
    return {fn.__name__ for fn in jarvis_mcp_server._TOOL_FUNCS}


def _next_run_at(trigger):
    if trigger["type"] == "interval":
        return time.time() + trigger["minutes"] * 60
    if trigger["type"] == "once":
        return time.time()
    return None  # manual


def _validate_trigger(trigger):
    trigger = trigger or {"type": "manual"}
    t = (trigger.get("type") or "").strip().lower()
    if t not in _TRIGGER_TYPES:
        return None, f"trigger.type must be one of {_TRIGGER_TYPES}"
    if t == "interval":
        try:
            minutes = float(trigger.get("minutes"))
        except (TypeError, ValueError):
            return None, "trigger.minutes must be a number"
        if minutes < _MIN_INTERVAL_MINUTES:
            return None, f"trigger.minutes must be at least {_MIN_INTERVAL_MINUTES}"
        return {"type": "interval", "minutes": minutes}, None
    return {"type": t}, None


def create_agent(name: str, kind: str, tool: str = None, args: dict = None,
                  role: str = None, job: str = None, trigger: dict = None):
    """Validates and persists one agent, returns (record, None) on success
    or (None, error_message) on failure. kind == "tool" needs `tool`
    (must already be in the tool catalog) + optional `args`; kind ==
    "role" needs `role` (one of employees.ROLES) + `job`, same as the
    dashboard's existing "Hire someone" form."""
    name = (name or "").strip()
    if not name:
        return None, "Name is required"

    kind = (kind or "").strip().lower()
    if kind == "tool":
        tool = (tool or "").strip()
        if tool not in _tool_names():
            return None, f"Unknown tool '{tool}'"
        role, job = None, None
        args = args or {}
    elif kind == "role":
        role = (role or "").strip().lower()
        job = (job or "").strip()
        if role not in employees.ROLES:
            return None, f"Unknown role '{role}' -- must be one of {list(employees.ROLES)}"
        if not job:
            return None, "job is required for a role-kind agent"
        tool, args = None, None
    else:
        return None, "kind must be 'tool' or 'role'"

    trigger, error = _validate_trigger(trigger)
    if error:
        return None, error

    record = {
        "id": uuid.uuid4().hex[:12], "name": name, "kind": kind,
        "tool": tool, "args": args, "role": role, "job": job,
        "trigger": trigger, "enabled": True,
        "created_at": time.time(), "last_run_at": None, "next_run_at": _next_run_at(trigger),
        "last_status": None, "last_summary": "",
    }
    with _locked():
        agents = _load()
        agents.append(record)
        _save(agents)
    bot_events.publish("agent_created", record)
    return record, None


def list_agents():
    return _load()


def get_agent(agent_id):
    for a in _load():
        if a["id"] == agent_id:
            return a
    return None


def update_agent(agent_id, **fields):
    """Used for enable/disable today; general enough for editing a
    trigger later without a new function."""
    with _locked():
        agents = _load()
        updated = None
        for a in agents:
            if a["id"] == agent_id:
                a.update(fields)
                if "trigger" in fields:
                    a["next_run_at"] = _next_run_at(a["trigger"]) if a.get("enabled", True) else None
                updated = dict(a)
                break
        if updated:
            _save(agents)
    if updated:
        bot_events.publish("agent_updated", updated)
    return updated


def delete_agent(agent_id):
    with _locked():
        agents = _load()
        remaining = [a for a in agents if a["id"] != agent_id]
        deleted = len(remaining) != len(agents)
        if deleted:
            _save(remaining)
    if deleted:
        bot_events.publish("agent_deleted", {"id": agent_id})
    return deleted


def due_agents(now: float = None):
    now = time.time() if now is None else now
    return [a for a in _load()
            if a.get("enabled") and a.get("trigger", {}).get("type") in ("interval", "once")
            and a.get("next_run_at") is not None and a["next_run_at"] <= now]


def mark_ran(agent_id, ok: bool, summary: str):
    with _locked():
        agents = _load()
        updated = None
        for a in agents:
            if a["id"] != agent_id:
                continue
            a["last_run_at"] = time.time()
            a["last_status"] = "ok" if ok else "failed"
            a["last_summary"] = (summary or "")[:500]
            if a.get("trigger", {}).get("type") == "interval":
                a["next_run_at"] = _next_run_at(a["trigger"])
            else:  # "once" (or manual, though that never reaches here)
                a["next_run_at"] = None
                if a.get("trigger", {}).get("type") == "once":
                    a["enabled"] = False
            updated = dict(a)
            break
        if updated:
            _save(agents)
    if updated:
        bot_events.publish("agent_updated", updated)
    return updated


def run_agent_now(agent_id, auto: bool = True):
    """The single entry point both the scheduler thread and the
    dashboard's "Run now" button use -- just enqueues into employees.py's
    existing job queue/worker, so execution (and every safety property
    that already applies to that queue) is identical either way. Returns
    (job_record, None) or (None, error_message)."""
    agent = get_agent(agent_id)
    if not agent:
        return None, "No such agent"
    if agent["kind"] == "tool":
        return employees.enqueue_job(tool=agent["tool"], args=agent.get("args") or {},
                                      agent_id=agent_id, name_hint=agent["name"], auto=auto)
    return employees.enqueue_job(role=agent["role"], job=agent["job"],
                                  agent_id=agent_id, name_hint=agent["name"], auto=auto)


def _on_bot_event(event):
    if event.get("type") != "employee_updated":
        return
    payload = event.get("payload") or {}
    agent_id = payload.get("agent_id")
    status = payload.get("status")
    if not agent_id or status not in ("done", "failed"):
        return
    if get_agent(agent_id) is None:
        return  # agent was deleted while its job was running
    mark_ran(agent_id, ok=(status == "done"), summary=payload.get("result", ""))


bot_events.subscribe(_on_bot_event)
