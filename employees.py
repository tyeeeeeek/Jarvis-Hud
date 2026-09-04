# ================================================================
#   J.A.R.V.I.S — real background "employees"
#
#   Replaces tools.hire_employee's old synchronous behavior (the job ran
#   inline and blocked whatever channel asked for it -- up to 10 minutes
#   for self_improve/build_creation) with a real queue: hire_employee now
#   enqueues and returns immediately, and a dedicated worker thread
#   (jarvis.py's _employee_worker_thread, started once for the life of the
#   backend) picks jobs up one at a time and runs them.
#
#   IMPORTANT process boundary: enqueue_job() is called from wherever
#   hire_employee runs, which is usually the short-lived per-command MCP
#   subprocess brain.py spawns (a DIFFERENT OS process than jarvis.py's own
#   long-lived process, which is where worker_loop() actually runs). A
#   plain threading.Lock() only protects against races inside one process,
#   so the job file uses real cross-process file locking (_locked() below)
#   instead -- otherwise a hire_employee call landing at the same instant
#   the worker thread is writing a status update could silently lose one
#   of the two writes.
#
#   Every role here still only ever calls one of tools.py's existing named
#   functions (self_improve/build_creation/ask_claude_web/
#   get_financial_insights) -- hiring an employee never grants any new
#   capability, only a name, a queue slot, and a background thread to run
#   in.
# ================================================================
import contextlib
import json
import os
import re
import time
import uuid

import bot_events

HOME = os.path.expanduser("~")
JARVIS_DIR = os.path.join(HOME, ".jarvis")
JOBS_PATH = os.path.join(JARVIS_DIR, "employee_jobs.json")
_LOCK_PATH = JOBS_PATH + ".lock"
_LOCK_TIMEOUT_SECS = 5.0

ROLES = {
    "developer": "code/self-improvement work on Jarvis itself",
    "designer": "building a dashboard or webpage",
    "researcher": "answering something that needs live web research",
    "analyst": "financial insights and spending tips from synced statements",
}
_ROLE_TITLES = {"developer": "Dev", "designer": "Design", "researcher": "Research", "analyst": "Finance"}


@contextlib.contextmanager
def _locked():
    """Cross-process exclusive lock via an O_CREAT|O_EXCL sentinel file --
    portable (works the same on POSIX and Windows, unlike fcntl.flock),
    dependency-free, and fine for this volume (a handful of job records,
    not a high-throughput queue). Breaks a stale lock rather than hanging
    forever if a crashed process never cleaned up -- job records are
    low-stakes JSON, not worth a permanent deadlock over."""
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


