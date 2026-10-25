"""Pass-through transfers are not spending.

The 20–26 Oct 2026 week counted three Osko withdrawals ($4,932, $4,061,
$2,000) as Other. They were a friend's stock proceeds paid straight back
out. Card purchases, categorised Oskos, salary, and own-account moves stay.
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

import pytest

import finance_tracker
import subscription_audit
import term_context
from test_finance_routes import AUTH, _client, _dmy, _patch_files, _today

START = datetime.date(2026, 10, 20)
END = datetime.date(2026, 10, 26)


@pytest.fixture(autouse=True)
def _isolated_term_context(tmp_path, monkeypatch):
    monkeypatch.setattr(term_context, "CONTEXT_FILE", tmp_path / "term_context.json")


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


def _stock_outflows():
    return [
        _txn(_day(26), "Osko Withdrawal 26Oct09:19 F40S And R32S", debit=4932),
        _txn(_day(25), "Osko Withdrawal 25Oct13:01 Ferraris", debit=4061),
        _txn(_day(25), "Osko Withdrawal 25Oct13:03 American", debit=2000),
    ]


def _real_spend():
    """The week's genuine spend, summing to $224.27."""
    return [
        _txn(_day(22), "Visa Purchase WOOLWORTHS", debit=84.19),
        _txn(_day(23), "Visa Purchase COLES", debit=40),
        _txn(_day(24), "Visa Purchase UBER TRIP", debit=90.08),
        _txn(_day(21), "Osko Withdrawal 21Oct Change For Hot Wheels W Tynan", debit=10),
    ]


def _summary(txns, extra_inflows=None):
    kwargs = {}
    if extra_inflows is not None:
        kwargs["extra_inflows"] = extra_inflows
    return finance_tracker.summarise_spending(txns, START, END, 75, **kwargs)


def test_matching_deposits_drop_the_three_oskos_and_leave_224_27():
    txns = _stock_outflows() + _real_spend() + [
        _txn(_day(26), "Osko Deposit 26Oct09:01 Buyer F40", credit=4932),
        _txn(_day(24), "Osko Deposit 24Oct Card Sale", credit=4061),
        _txn(_day(25), "Osko Deposit 25Oct10:00 Friend", credit=2000),
    ]
    summary = _summary(txns, extra_inflows=[])
    assert summary["total_spend"] == 224.27
    assert summary["by_category"]["Food & dining"] == 124.19
    assert summary["by_category"]["Transport"] == 90.08
    assert summary["by_category"]["Other"] == 10
    excluded = summary["excluded_transfers"]
    assert excluded["count"] == 3
    assert excluded["total"] == 10993
    assert {item["amount"] for item in excluded["items"]} == {4932, 4061, 2000}
    assert "F40S And R32S" in excluded["items"][-1]["description"]
    flagged = {row["amount"] for row in summary["flagged"]}
    assert flagged == {90.08, 84.19}
    assert 4932 not in flagged


def test_one_deposit_covers_the_three_withdrawals():
    txns = _stock_outflows() + [
        _txn(_day(24), "Osko Deposit 24Oct Auction House", credit=10993),
        _txn(_day(22), "Visa Purchase COLES", debit=22.50),
    ]
    summary = _summary(txns, extra_inflows=[])
    assert summary["total_spend"] == 22.50
    assert summary["excluded_transfers"]["count"] == 3
    assert summary["excluded_transfers"]["total"] == 10993
    assert summary["flagged"] == []


def test_several_deposits_cover_one_withdrawal():
    txns = [
        _txn(_day(24), "Osko Deposit 24Oct Part A", credit=3000),
        _txn(_day(24), "Osko Deposit 24Oct Part B", credit=1932),
        _txn(_day(25), "Osko Withdrawal 25Oct13:01 Ferraris", debit=4932),
        _txn(_day(25), "Visa Purchase COLES", debit=12.50),
    ]
    summary = _summary(txns, extra_inflows=[])
    assert summary["total_spend"] == 12.50
    assert summary["excluded_transfers"] == {
        "count": 1,
        "total": 4932,
        "items": [{
            "date": "2026-10-25",
            "amount": 4932,
            "description": "Osko Withdrawal 25Oct13:01 Ferraris",
        }],
    }


