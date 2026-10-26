"""Commission income is the reselling sheet, not the bank leftover.

The labelled lines decide what stays out of spending. For the Nilesh
stock, the sheet row is 'banga selling commission': sold $13,870,
commission $1,387, so $12,483 is due. The bank forwarded $10,993, and
the inflows minus outflows ($381.37) are not the commission.
"""

import datetime
import os

os.environ.setdefault("YOUR_EMAIL", "test@example.com")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("HEVY_API_KEY", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")

import finance_tracker
from reselling_tracker import Item
from test_passthrough import (
    END,
    FORWARDS,
    START,
    _day,
    _remember,
    _summary,
    _txn,
)

BANK_IN = 11374.37
COMMISSION = 1387.0
SOLD = 13870.0
DUE = 12483.0
NOT_FORWARDED = 1490.0


def _commission(sale_date=None, sale_price=SOLD, sheet_net=COMMISSION, name="banga selling commission"):
    return Item(
        status="sold",
        category="Other",
        name=name,
        cost=0,
        sale_price=sale_price,
        sheet_net=sheet_net,
        sale_date=sale_date,
    )


def _labelled_week(monkeypatch, items, amounts=(BANK_IN,)):
    monkeypatch.setattr(finance_tracker, "_load_sheet_items", lambda: items)
    _remember("Nilesh Banga")
    txns = [
        _txn(_day(26), "Osko Deposit 26Oct Buyer F40", credit=amounts[0]),
        _txn(_day(26), "Osko Withdrawal 26Oct09:19 F40S Nilesh Banga", debit=4932),
        _txn(_day(25), "Osko Withdrawal 25Oct13:01 Ferraris Nilesh Banga", debit=4061),
        _txn(_day(25), "Osko Withdrawal 25Oct13:03 American Nilesh Banga", debit=2000),
        _txn(_day(22), "Visa Purchase Woolworths Sydney", debit=40),
    ]
    import counterparties
    assert counterparties.accept_resale(txns, START, END) == 4
    return _summary(txns)


def test_sheet_commission_is_the_income_and_the_gap_is_shown(monkeypatch):
    summary = _labelled_week(monkeypatch, [_commission()])
    resale = summary["resale"]
    assert resale["in"] == BANK_IN
    assert resale["out"] == FORWARDS
    assert resale["net"] == round(BANK_IN - FORWARDS, 2)
    assert resale["income"] == COMMISSION
    assert summary["income"] == []
    assert summary["total_spend"] == 40
    assert resale["difference"]["sold"] == SOLD
    assert resale["difference"]["due"] == DUE
    assert resale["difference"]["forwarded"] == FORWARDS
    assert resale["difference"]["not_in_week"] == NOT_FORWARDED
    assert resale["difference"]["bank_net"] == round(BANK_IN - FORWARDS, 2)
    note = resale["note"]
    assert "Sold $13,870.00" in note
    assert "commission $1,387.00" in note
    assert "due to banga" in note
    assert "$12,483.00" in note
    assert "Forwarded $10,993.00 in this week's bank lines" in note
    assert "$1,490.00 is not in this week's bank lines." in note
    assert "owed" not in note.lower()
    assert "error" not in note.lower()


def test_an_earlier_forward_is_left_outside_this_week(monkeypatch):
    """The $1,490 is other weeks. The spending window stays 20–26 Oct."""
    monkeypatch.setattr(finance_tracker, "_load_sheet_items", lambda: [_commission()])
    _remember("Nilesh Banga")
    earlier = datetime.date(2026, 10, 13)
    txns = [
        _txn(_day(26), "Osko Deposit 26Oct Buyer F40", credit=BANK_IN),
        _txn(_day(26), "Osko Withdrawal 26Oct09:19 F40S Nilesh Banga", debit=4932),
        _txn(_day(25), "Osko Withdrawal 25Oct13:01 Ferraris Nilesh Banga", debit=4061),
        _txn(_day(25), "Osko Withdrawal 25Oct13:03 American Nilesh Banga", debit=2000),
        _txn(earlier, "Osko Withdrawal 13Oct Earlier Nilesh Banga", debit=NOT_FORWARDED),
    ]
    import counterparties
    counterparties.label_line(earlier, NOT_FORWARDED, "earlier nilesh", "resale", "out")
    assert counterparties.accept_resale(txns, START, END) == 4
    summary = _summary(txns)
    assert summary["start"] == START.isoformat()
    assert summary["end"] == END.isoformat()
    assert summary["resale"]["out"] == FORWARDS
    assert summary["resale"]["difference"]["not_in_week"] == NOT_FORWARDED
    assert "not in this week's bank lines" in summary["resale"]["note"]


def test_commission_is_not_repeated_on_a_quiet_week(monkeypatch):
    monkeypatch.setattr(finance_tracker, "_load_sheet_items", lambda: [_commission()])
    summary = _summary([
        _txn(_day(22), "Visa Purchase Woolworths Sydney", debit=40),
    ])
    assert summary["resale"]["count"] == 0
    assert summary["resale"]["income"] == 0
    assert summary["resale"]["note"] is None
    assert summary["income"] == []


def test_a_commission_dated_in_another_week_is_not_this_weeks_income(monkeypatch):
    older = _commission(sale_date=datetime.datetime(2026, 9, 1))
    summary = _labelled_week(monkeypatch, [older])
    assert summary["resale"]["out"] == FORWARDS
    assert summary["resale"]["income"] == 0
    assert summary["resale"]["note"] is None
    assert summary["income"] == []


def test_a_dated_commission_in_the_window_counts_without_a_sale_price_gap(monkeypatch):
    """The row records only the commission. Do not invent the stock total."""
    row = _commission(
        sale_date=datetime.datetime(2026, 10, 25),
        sale_price=COMMISSION,
        sheet_net=COMMISSION,
    )
    summary = _labelled_week(monkeypatch, [row])
    assert summary["resale"]["income"] == COMMISSION
    assert summary["resale"]["difference"]["sold"] is None
    assert summary["resale"]["difference"]["due"] is None
    note = summary["resale"]["note"]
    assert "Sheet commission $1,387.00" in note
    assert "This week's bank lines net to $381.37" in note
    assert "is not in this week's bank lines." in note
    assert "due to" not in note
    assert "owed" not in note.lower()