def _load_jobs():
    try:
        with open(JOBS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save_jobs(jobs):
    os.makedirs(JARVIS_DIR, exist_ok=True)
    tmp = JOBS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(jobs, f)
    os.replace(tmp, JOBS_PATH)


def _next_name(jobs, title, key, value):
    """Sequential name (Dev-1, Dev-2, Backup Watcher-1, ...) counted from
    the job history itself so names stay stable across restarts without a
    separate counter file to keep in sync. `key`/`value` picks what counts
    as "the same series" -- role for a hire-role job, agent_id for a
    schedule-triggered tool job."""
    count = sum(1 for j in jobs if j.get(key) == value)
    return f"{title}-{count + 1}"


def _tool_registry():
    """Lazy import: tools.py imports employees at module load time (for
    hire_employee), and jarvis_mcp_server.py imports tools -- importing
    jarvis_mcp_server at this module's top level would complete the cycle,
    so it's deferred to call time instead, same reasoning as _run_job's
    local `import tools` below."""
    import jarvis_mcp_server
    return {fn.__name__: fn for fn in jarvis_mcp_server._TOOL_FUNCS}


def enqueue_job(role: str = None, job: str = None, *, tool: str = None, args: dict = None,
                 auto: bool = False, agent_id: str = None, name_hint: str = None):
    """Validates and queues one job, returns (record, None) on success or
    (None, error_message) on failure. Either pass (role, job) for one of
    the 4 fixed hire roles, or (tool, args, agent_id, name_hint) to run any
    already-registered tool directly by name -- both go through the same
    queue and worker (_run_job/worker_loop below). A tool-kind job can only
    ever name a function already exposed in jarvis_mcp_server._TOOL_FUNCS
    -- the same catalog a manual chat command already has access to -- so
    this grants no capability beyond what's already reachable today; a
    tool gated behind JarvisAdmin approval (system_power, run_admin_action)
    stays gated, since that check lives inside the tool function itself in
    tools.py, not in how it's called. `agent_id`, if set, ties this job
    back to the agent_registry entry that spawned it. `auto=True` marks a
    job an employee/agent hired on its own, not the user directly --
    surfaced in list_employees so it's clear which ones the user actually
    asked for."""
    if tool is not None:
        tool = tool.strip()
        fn = _tool_registry().get(tool)
        if not fn:
            return None, f"I don't have a tool called '{tool}' sir."
        args = args or {}
        title = name_hint or tool
        with _locked():
            jobs = _load_jobs()
            desc = f"{tool}({', '.join(f'{k}={v}' for k, v in args.items())})" if args else tool
            record = {
                "id": uuid.uuid4().hex[:12], "name": _next_name(jobs, title, "agent_id", agent_id),
                "role": None, "job": desc, "kind": "tool", "tool": tool, "args": args, "agent_id": agent_id,
                "status": "queued", "auto": auto,
                "created_at": time.time(), "started_at": None, "finished_at": None, "result": "",
            }
            jobs.append(record)
            _save_jobs(jobs)
        bot_events.publish("employee_queued", record)
        return record, None

    role = (role or "").strip().lower()
    job = (job or "").strip()
    if role not in ROLES:
        return None, (f"I don't have a '{role}' role sir -- I can hire a: "
                       + ", ".join(f"{r} ({d})" for r, d in ROLES.items()))
    if not job:
        return None, "What should they work on sir?"

    with _locked():
        jobs = _load_jobs()
        title = _ROLE_TITLES.get(role, role.title())
        record = {
            "id": uuid.uuid4().hex[:12], "name": _next_name(jobs, title, "role", role), "role": role, "job": job,
            "kind": "role", "tool": None, "args": None, "agent_id": agent_id,
            "status": "queued", "auto": auto,
            "created_at": time.time(), "started_at": None, "finished_at": None, "result": "",
        }
        jobs.append(record)
        _save_jobs(jobs)
    bot_events.publish("employee_queued", record)
    return record, None


def list_jobs(limit: int = 10):
    """Most-recent-last, same order the ledger is written in. Read without
    the lock -- a torn read can't happen (writes are atomic via tmp+
    os.replace) and a slightly-stale read is fine for a status listing."""
    return _load_jobs()[-limit:]


def _update(job_id, **fields):
    with _locked():
        jobs = _load_jobs()
        updated = None
        for j in jobs:
            if j["id"] == job_id:
                j.update(fields)
                updated = dict(j)
                break
        _save_jobs(jobs)
    if updated:
        bot_events.publish("employee_updated", updated)
    return updated


def claim_next_queued():
    """Called only by worker_loop(). Atomically finds the oldest "queued"
    job and marks it "running" in the same locked section, so two worker
    ticks (or a worker restart mid-poll) can never double-claim the same
    job."""
    with _locked():
        jobs = _load_jobs()
        claimed = None
        for j in jobs:
            if j.get("status") == "queued":
                j["status"] = "running"
                j["started_at"] = time.time()
                claimed = dict(j)
                break
        if claimed:
            _save_jobs(jobs)
    if claimed:
        bot_events.publish("employee_updated", claimed)
    return claimed


def finish_job(job_id, ok, result):
    return _update(job_id, status=("done" if ok else "failed"),
                   finished_at=time.time(), result=(result or "")[:1000])


def _run_job(record):
    # Local import: tools.py imports employees at module load time (to call
    # enqueue_job from hire_employee), so importing tools back at module
    # level here would be circular -- same reason jarvis_admin.py/
    # jarvis_cpu_alerts.py/jarvis_security.py all import tools lazily.
    import tools

    if record.get("kind") == "tool":
        # A schedule-triggered (or otherwise directly-dispatched) tool
        # call, from agent_registry.run_agent_now -- runs the exact same
        # named function a manual chat/dashboard "run tool" call would,
        # so any approval gate the tool itself enforces (system_power,
        # run_admin_action) still applies unchanged.
        try:
            fn = _tool_registry().get(record["tool"])
            if not fn:
                ok, result = False, f"tool '{record['tool']}' no longer exists"
            else:
                result = fn(**(record.get("args") or {}))
                ok = True
        except Exception as e:
            ok, result = False, f"ran into an error: {e}"
        finish_job(record["id"], ok, str(result) if result is not None else "")
        return

    role, job = record["role"], record["job"]
    try:
        if role == "developer":
            data = json.loads(tools.self_improve(job))
            ok = bool(data.get("ok"))
            result = data.get("message") or data.get("summary") or ""
        elif role == "designer":
            kind = "webpage" if re.search(r"\bwebsite\b|\bwebpage\b|\bsite\b", job.lower()) else "dashboard"
            data = json.loads(tools.build_creation(job, kind=kind))
            ok = bool(data.get("ok"))
            # This job runs in jarvis.py's own long-lived process (see
            # worker_loop's caller), not the short-lived interactive MCP
            # subprocess -- so unlike the brain.py-driven path, it's safe
            # (and necessary, this build would otherwise notify no one) to
            # work out the phone link and notify right here.
            lan_url = tools.notify_creation_ready(data) if ok else None
            result = data.get("message") or (
                f"Built and saved it sir: {lan_url or data.get('path', '')}" if ok else "")
        elif role == "researcher":
            result = tools.ask_claude_web(job)
            ok = not result.startswith("I couldn't") and not result.startswith("That took too long")
        else:  # analyst
            result = tools.get_financial_insights()
            ok = "I don't have enough spending data" not in result
            # Analyst -> researcher chaining: if there's a real, still-open
            # spending anomaly right now, automatically hire a researcher to
            # dig into it -- the concrete "one employee's job results in
            # hiring another" behavior the user asked for.
            if ok and tools.STATEMENTS_AVAILABLE:
                try:
                    anomalies = tools.statements_service.check_spending_anomalies()
                except Exception:
                    anomalies = []
                if anomalies:
                    enqueue_job(
                        "researcher",
                        f"Look into this spending anomaly and suggest what might be causing it: {anomalies[0]['detail']}",
                        auto=True,
                    )
    except Exception as e:
        ok, result = False, f"ran into an error: {e}"

    finish_job(record["id"], ok, result)


def worker_loop(pipeline_stop):
    """Runs in jarvis.py's own long-lived process (see
    jarvis.py's _employee_worker_thread). Picks up one queued job at a
    time -- sequential on purpose, since self_improve/build_creation are
    already expensive nested Claude Code subprocess calls; running two at
    once would just make both slower for no benefit on a single machine."""
    while not pipeline_stop.is_set():
        record = claim_next_queued()
        if record:
            _run_job(record)
            continue  # check immediately for another queued job
        if pipeline_stop.wait(2.0):
            break
