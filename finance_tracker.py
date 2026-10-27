"""
Jarvis — Finance Tracker
=========================
Reads St. George Bank CSV exports and produces a finance summary
for the morning brief.

HOW IT WORKS:
  1. You export CSVs from St. George Internet Banking (takes 30 seconds)
  2. Drop them in the jarvis/finance/ folder:
       finance/everyday.csv   — your spending account
       finance/savings1.csv   — main savings (US exchange Jan 2027 goal)
       finance/investing.csv  — reselling working capital (separate P&L)
       finance/revolut.csv    — Revolut reselling account (preferred over investing.csv)
  3. Morning brief calls get_finance_summary() and injects the result

CSV FORMAT (St. George):
  Date,Description,Debit,Credit,Balance
  13/05/2026,Visa Purchase ...,48.50,,657.47,

  Everyday's goal balance prefers an available figure when the export has
  one. Column names: Available, Available Balance, Avail, Avail Balance,
  Avail Bal. An account line before the header works too
  (Available Balance,5800.00). Pending rows (Status Pending, or a
  description starting with PENDING) are left out of spending. If there is
  no available figure, pending debits that have not posted are subtracted
  from the ledger balance. Savings and investing keep the ledger balance.

CATEGORIES:
  Food & dining  — supermarkets, restaurants, cafes, delivery
  Entertainment  — streaming, tickets, games, bars, nightlife
  Shopping       — retail, clothing, online shopping, Amazon
  Transport      — Uber, fuel, parking, public transport
  Subscriptions  — recurring charges (Netflix, Spotify, etc.)
  Other          — anything uncategorised

UPDATE WEEKLY:
  Export last 30 days from St. George → save to finance/ folder → done.
"""

import csv
import datetime
import json
import os
import re
from pathlib import Path

import pytz
import config

try:
    from live_spend import entries_between
    LIVE_SPEND_AVAILABLE = True
except Exception:
    LIVE_SPEND_AVAILABLE = False

# ── Paths ─────────────────────────────────────────────────────────────────────

SCRIPT_DIR   = Path(__file__).parent
FINANCE_DIR  = SCRIPT_DIR / "finance"
EVERYDAY_CSV  = FINANCE_DIR / "everyday.csv"
SAVINGS1_CSV  = FINANCE_DIR / "savings1.csv"
INVESTING_CSV = FINANCE_DIR / "investing.csv"
REVOLUT_CSV   = FINANCE_DIR / "revolut.csv"

# Default owner name for Revolut transfers. Live checks use get_own_account_rules()
# so term_context.json "own_accounts" can override this without a code change.
REVOLUT_OWN_NAMES = ("manav jain",)

FINANCE_DIR.mkdir(exist_ok=True)

TIMEZONE = pytz.timezone(config.TIMEZONE)

# ── Savings goal ──────────────────────────────────────────────────────────────
# Goals themselves live in term_context.json (single source of truth, editable
# via proposals) instead of being hardcoded here.
try:
    from term_context import (
        get_finance_goals,
        get_income_payers,
        get_own_account_rules,
        get_refund_window_days,
    )
except Exception:
    def get_finance_goals():
        return {
            "savings_goal": 35000.00, "savings_deadline": "2027-01-01",
            "monthly_income": 2800.00, "monthly_budget": 300.00, "weekly_budget": 75.00,
        }

    def get_own_account_rules():
        """Fallback when term_context cannot be imported. Mirrors its defaults."""
        return {
            "owner_names": ["manav jain"],
            "withdrawal_markers": [
                "osko withdrawal", "internet withdrawal", "sct withdrawal",
            ],
            "card_patterns": ["revolut**5228", "revolut"],
            "card_rails": ["visa purchase", "eftpos", "top-up", "topup"],
        }

    def get_refund_window_days():
        """Fallback when term_context cannot be imported."""
        return 14

    def get_income_payers():
        """Fallback when term_context cannot be imported: no transfer is income."""
        return []


# ── Category rules ────────────────────────────────────────────────────────────
# Keywords pick the category. They do not decide whether a line is
# spending. The rail does: see _classified.

CATEGORY_RULES = [
    ("Food & dining", [
        "woolworths", "coles", "aldi", "iga", "harris farm", "costco",
        "mcdonald", "kfc", "subway", "domino", "pizza", "hungry jack",
        "uber eat", "doordash", "menulog", "deliveroo",
        "restaurant", "cafe", "coffee", "bakery", "sushi", "noodle",
        "food", "dining", "eat", "grill", "burger", "thai", "indian",
        "chinese", "italian", "kebab", "chatime", "gong cha", "boost",
    ]),
    ("Entertainment", [
        "cinema", "event cinema", "hoyts", "village cinema",
        "ticketek", "ticketmaster", "tickets", "ticket",
        "spotify", "netflix", "disney", "stan ", "binge", "paramount",
        "playstation", "xbox", "steam", "nintendo", "epic games",
        "apple.com/bill", "itunes", "google play",
        "bar ", "pub ", "club ", "nightclub", "drinks",
        "bowling", "escape room", "laser tag", "entertainment",
        "ko ticket", "knockout", "ko ", "exodus", "concert", "festival", "show ", "rave",
        "moshtix", "humanitix", "oztix",
    ]),
    ("Shopping", [
        "amazon", "ebay", "kmart", "target", "big w", "myer", "david jones",
        "apple.com/bill", "apple.com",
        "uniqlo", "h&m", "zara", "cotton on", "glue store",
        "jb hi-fi", "officeworks", "apple store", "harvey norman",
        "chemist warehouse", "priceline", "pharmacy",
        "rebel sport", "sports", "asos", "the iconic",
        "shopping", "retail", "store", "shop",
    ]),
    ("Sport & leisure", [
        "golf", "muirfield", "tennis", "swimming", "aquatic",
        "sport", "leisure", "recreation", "fitness", "yoga", "pilates",
        "surfing", "climbing", "crossfit",
    ]),
    ("Education", [
        "unsw", "university", "tafe", "tuition", "textbook", "course fee",
        "student fee", "enrolment",
    ]),
    ("Transport", [
        "uber ", "ola ", "didi ", "taxi",
        "opal", "transport nsw", "train", "bus ",
        "fuel", "petrol", "shell", "bp ", "caltex", "ampol",
        "parking", "wilson parking", "care park",
        "service nsw", "car wash",
    ]),
    ("Subscriptions", [
        "netflix", "spotify", "disney+", "stan ", "binge", "prime video",
        "icloud", "google one", "microsoft", "adobe", "canva",
        "anthropic", "openai", "chatgpt",
        "gym", "anytime fitness", "fitness first", "goodlife",
        "subscription", "monthly", "annual",
    ]),
]


