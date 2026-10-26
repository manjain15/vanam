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
    from live_spend import get_live_summary
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
# Manual marks for one transfer. data/ is gitignored, so a rule edited on the
# VPS is not overwritten by deploy. Read on every summary; no restart.
TRANSFER_OVERRIDES_FILE = SCRIPT_DIR / "data" / "transfer_overrides.json"

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
        get_own_account_rules,
        get_refund_window_days,
        get_transfer_carry_days,
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

    def get_transfer_carry_days():
        """Fallback when term_context cannot be imported."""
        return 7


# ── Category rules ────────────────────────────────────────────────────────────
# Each rule is a list of keywords (case-insensitive) found in the description.
# First match wins.

# ── Income source rules ──────────────────────────────────────────────────────
# Keywords in credit transaction descriptions → income category

INCOME_RULES = [
    # Automation/PropWealth salary — main job
    ("Automation",  ["propwealth", "prop wealth"]),
    # Tutoring — Axis Education platform
    ("Tutoring",    ["axis ed", "axis education", "tutoring", "tutor"]),
    # Pokemon / marketplace sales
    ("Pokemon Sales", ["ivan christian", "celestino"]),
    # Note: internet deposits and sct deposits are excluded above as own-account transfers
    # Refunds and one-offs
    ("Refund",      ["refund", "cashback", "rebate", "exchange application"]),
    # Friends splitting bills — NOT income, filter these out
    # These are identified by being small Osko deposits from friends
]

# Names/keywords that indicate friend bill-splits (not income)
# Osko deposits from these are excluded from income
FRIEND_KEYWORDS = [
    "sagar ahuja", "adesh sunkari", "nilesh banga", "banga n",
    "samuel selvadoss", "sai satpute", "muhunthan", "michael",
    "paint sprayer",  # one-off reimbursement
    "banga", "grace", "mother s day", "bangas",
    "exchange application fee",  # deposit from own savings
]

def is_friend_split(description):
    desc_lower = description.lower()
    return any(kw in desc_lower for kw in FRIEND_KEYWORDS)

def categorise_income(description):
    desc_lower = description.lower()
    for category, keywords in INCOME_RULES:
        for kw in keywords:
            if kw in desc_lower:
                return category
    return "Other"


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

# Description fragments that are internal no matter who the payee is.
# "transfer" is the pre-existing morning-brief rule; internet withdrawals too.
_ALWAYS_INTERNAL_MARKERS = ("internet withdrawal", "transfer")


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
    """True for moves between the owner's own accounts, which are not spending.

    Internet withdrawals and anything described as a transfer are always
    internal. Osko, internet, and Sct withdrawals are internal when the payee
    is an owner name, wherever that name sits in the description. Card top-ups
    of an own account (Revolut) are internal too. Names and patterns come from
    term_context.get_own_account_rules().
    """
    desc = _normalise_desc(description)
    if any(marker in desc for marker in _ALWAYS_INTERNAL_MARKERS):
        return True
    rules = get_own_account_rules()
    names = rules.get("owner_names") or []
    markers = rules.get("withdrawal_markers") or []
    if names and any(marker in desc for marker in markers) and any(name in desc for name in names):
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


# ── Refunds ───────────────────────────────────────────────────────────────────
# A credit reduces a counted debit only when every rule below holds. Ordinary
# income (salary, reselling payouts, friend deposits, own-account transfers)
# fails these rules and stays out of the spending total.
#
# 1. The credit is not an own-account transfer, a friend split, or an inbound
#    payment marked advance / pokemon / bank (customer payouts, not refunds).
# 2. The credit is on or after the debit, and no later than refund_window_days
#    (default 14, spending.refund_window_days in term_context.json). Clock
#    times embedded in the description (22Sep18:35) order same-day rows.
#    Untimed rows sit at 12:00.
# 3. The credit is the same amount as the debit, or smaller (a partial refund).
#    A larger credit never offsets a smaller debit.
# 4. Identity: shared surname plus a given name or initial ("R Jaiswal" /
#    "Rishi Jaiswal"). Surname alone does not match. Merchant card refunds
#    (description contains refund, reversal, chargeback, or "visa credit")
#    match when they share a merchant token such as "woolworths".
# 5. Each debit takes at most one credit, and each credit pays one debit.
#    Exact amount wins over a partial; otherwise the latest debit still
#    before the credit wins. A later repurchase is left intact.

