"""Person-to-person transfers are netted, not matched one to one.

The 20–26 Oct 2026 week had about $10,993 of Osko withdrawals covered by
larger sale-proceeds deposits on the same everyday account. Card purchases
stay. Only a positive net remainder is spending. A manual rule can force
one withdrawal to count, or to drop out.
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
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(term_context, "CONTEXT_FILE", tmp_path / "term_context.json")
    monkeypatch.setattr(
        finance_tracker, "TRANSFER_OVERRIDES_FILE", tmp_path / "transfer_overrides.json",
    )


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


def _cards():
    """Visa purchases that week, summing to $214.27."""
    return [
        _txn(_day(22), "Visa Purchase WOOLWORTHS", debit=84.19),
        _txn(_day(23), "Visa Purchase COLES", debit=40),
        _txn(_day(24), "Visa Purchase UBER TRIP", debit=90.08),
    ]


def _hot_wheels():
    return _txn(_day(21), "Osko Withdrawal 21Oct Change For Hot Wheels W Tynan", debit=10)


def _uneven_deposits():
    """Larger than the withdrawals, and not by one fixed gap."""
    return [
        _txn(_day(26), "Osko Deposit 26Oct09:01 Buyer F40", credit=5200),
        _txn(_day(24), "Osko Deposit 24Oct Card Sale", credit=4510),
        _txn(_day(25), "Osko Deposit 25Oct10:00 Friend", credit=2311),
    ]


def _summary(txns, extra_inflows=None):
    kwargs = {}
    if extra_inflows is not None:
        kwargs["extra_inflows"] = extra_inflows
    return finance_tracker.summarise_spending(txns, START, END, 75, **kwargs)


def _write_overrides(tmp_path, rules):
    (tmp_path / "transfer_overrides.json").write_text(json.dumps({"rules": rules}))


def test_larger_uneven_deposits_leave_the_card_spend_and_a_negative_net():
    txns = _stock_outflows() + _cards() + [_hot_wheels()] + _uneven_deposits()
    summary = _summary(txns, extra_inflows=[])
    # The $10 Hot Wheels line is itself an Osko, so it sits in the net.
    assert summary["total_spend"] == 214.27
    assert summary["by_category"]["Food & dining"] == 124.19
    assert summary["by_category"]["Transport"] == 90.08
    assert "Other" not in summary["by_category"]
    net = summary["transfers_net"]
    assert net["in"] == 12021
    assert net["out"] == 11003
    assert net["carry_in"] == 0
    assert net["net"] == -1018
    assert net["counted"] == 0
    excluded = summary["excluded_transfers"]
    assert excluded["count"] == 4
    assert excluded["total"] == 11003
    assert {item["amount"] for item in excluded["items"]} == {4932, 4061, 2000, 10}
    flagged = {row["amount"] for row in summary["flagged"]}
    assert flagged == {90.08, 84.19}
    assert 4932 not in flagged


def test_marking_the_small_osko_as_spending_leaves_224_27(tmp_path):
    _write_overrides(tmp_path, [{
        "date": "2026-10-21",
        "amount": 10,
        "description": "Hot Wheels",
        "treat": "spending",
    }])
    txns = _stock_outflows() + _cards() + [_hot_wheels()] + _uneven_deposits()
    summary = _summary(txns, extra_inflows=[])
    assert summary["total_spend"] == 224.27
    assert summary["by_category"]["Other"] == 10
    assert summary["transfers_net"]["out"] == 10993
    assert summary["transfers_net"]["counted"] == 0
    assert summary["transfers_net"]["net"] < 0
    assert summary["excluded_transfers"]["total"] == 10993
    assert summary["excluded_transfers"]["count"] == 3


def test_one_larger_deposit_covers_the_three_withdrawals():
    txns = _stock_outflows() + [
        _txn(_day(24), "Osko Deposit 24Oct Auction House", credit=12000),
        _txn(_day(22), "Visa Purchase COLES", debit=22.50),
    ]
    summary = _summary(txns, extra_inflows=[])
    assert summary["total_spend"] == 22.50
    assert summary["transfers_net"]["in"] == 12000
    assert summary["transfers_net"]["out"] == 10993
    assert summary["transfers_net"]["net"] == -1007
    assert summary["transfers_net"]["counted"] == 0
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
    assert summary["transfers_net"]["net"] == 0
    assert summary["transfers_net"]["counted"] == 0
    assert summary["excluded_transfers"] == {
        "count": 1,
        "total": 4932,
        "items": [{
            "date": "2026-10-25",
            "amount": 4932,
            "description": "Osko Withdrawal 25Oct13:01 Ferraris",
        }],
    }


def test_positive_remainder_counts_and_does_not_shrink_card_spend():
    txns = [
        _txn(_day(24), "Osko Deposit 24Oct Buyer", credit=1100),
        _txn(_day(25), "Osko Withdrawal 25Oct Ferraris", debit=1000),
        _txn(_day(25), "Sct Withdrawal 25Oct American", debit=400),
        _txn(_day(25), "Visa Purchase COLES", debit=22.50),
    ]
    summary = _summary(txns, extra_inflows=[])
    assert summary["transfers_net"]["in"] == 1100
    assert summary["transfers_net"]["out"] == 1400
    assert summary["transfers_net"]["net"] == 300
    assert summary["transfers_net"]["counted"] == 300
    assert summary["by_category"]["Food & dining"] == 22.50
    assert summary["by_category"]["Other"] == 300
    assert summary["total_spend"] == 322.50
    assert summary["excluded_transfers"]["total"] == 1100
    assert {item["amount"] for item in summary["excluded_transfers"]["items"]} == {1000, 400}
    assert all(row["description"] != "Osko Withdrawal 25Oct Ferraris" for row in summary["flagged"])


def test_negative_net_does_not_reduce_card_spend():
    txns = [
        _txn(_day(24), "Osko Deposit 24Oct Buyer", credit=5000),
        _txn(_day(25), "Osko Withdrawal 25Oct Ferraris", debit=100),
        _txn(_day(25), "Visa Purchase COLES", debit=40),
    ]
    summary = _summary(txns, extra_inflows=[])
    assert summary["total_spend"] == 40
    assert summary["transfers_net"]["net"] == -4900
    assert summary["transfers_net"]["counted"] == 0
    assert summary["by_category"] == {"Food & dining": 40}


def test_card_bpay_and_categorised_osko_stay():
    txns = [
        _txn(_day(24), "Osko Deposit 24Oct Someone", credit=120),
        _txn(_day(25), "Osko Withdrawal 25Oct Knockout R Jaiswal", debit=120),
        _txn(_day(24), "Osko Deposit 24Oct Groceries", credit=50),
        _txn(_day(25), "Visa Purchase WOOLWORTHS", debit=50),
        _txn(_day(24), "Sct Deposit 24Oct Sent From Revolut Manav Jain", credit=4932),
        _txn(_day(25), "Osko Withdrawal 25Oct13:01 Ferraris", debit=4932),
        _txn(_day(24), "Osko Deposit 24Oct Propwealth Salary", credit=4061),
        _txn(_day(25), "Osko Withdrawal 25Oct13:03 American", debit=4061),
        _txn(_day(25), "BPAY Energy Australia", debit=60),
        _txn(_day(25), "Direct Debit Council Rates", debit=15.99),
        _txn(_day(27), "Osko Deposit 27Oct After The Window", credit=9000),
    ]
    summary = _summary(txns, extra_inflows=[])
    assert summary["by_category"]["Entertainment"] == 120
    assert summary["by_category"]["Food & dining"] == 50
    assert summary["by_category"]["Other"] == round(60 + 15.99 + 8823, 2)
    # Someone + groceries are inflows. Salary, the Revolut move, and the
    # deposit after the window are not. Both Oskos are outflows.
    assert summary["transfers_net"]["in"] == 170
    assert summary["transfers_net"]["out"] == 8993
    assert summary["transfers_net"]["counted"] == 8823
    assert summary["total_spend"] == 120 + 50 + 60 + 15.99 + 8823


def test_a_later_deposit_inside_the_window_does_reduce_the_net():
    outflow = _txn(_day(25), "Osko Withdrawal 25Oct13:04 American", debit=2000)
    later = _txn(_day(26), "Osko Deposit 26Oct Later", credit=2000)
    summary = _summary([outflow, later], extra_inflows=[])
    assert summary["total_spend"] == 0
    assert summary["transfers_net"]["net"] == 0
    assert summary["transfers_net"]["counted"] == 0


def test_carry_uses_unused_inflow_and_not_a_deposit_already_spent(tmp_path):
    outflow = _txn(_day(26), "Osko Withdrawal 26Oct Ferraris", debit=4932)
    too_old = _txn(_day(12), "Osko Deposit 12Oct Buyer", credit=4932)
    on_the_edge = _txn(_day(13), "Osko Deposit 13Oct Buyer", credit=4932)
    day_before = _txn(_day(19), "Osko Deposit 19Oct Buyer", credit=4932)
    already_spent = _txn(_day(14), "Osko Withdrawal 14Oct Ferraris", debit=4932)

    outside = _summary([outflow, too_old], extra_inflows=[])
    assert outside["total_spend"] == 4932
    assert outside["transfers_net"]["carry_in"] == 0
    assert outside["excluded_transfers"]["count"] == 0

    carried = _summary([outflow, on_the_edge], extra_inflows=[])
    assert carried["total_spend"] == 0
    assert carried["transfers_net"]["carry_in"] == 4932
    assert carried["transfers_net"]["counted"] == 0

    consumed = _summary([outflow, on_the_edge, already_spent], extra_inflows=[])
    assert consumed["total_spend"] == 4932
    assert consumed["transfers_net"]["carry_in"] == 0

    (tmp_path / "term_context.json").write_text(json.dumps({
        "spending": {"transfer_carry_days": 0},
    }))
    disabled = _summary([outflow, day_before], extra_inflows=[])
    assert disabled["total_spend"] == 4932
    assert disabled["transfers_net"]["carry_in"] == 0
    assert finance_tracker.get_transfer_carry_days() == 0

    (tmp_path / "term_context.json").write_text(json.dumps({
        "spending": {"transfer_carry_days": 14},
    }))
    wider = _summary([outflow, too_old], extra_inflows=[])
    assert wider["total_spend"] == 0
    assert wider["transfers_net"]["carry_in"] == 4932

    (tmp_path / "term_context.json").write_text(json.dumps({
        "spending": {"transfer_carry_days": 99},
    }))
    assert finance_tracker.get_transfer_carry_days() == 7


def test_salary_and_own_account_moves_are_not_in_the_net():
    txns = [
        _txn(_day(24), "Osko Deposit 24Oct Buyer", credit=500),
        _txn(_day(25), "Osko Withdrawal 25Oct Manav Jain", debit=500),
        _txn(_day(25), "Internet Withdrawal To savings", debit=40),
        _txn(_day(24), "Direct Credit 24Oct Employer Pty Ltd", credit=80),
        _txn(_day(25), "Osko Withdrawal 25Oct Landlord", debit=80),
        _txn(_day(25), "Visa Purchase COLES", debit=8),
    ]
    summary = _summary(txns, extra_inflows=[])
    # The $500 buyer deposit covers the $80 landlord Osko. The direct
    # credit does not. The payment to himself is not in the net or the spend.
    assert summary["total_spend"] == 8
    assert summary["transfers_net"]["in"] == 500
    assert summary["transfers_net"]["out"] == 80
    assert summary["transfers_net"]["counted"] == 0
    assert summary["excluded_transfers"]["count"] == 1
    blob = json.dumps(summary["excluded_transfers"]).lower()
    assert "manav" not in blob
    assert "revolut" not in blob


def test_override_can_force_spending_or_pass_through(tmp_path):
    _write_overrides(tmp_path, [
        {
            "date": "2026-10-25",
            "amount": 80,
            "description": "landlord",
            "treat": "spending",
        },
        {
            "date": "2026-10-26",
            "amount": 40,
            "description": "Corner Lane",
            "treat": "pass-through",
        },
    ])
    txns = [
        _txn(_day(24), "Osko Deposit 24Oct Buyer", credit=500),
        _txn(_day(25), "Osko Withdrawal 25Oct Landlord", debit=80),
        _txn(_day(26), "Osko Withdrawal 26Oct Corner Lane", debit=40),
        _txn(_day(25), "Visa Purchase COLES", debit=8),
    ]
    summary = _summary(txns, extra_inflows=[])
    assert summary["by_category"]["Other"] == 80
    assert summary["total_spend"] == 88
    assert summary["transfers_net"]["out"] == 0
    assert summary["transfers_net"]["counted"] == 0
    assert summary["excluded_transfers"]["total"] == 40
    assert summary["excluded_transfers"]["items"][0]["description"].endswith("Corner Lane")

    (tmp_path / "transfer_overrides.json").write_text("not json")
    broken = _summary(txns, extra_inflows=[])
    assert broken["total_spend"] == 8
    assert broken["transfers_net"]["out"] == 120


def test_a_rule_with_the_wrong_fragment_does_not_match(tmp_path):
    _write_overrides(tmp_path, [{
        "date": "2026-10-25",
        "amount": 80,
        "description": "someone else",
        "treat": "pass-through",
    }])
    txns = [
        _txn(_day(25), "Osko Withdrawal 25Oct Landlord", debit=80),
        _txn(_day(25), "Visa Purchase COLES", debit=8),
    ]
    summary = _summary(txns, extra_inflows=[])
    assert summary["total_spend"] == 88


def test_payid_and_npp_are_in_the_net():
    txns = [
        _txn(_day(24), "PayID Payment From Buyer", credit=50),
        _txn(_day(25), "PayID Withdrawal To Alex", debit=80),
        _txn(_day(25), "NPP Credit Week Split", credit=10),
        _txn(_day(25), "Visa Purchase COLES", debit=5),
    ]
    summary = _summary(txns, extra_inflows=[])
    assert summary["transfers_net"]["in"] == 60
    assert summary["transfers_net"]["out"] == 80
    assert summary["transfers_net"]["counted"] == 20
    assert summary["total_spend"] == 25
    assert summary["by_category"]["Food & dining"] == 5
    assert summary["by_category"]["Other"] == 20


def test_a_refund_credit_is_not_also_a_transfer_inflow():
    txns = [
        _txn(_day(22), "Osko Withdrawal 22Oct18:35 Knockout R Jaiswal", debit=120),
        _txn(_day(23), "Sct Deposit 23Oct09:11 Rishi Jaiswal", credit=120),
        _txn(_day(25), "Osko Withdrawal 25Oct Landlord", debit=120),
    ]
    summary = _summary(txns, extra_inflows=[])
    assert summary["total_spend"] == 120
    assert summary["by_category"] == {"Other": 120}
    assert summary["transfers_net"]["in"] == 0
    assert summary["transfers_net"]["out"] == 120
    assert summary["transfers_net"]["counted"] == 120


def _revolut_file(path, rows):
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow([
            "State", "Type", "Completed Date", "Description",
            "Amount", "Fee", "Currency", "Balance",
        ])
        writer.writerows(rows)


def test_revolut_inflow_does_not_cover_everyday_oskos(tmp_path, monkeypatch):
    path = tmp_path / "revolut.csv"
    monkeypatch.setattr(finance_tracker, "REVOLUT_CSV", path)
    _revolut_file(path, [
        ["COMPLETED", "TRANSFER", "2026-10-24 15:00:00", "Buyer payout", "10993.00", "0", "AUD", "10993.00"],
        ["COMPLETED", "CARD_PAYMENT", "2026-10-24 12:00:00", "EB Games", "-15.00", "0", "AUD", "15.00"],
        ["COMPLETED", "TRANSFER", "2026-10-24 11:00:00", "From Manav Jain", "15.00", "0", "AUD", "30.00"],
        ["COMPLETED", "TOPUP", "2026-10-24 10:00:00", "Topup", "4932.00", "0", "AUD", "4962.00"],
        ["COMPLETED", "TRANSFER", "2026-10-25 10:00:00", "Supplier invoice", "-30.00", "0", "AUD", "30.00"],
    ])
    txns = _stock_outflows() + [
        _txn(_day(25), "Osko Withdrawal 25Oct Corner Lane", debit=15),
        _txn(_day(22), "Visa Purchase COLES", debit=18),
    ]
    summary = _summary(txns)
    assert summary["total_spend"] == 10993 + 15 + 18
    assert summary["transfers_net"]["in"] == 0
    assert summary["transfers_net"]["counted"] == 11008
    assert summary["excluded_transfers"]["count"] == 0
    reselling = summary["transfers_net"]["reselling"]
    assert reselling["in"] == 10993
    assert reselling["out"] == 30
    assert reselling["net"] == -10963
    assert reselling["counted"] == 0
    assert "revolut" not in json.dumps(summary).lower()


def test_owner_revolut_transfer_is_not_in_the_reselling_net(tmp_path, monkeypatch):
    path = tmp_path / "revolut.csv"
    monkeypatch.setattr(finance_tracker, "REVOLUT_CSV", path)
    _revolut_file(path, [
        ["COMPLETED", "TRANSFER", "2026-10-24 15:00:00", "From Manav Jain", "4932.00", "0", "AUD", "4932.00"],
    ])
    txns = [
        _txn(_day(25), "Osko Withdrawal 25Oct13:01 Ferraris", debit=4932),
        _txn(_day(25), "Visa Purchase COLES", debit=5),
    ]
    summary = _summary(txns)
    assert summary["total_spend"] == 4937
    assert summary["excluded_transfers"]["count"] == 0
    assert "reselling" not in summary["transfers_net"]
    assert "revolut" not in json.dumps(summary).lower()
    assert "manav" not in json.dumps(summary).lower()


def test_finance_route_reports_the_net(tmp_path, monkeypatch):
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
    assert body["transfers_net"]["in"] == 200
    assert body["transfers_net"]["out"] == 200
    assert body["transfers_net"]["net"] == 0
    assert body["transfers_net"]["counted"] == 0
    assert body["excluded_transfers"]["count"] == 1
    assert body["excluded_transfers"]["total"] == 200
    assert body["flagged"] == []
    bundle = _client().get("/finance", headers=AUTH).get_json()
    assert bundle["spending"]["transfers_net"]["counted"] == 0
    assert bundle["spending"]["excluded_transfers"]["total"] == 200
    assert bundle["spending"]["total_spend"] == 30


def test_s_ahuja_stays_in_the_subscription_review(tmp_path, monkeypatch):
    """Recurring Osko to S Ahuja stays in the review when spending nets it out.

    The subscription audit does not use the transfer net.
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
    assert summary["transfers_net"]["in"] == 1807
    assert summary["transfers_net"]["out"] == 1807
    assert summary["transfers_net"]["counted"] == 0
    assert summary["excluded_transfers"]["count"] == 2
    assert summary["excluded_transfers"]["total"] == 1807

    review = subscription_audit.summarise_subscriptions(months=3)
    assert review["available"] is True
    assert any(item["merchant"] == "s ahuja" for item in review["review"])
    assert review["monthly_review"] == 903.50