# Not a spending category. Own-account moves return this from categorise().
INTERNAL_CATEGORY = "Internal transfer"


def _keyword_matches(description_lower, keyword):
    """True when keyword occurs and is not stuck to a preceding letter.

    Short tokens such as 'ko ' sit inside 'osko '. A plain substring check
    was labelling every Osko withdrawal as Entertainment.
    """
    start = 0
    while True:
        idx = description_lower.find(keyword, start)
        if idx < 0:
            return False
        if idx == 0 or not description_lower[idx - 1].isalpha():
            return True
        start = idx + 1


def _is_own_card_topup(desc, rules):
    """True for a card top-up of an account the owner already holds.

    A masked descriptor (Revolut**5228) matches on its own. A bare merchant
    name such as 'revolut' only matches on a card rail, so an Osko reference
    that merely mentions Revolut is still spending.
    """
    patterns = [pat for pat in rules.get("card_patterns") or [] if pat and pat in desc]
    if not patterns:
        return False
    if any("**" in pat for pat in patterns):
        return True
    rails = rules.get("card_rails") or []
    return any(rail in desc for rail in rails)


def _normalise_desc(description):
    """Lowercase, and collapse the padded whitespace St. George puts in descriptions."""
    return re.sub(r"\s+", " ", (description or "").lower()).strip()


def _is_internal_spend(description):
    """True for a move between the owner's own accounts, which is not spending.

    Internet withdrawals and deposits, transfers to or from savings, Osko/Sct
    to or from his own name, and Revolut card top-ups. The bare word
    'transfer' is not enough, and a bare 'revolut' on an Osko is not self.
    """
    desc = _normalise_desc(description)
    if "internet withdrawal" in desc or "internet deposit" in desc:
        return True
    if "from 0000" in desc or "to 0000" in desc:
        return True
    if re.search(r"\btransfer (to|from) savings\b", desc):
        return True
    if "sent from revolut" in desc:
        return True
    rules = get_own_account_rules()
    names = [str(name).strip().lower() for name in (rules.get("owner_names") or []) if str(name).strip()]
    markers = rules.get("withdrawal_markers") or []
    named = any(name in desc for name in names)
    if named and any(marker in desc for marker in markers):
        return True
    if named and "deposit" in desc:
        return True
    return _is_own_card_topup(desc, rules)


def categorise(description):
    """Return a category string for a transaction description.

    Own-account transfers return 'Internal transfer' before any keyword rule,
    so a self-Osko that mentions Kmart or sits inside the 'osko ' token never
    lands in a spending category.
    """
    if _is_internal_spend(description):
        return INTERNAL_CATEGORY
    desc_lower = _normalise_desc(description)
    for category, keywords in CATEGORY_RULES:
        for kw in keywords:
            if _keyword_matches(desc_lower, kw):
                return category
    return "Other"


# ── CSV parsing ───────────────────────────────────────────────────────────────
# St George internet banking's usual download is
# Date,Description,Debit,Credit,Balance. Nothing in the repo shows an
# available-balance column, so the names below are the ones the parser
# accepts. Absent means the ledger balance, which is the old behaviour.

_HEADER_ALIASES = {
    "transaction date": "date",
    "processed date": "date",
    "value date": "date",
    "narrative": "description",
    "transaction description": "description",
    "details": "description",
    "debit amount": "debit",
    "credit amount": "credit",
    "current balance": "balance",
    "ledger balance": "balance",
    "ledger": "balance",
    "available balance": "available",
    "available funds": "available",
    "avail": "available",
    "avail balance": "available",
    "avail bal": "available",
    "available bal": "available",
    "transaction status": "status",
    "txn status": "status",
}
_PENDING_STATUSES = {
    "pending", "held", "authorised", "authorized", "pre-auth", "preauth",
    "pending transaction",
}


def _canonical_header(name):
    """Lowercase header or label, with known St George aliases collapsed."""
    key = re.sub(r"\s+", " ", (name or "").strip().lower()).rstrip(":")
    return _HEADER_ALIASES.get(key, key)


def _parse_money(raw):
    """Dollars from a bank cell, or None when the cell is blank.

    Strips a trailing comma, dollar signs, and thousands separators.
    Parentheses mark a negative amount.
    """
    text = (raw or "").strip().rstrip(",")
    if not text:
        return None
    text = text.replace("$", "").replace(",", "").replace(" ", "")
    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1]
    if text[:1] in "+":
        text = text[1:]
    try:
        value = float(text)
    except ValueError:
        return None
    if negative:
        value = -value
    return value


def _stated_available(cells):
    """Dollar amount when the cells are an account-level available balance."""
    cells = [str(cell).strip() for cell in cells if str(cell or "").strip()]
    if len(cells) == 1 and ":" in cells[0]:
        label, _, value = cells[0].partition(":")
        cells = [label.strip(), value.strip()]
    if len(cells) < 2:
        return None
    if _canonical_header(cells[0]) != "available":
        return None
    return _parse_money(cells[1])


def _row_is_pending(data, description):
    """True for a transaction the export still marks as pending."""
    status = _normalise_desc(data.get("status") or "")
    if status in _PENDING_STATUSES:
        return True
    flag = _normalise_desc(data.get("pending") or "")
    if flag in {"y", "yes", "true", "1", "pending"}:
        return True
    return _normalise_desc(description).startswith("pending")


def _interpret_stgeorge_row(data, seq):
    """One transaction dict, ('stated', amount), or None when the row is blank."""
    date_str = (data.get("date") or "").strip()
    description = re.sub(r"\s+", " ", (data.get("description") or "").strip())
    if not date_str:
        if _canonical_header(description) == "available":
            for key in ("available", "balance", "credit", "debit"):
                amount = _parse_money(data.get(key) or "")
                if amount is not None:
                    return ("stated", amount)
        return None
    try:
        date = datetime.datetime.strptime(date_str, "%d/%m/%Y").date()
    except ValueError:
        stated = _stated_available([
            date_str, data.get("description") or data.get("balance") or "",
        ])
        if stated is None:
            return None
        return ("stated", stated)

    debit = _parse_money(data.get("debit") or "")
    credit = _parse_money(data.get("credit") or "")
    balance = _parse_money(data.get("balance") or "")
    available = _parse_money(data.get("available") or "")
    return {
        "date": date,
        "description": description,
        "debit": 0.0 if debit is None else debit,
        "credit": 0.0 if credit is None else credit,
        "balance": balance,
        "available": available,
        "pending": _row_is_pending(data, description),
        "category": categorise(description),
        "_seq": seq,
    }