def test_card_spend_and_categorised_osko_stay_when_amounts_match():
    day = _day(25)
    txns = [
        _txn(_day(24), "Osko Deposit 24Oct Someone", credit=120),
        _txn(day, "Osko Withdrawal 25Oct Knockout R Jaiswal", debit=120),
        _txn(_day(24), "Osko Deposit 24Oct Groceries", credit=50),
        _txn(day, "Visa Purchase WOOLWORTHS", debit=50),
        _txn(_day(24), "Sct Deposit 24Oct Sent From Revolut Manav Jain", credit=4932),
        _txn(day, "Osko Withdrawal 25Oct13:01 Ferraris", debit=4932),
        _txn(_day(24), "Osko Deposit 24Oct Propwealth Salary", credit=4061),
        _txn(day, "Osko Withdrawal 25Oct13:03 American", debit=4061),
        _txn(_day(26), "Osko Deposit 26Oct Later", credit=2000),
        _txn(day, "Osko Withdrawal 25Oct13:04 American", debit=2000),
    ]
    summary = _summary(txns, extra_inflows=[])
    assert summary["by_category"]["Entertainment"] == 120
    assert summary["by_category"]["Food & dining"] == 50
    assert summary["by_category"]["Other"] == 4932 + 4061 + 2000
    assert summary["excluded_transfers"]["count"] == 0
    assert summary["total_spend"] == 120 + 50 + 4932 + 4061 + 2000


def test_window_and_tolerance_are_configurable(tmp_path):
    outflow = _txn(_day(26), "Osko Withdrawal 26Oct Ferraris", debit=4932)
    far = _txn(_day(18), "Osko Deposit 18Oct Buyer", credit=4932)
    near = _txn(_day(26), "Sct Deposit 26Oct Buyer", credit=4932.05)
    outside = _summary([outflow, far], extra_inflows=[])
    assert outside["total_spend"] == 4932
    assert outside["excluded_transfers"]["count"] == 0

    (tmp_path / "term_context.json").write_text(json.dumps({
        "spending": {"passthrough_window_days": 8},
    }))
    inside = _summary([outflow, far], extra_inflows=[])
    assert inside["total_spend"] == 0
    assert inside["excluded_transfers"]["total"] == 4932

    (tmp_path / "term_context.json").write_text(json.dumps({
        "spending": {"passthrough_tolerance": 0.05},
    }))
    fuzzy = _summary([outflow, near], extra_inflows=[])
    assert fuzzy["excluded_transfers"]["count"] == 1
    exact = finance_tracker.get_passthrough_settings()
    assert exact["tolerance"] == 0.05

    (tmp_path / "term_context.json").write_text(json.dumps({
        "spending": {"passthrough_window_days": 0, "passthrough_tolerance": 50},
    }))
    assert finance_tracker.get_passthrough_settings() == {
        "window_days": 7,
        "tolerance": 0.0,
        "max_group": 4,
    }


def test_own_account_moves_are_not_listed_as_pass_through():
    txns = [
        _txn(_day(24), "Osko Deposit 24Oct Buyer", credit=500),
        _txn(_day(25), "Osko Withdrawal 25Oct Manav Jain", debit=500),
        _txn(_day(25), "Internet Withdrawal To savings", debit=40),
        _txn(_day(24), "Direct Credit 24Oct Employer Pty Ltd", credit=80),
        _txn(_day(25), "Osko Withdrawal 25Oct Landlord", debit=80),
        _txn(_day(25), "Visa Purchase COLES", debit=8),
    ]
    summary = _summary(txns, extra_inflows=[])
    assert summary["total_spend"] == 88
    assert summary["by_category"]["Other"] == 80
    assert summary["excluded_transfers"]["count"] == 0


def test_revolut_external_transfer_covers_everyday_oskos(tmp_path, monkeypatch):
    path = tmp_path / "revolut.csv"
    monkeypatch.setattr(finance_tracker, "REVOLUT_CSV", path)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow([
            "State", "Type", "Completed Date", "Description",
            "Amount", "Fee", "Currency", "Balance",
        ])
        writer.writerow([
            "COMPLETED", "TRANSFER", "2026-10-24 15:00:00", "Buyer payout",
            "10993.00", "0", "AUD", "10993.00",
        ])
        writer.writerow([
            "COMPLETED", "CARD_PAYMENT", "2026-10-24 12:00:00", "EB Games",
            "15.00", "0", "AUD", "11008.00",
        ])
        writer.writerow([
            "COMPLETED", "TRANSFER", "2026-10-24 11:00:00", "From Manav Jain",
            "15.00", "0", "AUD", "11023.00",
        ])
        writer.writerow([
            "COMPLETED", "TOPUP", "2026-10-24 10:00:00", "Topup",
            "4932.00", "0", "AUD", "15955.00",
        ])
    txns = _stock_outflows() + [
        _txn(_day(25), "Osko Withdrawal 25Oct Corner Lane", debit=15),
        _txn(_day(22), "Visa Purchase COLES", debit=18),
    ]
    summary = _summary(txns)
    assert summary["total_spend"] == 33
    assert summary["excluded_transfers"]["count"] == 3
    assert summary["excluded_transfers"]["total"] == 10993
    assert summary["by_category"]["Other"] == 15