_REFUND_HINTS = ("refund", "reversal", "chargeback", "visa credit", "eftpos credit")
_NOT_REFUND_CREDIT = ("advance", "pokemon", "bank")
_NOISE_TOKENS = {
    "osko", "sct", "visa", "eftpos", "withdrawal", "deposit", "purchase",
    "internet", "transfer", "sent", "from", "the", "pty", "ltd", "payment",
    "direct", "credit", "refund", "reversal", "chargeback", "to", "for",
}
_PLACE_TOKENS = {
    "sydney", "melbourne", "australia", "nsw", "london", "online",
    "brisbane", "perth", "adelaide", "hobart", "canberra",
}


def _cents(amount):
    """Integer cents, so 14.95 and a matching refund compare equal."""
    return int(round(float(amount) * 100))


def _dollars(cents):
    """Cents as a dollar amount rounded to the cent."""
    return round(int(cents) / 100.0, 2)


def _event_dt(txn):
    """Datetime used to order a debit and its refund.

    Uses a HH:MM glued to the description date (23Sep09:11) when present.
    Rows with no clock time are treated as 12:00 on the CSV date.
    """
    desc = _normalise_desc(txn.get("description", ""))
    match = re.search(r"\d{2}[a-z]{3}(\d{2}):(\d{2})", desc)
    if not match:
        match = re.search(r"\b(\d{2}):(\d{2})\b", desc)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return datetime.datetime.combine(txn["date"], datetime.time(hour, minute))
    return datetime.datetime.combine(txn["date"], datetime.time(12, 0))


def _payee_tokens(description):
    """Alphabetic tokens left after the bank rail, date, and filler words."""
    desc = _normalise_desc(description)
    desc = re.sub(r"\d{2}[a-z]{3}(?:\d{2}:\d{2})?", " ", desc)
    desc = re.sub(r"\b\d{2}:\d{2}\b", " ", desc)
    desc = re.sub(r"[^a-z]+", " ", desc)
    return [token for token in desc.split() if token not in _NOISE_TOKENS]


def _surname_and_given(tokens):
    """Last word of 3+ letters is the surname. Earlier tokens are the given name."""
    long_indexes = [i for i, token in enumerate(tokens) if len(token) >= 3]
    if not long_indexes:
        return "", list(tokens)
    index = long_indexes[-1]
    return tokens[index], tokens[:index] + tokens[index + 1:]


def _given_names_corroborate(left_given, right_given):
    """True when a full given name or an initial lines up across the two sides."""
    left_words = {token for token in left_given if len(token) >= 3}
    right_words = {token for token in right_given if len(token) >= 3}
    if left_words & right_words:
        return True
    left_initials = {token for token in left_given if len(token) == 1}
    right_initials = {token for token in right_given if len(token) == 1}
    if any(word.startswith(initial) for initial in left_initials for word in right_words):
        return True
    if any(word.startswith(initial) for initial in right_initials for word in left_words):
        return True
    return bool(left_initials) and left_initials == right_initials


def _person_refund_match(debit_desc, credit_desc):
    """Surname plus given name or initial. 'R Jaiswal' matches 'Rishi Jaiswal'."""
    debit_surname, debit_given = _surname_and_given(_payee_tokens(debit_desc))
    credit_surname, credit_given = _surname_and_given(_payee_tokens(credit_desc))
    if not debit_surname or debit_surname != credit_surname:
        return False
    return _given_names_corroborate(debit_given, credit_given)