def read_stgeorge_export(filepath):
    """
    Read a St. George CSV into transactions and an optional available balance.

    Returns {"transactions", "stated_available"}. transactions are newest
    first, same as parse_stgeorge_csv. stated_available is the account-level
    figure from a line before the header (or a non-transaction row such as
    "Available Balance"), or None when the export does not state one.
    A missing file is an empty export. The classic five-column file leaves
    stated_available None and each row's available None.
    """
    if filepath is None or not Path(filepath).exists():
        return {"transactions": [], "stated_available": None}

    stated_available = None
    header = None
    header_index = None
    with open(filepath, newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.reader(handle))

    for index, row in enumerate(rows):
        canonical = [_canonical_header(cell) for cell in row]
        if "date" in canonical and "description" in canonical:
            header = canonical
            header_index = index
            break
        stated = _stated_available(row)
        if stated is not None:
            stated_available = stated
    if header is None:
        return {"transactions": [], "stated_available": stated_available}

    transactions = []
    for seq, row in enumerate(rows[header_index + 1:]):
        if not any(str(cell).strip() for cell in row):
            continue
        padded = list(row) + [""] * max(0, len(header) - len(row))
        data = {}
        for key, value in zip(header, padded):
            if not key:
                continue
            data[key] = value.strip() if isinstance(value, str) else value
        try:
            parsed = _interpret_stgeorge_row(data, seq)
        except (ValueError, KeyError):
            continue
        if parsed is None:
            continue
        if isinstance(parsed, tuple) and parsed[0] == "stated":
            stated_available = parsed[1]
            continue
        transactions.append(parsed)

    transactions.sort(key=lambda txn: (txn["date"], -txn["_seq"]), reverse=True)
    for txn in transactions:
        del txn["_seq"]
    return {"transactions": transactions, "stated_available": stated_available}


def parse_stgeorge_csv(filepath):
    """
    Parses a St. George CSV export.
    Returns a list of transaction dicts, newest first:
      {date, description, debit, credit, balance, available, pending, category}

    available is the row's available-balance cell, or None when that column
    is absent. balance is None when the Balance cell is blank. pending is
    True when the row has not posted. Callers that only need the ledger
    balance can keep reading balance on posted rows.
    """
    return read_stgeorge_export(filepath)["transactions"]


def parse_revolut_csv(filepath):
    """
    Parses a Revolut statement CSV export (AUD only, COMPLETED rows only).
    Returns transactions newest-first as {date, description, debit, credit,
    balance, category, internal, refund}. internal=True for top-ups and transfers
    to/from your own accounts (money moving in/out, not purchases or sales);
    refund=True for card refunds (reduce deployed, not sale proceeds).
    Ordered by completed time (file order breaks ties) so [0] holds the
    latest balance.
    """
    transactions = []
    if not filepath.exists():
        return transactions

    owner_names = get_own_account_rules().get("owner_names") or REVOLUT_OWN_NAMES
    with open(filepath, newline="", encoding="utf-8-sig") as f:
        for idx, row in enumerate(csv.DictReader(f)):
            try:
                if (row.get("State") or "").strip().upper() != "COMPLETED":
                    continue
                if (row.get("Currency") or "AUD").strip().upper() != "AUD":
                    continue
                completed = datetime.datetime.strptime(
                    row["Completed Date"].strip(), "%Y-%m-%d %H:%M:%S")
                amount = float(row["Amount"])
                fee    = float(row.get("Fee") or 0)
                balance_str = (row.get("Balance") or "").strip()
                balance = float(balance_str) if balance_str else None

                description = re.sub(r"\s+", " ", (row.get("Description") or "").strip())
                kind = (row.get("Type") or "").strip().lower()
                internal = kind == "topup" or (
                    kind == "transfer"
                    and any(n in description.lower() for n in owner_names)
                )

                transactions.append({
                    "date":        completed.date(),
                    "description": description,
                    "debit":       round(max(-amount, 0) + fee, 2),
                    "credit":      round(max(amount, 0), 2),
                    "balance":     balance,
                    "category":    "Reselling",
                    "internal":    internal,
                    "refund":      kind == "card refund",
                    "rail":        kind,
                    "_key":        (completed, idx),
                })
            except (ValueError, KeyError):
                continue

    transactions.sort(key=lambda t: t["_key"], reverse=True)
    for t in transactions:
        del t["_key"]
    return transactions


def _posted_ledger(transactions):
    """Newest posted ledger balance, skipping pending rows and blank cells."""
    for txn in transactions:
        if txn.get("pending"):
            continue
        if txn.get("balance") is not None:
            return txn["balance"]
    return None


def get_latest_balance(filepath):
    """Returns the most recent posted ledger balance from a CSV file.

    Pending rows are skipped so a hold dated today cannot replace the
    posted balance. A blank Balance cell is skipped too. Savings and
    investing use this. The everyday goal figure is
    resolve_everyday_balances, which can be lower.
    """
    return _posted_ledger(parse_stgeorge_csv(filepath))


def _cents(amount):
    """Integer cents, so a pending hold and its posted twin compare equal."""
    return int(round(float(amount) * 100))


# ── Weekly spending rule ──────────────────────────────────────────────────────
# Card, BPAY and direct debit lines count, in their category. Osko, Sct,
# PayID, NPP and other transfers never count as spending, and only count
# as income when spending.income_payers in term_context.json names the
# payer (wages paid by Osko). The rest are listed, marked not counted. Spends logged with the Back Tap
# shortcut (live_spend.py) count as logged. A card refund reduces an
# earlier card purchase at the same merchant inside the refund window.

TRANSFER_RAIL = "transfer"
_SPEND_RAILS = ("card", "bpay", "direct_debit", "other")
_REFUND_HINTS = ("refund", "reversal", "chargeback", "visa credit", "eftpos credit")
_MERCHANT_NOISE = {
    "visa", "eftpos", "purchase", "debit", "credit", "refund", "reversal",
    "chargeback", "pty", "ltd", "the", "and", "pending", "online",
    "sydney", "melbourne", "australia", "nsw", "london",
    "brisbane", "perth", "adelaide", "hobart", "canberra",
}


def rail_of(description):
    """Coarse rail for a St. George line.

    self, card, card_credit, bpay, direct_debit, direct_credit, transfer,
    or other. Any Osko, Sct, PayID, NPP or 'transfer' line is a transfer.
    """
    desc = _normalise_desc(description)
    if _is_internal_spend(description):
        return "self"
    if "visa" in desc or "eftpos" in desc:
        if any(hint in desc for hint in _REFUND_HINTS):
            return "card_credit"
        return "card"
    if "bpay" in desc:
        return "bpay"
    if "direct debit" in desc:
        return "direct_debit"
    if "direct credit" in desc or re.search(r"\b(payroll|salary|wages)\b", desc):
        return "direct_credit"
    if re.search(r"\b(osko|sct|payid|npp|transfer)\b", desc):
        return TRANSFER_RAIL
    return "other"


