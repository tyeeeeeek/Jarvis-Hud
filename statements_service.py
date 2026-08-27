# ================================================================
#   Local bank statement import — no API, no account linking.
#
#   Drop CSV or PDF exports from your bank/card's own website into
#   ~/JarvisStatements (or send them to the Telegram bot and say
#   "sync statements" -- see telegram_bridge.import_latest_to, which
#   lands them in the "latest bank statements" subfolder below) and
#   say "Jarvis sync my bank data". Everything is parsed and stored
#   locally (statements_ledger.json); nothing here ever leaves this
#   machine. PDF statements are only ever read for their text layer
#   (pypdf.extract_text) -- never rendered, opened in a viewer, or
#   allowed to execute embedded scripts.
# ================================================================
import os
import re
import csv
import json
import hashlib
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
                "Drop your bank/card CSV or PDF exports here (SoFi, Capital One, Discover, etc.), "
                "then say 'Jarvis sync my bank data'.\n\n"
                "Files sent as attachments to the Telegram bot land in the "
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


def _parse_csv_file(path):
    rows = []
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            return rows
        date_key = _find_key(reader.fieldnames, _DATE_KEYS)
        desc_key = _find_key(reader.fieldnames, _DESC_KEYS)
        amount_key = _find_key(reader.fieldnames, _AMOUNT_KEYS)
        debit_key = _find_key(reader.fieldnames, _DEBIT_KEYS)
        credit_key = _find_key(reader.fieldnames, _CREDIT_KEYS)
        category_key = _find_key(reader.fieldnames, _CATEGORY_KEYS)
        if not date_key or not desc_key:
            print(f"  [Statements] Couldn't find date/description columns in {os.path.basename(path)}")
            return rows

        for row in reader:
            try:
                date_obj = _parse_date(row[date_key])
                if date_obj is None:
                    continue
                desc = row[desc_key].strip()

                amount = None
                if amount_key and row.get(amount_key, "").strip():
                    amount = _parse_amount(row[amount_key])
                elif debit_key or credit_key:
                    debit = row.get(debit_key, "").strip() if debit_key else ""
                    credit = row.get(credit_key, "").strip() if credit_key else ""
                    if debit:
                        amount = abs(_parse_amount(debit))
                    elif credit:
                        amount = -abs(_parse_amount(credit))
                    else:
                        continue
                else:
                    continue

                category = row[category_key].strip() if category_key and row.get(category_key) else "Uncategorized"
                rows.append({
                    "date": date_obj.strftime("%Y-%m-%d"),
                    "description": desc,
                    "amount": amount,
                    "category": category,
                    "source_file": os.path.basename(path),
                })
            except Exception:
                continue

    # Some banks export debits as negative numbers in a single Amount
    # column (opposite of the "charge = positive" convention others use).
    # If most non-zero rows in this file are negative, it's that style --
    # flip so positive always means "money spent" across our ledger.
    neg = sum(1 for r in rows if r["amount"] < 0)
    pos = sum(1 for r in rows if r["amount"] > 0)
    if neg > pos * 2:
        for r in rows:
            r["amount"] = -r["amount"]

    return rows


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


def _parse_pdf_file(path):
    rows = []
    try:
        import pypdf
    except ImportError:
        print(f"  [Statements] Can't parse {os.path.basename(path)} -- the 'pypdf' package "
              f"isn't installed. Run: pip install pypdf")
        return rows

    try:
        reader = pypdf.PdfReader(path)
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception as e:
        print(f"  [Statements] Couldn't read PDF {os.path.basename(path)}: {e}")
        return rows

    default_year = _guess_statement_year(text)
    for line in text.splitlines():
        tx = _parse_pdf_line(line, default_year)
        if tx:
            tx["source_file"] = os.path.basename(path)
            rows.append(tx)

    if not rows:
        print(f"  [Statements] Found no recognizable transaction lines in {os.path.basename(path)}")

    # Same debit/credit-sign convention fixup as CSV imports.
    neg = sum(1 for r in rows if r["amount"] < 0)
    pos = sum(1 for r in rows if r["amount"] > 0)
    if neg > pos * 2:
        for r in rows:
            r["amount"] = -r["amount"]

    return rows


_PARSERS_BY_EXT = {".csv": _parse_csv_file, ".pdf": _parse_pdf_file}


def sync():
    ensure_statements_dir()
    ledger = _load_ledger()
    new_count = 0
    files_seen = 0
    # Walked (not glob("*.csv")/glob("*.pdf")) so any arbitrarily-named
    # attachment is picked up regardless of extension case -- glob patterns
    # are case-sensitive on Linux/Mac, so a bank/phone sending "Statement.PDF"
    # or "eStatement.Pdf" would otherwise be silently skipped. Recursive so
    # files imported into the "latest bank statements" subfolder (via
    # telegram_bridge.import_latest_to) get picked up too, not just ones
    # dropped directly in STATEMENTS_DIR.
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
