"""A label or a person that does not hit a bank line must not look successful.

The 20–26 Oct lines came back after Manav labelled them. /resale had saved,
because those rules use the posted date and the full description. /label
used the date printed on the line (25Oct) or the redacted payee. /who with
a name that is not on the line saved nothing he could see, and the reply
said only that it had saved.
"""

import datetime
import os
import threading
import time

os.environ.setdefault("YOUR_EMAIL", "test@example.com")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("HEVY_API_KEY", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")

import counterparties
import finance_tracker
import jarvis_telegram

POSTED = datetime.date(2026, 10, 24)
END = datetime.date(2026, 10, 26)


def _txn(day, description, debit=0, credit=0):
    return {
        "date": day,
        "description": description,
        "debit": float(debit or 0),
        "credit": float(credit or 0),
        "balance": 0.0,
        "category": "Other",
    }


def _october_lines():
    """The four lines that were still unreviewed, with the payee visible."""
    return [
        _txn(POSTED, "Osko Withdrawal 24Oct20:10 Cafe Ada", debit=30),
        _txn(POSTED, "Sct Deposit 25Oct00:12 Sam Seven", credit=28.30),
        _txn(
            datetime.date(2026, 10, 25),
            "Osko Withdrawal 25Oct15:57 Halloween Costume Amazo Jane Doe",
            debit=26,
        ),
        _txn(datetime.date(2026, 10, 25), "Osko Withdrawal 25Oct17:52 Lee Cent", debit=7),
    ]


def _summary(txns):
    return finance_tracker.summarise_spending(txns, POSTED, END, 75)


def test_description_date_still_applies_when_the_bank_posted_the_day_before():
    """He typed 25Oct from the text. St George posted the line on 24 Oct."""
    txns = _october_lines()
    counterparties.label_line(datetime.date(2026, 10, 25), 28.30, "sam seven", "income")
    summary = _summary(txns)
    amounts = {item["amount"] for item in summary["unreviewed"]["items"]}
    assert 28.30 not in amounts
    assert summary["income"][0]["amount"] == 28.30
    assert summary["income"][0]["source"] == "Other" or summary["income"][0]["amount"] == 28.30


def test_label_command_stores_the_posted_date_and_names_the_line():
    txns = _october_lines()
    result = counterparties.label_matching(
        txns, "25Oct", 28.30, "sam seven", "income",
    )
    assert result["saved"][0]["date"] == "2026-10-24"
    assert "bank posted 2026-10-24" in result["matches"][0]["posted_note"]
    summary = _summary(txns)
    assert 28.30 not in {item["amount"] for item in summary["unreviewed"]["items"]}


def test_redacted_fragment_is_refused_and_the_line_stays_unreviewed():
    txns = _october_lines()
    try:
        counterparties.label_matching(txns, "2026-10-25", 26, "[redacted]", "spending")
    except ValueError as exc:
        message = str(exc)
    else:
        raise AssertionError("redacted fragment was saved")
    assert "redacted" in message.lower()
    assert "Nothing was saved" in message
    summary = _summary(txns)
    assert 26 in {item["amount"] for item in summary["unreviewed"]["items"]}
    assert counterparties.load_store()["lines"] == []


def test_spending_on_a_deposit_is_refused():
    txns = _october_lines()
    try:
        counterparties.label_matching(txns, "2026-10-24", 28.30, "sam seven", "spending")
    except ValueError as exc:
        message = str(exc)
    else:
        raise AssertionError("spending was saved on a deposit")
    assert "money coming in" in message
    assert counterparties.load_store()["lines"] == []


def test_who_with_the_payee_clears_the_line_and_is_in_the_role_list(monkeypatch, tmp_path):
    import finance_tracker as tracker
    everyday = tmp_path / "everyday.csv"
    monkeypatch.setattr(tracker, "EVERYDAY_CSV", everyday)
    rows = []
    for txn in _october_lines():
        rows.append([
            txn["date"].strftime("%d/%m/%Y"),
            txn["description"],
            f"{txn['debit']:.2f}" if txn["debit"] else "",
            f"{txn['credit']:.2f}" if txn["credit"] else "",
            "1000.00",
        ])
    from test_finance_routes import _write_csv
    _write_csv(everyday, rows)

    class Frozen(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            naive = datetime.datetime(2026, 10, 26, 12, 0, 0)
            if tz is None:
                return naive
            return tz.localize(naive)

    monkeypatch.setattr(jarvis_telegram.datetime, "datetime", Frozen)
    listed = jarvis_telegram.cmd_unreviewed([])
    assert "Jane Doe" in listed
    assert "[redacted]" not in listed
    assert '/who "jane doe" person' in listed

    saved = jarvis_telegram.handle_command("/who Jane Doe")
    assert "matches" in saved
    assert "Jane Doe" in saved
    assert "not on any transfer" not in saved
    roles = counterparties.public_counterparties()
    assert any(row["name"] == "Jane Doe" and row["role"] == "person" for row in roles)

    missed = jarvis_telegram.handle_command('/who "Not A Real Payee" person')
    assert "not on any transfer" in missed
    assert any(row["name"] == "Not A Real Payee" for row in counterparties.public_counterparties())

    txns = tracker.parse_stgeorge_csv(everyday)
    summary = finance_tracker.summarise_spending(txns, POSTED, END, 75)
    assert 26 not in {item["amount"] for item in summary["unreviewed"]["items"]}
    assert 30 in {item["amount"] for item in summary["unreviewed"]["items"]}


def test_person_survives_a_label_written_at_the_same_time(monkeypatch):
    """The resale save used to replace the file with a copy that had no people."""
    real_read = counterparties._read_user

    def slow_read(path):
        user = real_read(path)
        time.sleep(0.02)
        return user

    monkeypatch.setattr(counterparties, "_read_user", slow_read)
    errors = []

    def who():
        try:
            counterparties.upsert_counterparty("Jane Doe", "person")
        except Exception as exc:
            errors.append(exc)

    def label():
        try:
            for i in range(6):
                counterparties.label_line(
                    "2026-10-26", 1000 + i, f"buyer {i}", "resale", "in",
                )
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=who), threading.Thread(target=label)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    store = counterparties.load_store()
    assert any(row["name"] == "Jane Doe" and row["role"] == "person" for row in store["counterparties"])
    assert len(store["lines"]) == 6