def _event_dt(txn):
    """When the line happened, using a clock glued to the description date.

    Rows with no clock sit at 12:00, so a 09:11 refund falls before a 10:30
    repurchase on the same morning.
    """
    desc = _normalise_desc(txn.get("description") or "")
    match = re.search(r"\d{2}[a-z]{3}(\d{2}):(\d{2})", desc)
    if not match:
        match = re.search(r"\b(\d{2}):(\d{2})\b", desc)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return datetime.datetime.combine(txn["date"], datetime.time(hour, minute))
    return datetime.datetime.combine(txn["date"], datetime.time(12, 0))


def _merchant_tokens(description):
    """Merchant words on a card line: 4+ letters, no rail, date or place words."""
    desc = _normalise_desc(description)
    desc = re.sub(r"\d{2}[a-z]{3}(?:\d{2}:\d{2})?", " ", desc)
    desc = re.sub(r"[^a-z]+", " ", desc)
    return {token for token in desc.split() if len(token) >= 4 and token not in _MERCHANT_NOISE}


def _net_card_refunds(spends, refunds, window_days):
    """Reduce earlier card purchases at the same merchant by each card refund.

    Amounts do not have to match. The latest earlier purchase is reduced
    first. A refund never pushes a purchase below zero, and what it does
    not use is dropped: it is not income.
    """
    for refund in sorted(refunds, key=_event_dt):
        tokens = _merchant_tokens(refund["description"])
        left = refund["amount"]
        when = _event_dt(refund)
        candidates = [
            spend for spend in spends
            if spend["remaining"] > 0
            and tokens & _merchant_tokens(spend["description"])
            and _event_dt(spend) <= when
            and (when.date() - spend["date"]).days <= window_days
        ]
        candidates.sort(key=_event_dt, reverse=True)
        for spend in candidates:
            if left <= 0:
                break
            take = min(left, spend["remaining"])
            spend["remaining"] = round(spend["remaining"] - take, 2)
            left = round(left - take, 2)


def _logged_spends(start, end):
    """Back Tap entries dated inside the window, as spending lines."""
    if not LIVE_SPEND_AVAILABLE:
        return []
    try:
        entries = entries_between(start, end)
    except Exception as exc:
        print(f"⚠️  logged spends unavailable: {exc}")
        return []
    return [
        {
            "date": entry["date"],
            "description": entry.get("note") or "Logged spend",
            "debit": round(float(entry["amount"]), 2),
            "credit": 0.0,
            "category": entry["category"],
            "logged": True,
        }
        for entry in entries
    ]


def _classified(transactions, start, end):
    """Spending, income, and not-counted transfers for an inclusive window.

    Own-account moves and pending rows are dropped. Card, BPAY, direct
    debit and other debits are spending, net of card refunds. Transfers
    go on their own list, except a transfer in from a configured income
    payer, which is income. Direct credits and other non-transfer credits
    are income. Logged spends are added to spending as they are.
    """
    window_days = get_refund_window_days()
    payers = get_income_payers()
    spends, refunds, income, transfers = [], [], [], []
    for txn in transactions:
        if txn.get("pending"):
            continue
        day = txn.get("date")
        if day is None or day < start or day > end:
            continue
        debit = float(txn.get("debit") or 0)
        credit = float(txn.get("credit") or 0)
        if debit <= 0 and credit <= 0:
            continue
        description = txn.get("description") or ""
        rail = rail_of(description)
        if rail == "self":
            continue
        direction = "out" if debit > 0 else "in"
        amount = round(debit if direction == "out" else credit, 2)
        row = {"date": day, "description": description, "amount": amount}
        payer = next((p for p in payers if p in _normalise_desc(description)), None)
        if rail == TRANSFER_RAIL and direction == "in" and payer:
            income.append({**row, "source": payer})
        elif rail == TRANSFER_RAIL:
            transfers.append({**row, "date": day.isoformat(), "direction": direction})
        elif direction == "out" and rail in _SPEND_RAILS:
            category = categorise(description)
            spends.append({**row, "remaining": amount, "category": category})
        elif rail == "card_credit" or rail == "card":
            refunds.append(row)
        else:
            income.append({**row, "source": "Direct credit" if rail == "direct_credit" else "Other"})

    _net_card_refunds(spends, refunds, window_days)
    spending = [
        {
            "date": spend["date"],
            "description": spend["description"],
            "debit": spend["remaining"],
            "credit": 0.0,
            "category": spend["category"],
        }
        for spend in spends
        if spend["remaining"] > 0
    ]
    spending.extend(_logged_spends(start, end))
    transfers.sort(key=lambda item: (item["date"], -item["amount"]))
    return {"spending": spending, "income": income, "transfers": transfers}


def _load_sheet_items():
    """Reselling sheet rows, or None when the sheet cannot be read."""
    try:
        from reselling_tracker import load_items
        return load_items()
    except Exception as exc:
        print(f"⚠️  resale commission: sheet unavailable: {exc}")
        return None


def _sheet_commission(start, end):
    """Commission income for the window, from the reselling sheet only.

    Rows with a sale date inside the window count once. Bank transfers are
    never the commission. total is None when the sheet cannot be read.
    """
    from reselling_tracker import commissions_for_window

    items = _load_sheet_items()
    if items is None:
        return {"total": None, "rows": []}
    rows = commissions_for_window(items, start, end)
    return {
        "total": round(sum(row["amount"] for row in rows), 2),
        "rows": [
            {
                "name": row["name"],
                "category": row["category"],
                "amount": row["amount"],
                "sold": row["sold"],
                "due": row["due"],
                "party": row["party"],
            }
            for row in rows
        ],
    }


def _transfer_summary(transfers):
    """Transfers in the window. Listed for reference; never in any total."""
    return {
        "counted": False,
        "count": len(transfers),
        "total_in": round(sum(item["amount"] for item in transfers if item["direction"] == "in"), 2),
        "total_out": round(sum(item["amount"] for item in transfers if item["direction"] == "out"), 2),
        "items": transfers,
    }


def _logged_summary(spending):
    """How much of the spending came from Back Tap logged entries."""
    logged = [txn for txn in spending if txn.get("logged")]
    return {"count": len(logged), "total": round(sum(txn["debit"] for txn in logged), 2)}


# ── Spending analysis ─────────────────────────────────────────────────────────


