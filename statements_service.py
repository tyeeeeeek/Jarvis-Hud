# ================================================================
#   Local bank statement import — no API, no account linking.
#
#   Drop CSV exports from your bank/card's own website into
#   ~/JarvisStatements (or send them to the Telegram bot and say
#   "sync statements" -- see telegram_bridge.import_latest_to, which
#   lands them in the "latest bank statements" subfolder below) and
#   say "Jarvis sync my bank data". Everything is parsed and stored
#   locally (statements_ledger.json); nothing here ever leaves this
#   machine.
# ================================================================
import os
import csv
import json
import glob
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


def ensure_statements_dir():
    os.makedirs(STATEMENTS_DIR, exist_ok=True)
    os.makedirs(LATEST_STATEMENTS_DIR, exist_ok=True)
    readme = os.path.join(STATEMENTS_DIR, "README.txt")
    if not os.path.exists(readme):
        with open(readme, "w") as f:
            f.write(
                "Drop your bank/card CSV exports here (SoFi, Capital One, Discover, etc.), "
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
    return float(val)


def _parse_date(raw):
    raw = raw.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
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


def sync():
    ensure_statements_dir()
    ledger = _load_ledger()
    new_count = 0
    files_seen = 0
    # Recursive so files imported into the "latest bank statements"
    # subfolder (via telegram_bridge.import_latest_to) get picked up too,
    # not just ones dropped directly in STATEMENTS_DIR.
    for path in glob.glob(os.path.join(STATEMENTS_DIR, "**", "*.csv"), recursive=True):
        files_seen += 1
        for tx in _parse_csv_file(path):
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
