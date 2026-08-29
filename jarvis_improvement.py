# ================================================================
#   J.A.R.V.I.S — JarvisImprovement sub-agent
#
#   Dedicated, send-only Telegram bot for the daily self-improvement
#   report. Kept as its own bot/module (separate from telegram_bridge.py's
#   two-way command channel and jarvis_cpu_alerts.py's PC health alerts)
#   so a leaked token here can only be used to read one status message a
#   day -- never to control anything, see command traffic, or see health
#   data.
#
#   Wired into jarvis.py's _improvement_watcher_thread: once every 24
#   hours, starting at 6 AM local time, that thread calls
#   tools.run_daily_self_improve() (the same self_improve.md-driven
#   mechanism as the nightly/on-demand self-improvement pass, capped at up
#   to 2 hours) and then send_report() fires exactly once, right after the
#   pass completes -- whether it made changes, found nothing safe to
#   improve, or hit the time-box.
#
#   Two-way, split by whether the incoming text reads as a question or an
#   instruction (see _looks_like_question()): a question ("what did you fix
#   yesterday?", "are you running right now?") gets an immediate reply,
#   grounded (via telegram_common.ask_grounded()) in tools.agent_status()'s
#   real, on-disk history -- never a git log, secret, or anything beyond
#   that same read-only status the user could already ask the main Jarvis
#   bot for. Anything else is treated as an on-demand self-improvement
#   request -- identical to the user asking Jarvis directly "improve on
#   ..." -- and run through the same tools.self_improve() (same hard
#   constraints, same syntax-check/build-verify gate before any commit)
#   rather than any free-form command. See poll_thread(). A leaked token
#   could waste a self-improve run or read agent status, but can never run
#   an arbitrary command or touch anything outside self_improve()'s own
#   constraints.
#
#   Fully inert until JARVIS_IMPROVEMENT_BOT_TOKEN is set in .env; reuses
#   TELEGRAM_CHAT_ID (same user, same phone) unless
#   JARVIS_IMPROVEMENT_CHAT_ID overrides it.
# ================================================================
import os
import re
import threading

import telegram_common

BOT_TOKEN = os.environ.get("JARVIS_IMPROVEMENT_BOT_TOKEN", "")
CHAT_ID = os.environ.get("JARVIS_IMPROVEMENT_CHAT_ID", "") or os.environ.get("TELEGRAM_CHAT_ID", "")

AVAILABLE = bool(BOT_TOKEN and CHAT_ID)


_AGENT_NAME = "JarvisImprovement"

# Distinguishes "what did you improve yesterday?" (answer it, don't spend
# several minutes on a Claude Code run) from "improve the weather widget"
# (an actual instruction, still routed to self_improve() as before). Errs
# toward treating anything ambiguous as an instruction -- self_improve()
# has its own hard constraints and a syntax-check/build-verify gate before
# any commit, so misrouting a real question into it wastes a run at worst,
# never causes harm; the reverse (silently skipping a real instruction)
# would be worse.
_QUESTION_WORDS_RE = re.compile(
    r"^(what|how|why|when|where|who|which|is|are|was|were|did|do|does|"
    r"have|has|can|could|will|would|should)\b", re.IGNORECASE)


def _looks_like_question(text: str) -> bool:
    t = (text or "").strip()
    return t.endswith("?") or bool(_QUESTION_WORDS_RE.match(t))


def _send(text: str) -> bool:
    """Send-only Telegram helper for this bot. Tags sends with this bot's
    agent name so a confirmed delivery lands in the shared supervision
    log."""
    return telegram_common.send(BOT_TOKEN, CHAT_ID, text, agent=_AGENT_NAME)