def analyse_spending(transactions, days=7, extra_inflows=None):
    """
    Analyses spending from the everyday account over the last N days.

    Uses the same rule as summarise_spending: card, BPAY and direct debit
    lines net of card refunds, plus logged spends. Transfers are reported
    beside the total, not in it. extra_inflows is ignored: another account
    cannot cancel this one.
    """
    del extra_inflows
    today = datetime.datetime.now(TIMEZONE).date()
    cutoff = today - datetime.timedelta(days=days)
    classified = _classified(transactions, cutoff, today)
    recent = classified["spending"]

    category_totals = {}
    for txn in recent:
        cat = txn["category"]
        category_totals[cat] = category_totals.get(cat, 0) + txn["debit"]

    big_transactions = [txn for txn in recent if txn["debit"] >= 80]
    total_spend = sum(txn["debit"] for txn in recent)

    return {
        "category_totals": category_totals,
        "big_transactions": big_transactions,
        "total_spend": total_spend,
        "transaction_count": len(recent),
        "transactions": recent,
        "days": days,
        "logged": _logged_summary(recent),
        "transfers": _transfer_summary(classified["transfers"]),
        "commission": _sheet_commission(cutoff, today),
    }


# ── Savings progress ──────────────────────────────────────────────────────────
# Dollars of the Revolut balance held for someone else. Unset, blank, or not
# a number counts as 0 (the whole Revolut balance is his). Documented in
# .env.example and docs/API.md.
REVOLUT_FRIEND_SHARE_ENV = "JARVIS_REVOLUT_FRIEND_SHARE"


def _money(value):
    """Round a dollar amount to cents."""
    return round(float(value), 2)


def revolut_friend_share():
    """
    Dollars of the Revolut balance held for someone else.

    Reads JARVIS_REVOLUT_FRIEND_SHARE. config loads .env at startup, so a
    change applies on the next process start. Missing, blank, or non-numeric
    values are 0. Negative values are 0.
    """
    raw = str(os.environ.get(REVOLUT_FRIEND_SHARE_ENV, "")).strip()
    raw = raw.replace(",", "").replace("$", "")
    if not raw:
        return 0.0
    try:
        value = float(raw)
    except ValueError:
        return 0.0
    if value < 0:
        return 0.0
    return _money(value)


_PENDING_NOISE = {
    "pending", "visa", "eftpos", "purchase", "osko", "withdrawal", "deposit",
    "debit", "credit", "card", "payment", "transaction",
}


def _pending_tokens(description):
    """Merchant tokens left after the pending marker and the bank rail."""
    desc = _normalise_desc(description)
    desc = re.sub(r"^pending\b", " ", desc)
    desc = re.sub(r"[^a-z0-9]+", " ", desc)
    return {
        token for token in desc.split()
        if len(token) >= 4 and token not in _PENDING_NOISE
    }


def _pending_matches_posted(pending, posted):
    """True when this pending debit is the same purchase already posted.

    Same amount, posted on the pending date or within 10 days after, and a
    shared merchant token. A pending line with no merchant token matches a
    same-day posted debit of the same amount only.
    """
    if _cents(pending.get("debit") or 0) != _cents(posted.get("debit") or 0):
        return False
    if _cents(pending.get("debit") or 0) <= 0:
        return False
    gap = (posted["date"] - pending["date"]).days
    if gap < 0 or gap > 10:
        return False
    pending_tokens = _pending_tokens(pending.get("description") or "")
    posted_tokens = _pending_tokens(posted.get("description") or "")
    if pending_tokens and posted_tokens and (pending_tokens & posted_tokens):
        return True
    return gap == 0 and not pending_tokens


def _unmatched_pending_debits(transactions):
    """Pending debits that are not already present as a posted debit."""
    posted = [
        txn for txn in transactions
        if not txn.get("pending") and (txn.get("debit") or 0) > 0
    ]
    used = set()
    unmatched = []
    for pending in transactions:
        if not pending.get("pending") or (pending.get("debit") or 0) <= 0:
            continue
        match_index = None
        for index, txn in enumerate(posted):
            if index in used:
                continue
            if _pending_matches_posted(pending, txn):
                match_index = index
                break
        if match_index is None:
            unmatched.append(pending)
        else:
            used.add(match_index)
    return unmatched


def resolve_everyday_balances(transactions, stated_available=None):
    """
    Ledger and available balances for the everyday account.

    Prefer stated_available (an account line in the export), then the
    available cell on the newest row that has one. Otherwise subtract
    pending debits that have not yet posted from the newest posted ledger
    balance. Pending credits are not added: a pending refund does not
    change available funds until it finalises. With neither signal the
    posted ledger balance is used, which is the previous behaviour.

    Returns {ledger, available, source}. source is "available", "pending",
    "ledger", or "none".
    """
    ledger = _posted_ledger(transactions)
    # Newest row only. An older row's available cell is a past figure.
    newest = transactions[0] if transactions else None
    row_available = None if newest is None else newest.get("available")
    explicit = stated_available if stated_available is not None else row_available
    if explicit is not None:
        return {
            "ledger": None if ledger is None else _money(ledger),
            "available": _money(explicit),
            "source": "available",
        }
    pending_debit = sum((txn.get("debit") or 0) for txn in _unmatched_pending_debits(transactions))
    if ledger is not None and pending_debit > 0:
        return {
            "ledger": _money(ledger),
            "available": _money(ledger - pending_debit),
            "source": "pending",
        }
    if ledger is None:
        return {"ledger": None, "available": None, "source": "none"}
    return {
        "ledger": _money(ledger),
        "available": _money(ledger),
        "source": "ledger",
    }


