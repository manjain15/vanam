"""Resale is a labelled group, not a payment rail.

Buyer deposits for Nilesh's stock land in the everyday account, then the
proceeds minus commission are forwarded. Only lines labelled resale are
kept out of spending. The commission is the reselling sheet, not the
bank leftover.
The $26, $7, and $1.70 payments stay in spending either way.
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
import subscription_audit
import term_context
from test_finance_routes import AUTH, _client, _dmy, _patch_files, _today, _write_csv

START = datetime.date(2026, 10, 20)
END = datetime.date(2026, 10, 26)
FORWARDS = 10993.0
DEPOSITS = 12000.0
CARDS = 189.57
SMALL = 34.70
SPEND = 224.27


def _remember(name, role="person", aliases=None, large_transfers="ask"):
    """Save a counterparty into the isolated store."""
    import counterparties
    counterparties.upsert_counterparty(
        name, role, aliases=aliases, large_transfers=large_transfers,
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


def _cards():
    """Visa purchases that week, summing to $189.57."""
    return [
        _txn(_day(22), "Visa Purchase Woolworths Sydney", debit=100),
        _txn(_day(23), "Visa Purchase Coles Sydney", debit=50),
        _txn(_day(24), "Visa Purchase Uber Trip Sydney", debit=39.57),
    ]


def _small_oskos():
    """Real payments to people. They are not part of the resale."""
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


def _deposits(amounts=(5200, 4300, 2500)):
    """Buyer money that landed before it was forwarded. Default leftover $1,007."""
    labels = ("Buyer F40", "Ferraris Buyer", "American Buyer")
    rows = []
    for day, amount, label in zip((26, 25, 24), amounts, labels):
        rows.append(_txn(
            _day(day), f"Osko Deposit {day}Oct {label}", credit=amount,
        ))
    return rows


def _remember_people():
    _remember("Nilesh Banga")
    _remember("Ada Costume")
    _remember("Sam Seven")
    _remember("Lee Cent")


def _week(amounts=(5200, 4300, 2500)):
    _remember_people()
    return _cards() + _small_oskos() + _forwards() + _deposits(amounts)


def _summary(txns):
    return finance_tracker.summarise_spending(txns, START, END, 75)


def test_unlabelled_resale_stays_unreviewed_and_small_oskos_count():
    summary = _summary(_week())
    assert summary["total_spend"] == SPEND
    assert summary["by_category"]["Food & dining"] == 150
    assert summary["by_category"]["Transport"] == 39.57
    assert summary["by_category"]["Other"] == SMALL
    assert summary["unfinished"] is True
    assert summary["resale"]["in"] == 0.0
    assert summary["resale"]["out"] == 0.0
    assert summary["resale"]["net"] == 0.0
    assert summary["resale"]["count"] == 0
    assert summary["resale"]["income"] is None
    assert summary["income"] == []
    amounts = {(item["amount"], item["direction"]) for item in summary["unreviewed"]["items"]}
    assert amounts == {
        (4932.0, "out"), (4061.0, "out"), (2000.0, "out"),
        (5200.0, "in"), (4300.0, "in"), (2500.0, "in"),
    }
    assert all(item["suggestion"] == "resale" for item in summary["unreviewed"]["items"])
    assert 26 not in {item["amount"] for item in summary["unreviewed"]["items"]}
    assert FORWARDS not in {row["amount"] for row in summary["flagged"]}


def test_accepting_the_suggestion_nets_only_the_resale_lines():
    import counterparties
    txns = _week()
    labelled = counterparties.accept_resale(txns, START, END)
    assert labelled == 6
    summary = _summary(txns)
    assert summary["total_spend"] == SPEND
    assert summary["by_category"]["Other"] == SMALL
    assert summary["unfinished"] is False
    assert summary["unreviewed"]["count"] == 0
    assert summary["resale"]["in"] == DEPOSITS
    assert summary["resale"]["out"] == FORWARDS
    assert summary["resale"]["net"] == 1007
    assert summary["resale"]["income"] is None
    assert summary["income"] == []


def test_resale_leftover_below_zero_is_not_negative_spending():
    import counterparties
    txns = _week(amounts=(4000, 4000, 2000))
    assert counterparties.accept_resale(txns, START, END) == 6
    summary = _summary(txns)
    assert summary["total_spend"] == SPEND
    assert summary["resale"]["net"] == round(10000 - FORWARDS, 2)
    assert summary["resale"]["income"] is None
    assert summary["income"] == []


def test_a_lone_forward_is_unreviewed_and_not_silently_netted():
    _remember("Nilesh Banga")
    _remember("Ada Costume")
    txns = _cards() + [
        _txn(_day(21), "Osko Withdrawal 21Oct Halloween Costume Ada Costume", debit=26),
        _txn(_day(26), "Osko Withdrawal 26Oct09:19 F40S Nilesh Banga", debit=4932),
    ]
    summary = _summary(txns)
    assert summary["total_spend"] == round(CARDS + 26, 2)
    assert summary["unfinished"] is True
    assert summary["resale"]["count"] == 0
    item = summary["unreviewed"]["items"][0]
    assert item["amount"] == 4932
    assert item["suggestion"] is None


def test_a_large_deposit_is_not_personal_income():
    txns = _cards() + [
        _txn(_day(24), "Osko Deposit 24Oct Buyer F40", credit=5200),
    ]
    summary = _summary(txns)
    assert summary["total_spend"] == CARDS
    assert summary["income"] == []
    assert summary["unreviewed"]["items"][0]["amount"] == 5200
    assert summary["unreviewed"]["items"][0]["suggestion"] is None


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
    assert summary["unfinished"] is True
    assert summary["unreviewed"]["total_in"] == 8000


def test_partial_reimbursement_does_not_need_matching_amounts():
    _remember("R Jaiswal", aliases=["rishi jaiswal"])
    day = datetime.date(2026, 9, 22)
    txns = [
        _txn(day, "Osko Withdrawal 22Sep18:35 Knockout R Jaiswal", debit=120),
        _txn(datetime.date(2026, 9, 23), "Sct Deposit 23Sep09:11 Rishi Jaiswal", credit=50),
    ]
    summary = finance_tracker.summarise_spending(txns, day, datetime.date(2026, 9, 28), 75)
    assert summary["total_spend"] == 70
    assert summary["income"] == []


def test_a_credit_larger_than_the_debit_is_income():
    _remember("R Jaiswal", aliases=["rishi jaiswal"])
    day = datetime.date(2026, 9, 22)
    txns = [
        _txn(day, "Osko Withdrawal 22Sep18:35 Knockout R Jaiswal", debit=120),
        _txn(datetime.date(2026, 9, 23), "Sct Deposit 23Sep09:11 Rishi Jaiswal", credit=200),
    ]
    summary = finance_tracker.summarise_spending(txns, day, datetime.date(2026, 9, 28), 75)
    assert summary["total_spend"] == 0
    assert summary["income"][0]["amount"] == 80
    assert summary["income"][0]["source"] == "R Jaiswal"


def test_a_small_gift_with_nothing_to_reimburse_is_income():
    _remember("Nilesh Banga")
    day = datetime.date(2026, 10, 20)
    txns = [_txn(day, "Osko Deposit 20Oct Nilesh Banga", credit=40)]
    summary = finance_tracker.summarise_spending(txns, day, END, 75)
    assert summary["total_spend"] == 0
    assert summary["unfinished"] is False
    assert summary["income"][0]["amount"] == 40


def test_a_large_unlabelled_credit_is_unreviewed():
    _remember("Nilesh Banga")
    day = datetime.date(2026, 10, 20)
    txns = [_txn(day, "Osko Deposit 20Oct Nilesh Banga", credit=900)]
    summary = finance_tracker.summarise_spending(txns, day, END, 75)
    assert summary["income"] == []
    assert summary["total_spend"] == 0
    assert summary["unfinished"] is True
    assert summary["unreviewed"]["items"][0]["amount"] == 900


def test_large_transfers_can_be_marked_as_ordinary_spending():
    _remember("S Ahuja", large_transfers="spending")
    day = datetime.date(2026, 10, 20)
    txns = [_txn(day, "Osko Withdrawal 20Oct Rent S Ahuja", debit=903.50)]
    summary = finance_tracker.summarise_spending(txns, day, END, 75)
    assert summary["total_spend"] == 903.50
    assert summary["unreviewed"]["count"] == 0


def test_repeating_person_is_not_a_subscription(tmp_path, monkeypatch):
    everyday = tmp_path / "everyday.csv"
    monkeypatch.setattr(finance_tracker, "EVERYDAY_CSV", everyday)
    monkeypatch.setattr(term_context, "CONTEXT_FILE", tmp_path / "term_context.json")
    _remember("S Ahuja")
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
    assert review["monthly_review"] == 0
    assert all("ahuja" not in item["merchant"] for item in review["known"])

    txns = finance_tracker.parse_stgeorge_csv(everyday)
    summary = finance_tracker.summarise_spending(
        txns, today - datetime.timedelta(days=6), today, 75,
    )
    assert summary["total_spend"] == 11.99
    assert summary["unreviewed"]["total_out"] == 903.50


def test_finance_routes_label_resale_in_one_action(tmp_path, monkeypatch):
    everyday, _savings = _patch_files(monkeypatch, tmp_path)
    _remember_people()
    rows = []
    for txn in _week():
        rows.append([
            _dmy(txn["date"]), txn["description"],
            f"{txn['debit']:.2f}" if txn["debit"] else "",
            f"{txn['credit']:.2f}" if txn["credit"] else "",
            "1000.00",
        ])
    _write_csv(everyday, rows)
    client = _client()
    before = client.get(
        "/finance/spending?start=2026-10-20&end=2026-10-26", headers=AUTH,
    ).get_json()
    assert before["total_spend"] == SPEND
    assert before["unfinished"] is True

    accepted = client.post(
        "/finance/resale?start=2026-10-20&end=2026-10-26", headers=AUTH, json={},
    )
    assert accepted.status_code == 200
    assert accepted.get_json()["labelled"] == 6

    after = client.get(
        "/finance/spending?start=2026-10-20&end=2026-10-26", headers=AUTH,
    ).get_json()
    assert after["total_spend"] == SPEND
    assert after["resale"]["net"] == 1007
    assert after["resale"]["income"] is None
    assert after["unfinished"] is False
    assert "revolut" not in accepted.get_data(as_text=True).lower()

    saved = client.post("/finance/counterparties", headers=AUTH, json={
        "name": "Priya Shah", "role": "person",
    })
    assert saved.status_code == 200
    assert saved.get_json()["role"] == "person"

    labelled = client.post("/finance/lines", headers=AUTH, json={
        "date": "2026-10-21", "amount": 26, "description": "halloween",
        "purpose": "spending",
    })
    assert labelled.status_code == 200
    assert labelled.get_json()["purpose"] == "spending"

    bundle = client.get("/finance?start=2026-10-20&end=2026-10-26", headers=AUTH)
    names = [row["name"] for row in bundle.get_json()["counterparties"]]
    assert "Priya Shah" in names
    assert "Nilesh Banga" in names


def test_telegram_lists_and_accepts_the_same_resale_group(tmp_path, monkeypatch):
    import jarvis_telegram
    everyday = tmp_path / "everyday.csv"
    monkeypatch.setattr(finance_tracker, "EVERYDAY_CSV", everyday)
    today = _today()
    _remember("Nilesh Banga")
    _remember("Ada Costume")
    _write_csv(everyday, [
        [_dmy(today), "Osko Deposit Buyer F40", "", "5200.00", "1000.00"],
        [_dmy(today), "Osko Withdrawal F40S Nilesh Banga", "4932.00", "", "1000.00"],
        [_dmy(today), "Osko Withdrawal Halloween Costume Ada Costume", "26.00", "", "1000.00"],
        [_dmy(today), "Visa Purchase Woolworths Sydney", "10.00", "", "1000.00"],
    ])
    listed = jarvis_telegram.cmd_unreviewed([])
    assert "unfinished" in listed
    assert "5200.00" in listed
    assert "4932.00" in listed
    assert "suggest resale" in listed
    assert "26.00" not in listed

    assert "resale" in jarvis_telegram.cmd_resale([]).lower()
    again = jarvis_telegram.cmd_unreviewed([])
    assert again.startswith("Nothing unreviewed")

    who = jarvis_telegram.handle_command('/who "Sam Seven" person')
    assert "Sam Seven" in who
    assert "not on any transfer" in who
    labelled = jarvis_telegram.handle_command(
        f"/label {today.isoformat()} 26 halloween spending"
    )
    assert "Labelled 1 line" in labelled
    assert "Ada Costume" in labelled
