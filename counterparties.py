"""
Who a bank line was with, and whether that line is personal spending.

Roles are self, merchant, and person. There is no partner role. Nilesh is
a person: money Manav pays him is spending, and money Nilesh pays him
reduces earlier spending with Nilesh. Stock he sells for Nilesh is not a
role. It is a label on the individual lines (purpose "resale"). Those
labelled lines stay out of personal spending. They are not the commission.
The commission is the reselling sheet. Card spend and small payments to
people are left alone.

Unknown Osko, Sct, PayID, and NPP lines are listed as unreviewed and do
not enter the total. A card, BPAY, or direct debit to a shop Vanam has
not seen yet still counts as spending. A transfer of $500 or more stays
unreviewed until Manav labels that line, so a forward of sale proceeds
cannot quietly become spending or wipe a costume.

The remembered names live in data/counterparties.json (not in git). Shops
from the category list and his own name are built in, so the file only
needs people he adds and lines he labels. The file is read on every
summary.
"""

import datetime
import json
import re
import threading
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
STORE_PATH = SCRIPT_DIR / "data" / "counterparties.json"

# A transfer at least this large is not silently spending and not silently
# income. It waits on the unreviewed list until Manav labels the line.
LARGE_TRANSFER = 500.0

ROLES = ("self", "merchant", "person")
PURPOSES = ("resale", "spending", "income")

# Category keywords that are too broad to be a counterparty. "ko" inside
# "osko" and "shop" inside a sentence must not become the payee.
_SKIP_ALIASES = {
    "eat", "food", "dining", "restaurant", "cafe", "coffee", "pizza", "burger",
    "grill", "noodle", "sushi", "thai", "indian", "chinese", "italian",
    "ticket", "tickets", "bar", "pub", "club", "show", "ko", "sports",
    "shopping", "retail", "store", "shop", "sport", "bus", "train", "fuel",
    "drinks", "monthly", "annual", "subscription", "fitness", "yoga",
    "leisure", "entertainment", "parking", "taxi", "pharmacy", "university",
    "tuition", "textbook", "petrol",
}

_SHORT_OK = {"kfc", "aldi", "iga", "ebay", "xbox", "uber", "myer", "stan", "opal"}

# Payees whose credits are wages, not a shop refund. Seeded as merchants so
# a salary deposit is income (nothing to reimburse) rather than unreviewed.
_INCOME_PAYERS = (
    {"id": "propwealth", "name": "PropWealth", "aliases": ["propwealth", "prop wealth"]},
    {"id": "axis-education", "name": "Axis Education", "aliases": ["axis ed", "axis education", "tutoring"]},
)

_SUBSCRIPTION_NAMES = {
    "spotify": "Spotify",
    "netflix": "Netflix",
    "disney": "Disney+",
    "stan": "Stan",
    "icloud": "iCloud",
    "youtube": "YouTube Premium",
    "playstation": "PlayStation",
    "anthropic": "Claude",
    "openai": "ChatGPT",
}

_PLACES = {
    "sydney", "melbourne", "australia", "nsw", "london", "online",
    "brisbane", "perth", "adelaide", "hobart", "canberra",
}

_RAIL_PHRASES = (
    "osko withdrawal", "osko deposit", "sct withdrawal", "sct deposit",
    "visa purchase", "visa refund", "visa credit", "visa debit",
    "eftpos debit", "eftpos credit", "eftpos purchase",
    "internet withdrawal", "internet deposit",
    "direct debit", "direct credit", "bpay",
)

_NOISE = {
    "osko", "sct", "payid", "npp", "visa", "eftpos", "withdrawal", "deposit",
    "purchase", "debit", "credit", "internet", "transfer", "sent", "from",
    "the", "to", "for", "and", "payment", "direct", "pending", "refund",
    "reversal", "chargeback", "pty", "ltd",
}


def _normalise(description):
    """Lowercase, with the bank's padded spaces collapsed."""
    return re.sub(r"\s+", " ", (description or "").lower()).strip()


def _cents(amount):
    """Integer cents, so 14.95 and a matching line compare equal."""
    return int(round(float(amount) * 100))


def _dollars(cents):
    """Cents as a dollar amount rounded to the cent."""
    return round(int(cents) / 100.0, 2)


def _slug(name):
    """Stable id from a display name."""
    slug = re.sub(r"[^a-z0-9]+", "-", _normalise(name)).strip("-")
    return slug or "counterparty"


