# ================================================================
#   Local bank statement import — no API, no account linking.
#
#   Drop a bank/card export from your bank's own website into
#   ~/JarvisStatements (or send it to the Telegram bot and say
#   "sync statements" -- see telegram_bridge.import_latest_to, which
#   lands it in the "latest bank statements" subfolder below) and
#   say "Jarvis sync my bank data". Everything is parsed and stored
#   locally (statements_ledger.json); nothing here ever leaves this
#   machine, and every format below is only ever *read*, never executed
#   or opened in an external viewer:
#     - CSV/TXT           csv.DictReader (or delimiter-sniffed for .txt)
#     - PDF                pypdf's text-layer extraction only
#     - XLSX/XLS           openpyxl, read-only cell values (no macros run)
#     - OFX/QFX            plain regex extraction of <STMTTRN> blocks
#     - JPG/PNG/WEBP        OCR'd to text via pytesseract, then parsed the
#                          same way a PDF's text layer would be
#     - ZIP                 safely expanded (path-traversal/zip-bomb
#                          guarded) and every file inside re-run through
#                          the list above, whatever it's named
#   Arbitrary/unexpected filenames are never a problem -- files are
#   discovered by walking the folder and dispatching on extension, not by
#   matching an expected name pattern.
# ================================================================
import os
import re
import csv
import json
import hashlib
import zipfile
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATEMENTS_DIR = os.path.join(os.path.expanduser("~"), "JarvisStatements")
LATEST_STATEMENTS_DIR = os.path.join(STATEMENTS_DIR, "latest bank statements")
LEDGER_PATH = os.path.join(BASE_DIR, "statements_ledger.json")

_DATE_KEYS = ["date", "transaction date", "posted date", "post date"]
_DESC_KEYS = ["description", "name", "merchant", "payee", "transaction description"]
_AMOUNT_KEYS = ["amount"]
_DEBIT_KEYS = ["debit", "withdrawal", "debit amount"]
_CREDIT_KEYS = ["credit", "deposit", "credit amount"]
_CATEGORY_KEYS = ["category", "type"]
_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y")

# PDF statements have no header row to key off of, so transactions are
# recovered line-by-line: a line must start with a recognizable date and
# contain at least one dollar amount to be treated as a transaction.
_PDF_LINE_DATE_RE = re.compile(r"^(\d{4}-\d{1,2}-\d{1,2}|\d{1,2}/\d{1,2}/\d{2,4}|\d{1,2}/\d{1,2})\b")
_MONEY_RE = re.compile(r"\(?-?\$?\d[\d,]*\.\d{2}\)?-?")
_YEAR_RE = re.compile(r"\b(20\d{2})\b")

# Some banks (e.g. SoFi) print transaction dates as "Jul 31, 2026" rather
# than a numeric format -- matched separately from _PDF_LINE_DATE_RE since
# it needs its own month-name-to-number lookup.
_MONTH_NAMES = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
_MONTH_LOOKUP = {m: i + 1 for i, m in enumerate(_MONTH_NAMES)}
_MONTH_DATE_RE = re.compile(r"^(" + "|".join(_MONTH_NAMES) + r")[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})\b", re.IGNORECASE)

# Some exports (again, e.g. SoFi) split one transaction across three lines --
# date+description, then a reference line like "Transaction ID: 2999-1",
# then the actual "$amount $balance" line -- rather than keeping everything
# on one line. This recognizes that middle reference line specifically, so
# the amount lookahead below only ever skips past a line that's clearly
# metadata, never blindly scanning ahead into unrelated statement text.
_METADATA_LINE_RE = re.compile(r"^(Transaction ID|Ref(?:erence)?\.?\s*#?|Confirmation\s*#?|Trace\s*#?)\s*:?", re.IGNORECASE)


