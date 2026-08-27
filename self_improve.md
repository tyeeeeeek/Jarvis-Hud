# Jarvis HUD — autonomous nightly maintenance pass

You are running unattended, on a schedule, with full local tool access to
this one project folder. Nobody will review your changes before they take
effect — act accordingly: be conservative, verify everything yourself, and
never leave the repo in a broken or worse state than you found it.

## Hard constraints — never violate these, no matter how good the reason seems

1. Never add a general shell-exec / arbitrary-code-execution tool to the set
   exposed in `jarvis_mcp_server.py`. Every capability the brain can call
   must stay a specific, narrow, named function in `tools.py`.
2. Never widen the `_SAFE_DIRS` filesystem scope in `tools.py` beyond adding
   a clearly-reasonable, explicitly-named folder (never a variable/arbitrary
   path).
3. Never make `delete_item` (or any new delete capability) bypass the
   Recycle Bin.
4. Never make `close_app` (or any new capability) able to close/kill a
   process outside the existing curated `_APP_ALIASES`/`_APP_IMAGE_NAMES`
   list.
5. Never make `draft_email` (or any new capability) send email automatically
   — draft-only, always.
6. Never weaken the sandboxing on generated content: `CreationPanel.tsx`'s
   iframe must keep `sandbox="allow-scripts"` with no `allow-same-origin`,
   `allow-top-navigation`, or `allow-popups`; `build_creation`'s prompt in
   `tools.py` must keep forbidding network calls/localStorage in generated
   pages.
7. Never touch files outside this project folder.
8. If you are genuinely unsure whether a change is safe, don't make it —
   note why in your summary instead of guessing.

## Verification gate — required before every commit

- Python changes: run a syntax check (e.g. `python -m py_compile <file>` or
  equivalent) on every changed `.py` file.
- Frontend changes: run `npm run build` and confirm it succeeds.
- If either check fails and you can't fix it within this run, revert that
  specific change (`git checkout -- <file>` or `git restore`) rather than
  leaving the repo broken. Never leave a failing build committed.

## Workflow

1. Look at what's actually here: recent runtime behavior if logs exist
   (`.jarvis/` folder, terminal output patterns), the current state of
   `tools.py`/`brain.py`/`jarvis.py`/the frontend, and anything that looks
   like a bug, a rough edge, a missing docstring detail that would help the
   brain pick the right tool, or a small genuinely useful capability that's
   obviously missing.
2. Make small, independent, verifiable changes — prefer several small
   correct commits over one large sweeping one. Each commit should be one
   logical change with a clear message explaining what and why.
3. After each change: run the relevant verification gate above, then
   `git add` + `git commit` if it passes.
4. When done (or if you found nothing worth changing), write a short
   summary to `.jarvis/self_improve_logs/<UTC timestamp>.md`: what you
   changed and why, or that nothing needed doing and what you checked.

Keep the whole pass modest in scope — this runs again tomorrow night.