def _merchant_refund_match(debit_desc, credit_desc):
    """Card refund / reversal that names the same merchant as the debit."""
    credit = _normalise_desc(credit_desc)
    if not any(hint in credit for hint in _REFUND_HINTS):
        return False
    debit_tokens = {
        token for token in _payee_tokens(debit_desc)
        if len(token) >= 4 and token not in _PLACE_TOKENS
    }
    credit_tokens = {
        token for token in _payee_tokens(credit_desc)
        if len(token) >= 4 and token not in _PLACE_TOKENS
    }
    return bool(debit_tokens & credit_tokens)


def _is_internal_credit(description):
    """True for money arriving from the owner's own accounts."""
    desc = _normalise_desc(description)
    if "internet deposit" in desc or "from 0000" in desc or "sent from revolut" in desc:
        return True
    names = get_own_account_rules().get("owner_names") or []
    if names and any(name in desc for name in names) and "deposit" in desc:
        return True
    return _is_own_card_topup(desc, get_own_account_rules())


def _credit_can_be_refund(description):
    """False for income and transfers that must not reduce personal spending."""
    if _is_internal_credit(description):
        return False
    if is_friend_split(description):
        return False
    desc = _normalise_desc(description)
    return not any(re.search(rf"\b{re.escape(word)}\b", desc) for word in _NOT_REFUND_CREDIT)


def _descriptions_match_refund(debit_desc, credit_desc):
    """Person-name refund, or a merchant card refund of the same shop."""
    if _merchant_refund_match(debit_desc, credit_desc):
        return True
    return _person_refund_match(debit_desc, credit_desc)


def _refund_offsets(debits, credits, window_days):
    """Map a debit index to the cents-as-dollars a single credit offsets.

    Credits are applied oldest first. Each debit and each credit is used
    once. See the refund rules at the top of this section.

    Returns (offsets, used_credit_ids). used_credit_ids are the id() of
    credit dicts that were applied, so the transfer net cannot reuse
    the same credit.
    """
    offsets = {}
    used = set()
    used_credits = set()
    eligible = [
        credit for credit in credits
        if credit.get("credit", 0) > 0
        and _credit_can_be_refund(credit.get("description", ""))
    ]
    eligible.sort(key=lambda credit: (_event_dt(credit), credit.get("credit", 0)))
    for credit in eligible:
        credit_cents = _cents(credit["credit"])
        if credit_cents <= 0:
            continue
        credit_dt = _event_dt(credit)
        best = None
        for index, debit in enumerate(debits):
            if index in used:
                continue
            debit_cents = _cents(debit.get("debit", 0))
            if credit_cents > debit_cents or debit_cents <= 0:
                continue
            debit_dt = _event_dt(debit)
            if credit_dt < debit_dt:
                continue
            if (credit_dt.date() - debit_dt.date()).days > window_days:
                continue
            if not _descriptions_match_refund(
                debit.get("description", ""), credit.get("description", ""),
            ):
                continue
            rank = (0 if credit_cents == debit_cents else 1, -debit_dt.timestamp(), index)
            if best is None or rank < best[0]:
                best = (rank, index)
        if best is None:
            continue
        index = best[1]
        used.add(index)
        used_credits.add(id(credit))
        offsets[index] = credit_cents / 100.0
    return offsets, used_credits


# Person-to-person rails that are netted, not counted one by one.
# Card purchases, direct debits, and BPAY do not match these, so they
# always stay in spending. Anything described as a transfer is already an
# own-account move and never reaches this net.
_TRANSFER_OUT_MARKERS = ("osko withdrawal", "sct withdrawal")
_TRANSFER_IN_MARKERS = ("osko deposit", "sct deposit")
# Identified income must not cancel a real transfer. Sale-proceeds Oskos
# that do not match these stay on the inflow side.
_TRANSFER_INCOME = ("Automation", "Tutoring", "Refund", "Pokemon Sales")
_NOT_INFLOW_TEXT = ("direct credit", "payroll", "salary", "wages")
_OVERRIDE_TREATS = {"spending": "spending", "pass-through": "pass-through", "passthrough": "pass-through"}