def _event_dt(txn):
    """When the line happened, using a clock glued to the description date.

    Rows with no clock sit at 12:00. A 09:11 repayment then falls before a
    10:30 repurchase on the same morning.
    """
    desc = _normalise(txn.get("description") or "")
    match = re.search(r"\d{2}[a-z]{3}(\d{2}):(\d{2})", desc)
    if not match:
        match = re.search(r"\b(\d{2}):(\d{2})\b", desc)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return datetime.datetime.combine(txn["date"], datetime.time(hour, minute))
    return datetime.datetime.combine(txn["date"], datetime.time(12, 0))


def rail_of(description, explicit=None):
    """Coarse rail: self, card, card_credit, bpay, direct_debit, direct_credit, transfer, other.

    explicit is the Revolut Type column when the line came from that CSV.
    """
    kind = _normalise(explicit or "").replace("_", " ")
    if kind in ("topup", "top up"):
        return "self"
    if kind in ("card refund",):
        return "card_credit"
    if kind in ("card payment", "card"):
        return "card"
    if kind == "transfer":
        return "transfer"
    desc = _normalise(description)
    if "internet withdrawal" in desc or "internet deposit" in desc:
        return "self"
    if re.search(r"\btransfer (to|from) savings\b", desc):
        return "self"
    if "visa" in desc or "eftpos" in desc:
        if any(word in desc for word in ("refund", "reversal", "chargeback", "visa credit", "eftpos credit")):
            return "card_credit"
        return "card"
    if "bpay" in desc:
        return "bpay"
    if "direct debit" in desc:
        return "direct_debit"
    if "direct credit" in desc or re.search(r"\b(payroll|salary|wages)\b", desc):
        return "direct_credit"
    if any(token in desc for token in ("osko", "sct")) or re.search(r"\bpayid\b", desc) or re.search(r"\bnpp\b", desc):
        return "transfer"
    return "other"


def payee_phrase(description):
    """The words left after the rail, the date, and filler words.

    This is what we match against a counterparty's aliases. It is not a
    guess at someone's surname.
    """
    desc = _normalise(description)
    for phrase in _RAIL_PHRASES:
        desc = desc.replace(phrase, " ")
    desc = re.sub(r"\d{2}[a-z]{3}(?:\d{2}:\d{2})?", " ", desc)
    desc = re.sub(r"\b\d{2}:\d{2}\b", " ", desc)
    desc = re.sub(r"[^a-z]+", " ", desc)
    tokens = [
        token for token in desc.split()
        if token not in _NOISE and token not in _PLACES and not token.isdigit()
    ]
    return " ".join(tokens)


def _alias_in(alias, phrase):
    """True when alias is a whole word or phrase inside phrase."""
    alias = _normalise(alias).strip()
    phrase = _normalise(phrase)
    if not alias or not phrase:
        return False
    return re.search(rf"(?:^| ){re.escape(alias)}(?: |$)", phrase) is not None


def _own_rules():
    """Owner name and Revolut card mask from term context, with a safe fallback."""
    try:
        from term_context import get_own_account_rules
        return get_own_account_rules()
    except Exception:
        return {
            "owner_names": ["manav jain"],
            "withdrawal_markers": ["osko withdrawal", "internet withdrawal", "sct withdrawal"],
            "card_patterns": ["revolut**5228", "revolut"],
            "card_rails": ["visa purchase", "eftpos", "top-up", "topup"],
        }


def _refund_window_days():
    """Days a credit can still reduce an earlier debit with the same counterparty."""
    try:
        from term_context import get_refund_window_days
        return get_refund_window_days()
    except Exception:
        return 14


def is_self(description, explicit_rail=None):
    """True for a move between Manav's own accounts.

    Internet withdrawal and internet deposit are St George's own-account
    rails. 'transfer to savings' and 'transfer from savings' are the same
    kind of line. The bare word 'transfer' is not enough: an Osko whose
    reference happens to say transfer is still someone else's payment.
    His name on a withdrawal or deposit, and a Revolut card top-up, are
    self. A bare 'revolut' on an Osko is not.
    """
    if rail_of(description, explicit_rail) == "self" and explicit_rail in ("topup", "TOPUP"):
        return True
    desc = _normalise(description)
    if "internet withdrawal" in desc or "internet deposit" in desc:
        return True
    if "from 0000" in desc or "to 0000" in desc:
        return True
    if re.search(r"\btransfer (to|from) savings\b", desc):
        return True
    if "sent from revolut" in desc:
        return True
    rules = _own_rules()
    names = [str(name).strip().lower() for name in (rules.get("owner_names") or []) if str(name).strip()]
    markers = rules.get("withdrawal_markers") or []
    named = any(name in desc for name in names)
    if named and any(marker in desc for marker in markers):
        return True
    if named and "deposit" in desc:
        return True
    patterns = [pat for pat in (rules.get("card_patterns") or []) if pat and pat in desc]
    if not patterns:
        return False
    if any("**" in pat for pat in patterns):
        return True
    rails = rules.get("card_rails") or []
    return any(rail in desc for rail in rails)


