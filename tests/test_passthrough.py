"""Weekly spending is card lines plus logged spends. Transfers never count.

The 20–26 Oct 2026 everyday-account week: $189.57 of Visa purchases, three
Osko forwards to Nilesh ($4,932, $4,061, $2,000), the buyer deposits that
came in before them, and three small Oskos to people. Only the card lines
and whatever was logged with the Back Tap shortcut are spending.
"""

import csv
import datetime
import json
import os

os.environ.setdefault("YOUR_EMAIL", "test@example.com")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("HEVY_API_KEY", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")

import finance_tracker
import live_spend
import subscription_audit
import term_context
from test_finance_routes import AUTH, _client, _dmy, _patch_files, _write_csv

START = datetime.date(2026, 10, 20)
END = datetime.date(2026, 10, 26)
FORWARDS = 10993.0
DEPOSITS = 12000.0
CARDS = 189.57
SMALL = 34.70


def _txn(day, description, debit=0, credit=0):
    return {
        "date": day,
        "description": description,
        "debit": float(debit or 0),
        "credit": float(credit or 0),
        "balance": 0.0,
        "category": finance_tracker.categorise(description),
    }


def _day(day):
    return datetime.date(2026, 10, day)


def _cards():
    """Visa purchases that week, summing to $189.57."""
    return [
        _txn(_day(22), "Visa Purchase Woolworths Sydney", debit=100),
        _txn(_day(23), "Visa Purchase Coles Sydney", debit=50),
        _txn(_day(24), "Visa Purchase Uber Trip Sydney", debit=39.57),
    ]


def _small_oskos():
    """Payments to people by Osko. Not counted unless logged."""
    return [
        _txn(_day(21), "Osko Withdrawal 21Oct Halloween Costume Ada Costume", debit=26),
        _txn(_day(22), "Osko Withdrawal 22Oct Split Sam Seven", debit=7),
        _txn(_day(23), "Osko Withdrawal 23Oct Split Lee Cent", debit=1.70),
    ]


def _forwards():
    return [
        _txn(_day(26), "Osko Withdrawal 26Oct09:19 F40S Nilesh Banga", debit=4932),
        _txn(_day(25), "Osko Withdrawal 25Oct13:01 Ferraris Nilesh Banga", debit=4061),
        _txn(_day(25), "Osko Withdrawal 25Oct13:03 American Nilesh Banga", debit=2000),
    ]


def _deposits():
    return [
        _txn(_day(26), "Osko Deposit 26Oct Buyer F40", credit=5200),
        _txn(_day(25), "Osko Deposit 25Oct Ferraris Buyer", credit=4300),
        _txn(_day(24), "Osko Deposit 24Oct American Buyer", credit=2500),
    ]


def _week():
    return _cards() + _small_oskos() + _forwards() + _deposits()


def _log(day, amount, category, note=""):
    """Append one Back Tap entry to the isolated live-spend file."""
    stamp = live_spend.TIMEZONE.localize(datetime.datetime.combine(day, datetime.time(13, 0)))
    with open(live_spend.SPEND_FILE, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "ts": stamp.isoformat(timespec="seconds"),
            "amount": amount,
            "category": category,
            "note": note,
        }) + "\n")


def _summary(txns):
    return finance_tracker.summarise_spending(txns, START, END, 75)


def test_week_of_20_oct_counts_card_lines_only():
    summary = _summary(_week())
    assert summary["total_spend"] == CARDS
    assert summary["by_category"] == {"Food & dining": 150.0, "Transport": 39.57}
    assert summary["transaction_count"] == 3
    assert summary["over_budget"] is True
    assert summary["income"] == []
    assert summary["logged"] == {"count": 0, "total": 0.0}
    assert all(row["amount"] < 4000 for row in summary["flagged"])
    for key in ("unreviewed", "unfinished", "resale"):
        assert key not in summary


def test_transfers_are_listed_and_marked_not_counted():
    transfers = _summary(_week())["transfers"]
    assert transfers["counted"] is False
    assert transfers["count"] == 9
    assert transfers["total_out"] == round(FORWARDS + SMALL, 2)
    assert transfers["total_in"] == DEPOSITS
    amounts = {(item["amount"], item["direction"]) for item in transfers["items"]}
    assert {(4932.0, "out"), (4061.0, "out"), (2000.0, "out")} <= amounts
    assert {(26.0, "out"), (7.0, "out"), (1.7, "out")} <= amounts