def test_owner_revolut_transfer_does_not_cover_an_osko(tmp_path, monkeypatch):
    path = tmp_path / "revolut.csv"
    monkeypatch.setattr(finance_tracker, "REVOLUT_CSV", path)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow([
            "State", "Type", "Completed Date", "Description",
            "Amount", "Fee", "Currency", "Balance",
        ])
        writer.writerow([
            "COMPLETED", "TRANSFER", "2026-10-24 15:00:00", "From Manav Jain",
            "4932.00", "0", "AUD", "4932.00",
        ])
    txns = [
        _txn(_day(25), "Osko Withdrawal 25Oct13:01 Ferraris", debit=4932),
        _txn(_day(25), "Visa Purchase COLES", debit=5),
    ]
    summary = _summary(txns)
    assert summary["total_spend"] == 4937
    assert summary["excluded_transfers"]["count"] == 0


def test_finance_route_reports_excluded_transfers(tmp_path, monkeypatch):
    today = _today()
    yesterday = today - datetime.timedelta(days=1)
    everyday, _savings = _patch_files(monkeypatch, tmp_path)
    everyday.parent.mkdir(parents=True, exist_ok=True)
    with everyday.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Date", "Description", "Debit", "Credit", "Balance"])
        writer.writerow([
            _dmy(yesterday), "Osko Withdrawal 25Oct Ferraris", "200.00", "", "800.00",
        ])
        writer.writerow([
            _dmy(yesterday), "Osko Deposit 25Oct Buyer", "", "200.00", "1000.00",
        ])
        writer.writerow([
            _dmy(today), "Visa Purchase COLES", "30.00", "", "770.00",
        ])
    res = _client().get("/finance/spending", headers=AUTH)
    assert res.status_code == 200
    body = res.get_json()
    assert body["total_spend"] == 30
    assert body["excluded_transfers"]["count"] == 1
    assert body["excluded_transfers"]["total"] == 200
    assert body["flagged"] == []
    bundle = _client().get("/finance", headers=AUTH).get_json()
    assert bundle["spending"]["excluded_transfers"]["total"] == 200
    assert bundle["spending"]["total_spend"] == 30


def test_s_ahuja_stays_in_the_subscription_review(tmp_path, monkeypatch):
    """Recurring Osko to S Ahuja is a review row even when spending drops it.

    Pass-through matching removes the withdrawals from the spend total when
    a deposit of the same amount arrived first. The subscription audit does
    not use that matcher, so the Sep/Oct 's ahuja' flag stays.
    """
    everyday = tmp_path / "everyday.csv"
    monkeypatch.setattr(finance_tracker, "EVERYDAY_CSV", everyday)
    monkeypatch.setattr(finance_tracker, "REVOLUT_CSV", tmp_path / "revolut.csv")
    today = datetime.datetime.now(subscription_audit.TIMEZONE).date()
    previous = today - datetime.timedelta(days=32)

    def row(day, description, debit="", credit=""):
        return [day.strftime("%d/%m/%Y"), description, debit, credit, "100.00"]

    everyday.parent.mkdir(parents=True, exist_ok=True)
    with everyday.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Date", "Description", "Debit", "Credit", "Balance"])
        writer.writerows([
            row(today, "Osko Withdrawal S Ahuja", "903.50"),
            row(today - datetime.timedelta(days=1), "Osko Deposit S Ahuja", "", "903.50"),
            row(previous, "Osko Withdrawal S Ahuja", "903.50"),
            row(previous - datetime.timedelta(days=1), "Osko Deposit S Ahuja", "", "903.50"),
        ])
    txns = finance_tracker.parse_stgeorge_csv(everyday)
    summary = finance_tracker.summarise_spending(
        txns, previous - datetime.timedelta(days=1), today, 75, extra_inflows=[],
    )
    assert summary["total_spend"] == 0
    assert summary["excluded_transfers"]["count"] == 2
    assert summary["excluded_transfers"]["total"] == 1807

    review = subscription_audit.summarise_subscriptions(months=3)
    assert review["available"] is True
    assert any(item["merchant"] == "s ahuja" for item in review["review"])
    assert review["monthly_review"] == 903.50