def build_savings_breakdown(savings_balance, revolut_balance, revolut_source,
                            everyday_balance, capital, friend_share,
                            everyday_ledger=None, everyday_source=None):
    """
    Add up every fund that counts toward the savings goal.

    savings_balance, revolut_balance, and everyday_balance are dollar amounts
    or None when that source was not read. capital is the dict from
    reselling_tracker.paid_capital, or None when the sheet was not read.
    friend_share is subtracted only when revolut_source is "revolut". His
    Revolut share is never negative: a share larger than the balance counts
    as zero of his own. A missing source is left out of the cash subtotal
    and the total, rather than treated as a zero balance.

    everyday_balance is the available balance when the everyday export
    states one, otherwise the ledger balance. everyday_ledger is the posted
    balance and everyday_source says which figure everyday_balance is.
    """
    friend_share = _money(max(float(friend_share or 0), 0))
    source = revolut_source or "none"
    savings = None if savings_balance is None else _money(savings_balance)
    revolut = None if revolut_balance is None else _money(revolut_balance)
    everyday = None if everyday_balance is None else _money(everyday_balance)
    if everyday_source is None:
        everyday_source = "none" if everyday is None else "ledger"
    if everyday_ledger is None and everyday_source == "ledger":
        everyday_ledger = everyday
    elif everyday_ledger is not None:
        everyday_ledger = _money(everyday_ledger)

    applied = revolut is not None and source == "revolut"
    if revolut is None:
        revolut_own = None
    elif applied:
        revolut_own = _money(max(revolut - friend_share, 0))
    else:
        revolut_own = revolut

    if capital is None:
        stock = pending = deposits = capital_total = None
    else:
        stock = _money(capital.get("stock") or 0)
        pending = _money(capital.get("pending_shipping") or 0)
        deposits = _money(capital.get("deposits_paid") or 0)
        capital_total = _money(stock + pending + deposits)

    cash = _money(sum(
        part for part in (savings, revolut_own, everyday) if part is not None
    ))
    total = _money(cash + (capital_total if capital_total is not None else 0))
    return {
        "savings": savings,
        "revolut_balance": revolut,
        "revolut_source": source,
        "friend_share": friend_share,
        "friend_share_applied": applied,
        "revolut_own": revolut_own,
        "everyday": everyday,
        "everyday_ledger": everyday_ledger,
        "everyday_source": everyday_source,
        "stock": stock,
        "pending_shipping": pending,
        "deposits_paid": deposits,
        "cash": cash,
        "capital": capital_total,
        "total": total,
    }


def _read_paid_capital():
    """Load the reselling sheet and return cash already paid. Raises on failure."""
    from reselling_tracker import load_items, paid_capital
    return paid_capital(load_items())


def load_paid_capital():
    """
    Cash already spent on unsold reselling stock, or None if the sheet
    cannot be read. A failure leaves that component out of the goal.
    """
    try:
        return _read_paid_capital()
    except Exception as e:
        print(f"⚠️  savings goal: reselling capital unavailable: {e}")
        return None


def _account_balance(path):
    """Latest posted St. George ledger balance, or None when unread.

    Used for the savings account. Pending rows are ignored. An available
    column on this file is not used.
    """
    if path is None or not path.exists():
        return None
    try:
        return get_latest_balance(path)
    except Exception as e:
        print(f"⚠️  savings goal: balance unavailable for {path.name}: {e}")
        return None


def _everyday_position(path):
    """Available and ledger balances for the everyday CSV.

    A missing or unreadable file is source 'none'. The savings account does
    not use this.
    """
    empty = {"ledger": None, "available": None, "source": "none"}
    if path is None or not path.exists():
        return empty
    try:
        export = read_stgeorge_export(path)
    except Exception as e:
        print(f"⚠️  savings goal: everyday balance unavailable: {e}")
        return empty
    return resolve_everyday_balances(export["transactions"], export["stated_available"])


def _reselling_cash_balance():
    """
    Balance from analyse_reselling (Revolut CSV, otherwise the investing CSV).

    Returns (balance or None, source). A read error is an absent balance.
    """
    try:
        cash = analyse_reselling(days=1)
    except Exception as e:
        print(f"⚠️  savings goal: reselling cash balance unavailable: {e}")
        return None, "none"
    source = cash.get("source") or "none"
    balance = cash.get("balance")
    if balance is None:
        return None, source
    return balance, source


def savings_breakdown_lines(breakdown):
    """
    Brief lines for each counted component, the cash subtotal, and capital
    tied up. A source that was not read is left out of the text.
    """
    if not breakdown:
        return []
    lines = []
    cash_bits = []
    if breakdown.get("savings") is not None:
        cash_bits.append(f"savings ${breakdown['savings']:,.2f}")
    if breakdown.get("revolut_own") is not None:
        bit = f"Revolut own ${breakdown['revolut_own']:,.2f}"
        if breakdown.get("friend_share_applied") and breakdown.get("friend_share"):
            bit += (
                f" (balance ${breakdown['revolut_balance']:,.2f},"
                f" less ${breakdown['friend_share']:,.2f} held for a friend)"
            )
        cash_bits.append(bit)
    if breakdown.get("everyday") is not None:
        bit = f"everyday ${breakdown['everyday']:,.2f}"
        ledger = breakdown.get("everyday_ledger")
        if (
            breakdown.get("everyday_source") in ("available", "pending")
            and ledger is not None
            and ledger != breakdown["everyday"]
        ):
            bit += f" available (ledger ${ledger:,.2f})"
        cash_bits.append(bit)
    if cash_bits:
        lines.append(f"  Cash ${breakdown['cash']:,.2f}: " + ", ".join(cash_bits))
    if breakdown.get("capital") is not None:
        bits = []
        if breakdown.get("stock") is not None:
            bits.append(f"stock ${breakdown['stock']:,.2f}")
        if breakdown.get("pending_shipping") is not None:
            bits.append(f"pending shipping ${breakdown['pending_shipping']:,.2f}")
        if breakdown.get("deposits_paid") is not None:
            bits.append(f"deposits paid ${breakdown['deposits_paid']:,.2f}")
        detail = f": {', '.join(bits)}" if bits else ""
        lines.append(f"  Tied up ${breakdown['capital']:,.2f}{detail}")
    return lines


def analyse_savings():
    """
    Progress toward the savings goal (term_context.get_finance_goals()).

    The total is every fund he holds: the savings-account balance, his share
    of the Revolut balance (the reselling cashflow balance minus
    JARVIS_REVOLUT_FRIEND_SHARE), the St. George everyday available balance
    when that CSV states one (otherwise its posted ledger balance), and
    reselling capital already paid (in-stock cost,
    paid-pending-shipping cost, and deposits paid on deposit-paid rows).
    A source that cannot be read is skipped. pct, remaining, on_track, and
    projected_date all use that total. breakdown names each piece.
    """
    goals            = get_finance_goals()
    savings_goal     = goals["savings_goal"]
    savings_deadline = datetime.date.fromisoformat(goals["savings_deadline"])

    bal1 = _account_balance(SAVINGS1_CSV)
    revolut_balance, revolut_source = _reselling_cash_balance()
    everyday_position = _everyday_position(EVERYDAY_CSV)
    breakdown = build_savings_breakdown(
        bal1,
        revolut_balance,
        revolut_source,
        everyday_position["available"],
        load_paid_capital(),
        revolut_friend_share(),
        everyday_ledger=everyday_position["ledger"],
        everyday_source=everyday_position["source"],
    )

    total = breakdown["total"]
    remaining = _money(max(0, savings_goal - total))
    pct = (total / savings_goal * 100) if savings_goal > 0 else 0

    # Project completion date based on the gap still left after every fund.
    today = datetime.datetime.now(TIMEZONE).date()
    days_to_deadline = (savings_deadline - today).days

    monthly_savings = goals["monthly_income"] - goals["monthly_budget"]
    months_needed   = remaining / monthly_savings if monthly_savings > 0 else 999
    projected_date  = today + datetime.timedelta(days=months_needed * 30.4)

    on_track = projected_date <= savings_deadline

    return {
        "balance1":        bal1,
        "total":           total,
        "goal":            savings_goal,
        "deadline":        savings_deadline,
        "remaining":       remaining,
        "pct":             pct,
        "on_track":        on_track,
        "projected_date":  projected_date,
        "days_to_deadline": days_to_deadline,
        "monthly_savings": monthly_savings,
        "breakdown":       breakdown,
    }


