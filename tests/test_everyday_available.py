"""Everyday goal balance uses the available figure when the export has one.

The classic St George file (Date, Description, Debit, Credit, Balance) still
uses the posted ledger balance. Savings and Revolut are unchanged.
"""

import csv
import datetime
import os

os.environ.setdefault("YOUR_EMAIL", "test@example.com")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("HEVY_API_KEY", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")

import finance_tracker
from test_finance_routes import AUTH, _client, _dmy, _patch_files

OCT25 = datetime.date(2026, 10, 25)
OCT26 = datetime.date(2026, 10, 26)


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_classic_csv_still_uses_the_ledger_balance(tmp_path, monkeypatch):
    everyday, savings = _patch_files(monkeypatch, tmp_path)
    _write(everyday, (
        "Date,Description,Debit,Credit,Balance\n"
        "26/10/2026,Visa Purchase WOOLWORTHS,12.00,,5932.03,\n"
        "25/10/2026,Salary,,100.00,5944.03\n"
    ))
    _write(savings, (
        "Date,Description,Debit,Credit,Balance\n"
        f"{_dmy(OCT25)},Interest,,1.00,100.00\n"
    ))
    breakdown = finance_tracker.analyse_savings()["breakdown"]
    assert breakdown["everyday"] == 5932.03
    assert breakdown["everyday_ledger"] == 5932.03
    assert breakdown["everyday_source"] == "ledger"
    assert breakdown["savings"] == 100
    assert breakdown["cash"] == 6032.03
    text = "\n".join(finance_tracker.savings_breakdown_lines(breakdown))
    assert "everyday $5,932.03" in text
    assert "available" not in text


def test_available_column_is_the_everyday_figure_only(tmp_path, monkeypatch):
    """Savings keeps its ledger balance even if that file has the column too."""
    everyday, savings = _patch_files(monkeypatch, tmp_path)
    _write(everyday, (
        "Date,Description,Debit,Credit,Balance,Available\n"
        "26/10/2026,Visa Purchase WOOLWORTHS,12.00,,5932.03,5800.00\n"
        "25/10/2026,Salary,,100.00,5944.03,5812.00\n"
    ))
    _write(savings, (
        "Date,Description,Debit,Credit,Balance,Available\n"
        "25/10/2026,Interest,,1.00,1000.00,50.00\n"
    ))
    _write_revolut(tmp_path / "revolut.csv", "200.00")
    monkeypatch.delenv("JARVIS_REVOLUT_FRIEND_SHARE", raising=False)

    breakdown = finance_tracker.analyse_savings()["breakdown"]
    assert breakdown["everyday"] == 5800
    assert breakdown["everyday_ledger"] == 5932.03
    assert breakdown["everyday_source"] == "available"
    assert breakdown["savings"] == 1000
    assert breakdown["revolut_balance"] == 200
    assert breakdown["revolut_own"] == 200
    assert breakdown["cash"] == 7000
    assert breakdown["total"] == 7000
    text = "\n".join(finance_tracker.savings_breakdown_lines(breakdown))
    assert "everyday $5,800.00 available (ledger $5,932.03)" in text

    body = _client().get("/finance/savings", headers=AUTH).get_json()
    assert body["breakdown"]["everyday"] == 5800
    assert body["breakdown"]["everyday_ledger"] == 5932.03
    assert body["breakdown"]["everyday_source"] == "available"
    assert body["breakdown"]["savings"] == 1000
    assert body["balance"] == 7000


def test_preamble_and_avail_header_are_accepted(tmp_path, monkeypatch):
    everyday, savings = _patch_files(monkeypatch, tmp_path)
    _write(savings, "Date,Description,Debit,Credit,Balance\n")
    _write(everyday, (
        'Avail,"$5,800.00"\n'
        "Current Balance,\"$5,932.03\"\n"
        "Date,Description,Debit,Credit,Balance,Avail Bal\n"
        "25/10/2026,Salary,,100.00,5944.03,5812.00\n"
        "26/10/2026,Visa Purchase WOOLWORTHS,12.00,,5932.03,5790.00\n"
    ))
    # The account line wins over the column. Newest row is 26 Oct.
    breakdown = finance_tracker.analyse_savings()["breakdown"]
    assert breakdown["everyday"] == 5800
    assert breakdown["everyday_ledger"] == 5932.03
    assert breakdown["everyday_source"] == "available"


def test_older_available_cell_is_not_used(tmp_path, monkeypatch):
    everyday, _savings = _patch_files(monkeypatch, tmp_path)
    _write(everyday, (
        "Date,Description,Debit,Credit,Balance,Available\n"
        "26/10/2026,Visa Purchase WOOLWORTHS,12.00,,5932.03,\n"
        "25/10/2026,Salary,,100.00,5944.03,100.00\n"
    ))
    breakdown = finance_tracker.analyse_savings()["breakdown"]
    assert breakdown["everyday"] == 5932.03
    assert breakdown["everyday_source"] == "ledger"


def test_pending_debits_reduce_the_ledger_when_no_available_column(tmp_path, monkeypatch):
    everyday, _savings = _patch_files(monkeypatch, tmp_path)
    _write(everyday, (
        "Date,Description,Debit,Credit,Balance,Status\n"
        "26/10/2026,PENDING Visa Purchase JB HI FI,132.03,,5932.03,Pending\n"
        "26/10/2026,PENDING Visa Refund WOOLWORTHS,,50.00,5932.03,Authorised\n"
        "25/10/2026,Visa Purchase COLES,10.00,,5932.03,Posted\n"
    ))
    breakdown = finance_tracker.analyse_savings()["breakdown"]
    assert breakdown["everyday_ledger"] == 5932.03
    assert breakdown["everyday"] == 5800
    assert breakdown["everyday_source"] == "pending"

    txns = finance_tracker.parse_stgeorge_csv(everyday)
    summary = finance_tracker.summarise_spending(
        txns, OCT25, OCT26, 75, extra_inflows=[],
    )
    assert summary["total_spend"] == 10
    assert summary["transfers"]["count"] == 0


def test_posted_twin_is_not_subtracted_again_or_counted_twice(tmp_path, monkeypatch):
    everyday, _savings = _patch_files(monkeypatch, tmp_path)
    _write(everyday, (
        "Date,Description,Debit,Credit,Balance,Status\n"
        "26/10/2026,PENDING Visa Purchase WOOLWORTHS,80.00,,5932.03,Pending\n"
        "26/10/2026,Visa Purchase WOOLWORTHS,80.00,,5852.03,Posted\n"
        "25/10/2026,Visa Purchase COLES,10.00,,5932.03,Posted\n"
    ))
    breakdown = finance_tracker.analyse_savings()["breakdown"]
    assert breakdown["everyday"] == 5852.03
    assert breakdown["everyday_source"] == "ledger"
    summary = finance_tracker.summarise_spending(
        finance_tracker.parse_stgeorge_csv(everyday), OCT25, OCT26, 75, extra_inflows=[],
    )
    assert summary["total_spend"] == 90
    assert summary["by_category"]["Food & dining"] == 90


def test_available_column_wins_over_pending_rows(tmp_path, monkeypatch):
    everyday, _savings = _patch_files(monkeypatch, tmp_path)
    _write(everyday, (
        "Date,Description,Debit,Credit,Balance,Available,Status\n"
        "26/10/2026,PENDING Visa Purchase JB HI FI,132.03,,5932.03,5700.00,Pending\n"
        "25/10/2026,Salary,,100.00,5932.03,5832.03,Posted\n"
    ))
    breakdown = finance_tracker.analyse_savings()["breakdown"]
    assert breakdown["everyday"] == 5700
    assert breakdown["everyday_ledger"] == 5932.03
    assert breakdown["everyday_source"] == "available"


def test_description_prefix_marks_pending_without_a_status_column(tmp_path, monkeypatch):
    everyday, _savings = _patch_files(monkeypatch, tmp_path)
    _write(everyday, (
        "Date,Description,Debit,Credit,Balance\n"
        "26/10/2026,PENDING Visa Purchase JB HI FI,132.03,,5932.03\n"
        "25/10/2026,Visa Purchase COLES,10.00,,5932.03\n"
    ))
    breakdown = finance_tracker.analyse_savings()["breakdown"]
    assert breakdown["everyday"] == 5800
    assert breakdown["everyday_source"] == "pending"


def _write_revolut(path, balance, stamp="2026-10-17 12:00:00"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow([
            "State", "Type", "Completed Date", "Description",
            "Amount", "Fee", "Currency", "Balance",
        ])
        writer.writerow([
            "COMPLETED", "TOPUP", stamp, "Balance", balance, "0", "AUD", balance,
        ])
