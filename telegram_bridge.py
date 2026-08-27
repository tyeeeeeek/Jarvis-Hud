# ================================================================
#   J.A.R.V.I.S — Telegram bridge
#
#   Free, no-card-ever alternative to the Twilio SMS bridge: same
#   idea (message Jarvis, it runs the command, replies back), but
#   over Telegram's official Bot API instead of SMS. Uses long
#   polling (getUpdates with a server-side wait) -- no public
#   webhook/inbound networking needed, and no per-message cost ever.
#
#   Locked to a single chat_id so a bot token leak or a guessed
#   username can't hand a stranger command access to your PC.
#
#   Also accepts file attachments (bank statement CSV/OFX/QFX exports):
#   they're downloaded into a private inbox under ~/.jarvis, and the
#   "sync statements" command (tools.sync_bank_data) moves whatever is
#   waiting there into statements_service's "latest bank statements"
#   folder before parsing it into the ledger. See import_latest_to().
#
#   Fully inert until TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are set
#   in .env -- see README's "Text Jarvis (Telegram)" section.
# ================================================================
import os
import re
import time
import shutil

import requests

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"

TELEGRAM_AVAILABLE = bool(BOT_TOKEN and CHAT_ID)

# Staging area for files sent over Telegram before they're imported --
# separate from statements_service's own folder so a file only lands in
# the real "latest bank statements" folder once the user actually asks
# to sync, never silently on receipt.
INBOX_DIR = os.path.join(os.path.expanduser("~"), ".jarvis", "telegram_inbox")

# Only formats statements_service actually knows how to parse (CSV, plus
# a couple of extensions some banks use for the same delimited-text
# format). Deliberately no PDFs/images/archives -- nothing here is ever
# executed or opened, only read as text by the CSV parser.
_ALLOWED_EXTS = {".csv", ".txt", ".ofx", ".qfx"}


def send_message(text: str) -> bool:
    """Message the configured chat. Best-effort -- returns False on failure
    rather than raising, so a network hiccup never crashes a command."""
    if not TELEGRAM_AVAILABLE or not text:
        return False
    try:
        r = requests.post(f"{API_BASE}/sendMessage", json={
            "chat_id": CHAT_ID, "text": text[:4000],
        }, timeout=15)
        return r.status_code == 200
    except Exception as e:
        print(f"  [Telegram] Send error: {e}")
        return False


def _sanitize_filename(name):
    name = os.path.basename((name or "").strip()) or "statement"
    name = re.sub(r'[\\/:*?"<>|]', "", name).replace("..", "")
    return name.strip() or "statement"


def _download_file(file_id):
    r = requests.get(f"{API_BASE}/getFile", params={"file_id": file_id}, timeout=15)
    r.raise_for_status()
    file_path = r.json()["result"]["file_path"]
    file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
    resp = requests.get(file_url, timeout=60)
    resp.raise_for_status()
    return resp.content


def _handle_document(document):
    """Download an incoming file attachment into INBOX_DIR. Rejects anything
    that isn't a statement-like format up front -- files here are only ever
    read as text by statements_service's CSV parser, never executed."""
    filename = _sanitize_filename(document.get("file_name"))
    ext = os.path.splitext(filename)[1].lower()
    if ext not in _ALLOWED_EXTS:
        send_message(
            f"I can only import bank statement exports (.csv, .txt, .ofx, .qfx) sir -- "
            f"{filename} isn't one of those.")
        return
    file_id = document.get("file_id")
    if not file_id:
        return
    try:
        data = _download_file(file_id)
    except Exception as e:
        print(f"  [Telegram] Document download error: {e}")
        send_message("I couldn't download that file sir.")
        return
    os.makedirs(INBOX_DIR, exist_ok=True)
    dest = os.path.join(INBOX_DIR, f"{int(time.time())}_{filename}")
    try:
        with open(dest, "wb") as f:
            f.write(data)
    except Exception as e:
        print(f"  [Telegram] Couldn't save document: {e}")
        send_message("I downloaded that file but couldn't save it sir.")
        return
    print(f"  [Telegram] Saved document -> {dest}")
    send_message(f"Got {filename} sir. Say \"sync statements\" and I'll import it.")


def import_latest_to(dest_dir):
    """Move every file currently waiting in the Telegram inbox into dest_dir
    (statements_service's "latest bank statements" folder), oldest-received
    first. Called by tools.sync_bank_data on a "sync statements" command.
    Clears the inbox as it goes, so the next sync only picks up files sent
    after this one. Returns the list of imported filenames."""
    if not os.path.isdir(INBOX_DIR):
        return []
    pending = sorted(
        (f for f in os.listdir(INBOX_DIR) if os.path.splitext(f)[1].lower() in _ALLOWED_EXTS),
        key=lambda f: os.path.getmtime(os.path.join(INBOX_DIR, f)),
    )
    if not pending:
        return []
    os.makedirs(dest_dir, exist_ok=True)
    imported = []
    for fname in pending:
        src = os.path.join(INBOX_DIR, fname)
        # Strip the epoch-timestamp prefix _handle_document added so the
        # imported copy has the bank's original, readable filename.
        clean_name = fname.split("_", 1)[1] if "_" in fname else fname
        dst = os.path.join(dest_dir, clean_name)
        try:
            shutil.move(src, dst)
            imported.append(clean_name)
        except Exception as e:
            print(f"  [Telegram] Couldn't import {fname}: {e}")
    return imported


def poll_thread(on_command, pipeline_stop):
    """Background loop using long polling: each getUpdates call blocks
    server-side for up to ~25s waiting for a new message, so this is near-
    instant without hammering the API. Calls on_command(text) for every new
    text message from the authorized chat_id; file attachments are handled
    directly here (see _handle_document). Silently ignores anyone else."""
    if not TELEGRAM_AVAILABLE:
        print("  [Telegram] Not configured -- texting Jarvis is disabled. See README.")
        return

    print(f"  [Telegram] Watching for messages from chat {CHAT_ID}")

    # Consume any backlog without acting on it -- only react to messages
    # sent after Jarvis started watching.
    offset = None
    try:
        r = requests.get(f"{API_BASE}/getUpdates", params={"timeout": 0}, timeout=15)
        updates = r.json().get("result", [])
        if updates:
            offset = updates[-1]["update_id"] + 1
    except Exception as e:
        print(f"  [Telegram] Startup sync error: {e}")

    while not pipeline_stop.is_set():
        try:
            params = {"timeout": 25}
            if offset is not None:
                params["offset"] = offset
            r = requests.get(f"{API_BASE}/getUpdates", params=params, timeout=35)
            updates = r.json().get("result", [])
            for update in updates:
                offset = update["update_id"] + 1
                message = update.get("message") or {}
                chat_id = str(message.get("chat", {}).get("id", ""))
                if chat_id != CHAT_ID:
                    continue

                document = message.get("document")
                if document:
                    print(f"  [Telegram] Received document: {document.get('file_name')!r}")
                    try:
                        _handle_document(document)
                    except Exception as e:
                        print(f"  [Telegram] Document handling error: {e}")
                        send_message("Something went wrong saving that file sir.")
                    continue

                text = (message.get("text") or "").strip()
                if not text:
                    continue
                print(f"  [Telegram] Received: {text!r}")
                try:
                    on_command(text)
                except Exception as e:
                    print(f"  [Telegram] Command handling error: {e}")
                    send_message("Something went wrong handling that sir.")
        except requests.Timeout:
            continue
        except Exception as e:
            print(f"  [Telegram] Poll error: {e}")
            time.sleep(5)
