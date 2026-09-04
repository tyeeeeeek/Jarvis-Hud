# ================================================================
#   J.A.R.V.I.S — long-term memory
#
#   Replaces "Jarvis only remembers the last few conversation turns" with
#   real memory that survives across days and restarts. SQLite-backed,
#   zero new dependencies -- sqlite3 is in the Python standard library,
#   and FTS5 (full-text search, also built into SQLite) gives relevance-
#   ranked recall without needing an embeddings model or a vector DB.
#   "Good enough" semantic-ish search, not real semantic search -- a
#   deliberate simplicity tradeoff, consistent with this project's
#   existing preference for cheap/local/no-new-dependency solutions
#   (e.g. email_watcher.py's classify-with-local-Ollama pattern, which
#   maybe_capture() below reuses directly).
#
#   Two ways something gets remembered:
#     - Explicit: tools.remember_this(text) -- "Jarvis, remember that ..."
#     - Passive: brain.py calls maybe_capture() after every completed
#       turn; a cheap local Ollama call (never Claude -- this runs after
#       EVERY command, so it has to be free) decides whether the exchange
#       contained a durable fact/preference/decision worth keeping, and
#       stores just the one-sentence extraction, never the raw transcript.
# ================================================================
import os
import re
import sqlite3
import time

HOME = os.path.expanduser("~")
JARVIS_DIR = os.path.join(HOME, ".jarvis")
DB_PATH = os.path.join(JARVIS_DIR, "memory.db")

_OLLAMA_URL = "http://localhost:11434/api/generate"
_OLLAMA_MODEL = "llama3.2"

# Every stored memory is phrased starting with "User ..." (see
# _CLASSIFY_PROMPT below), so "user" alone would match nearly everything
# in an OR query -- filtering common words out meaningfully sharpens
# recall() without needing real NLP; not exhaustive, just enough to stop
# the most common noise words from flooding an OR-of-words match.
_STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "am",
    "do", "does", "did", "has", "have", "had", "user", "users", "what",
    "when", "where", "who", "how", "why", "which", "this", "that",
    "of", "to", "in", "on", "for", "with", "and", "or", "but", "not",
    "my", "me", "i", "it", "its", "their", "they", "them",
}


def _connect():
    os.makedirs(JARVIS_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'fact',
            created_at REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
            text, content='memories', content_rowid='id'
        )
    """)
    # Triggers keep the FTS index in sync automatically, so every write
    # path (just remember() below) only ever has to touch the one real
    # table -- no risk of the two tables drifting out of sync.
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
            INSERT INTO memories_fts(rowid, text) VALUES (new.id, new.text);
        END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
            INSERT INTO memories_fts(memories_fts, rowid, text) VALUES('delete', old.id, old.text);
        END
    """)
    return conn


def remember(text: str, category: str = "fact") -> int:
    """Store one durable memory. Returns its row id, or -1 if text is
    empty."""
    text = (text or "").strip()
    if not text:
        return -1
    conn = _connect()
    try:
        cur = conn.execute(
            "INSERT INTO memories (text, category, created_at) VALUES (?, ?, ?)",
            (text, category, time.time()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def recall(query: str, limit: int = 5, fallback_to_recent: bool = True) -> list:
    """Full-text search over everything remembered, ranked by relevance
    (SQLite FTS5's bm25 ranking). By default falls back to the most recent
    memories if the query has no FTS matches at all -- appropriate for the
    explicit recall_memory tool, where the user asked "what do you
    remember" and wants something back regardless. brain.py's ambient
    per-command recall passes fallback_to_recent=False instead, since
    showing unrelated recent memories on every single command would just
    be noise, not context."""
    query = (query or "").strip()
    if not query:
        return recent(limit) if fallback_to_recent else []
    conn = _connect()
    try:
        # A raw natural-language sentence breaks FTS5's query syntax on
        # stray punctuation (quotes, hyphens, etc.) -- reduce to a simple
        # OR-of-words query instead of passing the sentence through as-is.
        words = [w for w in re.findall(r"[A-Za-z0-9']+", query) if w.lower() not in _STOPWORDS]
        if not words:
            return recent(limit) if fallback_to_recent else []
        fts_query = " OR ".join(words[:12])
        rows = conn.execute(
            """SELECT m.id, m.text, m.category, m.created_at
               FROM memories_fts f JOIN memories m ON m.id = f.rowid
               WHERE memories_fts MATCH ? ORDER BY bm25(memories_fts) LIMIT ?""",
            (fts_query, limit),
        ).fetchall()
        if not rows:
            return recent(limit) if fallback_to_recent else []
        return [{"id": r[0], "text": r[1], "category": r[2], "created_at": r[3]} for r in rows]
    except sqlite3.OperationalError:
        return recent(limit) if fallback_to_recent else []
    finally:
        conn.close()


def recent(limit: int = 10) -> list:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id, text, category, created_at FROM memories ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [{"id": r[0], "text": r[1], "category": r[2], "created_at": r[3]} for r in rows]
    finally:
        conn.close()


def forget(memory_id: int) -> bool:
    conn = _connect()
    try:
        cur = conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


_CLASSIFY_PROMPT = (
    "You decide whether a short exchange between a user and their AI assistant contains "
    "something worth remembering long-term: a stated preference, a fact about the user or "
    "their life/work, a decision they made, a recurring habit or plan. Most exchanges (a "
    "time check, a media control, small talk, a one-off tool result) contain nothing worth "
    "remembering -- only flag genuinely durable, personally-specific information.\n\n"
    "Reply with EXACTLY one line: either the word NONE, or a single, self-contained sentence "
    "stating the fact in third person (e.g. \"User prefers dark roast coffee.\" or \"User's "
    "dentist appointment recurs every 6 months.\"). No markdown, no preamble.\n\n"
    "User said: {command}\nAssistant replied: {reply}\n"
)


def maybe_capture(command: str, reply: str) -> None:
    """Passive capture, called after every completed command. Best-effort
    and silent on any failure (Ollama not running, a bad response) -- this
    must never block or break a real command's reply, which is why brain.py
    calls it in a background thread rather than inline."""
    if not command or not reply:
        return
    try:
        import requests
        r = requests.post(_OLLAMA_URL, json={
            "model": _OLLAMA_MODEL,
            "prompt": _CLASSIFY_PROMPT.format(command=command[:300], reply=reply[:300]),
            "stream": False,
        }, timeout=15)
        if r.status_code != 200:
            return
        text = (r.json().get("response") or "").strip()
        if not text or text.upper().startswith("NONE"):
            return
        remember(text, category="auto")
    except Exception:
        pass