def _keyword_alias(keyword):
    """A category keyword worth remembering as a merchant, or None."""
    alias = _normalise(keyword).strip()
    if not alias or alias in _SKIP_ALIASES:
        return None
    if len(alias) < 4 and alias not in _SHORT_OK:
        return None
    return alias


def _blank_counterparty(id_, name, role, aliases, category=None, subscription=False):
    """One stored counterparty."""
    clean = []
    for alias in aliases:
        text = _normalise(alias).strip()
        if text and text not in clean:
            clean.append(text)
    return {
        "id": id_,
        "name": name,
        "role": role,
        "aliases": clean,
        "category": category,
        "subscription": bool(subscription),
        "large_transfers": "ask",
    }


def default_counterparties():
    """Shops from the category keywords, wage payers, and subscription flags.

    His own name is not in this list. is_self() handles that from
    own_accounts, so a different owner name in term context still works.
    Generic words (shop, ticket, ko) are left out so they cannot become
    the payee.
    """
    import finance_tracker

    found = {}
    for category, keywords in finance_tracker.CATEGORY_RULES:
        for keyword in keywords:
            alias = _keyword_alias(keyword)
            if not alias:
                continue
            id_ = _slug(alias)
            current = found.get(id_)
            if current is None:
                found[id_] = _blank_counterparty(id_, alias.title(), "merchant", [alias], category)
            elif alias not in current["aliases"]:
                current["aliases"].append(alias)
    for payer in _INCOME_PAYERS:
        found[payer["id"]] = _blank_counterparty(
            payer["id"], payer["name"], "merchant", payer["aliases"],
        )
    for alias, name in _SUBSCRIPTION_NAMES.items():
        id_ = _slug(alias)
        current = found.get(id_)
        if current is None:
            found[id_] = _blank_counterparty(id_, name, "merchant", [alias], "Subscriptions", True)
        else:
            current["subscription"] = True
            if alias not in current["aliases"]:
                current["aliases"].append(alias)
    return list(found.values())


def _empty_user():
    """The on-disk layer: people he added and lines he labelled. Not the shops."""
    return {"counterparties": [], "lines": []}


def _read_user(path):
    """User layer from disk. A missing or broken file is an empty layer."""
    path = Path(path)
    if not path.exists():
        return _empty_user()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _empty_user()
    if not isinstance(raw, dict):
        return _empty_user()
    people = raw.get("counterparties") if isinstance(raw.get("counterparties"), list) else []
    lines = raw.get("lines") if isinstance(raw.get("lines"), list) else []
    return {"counterparties": people, "lines": lines}


def _normalise_user_counterparty(raw):
    """One file record, or None when the role or the name is unusable."""
    if not isinstance(raw, dict):
        return None
    role = str(raw.get("role") or "").strip().lower()
    if role not in ROLES or role == "self":
        return None
    name = str(raw.get("name") or "").strip()
    if not name:
        return None
    aliases = raw.get("aliases") if isinstance(raw.get("aliases"), list) else [name]
    record = _blank_counterparty(
        str(raw.get("id") or _slug(name)),
        name,
        role,
        aliases or [name],
        raw.get("category") or None,
        bool(raw.get("subscription")),
    )
    large = str(raw.get("large_transfers") or "ask").strip().lower()
    record["large_transfers"] = "spending" if large == "spending" else "ask"
    if record["role"] != "person":
        record["subscription"] = bool(raw.get("subscription"))
    else:
        record["subscription"] = False
    return record


def _normalise_line(raw):
    """One labelled line, or None when it is incomplete."""
    if not isinstance(raw, dict):
        return None
    purpose = str(raw.get("purpose") or "").strip().lower().replace("_", " ")
    if purpose in ("pass-through", "passthrough", "pass through"):
        purpose = "resale"
    if purpose not in PURPOSES:
        return None
    try:
        day = datetime.date.fromisoformat(str(raw.get("date") or "")[:10])
        amount = _cents(raw.get("amount"))
    except (TypeError, ValueError):
        return None
    if amount <= 0:
        return None
    direction = str(raw.get("direction") or "").strip().lower()
    if direction not in ("in", "out", ""):
        return None
    return {
        "date": day.isoformat(),
        "amount": _dollars(amount),
        "direction": direction,
        "description": _normalise(raw.get("description") or ""),
        "purpose": purpose,
    }


