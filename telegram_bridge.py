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
#   Also accepts bank statement attachments -- any file type/name a bank or
#   phone hands us (CSV/TXT/PDF/XLSX/XLS/OFX/QFX/ZIP, a generically-named
#   "document_<id>.pdf", or a screenshot sent as a photo or as a file): none
#   of that is filtered by extension on receipt, since statements_service's
#   parsers (and sync_bank_data's silent skip of anything unrecognized) are
#   the ones equipped to judge what a file actually is. They're downloaded
#   into a private inbox under ~/.jarvis, and the "sync statements" command
#   (tools.sync_bank_data) moves whatever is waiting there into
#   statements_service's "latest bank statements" folder before parsing it
#   into the ledger. See import_latest_to().
#
#   Fully inert until TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are set
#   in .env -- see README's "Text Jarvis (Telegram)" section.
# ================================================================
import os
import re
import time
import shutil

import requests

import telegram_common

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"

TELEGRAM_AVAILABLE = bool(BOT_TOKEN and CHAT_ID)

# Staging area for files sent over Telegram before they're imported --
# separate from statements_service's own folder so a file only lands in
# the real "latest bank statements" folder once the user actually asks
# to sync, never silently on receipt.
INBOX_DIR = os.path.join(os.path.expanduser("~"), ".jarvis", "telegram_inbox")

# Formats statements_service actually has a parser for -- used only to pick
# the confirmation wording below (a heads-up when we likely can't read a
# file, e.g. a .heic photo or a .docx), never to reject a file on receipt.
# Every one of these is read-only on receipt -- never executed, never opened
# in an external viewer/macro-enabled app: CSV/TXT via csv.DictReader, PDF
# via pypdf's text-layer extraction, XLSX/XLS via openpyxl's read-only cell
# values (macros are never run), OFX/QFX via plain regex, images via OCR
# (pixels only, through Pillow), and ZIPs via a path-traversal/zip-bomb
# guarded extraction (see statements_service._extract_zip). A file with some
# other extension (e.g. a bank's generic "document_<id>.pdf" -- already
# covered above -- or a format we don't recognize at all) is still accepted:
# it's saved and handed to sync_bank_data like any other, which walks by
# extension and silently skips anything it has no parser for, rather than us
# guessing wrong here.
_KNOWN_EXTS = {
    ".csv", ".txt", ".ofx", ".qfx", ".pdf",
    ".xlsx", ".xls", ".zip",
    ".jpg", ".jpeg", ".png", ".webp",
}


def send_message(text: str) -> bool:
    """Message the configured chat. Best-effort -- returns False on failure
    rather than raising, so a network hiccup never crashes a command."""
    return telegram_common.send(BOT_TOKEN, CHAT_ID, text)


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


def _save_incoming_file(filename, file_id):
    """Shared by _handle_document and _handle_photo: download file_id and
    stash it in INBOX_DIR under a timestamp-prefixed (collision-proof) name."""
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
    ext = os.path.splitext(filename)[1].lower()
    if ext in _KNOWN_EXTS:
        send_message(f"Got {filename} sir. Say \"sync statements\" and I'll import it.")
    else:
        send_message(
            f"Got {filename} sir -- I don't recognize that as a bank export format, but "
            f"I'll take a look when you say \"sync statements\".")


def _handle_document(document):
    """Download an incoming file attachment into INBOX_DIR. Accepts any file
    type/name -- including a bank's generically-named "document_<id>.pdf" --
    rather than rejecting on extension; files here are only ever read (never
    executed) by statements_service's parsers, and sync_bank_data silently
    skips anything it doesn't recognize at sync time rather than us guessing
    wrong here on receipt."""
    filename = _sanitize_filename(document.get("file_name"))
    file_id = document.get("file_id")
    if not file_id:
        return
    _save_incoming_file(filename, file_id)


def _handle_photo(photo_sizes):
    """Screenshots of a banking app or a statement (photographed or
    screenshotted) usually arrive as a compressed Telegram 'photo', not a
    'document'. Telegram sends several resolutions of the same image --
    grab the largest for the clearest OCR read later. Named uniquely from
    the file_id so back-to-back screenshots in the same second never
    collide/overwrite each other in INBOX_DIR."""
    if not photo_sizes:
        return
    largest = max(photo_sizes, key=lambda p: p.get("file_size") or (p.get("width", 0) * p.get("height", 0)))
    file_id = largest.get("file_id")
    if not file_id:
        return
    suffix = re.sub(r"[^A-Za-z0-9]", "", file_id)[-10:] or "img"
    _save_incoming_file(f"statement_photo_{suffix}.jpg", file_id)


def import_latest_to(dest_dir):
    """Move every file currently waiting in the Telegram inbox into dest_dir
    (statements_service's "latest bank statements" folder), oldest-received
    first. Called by tools.sync_bank_data on a "sync statements" command.
    Clears the inbox as it goes, so the next sync only picks up files sent
    after this one. Returns the list of imported filenames."""
    if not os.path.isdir(INBOX_DIR):
        return []
    # Every file waiting here was already accepted on receipt (see
    # _handle_document) regardless of extension, so move all of them -- not
    # just ones matching _KNOWN_EXTS -- and let statements_service.sync()
    # decide what it can parse.
    pending = sorted(
        (f for f in os.listdir(INBOX_DIR) if os.path.isfile(os.path.join(INBOX_DIR, f))),
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

                photo = message.get("photo")
                if photo:
                    print(f"  [Telegram] Received photo ({len(photo)} size(s))")
                    try:
                        _handle_photo(photo)
                    except Exception as e:
                        print(f"  [Telegram] Photo handling error: {e}")
                        send_message("Something went wrong saving that photo sir.")
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