def _rail_is_person_transfer(description, direction):
    """True when the description is Osko, Sct, PayID, or NPP.

    direction is 'out' or 'in'. PayID and NPP count in either direction;
    the debit or credit says which way the money moved.
    """
    desc = _normalise_desc(description)
    if re.search(r"\bpayid\b", desc) or re.search(r"\bnpp\b", desc):
        return True
    markers = _TRANSFER_OUT_MARKERS if direction == "out" else _TRANSFER_IN_MARKERS
    return any(marker in desc for marker in markers)


def _is_everyday_transfer_out(txn):
    """True for an uncategorised person-to-person withdrawal on this account.

    A categorised Osko (a ticket, a shop) is a purchase and stays in
    spending. Own-account moves and pending rows are not part of the net.
    """
    if txn.get("pending") or txn.get("rail") is not None:
        return False
    if (txn.get("debit") or 0) <= 0:
        return False
    description = txn.get("description") or ""
    if _is_internal_spend(description):
        return False
    category = txn.get("category") or categorise(description)
    if category != "Other":
        return False
    return _rail_is_person_transfer(description, "out")


def _is_everyday_transfer_in(txn, refunded_ids):
    """True for person-to-person money arriving on this account.

    A credit already used as a refund is not reused. Own-account deposits,
    salary, payroll, tutoring, pokemon-sale income, and refund credits are
    not inflows, so pay does not cancel a withdrawal. Revolut rows carry a
    rail and are netted on that account instead.
    """
    if txn.get("rail") is not None:
        return False
    if id(txn) in refunded_ids or txn.get("pending"):
        return False
    if txn.get("internal") or txn.get("refund"):
        return False
    if (txn.get("credit") or 0) <= 0:
        return False
    description = txn.get("description") or ""
    if _is_internal_credit(description):
        return False
    if categorise_income(description) in _TRANSFER_INCOME:
        return False
    desc = _normalise_desc(description)
    if any(marker in desc for marker in _NOT_INFLOW_TEXT):
        return False
    return _rail_is_person_transfer(description, "in")


def _is_revolut_external_transfer(txn):
    """True for a completed external Revolut transfer with an amount.

    Top-ups, owner transfers, and card payments are not included.
    """
    if txn.get("internal") or txn.get("refund") or txn.get("pending"):
        return False
    if txn.get("rail") != "transfer":
        return False
    return (txn.get("debit") or 0) > 0 or (txn.get("credit") or 0) > 0


def revolut_external_transfers():
    """External Revolut transfers in and out. Missing or unreadable file is []."""
    path = REVOLUT_CSV
    if path is None or not Path(path).exists():
        return []
    try:
        rows = parse_revolut_csv(path)
    except Exception as exc:
        print(f"⚠️  transfer net: revolut transfers unavailable: {exc}")
        return []
    return [row for row in rows if _is_revolut_external_transfer(row)]


def load_transfer_overrides(path=None):
    """Rules that force one transfer to count as spending or as pass-through.

    Reads data/transfer_overrides.json on every call. The file is either
    {"rules": [...]} or a list. Each rule needs date (YYYY-MM-DD), amount
    (dollars), description (a fragment of the bank line), and treat
    ("spending" or "pass-through"). A missing, empty, or invalid file is
    []. Own-account lines never reach these rules.
    """
    path = Path(path) if path is not None else TRANSFER_OVERRIDES_FILE
    if path is None or not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if isinstance(raw, dict):
        raw = raw.get("rules")
    if not isinstance(raw, list):
        return []
    rules = []
    for rule in raw:
        if not isinstance(rule, dict):
            continue
        treat_key = str(rule.get("treat") or "").strip().lower().replace("_", " ").replace("-", " ")
        treat_key = "-".join(treat_key.split())
        treat = _OVERRIDE_TREATS.get(treat_key)
        fragment = _normalise_desc(rule.get("description") or "")
        if treat is None or not fragment:
            continue
        try:
            day = datetime.date.fromisoformat(str(rule.get("date") or "")[:10])
            amount = _cents(rule.get("amount"))
        except (TypeError, ValueError):
            continue
        if amount <= 0:
            continue
        rules.append({
            "date": day,
            "amount": amount,
            "description": fragment,
            "treat": treat,
        })
    return rules


