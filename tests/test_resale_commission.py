"""Commission income is the reselling sheet, counted once.

For the Nilesh stock, the sheet row is 'banga selling commission': sold
$13,870, commission $1,387, so $12,483 is due. The bank deposits and
forwards are transfers. They are not the commission and not spending.
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
from test_passthrough import FORWARDS, _day, _summary, _txn

COMMISSION = 1387.0
SOLD = 13870.0
DUE = 12483.0


def _commission(sale_date=datetime.datetime(2026, 10, 25), sale_price=SOLD,
                sheet_net=COMMISSION, name="banga selling commission"):
    return Item(
        status="sold",
        category="Other",
        name=name,
        cost=0,
        sale_price=sale_price,
        sheet_net=sheet_net,
        sale_date=sale_date,
    )


def _resale_week():
    return [
        _txn(_day(26), "Osko Deposit 26Oct Buyer F40", credit=11374.37),
        _txn(_day(26), "Osko Withdrawal 26Oct09:19 F40S Nilesh Banga", debit=4932),
        _txn(_day(25), "Osko Withdrawal 25Oct13:01 Ferraris Nilesh Banga", debit=4061),
        _txn(_day(25), "Osko Withdrawal 25Oct13:03 American Nilesh Banga", debit=2000),
        _txn(_day(22), "Visa Purchase Woolworths Sydney", debit=40),
    ]


def test_sheet_commission_is_the_income(monkeypatch):
    monkeypatch.setattr(finance_tracker, "_load_sheet_items", lambda: [_commission()])
    summary = _summary(_resale_week())
    assert summary["total_spend"] == 40
    assert summary["income"] == []
    assert summary["commission"]["total"] == COMMISSION
    row = summary["commission"]["rows"][0]
    assert row["sold"] == SOLD
    assert row["due"] == DUE
    assert row["party"] == "banga"
    assert summary["transfers"]["total_out"] == FORWARDS


def test_a_commission_dated_in_another_week_is_not_this_weeks_income(monkeypatch):
    older = _commission(sale_date=datetime.datetime(2026, 9, 1))
    monkeypatch.setattr(finance_tracker, "_load_sheet_items", lambda: [older])
    summary = _summary(_resale_week())
    assert summary["commission"] == {"total": 0, "rows": []}


def test_an_undated_commission_is_not_tied_to_any_week(monkeypatch):
    undated = _commission(sale_date=None)
    monkeypatch.setattr(finance_tracker, "_load_sheet_items", lambda: [undated])
    summary = _summary(_resale_week())
    assert summary["commission"]["total"] == 0


def test_a_commission_without_a_sale_price_gap_has_no_due(monkeypatch):
    row = _commission(sale_price=COMMISSION)
    monkeypatch.setattr(finance_tracker, "_load_sheet_items", lambda: [row])
    summary = _summary(_resale_week())
    assert summary["commission"]["total"] == COMMISSION
    assert summary["commission"]["rows"][0]["sold"] is None
    assert summary["commission"]["rows"][0]["due"] is None


def test_an_unreadable_sheet_leaves_commission_unknown(monkeypatch):
    monkeypatch.setattr(finance_tracker, "_load_sheet_items", lambda: None)
    summary = _summary(_resale_week())
    assert summary["commission"] == {"total": None, "rows": []}
    assert summary["total_spend"] == 40