def load_store(path=None):
    """Defaults plus the user file. The file wins when an id overlaps.

    Shops stay in memory. People and line labels come from the file.
    """
    path = Path(path) if path is not None else STORE_PATH
    user = _read_user(path)
    by_id = {row["id"]: row for row in default_counterparties()}
    for raw in user["counterparties"]:
        record = _normalise_user_counterparty(raw)
        if record is None:
            continue
        current = by_id.get(record["id"])
        if current is None:
            by_id[record["id"]] = record
            continue
        merged = dict(current)
        merged["name"] = record["name"]
        merged["role"] = record["role"]
        merged["subscription"] = record["subscription"] if record["role"] != "person" else False
        merged["large_transfers"] = record["large_transfers"]
        if record.get("category"):
            merged["category"] = record["category"]
        for alias in record["aliases"]:
            if alias not in merged["aliases"]:
                merged["aliases"].append(alias)
        by_id[record["id"]] = merged
    lines = []
    for raw in user["lines"]:
        line = _normalise_line(raw)
        if line is not None:
            lines.append(line)
    return {"counterparties": list(by_id.values()), "lines": lines}


_PATH_LOCKS = {}
_PATH_LOCKS_GUARD = threading.Lock()

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _path_lock(path):
    """In-process lock for one file. flock alone does not separate threads."""
    key = str(Path(path))
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _PATH_LOCKS[key] = lock
        return lock


def _write_user(user, path):
    """Write the user layer. Defaults are not copied into the file."""
    from json_store import atomic_write_json
    atomic_write_json(Path(path), user)


def _update_user(path, mutate):
    """Read the user file, change it, and write it back as one locked step.

    Telegram and the API both write this file. A label that read the file
    before a /who, then wrote afterwards, used to drop the person. The
    resale lines survived because they were in the stale copy.
    """
    from json_store import file_lock
    path = Path(path)
    with _path_lock(path):
        with file_lock(path):
            user = _read_user(path)
            mutate(user)
            _save_user(user, path)


def _save_user(user, path):
    """Persist counterparties and lines, dropping anything that will not load."""
    people = []
    for raw in user.get("counterparties") or []:
        record = _normalise_user_counterparty(raw)
        if record is not None:
            people.append(record)
    lines = []
    for raw in user.get("lines") or []:
        line = _normalise_line(raw)
        if line is not None:
            lines.append(line)
    _write_user({"counterparties": people, "lines": lines}, path)


def match_counterparty(description, store=None, explicit_rail=None):
    """The counterparty for this description, or None when nobody is remembered.

    Self is returned as a small dict with role 'self'. The longest alias
    wins, so 'r jaiswal' beats the shop word 'knockout' on the same line
    once both spellings have been saved on the person.
    """
    if is_self(description, explicit_rail):
        return {"id": "self", "name": "Self", "role": "self", "aliases": [], "category": None,
                "subscription": False, "large_transfers": "ask"}
    store = store or load_store()
    phrase = payee_phrase(description)
    best = None
    best_len = 0
    for record in store["counterparties"]:
        for alias in record.get("aliases") or []:
            if _alias_in(alias, phrase) and len(alias) > best_len:
                best = record
                best_len = len(alias)
    return best


def suggest_category(description):
    """Category from the shop keywords, for display. None when nothing matches.

    This does not decide whether the line is spending. A ticket paid by
    Osko can be Entertainment and still be a person.
    """
    import finance_tracker

    desc = finance_tracker._normalise_desc(description)
    for category, keywords in finance_tracker.CATEGORY_RULES:
        for keyword in keywords:
            if finance_tracker._keyword_matches(desc, keyword):
                return category
    return None


def description_dates(description, year):
    """Dates printed in the bank text, such as 25Oct or 25Oct00:12.

    St George often posts a midnight line on the previous date. The text
    still says 25Oct. A label typed from that text has to hit the line.
    """
    found = set()
    for match in re.finditer(r"\b(\d{2})([a-z]{3,4})(?:\d{2}:\d{2})?\b", _normalise(description)):
        month = _MONTHS.get(match.group(2))
        if month is None:
            continue
        try:
            found.add(datetime.date(int(year), month, int(match.group(1))).isoformat())
        except ValueError:
            continue
    return found


def _parse_day(value, year=None):
    """A date, YYYY-MM-DD, or the 25Oct form printed on a bank line."""
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    text = str(value or "").strip()
    try:
        return datetime.date.fromisoformat(text[:10])
    except ValueError:
        pass
    match = re.fullmatch(r"(\d{1,2})([A-Za-z]{3,4})(\d{4})?", text)
    if match is None:
        raise ValueError("date must be YYYY-MM-DD, like 2026-10-25")
    if year is None:
        year = int(match.group(3) or _today().year)
    month = _MONTHS.get(match.group(2).lower())
    if month is None:
        raise ValueError("date must be YYYY-MM-DD, like 2026-10-25")
    try:
        return datetime.date(int(year), month, int(match.group(1)))
    except ValueError as exc:
        raise ValueError("date must be YYYY-MM-DD, like 2026-10-25") from exc