def ensure_statements_dir():
    os.makedirs(STATEMENTS_DIR, exist_ok=True)
    os.makedirs(LATEST_STATEMENTS_DIR, exist_ok=True)
    readme = os.path.join(STATEMENTS_DIR, "README.txt")
    if not os.path.exists(readme):
        with open(readme, "w") as f:
            f.write(
                "Drop your bank/card exports here (SoFi, Capital One, Discover, etc.) -- "
                "CSV, TXT, PDF, XLSX/XLS, OFX/QFX, a screenshot (JPG/PNG/WEBP), or a ZIP of "
                "any of those, named however your bank named it -- then say "
                "'Jarvis sync my bank data'.\n\n"
                "Files sent as attachments (or photos) to the Telegram bot land in the "
                "'latest bank statements' subfolder automatically once you say "
                "'sync statements'.\n\n"
                "These files are read locally only -- nothing in this folder is ever "
                "uploaded anywhere.\n"
            )


def _find_key(fieldnames, candidates):
    lower = {f.lower().strip(): f for f in fieldnames}
    for c in candidates:
        if c in lower:
            return lower[c]
    return None


def _load_ledger():
    if not os.path.exists(LEDGER_PATH):
        return {}
    try:
        with open(LEDGER_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_ledger(ledger):
    with open(LEDGER_PATH, "w") as f:
        json.dump(ledger, f)


def _fingerprint(date_str, desc, amount):
    raw = f"{date_str}|{desc.strip().lower()}|{amount:.2f}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _parse_amount(val):
    val = val.replace("$", "").replace(",", "").strip()
    if val.startswith("(") and val.endswith(")"):
        val = "-" + val[1:-1]
    # Some statements (PDFs especially) mark debits with a trailing minus,
    # e.g. "123.45-", instead of a leading one.
    if val.endswith("-"):
        val = "-" + val[:-1]
    return float(val)


def _parse_date(raw, default_year=None):
    raw = raw.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    # PDF statements often print transaction dates as bare "MM/DD" and rely
    # on the statement period (shown once, elsewhere on the page) for the
    # year -- default_year lets callers supply that.
    if default_year:
        try:
            return datetime.strptime(f"{raw}/{default_year}", "%m/%d/%Y")
        except ValueError:
            return None
    return None


def _fix_sign_convention(rows):
    """Some banks export debits as negative numbers in a single Amount
    column (opposite of the "charge = positive" convention others use).
    If most non-zero rows in this file are negative, it's that style --
    flip so positive always means "money spent" across our ledger."""
    neg = sum(1 for r in rows if r["amount"] < 0)
    pos = sum(1 for r in rows if r["amount"] > 0)
    if neg > pos * 2:
        for r in rows:
            r["amount"] = -r["amount"]
    return rows


def _row_to_transaction(row, date_key, desc_key, amount_key, debit_key, credit_key, category_key, source_name):
    """Shared by the delimited-text (CSV/TXT) and XLSX/XLS parsers -- `row`
    is a dict keyed by column header, values may be strings (CSV) or native
    cell types like datetime/float (XLSX)."""
    raw_date = row.get(date_key)
    if isinstance(raw_date, datetime):
        date_obj = raw_date
    else:
        date_obj = _parse_date(str(raw_date if raw_date is not None else "").strip())
    if date_obj is None:
        return None

    raw_desc = row.get(desc_key)
    desc = str(raw_desc if raw_desc is not None else "").strip()

    amount = None
    raw_amount = row.get(amount_key) if amount_key else None
    if amount_key and str(raw_amount if raw_amount is not None else "").strip():
        amount = _parse_amount(str(raw_amount))
    elif debit_key or credit_key:
        raw_debit = row.get(debit_key) if debit_key else None
        raw_credit = row.get(credit_key) if credit_key else None
        debit = str(raw_debit if raw_debit is not None else "").strip()
        credit = str(raw_credit if raw_credit is not None else "").strip()
        if debit:
            amount = abs(_parse_amount(debit))
        elif credit:
            amount = -abs(_parse_amount(credit))
        else:
            return None
    else:
        return None

    raw_category = row.get(category_key) if category_key else None
    category = str(raw_category).strip() if raw_category not in (None, "") else ""
    return {
        "date": date_obj.strftime("%Y-%m-%d"),
        "description": desc,
        "amount": amount,
        "category": category or "Uncategorized",
        "source_file": source_name,
    }


def _parse_delimited_file(path, sniff=False):
    """CSV parsing, and (when `sniff` is set, for .txt exports of unknown
    delimiter) auto-detection of comma/tab/semicolon/pipe-delimited text."""
    rows = []
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
        dialect = csv.excel
        if sniff:
            sample = f.read(4096)
            f.seek(0)
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
            except csv.Error:
                dialect = csv.excel
        reader = csv.DictReader(f, dialect=dialect)
        if not reader.fieldnames:
            return rows
        date_key = _find_key(reader.fieldnames, _DATE_KEYS)
        desc_key = _find_key(reader.fieldnames, _DESC_KEYS)
        amount_key = _find_key(reader.fieldnames, _AMOUNT_KEYS)
        debit_key = _find_key(reader.fieldnames, _DEBIT_KEYS)
        credit_key = _find_key(reader.fieldnames, _CREDIT_KEYS)
        category_key = _find_key(reader.fieldnames, _CATEGORY_KEYS)
        if not date_key or not desc_key:
            if not sniff:
                print(f"  [Statements] Couldn't find date/description columns in {os.path.basename(path)}")
            return rows

        source_name = os.path.basename(path)
        for row in reader:
            try:
                tx = _row_to_transaction(row, date_key, desc_key, amount_key, debit_key, credit_key, category_key, source_name)
                if tx:
                    rows.append(tx)
            except Exception:
                continue

    return _fix_sign_convention(rows)


def _parse_csv_file(path):
    return _parse_delimited_file(path, sniff=False)


def _parse_txt_file(path):
    """.txt statement exports vary a lot: delimited tables (tab/semicolon/
    pipe as often as comma), OFX/QFX text some banks mislabel, or plain text
    that reads like a PDF's extracted text layer. Try each in turn."""
    rows = _parse_delimited_file(path, sniff=True)
    if rows:
        return rows

    try:
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            text = f.read()
    except Exception as e:
        print(f"  [Statements] Couldn't read {os.path.basename(path)}: {e}")
        return []

    if _OFX_TXN_RE.search(text):
        return _parse_ofx_text(text, os.path.basename(path))

    rows = _parse_statement_text(text, os.path.basename(path))
    if not rows:
        print(f"  [Statements] Couldn't find a transaction table or recognizable "
              f"transaction lines in {os.path.basename(path)}")
    return rows


def _parse_xlsx_file(path):
    try:
        import openpyxl
    except ImportError:
        print(f"  [Statements] Can't parse {os.path.basename(path)} -- the 'openpyxl' package "
              f"isn't installed. Run: pip install openpyxl")
        return []

    try:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb.active
        rows_iter = ws.iter_rows(values_only=True)
        header = next(rows_iter, None)
    except Exception as e:
        print(f"  [Statements] Couldn't read {os.path.basename(path)}: {e}")
        return []

    if not header:
        return []
    fieldnames = [str(h).strip() if h is not None else "" for h in header]
    date_key = _find_key(fieldnames, _DATE_KEYS)
    desc_key = _find_key(fieldnames, _DESC_KEYS)
    amount_key = _find_key(fieldnames, _AMOUNT_KEYS)
    debit_key = _find_key(fieldnames, _DEBIT_KEYS)
    credit_key = _find_key(fieldnames, _CREDIT_KEYS)
    category_key = _find_key(fieldnames, _CATEGORY_KEYS)
    if not date_key or not desc_key:
        print(f"  [Statements] Couldn't find date/description columns in {os.path.basename(path)}")
        return []

    source_name = os.path.basename(path)
    rows = []
    for values in rows_iter:
        row = {fieldnames[i]: v for i, v in enumerate(values) if i < len(fieldnames)}
        try:
            tx = _row_to_transaction(row, date_key, desc_key, amount_key, debit_key, credit_key, category_key, source_name)
            if tx:
                rows.append(tx)
        except Exception:
            continue

    return _fix_sign_convention(rows)


def _guess_statement_year(text):
    """Best-effort year for bare "MM/DD" transaction lines: the first
    4-digit year anywhere on the statement (almost always present in a
    "Statement Period"/"Closing Date" header near the top of page one)."""
    m = _YEAR_RE.search(text)
    return m.group(1) if m else str(datetime.now().year)


def _match_leading_date(line, default_year):
    """Try both supported "start of line" date styles -- numeric
    (_PDF_LINE_DATE_RE, e.g. "07/29/2026" or a bare "07/29") and month-name
    (_MONTH_DATE_RE, e.g. "Jul 29, 2026"). Returns (date_obj,
    rest_of_line_after_the_date) or (None, None)."""
    m = _PDF_LINE_DATE_RE.match(line)
    if m:
        date_obj = _parse_date(m.group(1), default_year)
        if date_obj is not None:
            return date_obj, line[m.end():].strip()
    m = _MONTH_DATE_RE.match(line)
    if m:
        try:
            date_obj = datetime(int(m.group(3)), _MONTH_LOOKUP[m.group(1)[:3].lower()], int(m.group(2)))
        except ValueError:
            return None, None
        return date_obj, line[m.end():].strip()
    return None, None


def _parse_statement_text(text, source_name):
    """"Date + $ amount" recovery shared by PDF text layers, plain-text
    (.txt) statements, and OCR'd image text -- all three end up as loose
    lines with no header row to key off of. Handles two record shapes:
    same-line (date, description, and amount all on one line -- most CSV-
    style exports) and multi-line (a date+description line, optionally
    followed by a reference line like "Transaction ID: ...", with the
    amount appearing on the next line instead -- confirmed live against a
    real SoFi PDF statement, where a same-line-only parser found zero
    transactions in genuine statements). Only ever looks 1-2 lines past a
    date line (and only past a second one when the first is recognizably a
    reference/metadata line, never a blind scan), so unrelated date-like
    text elsewhere in a statement (an "as of <date>" balance blurb, a
    statement-period header) can't be mistaken for a transaction just
    because a dollar figure happens to appear a few lines later."""
    rows = []
    default_year = _guess_statement_year(text)
    lines = [l.strip() for l in text.splitlines()]
    i = 0
    while i < len(lines):
        date_obj, rest = _match_leading_date(lines[i], default_year)
        if date_obj is None:
            i += 1
            continue

        amount = None
        desc = rest
        same_line_money = list(_MONEY_RE.finditer(rest))
        if same_line_money:
            # First dollar figure after the description is the transaction
            # amount; a second one (a running-balance column) is ignored.
            first = same_line_money[0]
            desc = rest[:first.start()].strip(" -\t")
            try:
                amount = _parse_amount(first.group(0))
            except ValueError:
                amount = None
        else:
            j = i + 1
            if j < len(lines) and _METADATA_LINE_RE.match(lines[j]):
                j += 1
            if j < len(lines):
                lookahead_money = list(_MONEY_RE.finditer(lines[j]))
                if lookahead_money:
                    try:
                        amount = _parse_amount(lookahead_money[0].group(0))
                    except ValueError:
                        amount = None

        i += 1
        desc = desc.strip(" -\t")
        if amount is None or not desc:
            continue
        rows.append({
            "date": date_obj.strftime("%Y-%m-%d"), "description": desc,
            "amount": amount, "category": "Uncategorized", "source_file": source_name,
        })
    return _fix_sign_convention(rows)


def _parse_pdf_file(path):
    try:
        import pypdf
    except ImportError:
        print(f"  [Statements] Can't parse {os.path.basename(path)} -- the 'pypdf' package "
              f"isn't installed. Run: pip install pypdf")
        return []

    try:
        reader = pypdf.PdfReader(path)
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception as e:
        print(f"  [Statements] Couldn't read PDF {os.path.basename(path)}: {e}")
        return []

    rows = _parse_statement_text(text, os.path.basename(path))
    if not rows:
        print(f"  [Statements] Found no recognizable transaction lines in {os.path.basename(path)}")
    return rows


# OFX/QFX are SGML-flavored: individual fields often aren't closed
# (<TRNAMT>-12.34 with no </TRNAMT>), so ordinary XML parsing doesn't work
# reliably -- pull each <STMTTRN>...</STMTTRN> block out and regex the
# fields we need from inside it instead.
_OFX_TXN_RE = re.compile(r"<STMTTRN>(.*?)</STMTTRN>", re.IGNORECASE | re.DOTALL)
_OFX_FIELD_RE = re.compile(r"<(\w+)>([^<\r\n]*)")


def _ofx_fields(block):
    fields = {}
    for m in _OFX_FIELD_RE.finditer(block):
        key = m.group(1).upper()
        if key not in fields:  # first occurrence wins if a tag repeats
            fields[key] = m.group(2).strip()
    return fields


def _parse_ofx_date(raw):
    m = re.match(r"(\d{4})(\d{2})(\d{2})", (raw or "").strip())
    if not m:
        return None
    try:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _parse_ofx_text(text, source_name):
    rows = []
    for block in _OFX_TXN_RE.findall(text):
        fields = _ofx_fields(block)
        date_obj = _parse_ofx_date(fields.get("DTPOSTED"))
        raw_amount = fields.get("TRNAMT")
        if date_obj is None or not raw_amount:
            continue
        try:
            # OFX convention: negative TRNAMT = money out. Ours is the
            # opposite (positive = spent), same flip CSV/XLSX get below.
            amount = -_parse_amount(raw_amount)
        except ValueError:
            continue
        desc = fields.get("NAME") or fields.get("MEMO") or fields.get("PAYEE") or "Transaction"
        rows.append({
            "date": date_obj.strftime("%Y-%m-%d"),
            "description": desc,
            "amount": amount,
            "category": "Uncategorized",
            "source_file": source_name,
        })
    if not rows:
        print(f"  [Statements] Found no <STMTTRN> transactions in {source_name}")
    return rows


def _parse_ofx_file(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except Exception as e:
        print(f"  [Statements] Couldn't read {os.path.basename(path)}: {e}")
        return []
    return _parse_ofx_text(text, os.path.basename(path))


def _parse_image_file(path):
    """Screenshots of a banking app or a photographed paper statement --
    OCR'd to text (pytesseract/Tesseract), then parsed exactly like a PDF's
    text layer. The image itself is only ever opened for pixel decoding by
    Pillow, never rendered in a viewer or otherwise executed."""
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        print(f"  [Statements] Can't read {os.path.basename(path)} -- OCR needs the "
              f"'pytesseract' package. Run: pip install pytesseract")
        return []

    try:
        with Image.open(path) as img:
            text = pytesseract.image_to_string(img)
    except pytesseract.TesseractNotFoundError:
        print(f"  [Statements] Can't read {os.path.basename(path)} -- pytesseract is installed "
              f"but the 'tesseract' OCR engine isn't. Install it via your OS package manager, "
              f"e.g. 'sudo apt install tesseract-ocr' or 'brew install tesseract'.")
        return []
    except Exception as e:
        print(f"  [Statements] Couldn't OCR {os.path.basename(path)}: {e}")
        return []

    rows = _parse_statement_text(text, os.path.basename(path))
    if not rows:
        print(f"  [Statements] OCR found no recognizable transaction lines in "
              f"{os.path.basename(path)} -- screenshots work best when each line clearly "
              f"shows a date and a dollar amount.")
    return rows


_PARSERS_BY_EXT = {
    ".csv": _parse_csv_file,
    ".txt": _parse_txt_file,
    ".pdf": _parse_pdf_file,
    ".xlsx": _parse_xlsx_file,
    ".xls": _parse_xlsx_file,
    ".ofx": _parse_ofx_file,
    ".qfx": _parse_ofx_file,
    ".jpg": _parse_image_file,
    ".jpeg": _parse_image_file,
    ".png": _parse_image_file,
    ".webp": _parse_image_file,
}

# Zip-bomb / zip-slip guardrails for _extract_zip -- statement archives are
# small (a few small files), so these limits are generous but not unbounded.
_MAX_ZIP_MEMBERS = 200
_MAX_ZIP_UNCOMPRESSED_BYTES = 200 * 1024 * 1024  # 200MB


def _extract_zip(zip_path):
    """Safely expand a zip archive into a sibling '<name>.zip_extracted'
    folder so whatever statements are inside -- however they're named --
    get picked up by the normal per-extension parsers below. Refuses to
    write outside that destination folder (zip-slip) and bails out on
    unreasonably large or numerous members (zip bomb) rather than exhaust
    disk space."""
    dest_dir = zip_path + "_extracted"
    if os.path.isdir(dest_dir):
        return dest_dir  # already expanded by a previous sync

    try:
        with zipfile.ZipFile(zip_path) as zf:
            members = [i for i in zf.infolist() if not i.is_dir()]
            if len(members) > _MAX_ZIP_MEMBERS:
                print(f"  [Statements] Skipping {os.path.basename(zip_path)} -- "
                      f"too many files inside ({len(members)}).")
                return None
            if sum(i.file_size for i in members) > _MAX_ZIP_UNCOMPRESSED_BYTES:
                print(f"  [Statements] Skipping {os.path.basename(zip_path)} -- "
                      f"too large once unzipped.")
                return None

            abs_dest = os.path.abspath(dest_dir)
            os.makedirs(dest_dir, exist_ok=True)
            for info in members:
                target = os.path.abspath(os.path.join(dest_dir, info.filename))
                if target != abs_dest and not target.startswith(abs_dest + os.sep):
                    continue  # zip-slip attempt (e.g. "../../etc/passwd") -- skip it
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as out:
                    out.write(src.read())
    except (zipfile.BadZipFile, OSError) as e:
        print(f"  [Statements] Couldn't open {os.path.basename(zip_path)} as a zip: {e}")
        return None
    return dest_dir


def sync():
    ensure_statements_dir()
    ledger = _load_ledger()
    new_count = 0
    files_seen = 0

    # Expand any zip archives first so their contents (statements named
    # however the bank/user named them) get picked up by the walk below
    # like any other file.
    for root, _dirs, files in os.walk(STATEMENTS_DIR):
        for name in files:
            if os.path.splitext(name)[1].lower() == ".zip":
                _extract_zip(os.path.join(root, name))

    # Walked (not glob("*.csv")/glob("*.pdf")) so any arbitrarily-named
    # attachment is picked up regardless of extension case -- glob patterns
    # are case-sensitive on Linux/Mac, so a bank/phone sending "Statement.PDF"
    # or "eStatement.Pdf" would otherwise be silently skipped. Recursive so
    # files imported into the "latest bank statements" subfolder (via
    # telegram_bridge.import_latest_to), and files extracted from a zip
    # above, get picked up too, not just ones dropped directly in
    # STATEMENTS_DIR.
    for root, _dirs, files in os.walk(STATEMENTS_DIR):
        for name in files:
            parser = _PARSERS_BY_EXT.get(os.path.splitext(name)[1].lower())
            if parser is None:
                continue
            path = os.path.join(root, name)
            files_seen += 1
            for tx in parser(path):
                fp = _fingerprint(tx["date"], tx["description"], tx["amount"])
                if fp not in ledger:
                    ledger[fp] = tx
                    new_count += 1
    _save_ledger(ledger)
    return {"files_seen": files_seen, "new_transactions": new_count, "total_transactions": len(ledger)}


def has_data():
    return len(_load_ledger()) > 0


def get_spending_summary(days=30):
    """Category/merchant breakdown for the last `days`, plus how that
    compares to the prior period of equal length (change_pct) and a
    month-by-month trend across all imported history (monthly_trend) --
    the "richer insight" data the finance dashboard and financial Q&A
    tools are grounded in."""
    ledger = _load_ledger()
    if not ledger:
        return None
    today = datetime.now().date()
    cutoff = today - timedelta(days=days)
    prev_cutoff = cutoff - timedelta(days=days)
    by_category, by_merchant, by_month = {}, {}, {}
    prev_by_category = {}
    total_spent = 0.0
    previous_spent = 0.0

    for tx in ledger.values():
        tx_date = datetime.strptime(tx["date"], "%Y-%m-%d").date()
        amount = tx["amount"]
        if amount <= 0:
            continue  # refunds/deposits, not spending

        # Month-over-month trend spans the whole ledger, independent of
        # the `days` window used for the category/merchant breakdown.
        by_month[tx["date"][:7]] = by_month.get(tx["date"][:7], 0.0) + amount

        if tx_date < cutoff:
            if tx_date >= prev_cutoff:
                previous_spent += amount
                category = tx["category"] or "Uncategorized"
                prev_by_category[category] = prev_by_category.get(category, 0.0) + amount
            continue

        category = tx["category"] or "Uncategorized"
        by_category[category] = by_category.get(category, 0.0) + amount
        by_merchant[tx["description"]] = by_merchant.get(tx["description"], 0.0) + amount
        total_spent += amount

    top_categories = sorted(by_category.items(), key=lambda x: -x[1])[:8]
    top_merchants = sorted(by_merchant.items(), key=lambda x: -x[1])[:8]
    trend = sorted(by_month.items())[-6:]  # last up to 6 calendar months present in the data

    change_pct = None
    if previous_spent > 0:
        change_pct = round((total_spent - previous_spent) / previous_spent * 100, 1)

    return {
        "period_days": days,
        "total_spent": round(total_spent, 2),
        "previous_period_spent": round(previous_spent, 2),
        "change_pct": change_pct,
        "by_category": [{"name": n, "amount": round(a, 2)} for n, a in top_categories],
        "previous_by_category": {n: round(a, 2) for n, a in prev_by_category.items()},
        "top_merchants": [{"name": n, "amount": round(a, 2)} for n, a in top_merchants],
        "monthly_trend": [{"month": m, "amount": round(a, 2)} for m, a in trend],
    }


def get_recurring_charges(min_occurrences=2, top_n=5):
    """Best-effort subscription/recurring-charge detector: groups the full
    synced ledger by (description, amount-to-the-cent) and returns anything
    seen `min_occurrences`+ times, sorted by total impact -- the shape of a
    subscription (same merchant, same amount, repeatedly) rather than a
    one-off purchase. Backs get_financial_insights' "these look recurring"
    tip and build_finance_dashboard's dashboard."""
    ledger = _load_ledger()
    seen = {}
    for tx in ledger.values():
        amount = tx.get("amount", 0)
        if amount <= 0:
            continue
        key = (tx.get("description", "").strip(), round(amount, 2))
        seen[key] = seen.get(key, 0) + 1
    recurring = [
        {"name": desc, "amount": amt, "count": count}
        for (desc, amt), count in seen.items() if count >= min_occurrences
    ]
    recurring.sort(key=lambda r: -(r["amount"] * r["count"]))
    return recurring[:top_n]


# ---- Proactive anomaly detection ------------------------------------------
# Backs jarvis.py's _finance_watcher_thread -- the "notice something wrong
# without being asked" counterpart to get_spending_summary/get_financial_
# insights, which only ever answer when asked. Dedup follows email_watcher.
# py's exact pattern (a JSON set of fingerprints already alerted on) so a
# spike that's still true on the next daily check doesn't re-fire every day.
ANOMALIES_SEEN_PATH = os.path.join(os.path.expanduser("~"), ".jarvis", "finance_anomalies_seen.json")
# Append-only history of every anomaly ever flagged, separate from the seen
# SET above -- ANOMALIES_SEEN_PATH exists purely to suppress re-alerting on
# the same thing, so it can never double as a display log (reading it would
# have no way to show "what was found," only "what's already been silenced").
# Read-only consumers (the phone dashboard's Finance tab) use
# recent_anomalies() against this file instead of ever calling
# check_spending_anomalies() themselves, which would incorrectly consume
# the "new" designation a real watcher check needs to actually alert on.
ANOMALIES_LOG_PATH = os.path.join(os.path.expanduser("~"), ".jarvis", "finance_anomalies_log.jsonl")

_SPIKE_THRESHOLD_PCT = 40.0
_CATEGORY_THRESHOLD_PCT = 50.0
_CATEGORY_MIN_AMOUNT = 30.0  # floor so a $5 category doubling to $10 doesn't count as a "spike"


def _load_anomalies_seen():
    try:
        with open(ANOMALIES_SEEN_PATH, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def _save_anomalies_seen(seen):
    os.makedirs(os.path.dirname(ANOMALIES_SEEN_PATH), exist_ok=True)
    trimmed = list(seen)[-2000:]
    tmp = ANOMALIES_SEEN_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(trimmed, f)
    os.replace(tmp, ANOMALIES_SEEN_PATH)


def _log_anomaly(anomaly):
    os.makedirs(os.path.dirname(ANOMALIES_LOG_PATH), exist_ok=True)
    with open(ANOMALIES_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": datetime.now().timestamp(), **anomaly}) + "\n")


def recent_anomalies(limit: int = 10) -> list:
    """Read-only history of anomalies actually flagged (most recent last),
    for display (the phone dashboard's Finance tab) -- never call
    check_spending_anomalies() itself for this purpose, since that mutates
    ANOMALIES_SEEN_PATH and would silently swallow the real watcher's next
    genuine alert."""
    try:
        with open(ANOMALIES_LOG_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return []
    out = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def check_spending_anomalies() -> list:
    """Compares the current 30-day window against the prior one and against
    known recurring charges, and returns anything crossing a threshold that
    hasn't already been flagged: [{"kind": "spike"|"category"|
    "new_recurring", "detail": "..."}, ...]. Empty list if nothing's
    notable, or if there isn't enough data yet. Each distinct anomaly is
    only ever returned once (see ANOMALIES_SEEN_PATH) -- a spike that's
    still true tomorrow won't re-fire every single day."""
    if not has_data():
        return []
    summary = get_spending_summary(days=30)
    if not summary:
        return []

    seen = _load_anomalies_seen()
    found = []
    month_key = datetime.now().strftime("%Y-%m")

    # 1. Overall spend spike vs. the prior 30-day window.
    change_pct = summary.get("change_pct")
    if change_pct is not None and change_pct >= _SPIKE_THRESHOLD_PCT:
        fp = f"spike|{month_key}"
        if fp not in seen:
            anomaly = {
                "kind": "spike",
                "detail": (f"Total spending is up {change_pct:.0f}% versus last month -- "
                           f"${summary['total_spent']:.0f} vs ${summary['previous_period_spent']:.0f}."),
            }
            found.append(anomaly)
            _log_anomaly(anomaly)
            seen.add(fp)

    # 2. Any single category spiking hard versus its own prior-period share.
    prev_by_category = summary.get("previous_by_category", {})
    for cat in summary.get("by_category", []):
        name, amount = cat["name"], cat["amount"]
        if amount < _CATEGORY_MIN_AMOUNT:
            continue
        prev_amount = prev_by_category.get(name, 0.0)
        cat_change = (
            None if prev_amount <= 0 else round((amount - prev_amount) / prev_amount * 100, 1)
        )
        if cat_change is not None and cat_change >= _CATEGORY_THRESHOLD_PCT:
            fp = f"category|{month_key}|{name}"
            if fp not in seen:
                anomaly = {
                    "kind": "category",
                    "detail": (f"{name} spending is up {cat_change:.0f}% this month -- "
                               f"${amount:.0f} vs ${prev_amount:.0f} last month."),
                }
                found.append(anomaly)
                _log_anomaly(anomaly)
                seen.add(fp)

    # 3. A recurring charge that's new since the last check (permanent
    # fingerprint, not month-scoped -- once flagged, never again unless the
    # amount itself changes).
    for r in get_recurring_charges():
        fp = f"new_recurring|{r['name']}|{r['amount']:.2f}"
        if fp not in seen:
            anomaly = {
                "kind": "new_recurring",
                "detail": f"Looks like a new recurring charge: {r['name']} at ${r['amount']:.2f}, seen {r['count']} times.",
            }
            found.append(anomaly)
            _log_anomaly(anomaly)
            seen.add(fp)

    if found:
        _save_anomalies_seen(seen)
    return found