# ── Reselling P&L ─────────────────────────────────────────────────────────────

def _reselling_card(txn):
    """True for a card purchase or card refund on the reselling account.

    A transfer is not a sale and not a stock purchase. Buyer payments and
    forwards are not reselling cashflow.
    Card purchases stay here because this account's card is how stock is
    bought. Own top-ups are already marked internal by the parser.
    """
    if txn.get("internal"):
        return False
    rail = (txn.get("rail") or "").lower()
    if rail in ("transfer", "topup"):
        return False
    if txn.get("refund") or rail in ("card_payment", "card payment", "card"):
        return True
    return False


def analyse_reselling(days=30):
    """
    Card purchases on the reselling account, net of card refunds.

    Transfers in and out are not sales and not stock. Commission income is
    the reselling sheet. Uses finance/revolut.csv when present, otherwise
    card-like rows on the St. George investing.csv.
    """
    today = datetime.datetime.now(TIMEZONE).date()
    cutoff = today - datetime.timedelta(days=days)

    if REVOLUT_CSV.exists():
        txns = parse_revolut_csv(REVOLUT_CSV)
        recent = [
            txn for txn in txns
            if txn["date"] >= cutoff and _reselling_card(txn)
        ]
        balance = next((txn["balance"] for txn in txns if txn["balance"] is not None), None)
        available = True
        source = "revolut"
    else:
        txns = parse_stgeorge_csv(INVESTING_CSV)
        recent = [
            txn for txn in txns
            if txn["date"] >= cutoff
            and rail_of(txn.get("description") or "") in ("card", "card_credit")
        ]
        balance = get_latest_balance(INVESTING_CSV)
        available = INVESTING_CSV.exists()
        source = "investing" if available else "none"

    refunds = sum(txn["credit"] for txn in recent if txn.get("refund") or txn["credit"] > 0 and txn["debit"] <= 0)
    deployed = sum(txn["debit"] for txn in recent) - refunds
    return {
        "deployed": round(deployed, 2),
        "returned": 0.0,
        "net": round(0.0 - deployed, 2),
        "balance": balance,
        "txn_count": len(recent),
        "days": days,
        "available": available,
        "source": source,
    }


# ── Monthly review check ──────────────────────────────────────────────────────

def is_monthly_review_day():
    """Returns True on the 1st of each month."""
    return datetime.datetime.now(TIMEZONE).day == 1


# ── Main summary function ─────────────────────────────────────────────────────

def analyse_income(transactions, days=30):
    """
    Income over the last N days: direct credits, other non-transfer credits,
    and transfers in from a listed income payer.

    Transfers in (Osko, Sct, PayID, NPP) are income only when their payer
    is listed in spending.income_payers in term_context.json.
    Card refunds reduce spending instead. Commission is not added here; it
    is the reselling sheet figure on the spending summary.
    """
    today = datetime.datetime.now(TIMEZONE).date()
    cutoff = today - datetime.timedelta(days=days)
    classified = _classified(transactions, cutoff, today)
    by_source = {}
    for txn in classified["income"]:
        source = txn["source"]
        bucket = by_source.setdefault(source, {"total": 0, "transactions": []})
        bucket["total"] += txn["amount"]
        label = txn["date"].strftime("%d %b") if hasattr(txn["date"], "strftime") else str(txn["date"])
        bucket["transactions"].append({
            "date": label,
            "description": (txn["description"] or "")[:35],
            "amount": round(txn["amount"], 2),
        })
    total = sum(bucket["total"] for bucket in by_source.values())
    return {
        "by_source": by_source,
        "total": round(total, 2),
        "days": days,
        "txn_count": len(classified["income"]),
    }