def _today():
    """Today in the configured timezone, for a 25Oct date with no year."""
    try:
        import pytz
        from config import TIMEZONE
        return datetime.datetime.now(pytz.timezone(TIMEZONE)).date()
    except Exception:
        return datetime.date.today()


def _parse_amount(amount):
    """Dollars, allowing a leading $ and a thousands comma."""
    text = str(amount).strip().replace("$", "").replace(",", "")
    return float(text)


def payee_name(description):
    """The last two words on the line. St George puts the payee there.

    Those are the words GET /finance redacts, so a label copied from the
    API often does not contain the name that is on the bank line.
    """
    words = payee_phrase(description).split()
    if len(words) >= 2:
        return " ".join(words[-2:])
    return " ".join(words)


def _line_purpose(day, direction, amount_cents, description, lines):
    """Stored purpose for this line, or None. The first match wins.

    The rule date may be the posted date or the date printed in the
    description. A fragment that is not in the bank text does not match,
    including the API's [redacted] marker.
    """
    desc = _normalise(description)
    printed = description_dates(description, day.year)
    for rule in lines:
        if _cents(rule["amount"]) != amount_cents:
            continue
        if rule.get("direction") and rule["direction"] != direction:
            continue
        fragment = rule.get("description") or ""
        if not fragment or fragment not in desc or "redacted" in fragment:
            continue
        if rule["date"] == day.isoformat() or rule["date"] in printed:
            return rule["purpose"]
    return None


def _sheet_amounts():
    """Sale prices and costs from the reselling sheet, in cents.

    Used only as a hint. A missing sheet, or a machine with no Google
    token, contributes nothing. Nothing is labelled automatically.
    """
    token = Path.home() / "jarvis" / "token.json"
    if not token.exists():
        return set()
    try:
        from reselling_tracker import load_items
        items = load_items()
    except Exception:
        return set()
    found = set()
    for item in items:
        for value in (getattr(item, "sale_price", None), getattr(item, "cost", None)):
            if value:
                found.add(_cents(value))
    return found


def _near_sheet(amount_cents, sheet_amounts):
    """True when the bank amount is within a dollar of a sheet sale or cost."""
    return any(abs(amount_cents - other) <= 100 for other in sheet_amounts)