def test_logged_spends_are_added_in_full_to_their_category():
    _log(_day(21), 26, "Entertainment", "halloween costume")
    _log(_day(22), 7, "Food & dining", "split lunch")
    _log(_day(25), 12.5, "Other")
    summary = _summary(_week())
    assert summary["total_spend"] == round(CARDS + 26 + 7 + 12.5, 2)
    assert summary["by_category"] == {
        "Food & dining": 157.0,
        "Transport": 39.57,
        "Entertainment": 26.0,
        "Other": 12.5,
    }
    assert summary["transaction_count"] == 6
    assert summary["logged"] == {"count": 3, "total": 45.5}
    # The matching Oskos are still not counted a second time.
    assert summary["transfers"]["total_out"] == round(FORWARDS + SMALL, 2)


def test_logged_spends_outside_the_window_are_left_out():
    _log(_day(19), 50, "Shopping")
    _log(_day(27), 50, "Shopping")
    summary = _summary(_cards())
    assert summary["total_spend"] == CARDS
    assert "Shopping" not in summary["by_category"]


def test_a_large_deposit_is_not_income_and_does_not_reduce_spending():
    txns = _cards() + [_txn(_day(24), "Osko Deposit 24Oct Buyer F40", credit=5200)]
    summary = _summary(txns)
    assert summary["total_spend"] == CARDS
    assert summary["income"] == []
    assert summary["transfers"]["total_in"] == 5200


def test_pay_by_sct_from_a_listed_payer_is_income(tmp_path, monkeypatch):
    monkeypatch.setattr(term_context, "CONTEXT_FILE", tmp_path / "term_context.json")
    (tmp_path / "term_context.json").write_text(json.dumps({
        "spending": {"income_payers": ["Acme Tutoring  Pay"]},
    }))
    pay = "Sct Deposit 23Oct18:55 Acme Tutoring Pay Jo Citizen"
    txns = _cards() + [
        _txn(_day(23), pay, credit=640.00),
        _txn(_day(24), "Osko Deposit 24Oct Buyer F40", credit=5200),
        _txn(_day(24), "Osko Withdrawal 24Oct Acme Tutoring Pay Refund", debit=10),
    ]
    summary = _summary(txns)
    assert summary["total_spend"] == CARDS
    assert summary["income"] == [{
        "date": "2026-10-23", "description": pay, "amount": 640.0, "source": "acme tutoring pay",
    }]
    assert summary["transfers"]["total_in"] == 5200
    assert summary["transfers"]["total_out"] == 10


def test_no_listed_payers_means_no_transfer_is_income(tmp_path, monkeypatch):
    monkeypatch.setattr(term_context, "CONTEXT_FILE", tmp_path / "term_context.json")
    txns = [_txn(_day(23), "Sct Deposit 23Oct Acme Tutoring Pay Jo Citizen", credit=640.00)]
    summary = _summary(txns)
    assert summary["income"] == []
    assert summary["transfers"]["total_in"] == 640.0


def test_a_person_paying_back_does_not_reduce_card_spending():
    txns = [
        _txn(_day(22), "Visa Purchase 22Oct18:35 Knockout Tickets", debit=120),
        _txn(_day(23), "Sct Deposit 23Oct09:11 Rishi Jaiswal", credit=60),
    ]
    summary = _summary(txns)
    assert summary["total_spend"] == 120
    assert summary["income"] == []


def test_bpay_and_direct_debit_count():
    txns = [
        _txn(_day(21), "BPAY Payment Opal Card Topup", debit=20),
        _txn(_day(22), "Direct Debit Spotify P1234", debit=11.99),
        _txn(_day(23), "Osko Withdrawal 23Oct Rent S Ahuja", debit=903.50),
    ]
    summary = _summary(txns)
    assert summary["total_spend"] == 31.99
    assert summary["transfers"]["total_out"] == 903.50


