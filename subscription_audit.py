"""
Jarvis — Subscription Audit
=============================
Scans your St. George CSV for recurring charges.
Identifies anything that appears monthly and flags it.

RUN:
  python subscription_audit.py

UPDATE YOUR CSV FIRST:
  Export last 90 days from St. George and save to finance/everyday.csv
"""

import datetime
import re
from pathlib import Path
from collections import defaultdict

import pytz
import config
from finance_tracker import parse_stgeorge_csv, EVERYDAY_CSV

TIMEZONE = pytz.timezone(config.TIMEZONE)


# Known subscriptions, matched as whole words on card, BPAY and direct debit
# lines. A transfer to a person is never a subscription.
SUBSCRIPTION_NAMES = {
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


def _subscription_for(description):
    """(id, display name) for a known subscription on this line, or None."""
    from finance_tracker import _normalise_desc

    words = set(re.sub(r"[^a-z0-9]+", " ", _normalise_desc(description)).split())
    for key, name in SUBSCRIPTION_NAMES.items():
        if key in words:
            return key, name
    return None


def find_recurring(transactions, months=3):
    """
    Debits to known subscriptions (SUBSCRIPTION_NAMES).

    Repeating text is not a subscription. Transfers, own-account moves and
    pending holds are skipped, so a person paid every month stays out.
    """
    from finance_tracker import rail_of

    today = datetime.datetime.now(TIMEZONE).date()
    cutoff = today - datetime.timedelta(days=months * 31)
    by_id = defaultdict(list)
    for txn in transactions:
        if txn["date"] < cutoff or txn.get("debit", 0) <= 0 or txn.get("pending"):
            continue
        description = txn.get("description") or ""
        if rail_of(description) not in ("card", "bpay", "direct_debit"):
            continue
        match = _subscription_for(description)
        if match is None:
            continue
        by_id[match[0]].append((txn, {"id": match[0], "name": match[1]}))

    recurring = []
    for _id, pairs in by_id.items():
        txns = [txn for txn, _match in pairs]
        match = pairs[0][1]
        months_seen = sorted({txn["date"].strftime("%Y-%m") for txn in txns})
        avg_amount = sum(txn["debit"] for txn in txns) / len(txns)
        recurring.append({
            "merchant": match["name"].lower(),
            "label": match["name"],
            "count": len(txns),
            "months": months_seen,
            "avg_amount": round(avg_amount, 2),
            "total": round(sum(txn["debit"] for txn in txns), 2),
            "last_date": max(txn["date"] for txn in txns),
            "sample": txns[-1]["description"],
        })
    return sorted(recurring, key=lambda item: -item["avg_amount"])


def summarise_subscriptions(months=3):
    """
    Known vs unrecognised recurring charges over the last `months`.

    Reads finance_tracker.EVERYDAY_CSV at call time. Returns available=False
    when that file is missing. Dates are ISO strings. `sample` is the full
    bank description. The agent API redacts sensitive text in it, then
    shortens it.
    """
    from finance_tracker import EVERYDAY_CSV, parse_stgeorge_csv

    if not EVERYDAY_CSV.exists():
        return {
            "available": False,
            "months": months,
            "known": [],
            "review": [],
            "monthly_known": 0.0,
            "monthly_review": 0.0,
            "monthly_total": 0.0,
        }

    recurring = find_recurring(parse_stgeorge_csv(EVERYDAY_CSV), months=months)
    known = []
    review = []
    for row in recurring:
        item = {
            "merchant": row["merchant"],
            "avg_amount": row["avg_amount"],
            "count": row["count"],
            "months": row["months"],
            "last_date": row["last_date"].isoformat(),
            "sample": row["sample"],
        }
        item["label"] = row.get("label") or row["merchant"]
        known.append(item)

    monthly_known = round(sum(r["avg_amount"] for r in known), 2)
    monthly_review = round(sum(r["avg_amount"] for r in review), 2)
    return {
        "available": True,
        "months": months,
        "known": known,
        "review": review,
        "monthly_known": monthly_known,
        "monthly_review": monthly_review,
        "monthly_total": round(monthly_known + monthly_review, 2),
    }


def run_audit():
    if not EVERYDAY_CSV.exists():
        print("❌  No everyday.csv found. Export 90 days from St. George first.")
        return

    print("\n🔍  Subscription Audit — scanning last 90 days\n")
    print("    Export 90 days of transactions for best results.")
    print("    ─────────────────────────────────────────────\n")

    txns      = parse_stgeorge_csv(EVERYDAY_CSV)
    recurring = find_recurring(txns, months=3)

    if not recurring:
        print("✅  No recurring charges detected.\n")
        return

    known = list(recurring)
    unknown = []

    # Monthly cost estimate
    monthly_known   = sum(r["avg_amount"] for r in known)
    monthly_unknown = sum(r["avg_amount"] for r in unknown)
    monthly_total   = monthly_known + monthly_unknown

    print(f"📋  KNOWN SUBSCRIPTIONS ({len(known)}):\n")
    for r in known:
        label = r.get("label") or r["merchant"]
        print(f"  ✓ {label:<25} ~${r['avg_amount']:.2f}/month")
        print(f"    Seen {r['count']}x across {len(r['months'])} months | Last: {r['last_date']}")

    if unknown:
        print(f"\n❓  UNRECOGNISED RECURRING CHARGES ({len(unknown)}) — review these:\n")
        for r in unknown:
            print(f"  ? {r['merchant']:<25} ~${r['avg_amount']:.2f}/month")
            print(f"    Seen {r['count']}x | Example: {r['sample'][:45]}")
            print(f"    Last charged: {r['last_date']}")
            print()

    print(f"─────────────────────────────────────────────")
    print(f"  Known subs:        ~${monthly_known:.2f}/month")
    if unknown:
        print(f"  Unrecognised:      ~${monthly_unknown:.2f}/month  ← investigate")
    print(f"  Total recurring:   ~${monthly_total:.2f}/month")
    annual = monthly_total * 12
    print(f"  Annual cost:       ~${annual:.2f}/year\n")

    if unknown:
        print("💡  Check your unrecognised charges — these might be:")
        print("    • Free trials that converted to paid")
        print("    • Forgotten subscriptions")
        print("    • Legitimate recurring bills (gym, insurance, etc.)\n")


if __name__ == "__main__":
    run_audit()