def classify(transactions, start, end, store=None, sheet_amounts=None):
    """Split a window into spending, income, resale, and unreviewed.

    Spending lines have already had a later credit from the same
    counterparty taken off. A credit with nothing left to reduce becomes
    income when it is small. It never pushes spending below zero. Resale
    lines stay out of spending. Their bank in and out are not commission.
    Large transfers and unknown transfers stay on unreviewed until
    labelled, and a week with any of those is unfinished.
    """
    store = store or load_store()
    if sheet_amounts is None:
        sheet_amounts = _sheet_amounts()
    window_days = _refund_window_days()
    prepared = []
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
        direction = "out" if debit > 0 else "in"
        amount = debit if direction == "out" else credit
        description = txn.get("description") or ""
        explicit = txn.get("rail")
        rail = rail_of(description, explicit)
        if is_self(description, explicit) or rail == "self":
            continue
        counterparty = match_counterparty(description, store, explicit)
        if counterparty and counterparty["role"] == "self":
            continue
        purpose = _line_purpose(day, direction, _cents(amount), description, store["lines"])
        category = suggest_category(description)
        if category is None and counterparty and counterparty.get("category"):
            category = counterparty["category"]
        prepared.append({
            "date": day,
            "description": description,
            "direction": direction,
            "amount": round(amount, 2),
            "remaining": round(amount, 2),
            "rail": rail,
            "counterparty": counterparty,
            "purpose": purpose,
            "category": category or "Other",
            "suggestion": None,
            "bucket": None,
        })

    candidates = [
        row for row in prepared
        if row["purpose"] is None
        and row["rail"] == "transfer"
        and row["amount"] >= LARGE_TRANSFER
    ]
    both_ways = any(row["direction"] == "in" for row in candidates) and any(
        row["direction"] == "out" for row in candidates
    )

    for row in prepared:
        purpose = row["purpose"]
        counterparty = row["counterparty"]
        role = counterparty["role"] if counterparty else "unknown"
        if purpose == "resale":
            row["bucket"] = "resale"
            continue
        if purpose == "spending" and row["direction"] == "out":
            row["bucket"] = "spend"
            continue
        if purpose == "spending" and row["direction"] == "in":
            # He marked the deposit as reviewed. Leaving it unreviewed made
            # the label look like it had not been saved.
            row["bucket"] = "credit"
            continue
        if purpose == "income" and row["direction"] == "in":
            row["bucket"] = "income"
            continue
        if row["rail"] in ("card", "bpay", "direct_debit", "other") and row["direction"] == "out":
            row["bucket"] = "spend"
            continue
        if row["rail"] == "card_credit" or (row["rail"] == "card" and row["direction"] == "in"):
            row["bucket"] = "credit"
            continue
        if row["rail"] in ("transfer", "direct_credit"):
            large_person = (
                role == "person"
                and row["amount"] >= LARGE_TRANSFER
                and counterparty.get("large_transfers") != "spending"
            )
            unknown = role == "unknown"
            if unknown or large_person:
                row["bucket"] = "unreviewed"
                if row["amount"] >= LARGE_TRANSFER and (
                    both_ways or _near_sheet(_cents(row["amount"]), sheet_amounts)
                ):
                    row["suggestion"] = "resale"
                continue
            if row["direction"] == "out":
                row["bucket"] = "spend"
            else:
                row["bucket"] = "credit"
            continue
        if row["direction"] == "out":
            row["bucket"] = "spend"
        else:
            row["bucket"] = "unreviewed"

    _apply_reimbursements(prepared, window_days)

    spending = []
    income = []
    resale_in = 0
    resale_out = 0
    resale_count = 0
    unreviewed = []
    for row in prepared:
        if row["bucket"] == "resale":
            resale_count += 1
            if row["direction"] == "in":
                resale_in += row["amount"]
            else:
                resale_out += row["amount"]
            continue
        if row["bucket"] == "unreviewed":
            unreviewed.append(_unreviewed_item(row))
            continue
        if row["bucket"] == "spend" and row["remaining"] > 0:
            spending.append({
                "date": row["date"],
                "description": row["description"],
                "debit": round(row["remaining"], 2),
                "credit": 0.0,
                "category": row["category"],
                "counterparty": row["counterparty"]["name"] if row["counterparty"] else None,
            })
            continue
        if row["bucket"] in ("credit", "income"):
            left = row.get("income_left", row["amount"] if row["bucket"] == "income" else 0.0)
            if left > 0:
                name = row["counterparty"]["name"] if row["counterparty"] else "Other"
                income.append({
                    "date": row["date"],
                    "description": row["description"],
                    "amount": round(left, 2),
                    "source": name,
                })

    resale_in = round(resale_in, 2)
    resale_out = round(resale_out, 2)
    net = round(resale_in - resale_out, 2)
    unreviewed.sort(key=lambda item: (item["date"], -item["amount"]))
    return {
        "spending": spending,
        "income": income,
        "resale": {
            "in": resale_in,
            "out": resale_out,
            "net": net,
            "count": resale_count,
            # Filled from the sheet. The bank residual is not the commission.
            "income": None,
            "commission": None,
            "note": None,
            "difference": None,
        },
        "unreviewed": unreviewed,
        "unfinished": bool(unreviewed),
    }


def _unreviewed_item(row):
    """One line the week is waiting on."""
    counterparty = row["counterparty"]
    return {
        "date": row["date"].isoformat(),
        "amount": row["amount"],
        "direction": row["direction"],
        "description": row["description"],
        "counterparty": counterparty["name"] if counterparty else None,
        "role": counterparty["role"] if counterparty else "unknown",
        "suggestion": row["suggestion"],
        "payee": payee_name(row["description"]),
    }


def _apply_reimbursements(rows, window_days):
    """A credit from someone reduces what Manav already paid that same someone.

    Amounts do not have to match. The latest earlier payment is reduced
    first. Anything the credit does not use is income_left. A credit cannot
    reduce a payment that happened after it, or one outside the window.
    """
    credits = [row for row in rows if row["bucket"] == "credit"]
    credits.sort(key=lambda row: (_event_dt(row), row["amount"]))
    spends = [row for row in rows if row["bucket"] == "spend"]
    for credit in credits:
        identity = credit["counterparty"]["id"] if credit["counterparty"] else None
        left = credit["amount"]
        if identity is not None:
            candidates = [
                spend for spend in spends
                if spend["counterparty"] and spend["counterparty"]["id"] == identity
                and spend["remaining"] > 0
                and _event_dt(credit) >= _event_dt(spend)
                and (_event_dt(credit).date() - _event_dt(spend).date()).days <= window_days
            ]
            candidates.sort(key=_event_dt, reverse=True)
            for spend in candidates:
                if left <= 0:
                    break
                take = min(left, spend["remaining"])
                spend["remaining"] = round(spend["remaining"] - take, 2)
                left = round(left - take, 2)
        credit["income_left"] = round(left, 2)