def _override_treat(txn, rules):
    """'spending', 'pass-through', or None. The first matching rule wins.

    The date and the amount to the cent must match, and the rule's
    description fragment must sit inside the bank description. A debit
    matches its debit; a credit matches its credit.
    """
    if not rules:
        return None
    day = txn.get("date")
    description = _normalise_desc(txn.get("description") or "")
    amount = _cents(txn.get("debit") or 0) or _cents(txn.get("credit") or 0)
    if amount <= 0 or not description:
        return None
    for rule in rules:
        if rule["date"] != day or rule["amount"] != amount:
            continue
        if rule["description"] in description:
            return rule["treat"]
    return None


def _transfer_net_dict(in_cents, out_cents, carry_cents):
    """Audit block for one account. counted is the positive remainder only."""
    net = out_cents - in_cents - carry_cents
    return {
        "in": _dollars(in_cents),
        "out": _dollars(out_cents),
        "carry_in": _dollars(carry_cents),
        "net": _dollars(net),
        "counted": _dollars(max(net, 0)),
    }


def _lookback_surplus_cents(transactions, start, carry_days, refunded_ids, rules):
    """Unused inflow from the days before start, in cents.

    Outflows in that lookback consume its inflows first, so a deposit that
    already funded last period's transfers is not reused. A deficit does
    not carry. A line forced to count as spending does not consume inflow.
    carry_days of 0 returns 0.
    """
    if carry_days <= 0:
        return 0
    begin = start - datetime.timedelta(days=carry_days)
    in_cents = 0
    out_cents = 0
    for txn in transactions:
        day = txn.get("date")
        if day is None or day < begin or day >= start:
            continue
        if _is_everyday_transfer_in(txn, refunded_ids):
            in_cents += _cents(txn["credit"])
            continue
        if not _is_everyday_transfer_out(txn):
            continue
        if _override_treat(txn, rules) == "spending":
            continue
        out_cents += _cents(txn["debit"])
    return max(0, in_cents - out_cents)


def _revolut_transfer_net(legs, start, end, carry_days):
    """Net of external Revolut transfers. Not added to personal spending."""
    begin = start - datetime.timedelta(days=carry_days) if carry_days else start
    window_in = window_out = prior_in = prior_out = 0
    for txn in legs or []:
        if not _is_revolut_external_transfer(txn):
            continue
        day = txn.get("date")
        if day is None:
            continue
        incoming = (txn.get("credit") or 0) > 0
        cents = _cents(txn["credit"] if incoming else txn["debit"])
        if start <= day <= end:
            if incoming:
                window_in += cents
            else:
                window_out += cents
        elif carry_days and begin <= day < start:
            if incoming:
                prior_in += cents
            else:
                prior_out += cents
    return _transfer_net_dict(window_in, window_out, max(0, prior_in - prior_out))


def _excluded_transfer_summary(txns, total_cents):
    """Withdrawals pooled out of the line items, and the dollars left out of spend.

    total is the amount that did not become spending. When the whole pool
    was left out, that equals the sum of items. A positive remainder is
    spending instead, so total is then smaller than the sum of items.
    """
    ordered = sorted(
        txns,
        key=lambda txn: (
            txn["date"],
            txn.get("description") or "",
            txn.get("debit") or 0,
        ),
    )
    items = []
    for txn in ordered:
        items.append({
            "date": txn["date"].isoformat(),
            "amount": round(txn["debit"], 2),
            "description": txn.get("description") or "",
        })
    return {
        "count": len(items),
        "total": _dollars(max(total_cents, 0)),
        "items": items,
    }


def _remainder_row(amount, when):
    """One Other debit for a positive transfer remainder."""
    return {
        "date": when,
        "description": "Net transfers",
        "debit": amount,
        "credit": 0.0,
        "balance": None,
        "category": "Other",
    }