def get_finance_summary():
    """
    Returns a formatted finance summary string for the morning brief.
    Called by morning_brief.py.
    """
    lines = []

    # ── Check if files exist ──────────────────────────────────────────────────
    if not EVERYDAY_CSV.exists():
        return (
            "FINANCE: No data available.\n"
            "  → Export your St. George CSV and save to jarvis/finance/everyday.csv"
        )

    # ── Spending analysis ─────────────────────────────────────────────────────
    everyday_txns = parse_stgeorge_csv(EVERYDAY_CSV)
    spending      = analyse_spending(everyday_txns, days=7)

    lines.append("SPENDING (last 7 days):")

    tracked_cats = [
        "Food & dining", "Entertainment", "Shopping", "Sport & leisure",
        "Education", "Transport", "Subscriptions",
    ]
    for cat in tracked_cats:
        amt = spending["category_totals"].get(cat, 0)
        if amt > 0:
            lines.append(f"  {cat:<18} ${amt:.2f}")

    other = spending["category_totals"].get("Other", 0)
    other += sum(
        v for k, v in spending["category_totals"].items()
        if k not in tracked_cats and k not in ("Other", INTERNAL_CATEGORY)
    )
    if other > 0:
        lines.append(f"  {'Other':<18} ${other:.2f}")

    weekly_budget = get_finance_goals()["weekly_budget"]
    over_budget = spending["total_spend"] > weekly_budget
    budget_str = f"  (⚠ over ~${weekly_budget:.0f}/week budget)" if over_budget else f"  (within ~${weekly_budget:.0f}/week budget)"
    lines.append(f"  {'Total spend':<18} ${spending['total_spend']:.2f}{budget_str}")
    logged = spending.get("logged") or {}
    if logged.get("count"):
        lines.append(
            f"  {'Includes logged':<18} ${logged['total']:.2f}"
            f"  ({logged['count']} Back Tap entries)"
        )
    transfers = spending.get("transfers") or {}
    if transfers.get("count"):
        lines.append(
            f"  {'Transfers':<18} ${transfers['total_out']:.2f} out, "
            f"${transfers['total_in']:.2f} in  (not counted)"
        )
    commission = spending.get("commission") or {}
    if commission.get("total"):
        lines.append(f"  {'Resale commission':<18} ${commission['total']:.2f}")

    # Flag big transactions
    if spending["big_transactions"]:
        lines.append("  Flagged transactions (>$50):")
        for t in spending["big_transactions"][:4]:  # max 4
            lines.append(f"    • {t['date'].strftime('%d %b')}  ${t['debit']:.2f}  {t['description'][:40]}")

    lines.append("")

    # ── Income tracking ───────────────────────────────────────────────────────
    income = analyse_income(everyday_txns, days=30)
    if income["total"] > 0:
        lines.append("INCOME (last 30 days):")
        for source, data in sorted(income["by_source"].items(), key=lambda x: -x[1]["total"]):
            if source not in ("Transfer In", "Other", "Refund") and data["total"] > 0:
                lines.append(f"  {source:<18} ${data['total']:.2f}")
        if "Transfer In" in income["by_source"]:
            ti = income["by_source"]["Transfer In"]["total"]
            if ti > 0:
                lines.append(f"  {'Transfers In':<18} ${ti:.2f}  (internal)")
        lines.append(f"  {'Total income':<18} ${income['total']:.2f}")
        import datetime as _dt, pytz as _ptz
        _tz    = _ptz.timezone(config.TIMEZONE)
        _today = _dt.datetime.now(_tz)
        _days_elapsed   = _today.day
        _days_in_month  = 30
        expected        = 2880.0
        prorated        = expected * (_days_elapsed / _days_in_month)
        if income["total"] < prorated * 0.75:
            lines.append(f"  ⚠ Behind pace — ${income['total']:.0f} earned, ~${prorated:.0f} expected by day {_days_elapsed}")
        else:
            lines.append(f"  ✓ On pace — ${income['total']:.0f} of ~${expected:.0f} expected this month")
        lines.append("")

    # ── Reselling P&L ─────────────────────────────────────────────────────────
    reselling = analyse_reselling(days=7)
    if reselling["available"]:
        lines.append("RESELLING (last 7 days):")
        lines.append(f"  Deployed         ${reselling['deployed']:.2f}  (inventory)")
        lines.append(f"  Returned         ${reselling['returned']:.2f}  (sales)")
        net = reselling["net"]
        net_sign = "+" if net >= 0 else "−"
        net_label = "profit" if net >= 0 else "burn"
        lines.append(f"  Net              {net_sign}${abs(net):.2f}  ({net_label} this week)")
        if reselling["balance"] is not None:
            lines.append(f"  Account balance  ${reselling['balance']:,.2f}")
        lines.append("")

    # ── Savings progress ──────────────────────────────────────────────────────
    savings = analyse_savings()

    if savings["total"] > 0:
        goals = get_finance_goals()
        bar_filled = int(savings["pct"] / 5)  # 20 char bar
        bar = "█" * bar_filled + "░" * (20 - bar_filled)
        deadline_str = savings["deadline"].strftime("%B %Y")
        lines.append(f"SAVINGS GOAL (${savings['goal']:,.0f} by {deadline_str}):")
        lines.append(f"  [{bar}] {savings['pct']:.1f}%")
        lines.append(f"  Total: ${savings['total']:,.2f}  |  Remaining: ${savings['remaining']:,.2f}")
        lines.extend(savings_breakdown_lines(savings.get("breakdown") or {}))

        if savings["on_track"]:
            lines.append(f"  ✓ On track — projected to hit goal {savings['projected_date'].strftime('%B %Y')}")
        else:
            lines.append(f"  ✗ Behind — projected {savings['projected_date'].strftime('%B %Y')} (deadline: {deadline_str})")

        lines.append(
            f"  Saving ~${savings['monthly_savings']:,.0f}/month "
            f"(~${goals['monthly_income']:,.0f} income - ~${goals['monthly_budget']:,.0f} spending)"
        )
    else:
        lines.append("SAVINGS: Add savings CSV files to see progress.")

    # ── Monthly review prompt ─────────────────────────────────────────────────
    if is_monthly_review_day():
        lines.append("")
        lines.append("📅 MONTHLY REVIEW DAY — review last month's spending and update your budget.")

    return "\n".join(lines)


def summarise_spending(transactions, start, end, weekly_budget, extra_inflows=None):
    """
    Category totals and weekly-budget comparison for an inclusive date range.

    Card, BPAY and direct debit lines count in their category, net of card
    refunds at the same merchant inside the refund window. Back Tap logged
    spends dated in the range are added as they are. Own-account moves and
    Revolut card top-ups are dropped. Transfers (Osko, Sct, PayID, NPP) are
    listed under transfers with counted false; they are never spending or
    income. Commission is the reselling sheet rows dated in the range.
    Descriptions on flagged rows are the full bank line. extra_inflows is
    ignored.
    """
    del extra_inflows
    classified = _classified(transactions, start, end)
    recent = classified["spending"]

    by_category = {}
    for txn in recent:
        cat = txn.get("category") or "Other"
        by_category[cat] = by_category.get(cat, 0) + txn["debit"]
    by_category = {cat: round(amount, 2) for cat, amount in by_category.items()}

    total = round(sum(by_category.values()), 2)
    span_days = (end - start).days + 1
    weekly_budget = round(float(weekly_budget), 2)
    window_budget = round(weekly_budget * span_days / 7.0, 2)

    flagged = []
    for txn in recent:
        if txn["debit"] < 80:
            continue
        flagged.append({
            "date": txn["date"].isoformat(),
            "amount": round(txn["debit"], 2),
            "category": txn.get("category") or "Other",
            "description": txn.get("description") or "",
        })
    flagged.sort(key=lambda row: row["amount"], reverse=True)

    income = []
    for txn in classified["income"]:
        day = txn.get("date")
        income.append({
            "date": day.isoformat() if hasattr(day, "isoformat") else day,
            "description": txn.get("description") or "",
            "amount": txn.get("amount"),
            "source": txn.get("source"),
        })
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "days": span_days,
        "by_category": by_category,
        "total_spend": total,
        "transaction_count": len(recent),
        "weekly_budget": weekly_budget,
        "budget_for_range": window_budget,
        "over_budget": total > window_budget,
        "weekly_equivalent": round(total * 7.0 / span_days, 2) if span_days else 0.0,
        "flagged": flagged[:4],
        "logged": _logged_summary(recent),
        "transfers": _transfer_summary(classified["transfers"]),
        "commission": _sheet_commission(start, end),
        "income": income,
    }


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\n💰  Finance summary\n")
    print(get_finance_summary())
    print()