def test_week_of_12_oct_is_unchanged_by_an_unrelated_deposit():
    start = datetime.date(2026, 10, 12)
    end = datetime.date(2026, 10, 18)
    txns = [
        _txn(datetime.date(2026, 10, 14), "Visa Purchase Knockout Tickets", debit=42.99),
        _txn(datetime.date(2026, 10, 15), "Visa Purchase Misc Bits", debit=29),
        _txn(datetime.date(2026, 10, 16), "Visa Purchase Tfnsw Opal", debit=12.95),
        _txn(datetime.date(2026, 10, 17), "Osko Deposit 17Oct Mystery Payer", credit=8000),
    ]
    summary = finance_tracker.summarise_spending(txns, start, end, 75)
    assert summary["total_spend"] == 84.94
    assert summary["by_category"]["Entertainment"] == 42.99
    assert summary["by_category"]["Other"] == 29
    assert summary["by_category"]["Transport"] == 12.95
    assert summary["income"] == []
    assert summary["transfers"]["total_in"] == 8000


def test_repeating_person_is_not_a_subscription(tmp_path, monkeypatch):
    everyday = tmp_path / "everyday.csv"
    monkeypatch.setattr(finance_tracker, "EVERYDAY_CSV", everyday)
    monkeypatch.setattr(term_context, "CONTEXT_FILE", tmp_path / "term_context.json")
    today = datetime.datetime.now(subscription_audit.TIMEZONE).date()
    previous = today - datetime.timedelta(days=32)

    def row(day, description, debit):
        return (day.strftime("%d/%m/%Y"), description, f"{debit:.2f}", "")

    with everyday.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Date", "Description", "Debit", "Credit", "Balance"])
        for day in (today, previous):
            writer.writerow([*row(day, "Visa Purchase SPOTIFY P1234", 11.99), "100.00"])
            writer.writerow([*row(day, "Osko Withdrawal Rent S Ahuja", 903.50), "100.00"])

    review = subscription_audit.summarise_subscriptions(months=3)
    assert review["monthly_known"] == 11.99
    assert review["review"] == []
    assert all("ahuja" not in item["merchant"] for item in review["known"])

    txns = finance_tracker.parse_stgeorge_csv(everyday)
    summary = finance_tracker.summarise_spending(
        txns, today - datetime.timedelta(days=6), today, 75,
    )
    assert summary["total_spend"] == 11.99
    assert summary["transfers"]["total_out"] == 903.50


def test_finance_routes_use_the_same_rule(tmp_path, monkeypatch):
    everyday, _savings = _patch_files(monkeypatch, tmp_path)
    rows = []
    for txn in _week():
        rows.append([
            _dmy(txn["date"]), txn["description"],
            f"{txn['debit']:.2f}" if txn["debit"] else "",
            f"{txn['credit']:.2f}" if txn["credit"] else "",
            "1000.00",
        ])
    _write_csv(everyday, rows)
    _log(_day(21), 26, "Entertainment", "halloween costume")
    client = _client()

    spending = client.get(
        "/finance/spending?start=2026-10-20&end=2026-10-26", headers=AUTH,
    ).get_json()
    assert spending["total_spend"] == round(CARDS + 26, 2)
    assert spending["by_category"]["Entertainment"] == 26.0
    assert spending["logged"] == {"count": 1, "total": 26.0}
    assert spending["transfers"]["counted"] is False
    assert spending["transfers"]["total_out"] == round(FORWARDS + SMALL, 2)
    assert "live" not in spending

    bundle = client.get("/finance?start=2026-10-20&end=2026-10-26", headers=AUTH).get_json()
    assert bundle["spending"]["total_spend"] == spending["total_spend"]
    assert "counterparties" not in bundle

    for path in ("/finance/counterparties", "/finance/lines", "/finance/resale"):
        assert client.post(path, headers=AUTH, json={}).status_code in (404, 405)


def test_telegram_labelling_commands_are_gone():
    import jarvis_telegram
    for command in ("/unreviewed", "/who", "/label", "/resale"):
        assert command not in jarvis_telegram.COMMANDS
        assert command not in jarvis_telegram.cmd_help([])