def send_report(added, timed_out=False) -> bool:
    """Sent once after every daily self-improvement pass completes.
    `added` is the list of commit-subject lines (one per safe, verified
    change actually committed during the run) -- an empty list means
    nothing safe to improve was found today. `timed_out` marks a run that
    hit the 2-hour cap rather than finishing on its own. Tone is the proud,
    meticulous engineer: it wants credit for the audit, not just the diff."""
    if not added:
        text = ("Ran today's full self-improvement audit -- combed the codebase end to end. "
                "Nothing cleared the bar for a safe, verified change today. Standards held.")
    else:
        n = len(added)
        bullets = "\n".join(f"✓ {line}" for line in added)
        text = (f"Today's self-improvement pass is done -- {n} change{'s' if n != 1 else ''} "
                f"shipped, each one syntax-checked and build-verified before commit:\n{bullets}")
    if timed_out:
        text += "\n\n(Hit the 2-hour time box -- more queued and ready for tomorrow's pass.)"
    return _send(text)


def send_ondemand_report(focus: str, ok: bool, summary: str = "", latest_commit: str = "") -> bool:
    """Sent once after every on-demand self_improve(focus) run -- i.e. every
    time the user asks Jarvis, live, to improve/upgrade/fix one of its own
    capabilities (distinct from send_report(), which only covers the daily
    6 AM audit). `focus` is what the user asked to have improved; `ok`
    mirrors self_improve()'s own success flag; `latest_commit` (if any) is
    the `git log -1` line captured right after the run, used here to prove
    a real, verified change landed rather than just describing one. A
    confirmed send lands in the shared delivery log same as any other send
    from this agent, so it also shows up in agent_status()."""
    focus = (focus or "").strip() or "something"
    if not ok:
        reason = summary.strip() if summary else "hit a problem partway through."
        text = f"Asked to improve \"{focus}\" -- couldn't finish: {reason}"
    elif latest_commit:
        text = f"Asked to improve \"{focus}\" -- done and committed: {latest_commit}"
    else:
        text = (f"Asked to improve \"{focus}\" -- looked into it, but nothing safe "
                "and verifiable to commit came out of this pass.")
    return _send(text)


def send_test_message() -> bool:
    """Manual, on-demand test send -- lets the user confirm this bot's
    token/chat ID still deliver right now, without waiting for the next
    6 AM report. Called from tools.send_agent_test_message() when the
    user asks Jarvis to test/ping this agent's Telegram channel. A
    confirmed send lands in the shared delivery log same as any other
    send from this agent, so it also shows up in agent_status()."""
    return _send("This is a manual test message, sir -- if you're reading this, "
                  "the JarvisImprovement Telegram pipeline is working.")


def poll_thread(pipeline_stop) -> None:
    """This bot's two-way half: long-polls its own token/chat (isolated from
    every other bot's inbox). A question (see _looks_like_question()) gets
    an immediate reply, grounded via telegram_common.ask_grounded() in
    tools.agent_status()'s real on-disk history -- no self-improve run
    triggered. Anything else is treated as an on-demand self-improvement
    request, run through tools.self_improve() -- the exact same mechanism,
    constraints, and build-verify gate as the "Jarvis, improve on ..."
    voice/text command. self_improve() can take several minutes (it shells
    out to Claude Code), so it's handed off to a background thread rather
    than run on the poll loop itself, which would otherwise sit unable to
    notice a stop signal or a second incoming message until the run
    finished; an immediate ack goes out first so the user knows it was
    received. self_improve() already sends its own JarvisImprovement report
    (via tools._report_ondemand_improve) once the run completes, so nothing
    further needs to be sent from here. tools is imported locally, not at
    module load time, because tools.py imports this module -- importing it
    back up top would be a circular import."""
    def _run(focus):
        import tools
        try:
            tools.self_improve(focus)
        except Exception as e:
            print(f"  [JarvisImprovement] on-demand run failed: {e}")

    def on_message(text):
        if _looks_like_question(text):
            import tools
            try:
                context = tools.agent_status()
            except Exception as e:
                context = f"Couldn't pull agent status: {e}"
            reply = telegram_common.ask_grounded(
                _AGENT_NAME, "running Jarvis's daily self-improvement passes and reporting on them",
                context, text)
            _send(reply)
            return
        _send(f"On it sir -- looking into \"{text.strip()}\" now. I'll report back when it's done.")
        threading.Thread(target=_run, args=(text,), daemon=True).start()

    telegram_common.poll_thread(BOT_TOKEN, CHAT_ID, on_message, pipeline_stop, agent=_AGENT_NAME)