def _net_spending_debits(transactions, start, end, extra_inflows=None):
    """Counted debits in the inclusive range, after refunds and the transfer net.

    Own-account transfers and pending rows are dropped first. A fully
    refunded debit is omitted. A partial refund keeps the category and the
    remaining amount. Person-to-person withdrawals on this account (Osko,
    Sct, PayID, NPP) are then netted against person-to-person deposits on
    the same account: outflows minus inflows, minus any inflow surplus from
    the carry lookback. Only a positive remainder is spending, as one Other
    line. A negative net does not reduce card, direct-debit, or BPAY
    spending. When nothing came in, each withdrawal stays as its own line.

    External Revolut transfers are netted on their own account and reported
    under transfers_net.reselling. That remainder is not added here, because
    the reselling ledger already counts that account and a Revolut deposit
    must not cancel an everyday Osko.

    A rule in data/transfer_overrides.json can force one withdrawal to
    count in full (treat spending) or to drop out (treat pass-through).
    The original transaction dicts are not mutated.

    Returns (debits, excluded_transfers, transfers_net). extra_inflows
    defaults to external Revolut transfers read from REVOLUT_CSV.
    """
    if extra_inflows is None:
        extra_inflows = revolut_external_transfers()
    debits = [
        txn for txn in transactions
        if start <= txn["date"] <= end
        and txn.get("debit", 0) > 0
        and not txn.get("pending")
        and not _is_internal_spend(txn.get("description", ""))
    ]
    offsets, refunded_ids = _refund_offsets(
        debits, transactions, get_refund_window_days(),
    )
    netted = []
    for index, txn in enumerate(debits):
        refunded = offsets.get(index, 0.0)
        remaining = round(txn["debit"] - refunded, 2)
        if remaining <= 0:
            continue
        if refunded:
            row = dict(txn)
            row["debit"] = remaining
            row["refunded"] = round(refunded, 2)
            netted.append(row)
        else:
            netted.append(txn)

    rules = load_transfer_overrides()
    merchant = []
    pool = []
    forced_spend = []
    forced_pass = []
    for txn in netted:
        if not _is_everyday_transfer_out(txn):
            merchant.append(txn)
            continue
        treat = _override_treat(txn, rules)
        if treat == "spending":
            forced_spend.append(txn)
        elif treat == "pass-through":
            forced_pass.append(txn)
        else:
            pool.append(txn)

    in_cents = sum(
        _cents(txn["credit"]) for txn in transactions
        if start <= txn["date"] <= end and _is_everyday_transfer_in(txn, refunded_ids)
    )
    carry_days = get_transfer_carry_days()
    carry_cents = _lookback_surplus_cents(
        transactions, start, carry_days, refunded_ids, rules,
    )
    out_cents = sum(_cents(txn["debit"]) for txn in pool)
    covered = in_cents + carry_cents
    counted_cents = max(out_cents - covered, 0)
    kept = list(merchant) + list(forced_spend)
    dropped = list(forced_pass)
    if covered > 0:
        dropped.extend(pool)
        if counted_cents > 0:
            when = end
            if end == datetime.date.max and pool:
                when = max(txn["date"] for txn in pool)
            kept.append(_remainder_row(_dollars(counted_cents), when))
        left_out = out_cents + sum(_cents(txn["debit"]) for txn in forced_pass) - counted_cents
    else:
        kept.extend(pool)
        left_out = sum(_cents(txn["debit"]) for txn in forced_pass)

    excluded = _excluded_transfer_summary(dropped, left_out)
    transfers_net = _transfer_net_dict(in_cents, out_cents, carry_cents)
    reselling = _revolut_transfer_net(extra_inflows, start, end, carry_days)
    if reselling["in"] or reselling["out"]:
        transfers_net["reselling"] = reselling
    return kept, excluded, transfers_net


# ── Spending analysis ─────────────────────────────────────────────────────────

