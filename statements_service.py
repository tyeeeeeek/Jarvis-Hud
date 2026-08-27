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


def _parse_pdf_line(line, default_year):
    line = line.strip()
    date_match = _PDF_LINE_DATE_RE.match(line)
    if not date_match:
        return None
    date_obj = _parse_date(date_match.group(1), default_year)
    if date_obj is None:
        return None

    rest = line[date_match.end():].strip()
    money_matches = list(_MONEY_RE.finditer(rest))
    if not money_matches:
        return None

    # First dollar figure after the description is the transaction amount;
    # a second one (common on statements with a running balance column) is
    # ignored rather than mistaken for the amount.
    first = money_matches[0]
    desc = rest[:first.start()].strip(" -\t")
    if not desc:
        return None
    try:
        amount = _parse_amount(first.group(0))
    except ValueError:
        return None

    return {"date": date_obj.strftime("%Y-%m-%d"), "description": desc, "amount": amount, "category": "Uncategorized"}


def _parse_statement_text(text, source_name):
    """Line-by-line "date + $ amount" recovery shared by PDF text layers,
    plain-text (.txt) statements, and OCR'd image text -- all three end up
    as loose lines of text with no header row to key off of."""
    rows = []
    default_year = _guess_statement_year(text)
    for line in text.splitlines():
        tx = _parse_pdf_line(line, default_year)
        if tx:
            tx["source_file"] = source_name
            rows.append(tx)
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
        "top_merchants": [{"name": n, "amount": round(a, 2)} for n, a in top_merchants],
        "monthly_trend": [{"month": m, "amount": round(a, 2)} for m, a in trend],
    }