def public_counterparties(store=None):
    """Roles for GET /finance. Aliases stay out of the payload."""
    store = store or load_store()
    rows = []
    for record in sorted(store["counterparties"], key=lambda item: (item["role"], item["name"].lower())):
        rows.append({
            "id": record["id"],
            "name": record["name"],
            "role": record["role"],
            "subscription": bool(record.get("subscription")),
            "large_transfers": record.get("large_transfers") or "ask",
        })
    return rows


def upsert_counterparty(name, role, aliases=None, category=None, subscription=False,
                        large_transfers="ask", path=None):
    """Remember a person or a shop. A person is never stored as a subscription.

    aliases defaults to the name. large_transfers 'spending' means a large
    Osko to this person counts as spending. The default 'ask' leaves a
    large transfer on the unreviewed list.
    """
    path = Path(path) if path is not None else STORE_PATH
    role = str(role or "").strip().lower()
    if role not in ("merchant", "person"):
        raise ValueError("role must be merchant or person")
    name = str(name or "").strip()
    if not name:
        raise ValueError("name is required")
    alias_list = [name]
    for alias in aliases or []:
        text = str(alias or "").strip()
        if text and text not in alias_list:
            alias_list.append(text)
    record = _blank_counterparty(
        _slug(name), name, role, alias_list, category, subscription and role != "person",
    )
    record["large_transfers"] = "spending" if str(large_transfers).lower() == "spending" else "ask"

    def mutate(user):
        people = []
        replaced = False
        for raw in user["counterparties"]:
            current = _normalise_user_counterparty(raw)
            if current and current["id"] == record["id"]:
                for alias in current["aliases"]:
                    if alias not in record["aliases"]:
                        record["aliases"].append(alias)
                people.append(record)
                replaced = True
            elif current:
                people.append(current)
        if not replaced:
            people.append(record)
        user["counterparties"] = people

    _update_user(path, mutate)
    return record


def label_line(date, amount, description, purpose, direction="", path=None):
    """Label one bank line. purpose is resale, spending, or income.

    date is a date or YYYY-MM-DD. description is a fragment of the bank
    text. A later call for the same date, amount, direction, and fragment
    replaces the purpose.
    """
    path = Path(path) if path is not None else STORE_PATH
    day = _parse_day(date)
    purpose = str(purpose or "").strip().lower().replace("_", " ")
    if purpose in ("pass-through", "passthrough", "pass through"):
        purpose = "resale"
    if purpose not in PURPOSES:
        raise ValueError("purpose must be resale, spending, or income")
    cents = _cents(_parse_amount(amount))
    if cents <= 0:
        raise ValueError("amount must be greater than zero")
    direction = str(direction or "").strip().lower()
    if direction not in ("", "in", "out"):
        raise ValueError("direction must be in or out")
    fragment = _normalise(description)
    if not fragment:
        raise ValueError("description fragment is required")
    rule = {
        "date": day.isoformat(),
        "amount": _dollars(cents),
        "direction": direction,
        "description": fragment,
        "purpose": purpose,
    }

    def mutate(user):
        kept = []
        for raw in user["lines"]:
            current = _normalise_line(raw)
            if current is None:
                continue
            same = (
                current["date"] == rule["date"]
                and _cents(current["amount"]) == cents
                and current["description"] == fragment
                and current["direction"] == direction
            )
            if not same:
                kept.append(current)
        kept.append(rule)
        user["lines"] = kept

    _update_user(path, mutate)
    return rule


def _txn_side(txn):
    """Direction and amount for one parsed line, or (None, 0) when it is empty."""
    debit = float(txn.get("debit") or 0)
    credit = float(txn.get("credit") or 0)
    if debit > 0:
        return "out", debit
    if credit > 0:
        return "in", credit
    return None, 0.0