def analyse_spending(transactions, days=7, extra_inflows=None):
    """
    Analyses spending from the everyday account over the last N days.
    Returns a dict with category totals, big transactions, and overall total.

    Own-account transfers (see _is_internal_spend) are left out of the totals,
    the category breakdown, and the flagged debits. Refund credits that match
    a counted debit (see the refund rules above) reduce that debit's category
    and the total. Person-to-person transfers are netted and reported on
    transfers_net; withdrawals that dropped out are listed on
    excluded_transfers. That is the same filter summarise_spending uses for
    GET /finance. extra_inflows defaults to external Revolut transfers, which
    are netted separately and do not reduce this total.
    """
    today    = datetime.datetime.now(TIMEZONE).date()
    cutoff   = today - datetime.timedelta(days=days)

    recent, excluded, transfers_net = _net_spending_debits(
        transactions, cutoff, today, extra_inflows,
    )

    # Category totals
    category_totals = {}
    for t in recent:
        cat = t["category"]
        category_totals[cat] = category_totals.get(cat, 0) + t["debit"]

    # Big transactions ($80+ after refunds, own-account transfers already removed)
    big_transactions = [t for t in recent if t["debit"] >= 80]

    total_spend = sum(t["debit"] for t in recent)

    return {
        "category_totals": category_totals,
        "big_transactions": big_transactions,
        "total_spend":      total_spend,
        "transaction_count": len(recent),
        "transactions":     recent,
        "days":             days,
        "excluded_transfers": excluded,
        "transfers_net":    transfers_net,
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

def analyse_reselling(days=30):
    """
    Analyses the reselling account's working capital.
    Debits = inventory purchases (capital deployed), net of card refunds.
    Credits = sale proceeds (capital returned).
    Uses finance/revolut.csv when present (own top-ups/transfers excluded via
    parse_revolut_csv), otherwise the St. George investing.csv (internal
    transfers excluded by description).
    """
    today  = datetime.datetime.now(TIMEZONE).date()
    cutoff = today - datetime.timedelta(days=days)

    if REVOLUT_CSV.exists():
        txns    = parse_revolut_csv(REVOLUT_CSV)
        recent  = [t for t in txns if t["date"] >= cutoff and not t["internal"]]
        balance = next((t["balance"] for t in txns if t["balance"] is not None), None)
        available = True
        source = "revolut"
    else:
        txns = parse_stgeorge_csv(INVESTING_CSV)
        recent = [
            t for t in txns
            if t["date"] >= cutoff
            and "internet deposit"    not in t["description"].lower()
            and "internet withdrawal" not in t["description"].lower()
            and "sct deposit"         not in t["description"].lower()
            and "transfer"            not in t["description"].lower()
        ]
        balance   = get_latest_balance(INVESTING_CSV)
        available = INVESTING_CSV.exists()
        source = "investing" if available else "none"

    refunds  = sum(t["credit"] for t in recent if t.get("refund"))
    deployed = sum(t["debit"]  for t in recent) - refunds
    returned = sum(t["credit"] for t in recent if not t.get("refund"))
    net      = returned - deployed

    return {
        "deployed":  round(deployed, 2),
        "returned":  round(returned, 2),
        "net":       round(net, 2),
        "balance":   balance,
        "txn_count": len(recent),
        "days":      days,
        "available": available,
        "source":    source,
    }


# ── Monthly review check ──────────────────────────────────────────────────────

def is_monthly_review_day():
    """Returns True on the 1st of each month."""
    return datetime.datetime.now(TIMEZONE).day == 1


# ── Main summary function ─────────────────────────────────────────────────────

def analyse_income(transactions, days=30):
    """
    Analyses income (credits) from the everyday account over the last N days.
    Returns dict with income by source and total.
    """
    import datetime as _dt
    import pytz as _pytz
    tz      = _pytz.timezone(config.TIMEZONE)
    today   = _dt.datetime.now(tz).date()
    cutoff  = today - _dt.timedelta(days=days)

    # Credits only, excluding internal transfers and friend bill-splits
    income_txns = [
        t for t in transactions
        if t["date"] >= cutoff
        and t["credit"] > 0
        and "internet withdrawal" not in t["description"].lower()
        and "osko withdrawal" not in t["description"].lower()
        and "from 0000" not in t["description"].lower()   # own savings transfers
        and "internet deposit" not in t["description"].lower()  # own account transfers
        and "sct deposit" not in t["description"].lower()       # own account transfers
        and not is_friend_split(t["description"])
    ]

    # Categorise each credit
    by_source = {}
    for t in income_txns:
        cat = categorise_income(t["description"])
        if cat not in by_source:
            by_source[cat] = {"total": 0, "transactions": []}
        by_source[cat]["total"] += t["credit"]
        by_source[cat]["transactions"].append({
            "date":        t["date"].strftime("%d %b"),
            "description": t["description"][:35],
            "amount":      round(t["credit"], 2),
        })

    total = sum(v["total"] for v in by_source.values())

    return {
        "by_source":   by_source,
        "total":       round(total, 2),
        "days":        days,
        "txn_count":   len(income_txns),
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
    excluded = spending.get("excluded_transfers") or {}
    if excluded.get("count"):
        lines.append(
            f"  {'Pass-through':<18} ${excluded['total']:.2f}"
            f"  ({excluded['count']} excluded)"
        )

    # Flag big transactions
    if spending["big_transactions"]:
        lines.append("  Flagged transactions (>$50):")
        for t in spending["big_transactions"][:4]:  # max 4
            lines.append(f"    • {t['date'].strftime('%d %b')}  ${t['debit']:.2f}  {t['description'][:40]}")

    lines.append("")

    # ── Live-logged spend (iPhone Back Tap) ───────────────────────────────────
    if LIVE_SPEND_AVAILABLE:
        try:
            live = get_live_summary(days=7, transactions=everyday_txns)
        except Exception:
            live = {"available": False}
        if live["available"]:
            lines.append("LIVE-LOGGED (Back Tap, last 7 days):")
            lines.append(f"  {'Total':<18} ${live['total']:.2f}  ({live['count']} entries)")
            for cat, amt in sorted(live["by_category"].items(), key=lambda x: -x[1]):
                lines.append(f"  {cat:<18} ${amt:.2f}")
            if live["matched"] is not None:
                lines.append(f"  Reconciled: {live['matched']}/{live['count']} matched in bank CSV")
                for e in live["unmatched"][:3]:
                    note = f" — {e['note']}" if e["note"] else ""
                    lines.append(f"    ⚠ not in CSV yet: ${e['amount']:.2f} {e['category']}{note}")
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

    Drops the same own-account transfers analyse_spending leaves out
    (internet withdrawals, transfers, Osko/Sct withdrawals to an owner name,
    Revolut card top-ups). A refund credit that matches a counted debit
    reduces that category and total_spend, so the category totals still add
    up to total_spend. Person-to-person transfers on this account are netted:
    only a positive remainder is spending. transfers_net has in, out,
    carry_in, net, and counted for that account. excluded_transfers lists
    the withdrawals that were not counted at their own amount.
    Descriptions on flagged rows are the full bank line.
    Callers that send this to an external agent must redact sensitive text
    before shortening it, so a number cut at a character limit cannot leave
    a short digit fragment. extra_inflows defaults to external Revolut
    transfers read from REVOLUT_CSV. Those are reported on
    transfers_net.reselling and are not added to total_spend.
    """
    recent, excluded, transfers_net = _net_spending_debits(
        transactions, start, end, extra_inflows,
    )

    by_category = {}
    for t in recent:
        cat = t.get("category") or "Other"
        by_category[cat] = by_category.get(cat, 0) + t["debit"]
    by_category = {cat: round(amount, 2) for cat, amount in by_category.items()}

    total = round(sum(by_category.values()), 2)
    span_days = (end - start).days + 1
    weekly_budget = round(float(weekly_budget), 2)
    window_budget = round(weekly_budget * span_days / 7.0, 2)

    flagged = []
    for t in recent:
        if t["debit"] < 80:
            continue
        flagged.append({
            "date": t["date"].isoformat(),
            "amount": round(t["debit"], 2),
            "category": t.get("category") or "Other",
            "description": t.get("description") or "",
        })
    flagged.sort(key=lambda row: row["amount"], reverse=True)

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
        "excluded_transfers": excluded,
        "transfers_net": transfers_net,
    }


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\n💰  Finance summary\n")
    print(get_finance_summary())
    print()