def find_label_hits(transactions, date, amount, description, direction=""):
    """Bank lines this label would apply to.

    A hit is the same amount, the fragment is in the bank text, and the
    date is either the posted date or the date printed in the description.
    """
    typed = _parse_day(date)
    cents = _cents(_parse_amount(amount))
    if cents <= 0:
        raise ValueError("amount must be greater than zero")
    fragment = _normalise(description)
    if not fragment:
        raise ValueError("description fragment is required")
    direction = str(direction or "").strip().lower()
    if direction not in ("", "in", "out"):
        raise ValueError("direction must be in or out")
    hits = []
    same_text = []
    for txn in transactions:
        if txn.get("pending"):
            continue
        day = txn.get("date")
        side, amt = _txn_side(txn)
        if day is None or side is None or _cents(amt) != cents:
            continue
        desc = txn.get("description") or ""
        if fragment not in _normalise(desc) or "redacted" in fragment:
            continue
        if direction and direction != side:
            continue
        row = {
            "date": day.isoformat(),
            "amount": round(amt, 2),
            "direction": side,
            "description": desc,
            "payee": payee_name(desc),
            "posted_note": "",
        }
        printed = description_dates(desc, day.year)
        if day == typed or typed.isoformat() in printed:
            if day != typed:
                row["posted_note"] = (
                    f" (the description says {typed.isoformat()}, "
                    f"the bank posted {day.isoformat()})"
                )
            hits.append(row)
        else:
            same_text.append(row)
    return hits, same_text


def label_matching(transactions, date, amount, description, purpose, direction="", path=None):
    """Label the lines this rule hits. Raise ValueError when it hits none.

    The stored date is the posted date, so a later read does not depend on
    the date that happened to be printed in the description. Purpose
    spending is refused for a deposit, and income is refused for a payment.
    """
    purpose_text = str(purpose or "").strip().lower().replace("_", " ")
    if purpose_text in ("pass-through", "passthrough", "pass through"):
        purpose_text = "resale"
    if purpose_text not in PURPOSES:
        raise ValueError("purpose must be resale, spending, or income")
    fragment = _normalise(description)
    if "redacted" in fragment:
        raise ValueError(
            "That fragment is the redacted payee, not the bank text. "
            "Use /unreviewed. It shows the name. Nothing was saved."
        )
    hits, same_text = find_label_hits(transactions, date, amount, description, direction)
    if not hits:
        typed = _parse_day(date)
        if same_text:
            posted = ", ".join(row["date"] for row in same_text)
            raise ValueError(
                f"No line on {typed.isoformat()} matches ${ _dollars(_cents(_parse_amount(amount))):.2f} "
                f"containing {fragment!r}. The same text is posted on {posted}. "
                "Use that posted date."
            )
        raise ValueError(
            f"No line matches ${ _dollars(_cents(_parse_amount(amount))):.2f} "
            f"containing {fragment!r}. Nothing was saved."
        )
    sides = {row["direction"] for row in hits}
    if purpose_text == "spending" and sides == {"in"}:
        raise ValueError(
            "That line is money coming in. Label it income, or resale if it is a buyer deposit. "
            "Nothing was saved."
        )
    if purpose_text == "income" and sides == {"out"}:
        raise ValueError(
            "That line is money going out. Label it spending, or resale if it is a forward. "
            "Nothing was saved."
        )
    saved = []
    seen = set()
    for row in hits:
        key = (row["date"], row["direction"], fragment, _cents(row["amount"]))
        if key in seen:
            continue
        seen.add(key)
        saved.append(label_line(
            row["date"], row["amount"], fragment, purpose_text, row["direction"], path,
        ))
    return {"saved": saved, "matches": hits}


def lines_for_counterparty(transactions, start, end, counterparty_id, path=None):
    """Transfers in the window whose payee is this saved counterparty."""
    store = load_store(path)
    hits = []
    for txn in transactions:
        if txn.get("pending"):
            continue
        day = txn.get("date")
        side, amount = _txn_side(txn)
        if day is None or side is None or day < start or day > end:
            continue
        if is_self(txn.get("description") or ""):
            continue
        if rail_of(txn.get("description") or "") not in ("transfer", "direct_credit"):
            continue
        match = match_counterparty(txn.get("description") or "", store)
        if not match or match.get("id") != counterparty_id:
            continue
        hits.append({
            "date": day.isoformat(),
            "amount": round(amount, 2),
            "direction": side,
            "description": txn.get("description") or "",
            "payee": payee_name(txn.get("description") or ""),
        })
    return hits


def accept_resale(transactions, start, end, path=None):
    """Label every suggested resale line in the window. One action.

    Suggestion is not a rule in code. This writes a purpose on each of
    those lines. Card spend and small payments are not in the suggestion.
    Returns how many lines were labelled.
    """
    path = Path(path) if path is not None else STORE_PATH
    result = classify(transactions, start, end, load_store(path))
    count = 0
    for item in result["unreviewed"]:
        if item.get("suggestion") != "resale":
            continue
        label_line(
            item["date"], item["amount"], item["description"], "resale",
            item["direction"], path,
        )
        count += 1
    return count
