"""Fronted and repaid entries: only my own share counts as spending.

kind "fronted" is the others' share of something I paid for; it comes off
my spending. kind "repaid" is money back; it clears what is owed and the
matching transfer in is never income. Entries without a kind are spends.
All names, amounts, and dates here are made up.
"""

import datetime
import json
import os

os.environ.setdefault("YOUR_EMAIL", "test@example.com")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("HEVY_API_KEY", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")

import pytest

import agent_api
import finance_tracker
import live_spend
import term_context

START = datetime.date(2030, 3, 4)
END = datetime.date(2030, 3, 10)
TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(autouse=True)
def _no_term_file(tmp_path, monkeypatch):
    """No income payers unless a test writes them."""
    monkeypatch.setattr(term_context, "CONTEXT_FILE", tmp_path / "term_context.json")


def _day(n):
    return datetime.date(2030, 3, n)


def _txn(day, description, debit=0, credit=0):
    return {
        "date": day,
        "description": description,
        "debit": float(debit or 0),
        "credit": float(credit or 0),
        "balance": 0.0,
        "category": finance_tracker.categorise(description),
    }


def _log(day, amount, category="Food & dining", note="", kind=None):
    """Append one Back Tap line. kind None writes an old-style line with no kind."""
    stamp = live_spend.TIMEZONE.localize(datetime.datetime.combine(day, datetime.time(13, 0)))
    row = {"ts": stamp.isoformat(timespec="seconds"), "amount": amount,
           "category": category, "note": note}
    if kind is not None:
        row["kind"] = kind
    with open(live_spend.SPEND_FILE, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")


def _summary(txns, start=START, end=END):
    return finance_tracker.summarise_spending(txns, start, end, 75)


DINNER = "Visa Purchase 05Mar Woolworths Sydney"


# ── Kinds on the logger ──────────────────────────────────────────────────────

def test_kind_defaults_to_spend_and_is_validated():
    assert live_spend.normalise_spend(5, "Other")["kind"] == "spend"
    assert live_spend.normalise_spend(5, "Other", kind=" Fronted ")["kind"] == "fronted"
    repaid = live_spend.normalise_spend(5, "", kind="repaid")
    assert repaid["category"] == "Other"
    for bad in ("refund", 3, "income"):
        with pytest.raises(ValueError):
            live_spend.normalise_spend(5, "Other", kind=bad)
    with pytest.raises(ValueError):
        live_spend.normalise_spend(5, "", kind="fronted")  # fronted needs its category


def test_old_entries_without_kind_still_count_as_spending():
    _log(_day(5), 12.5, "Transport", "bus top-up")
    summary = _summary([])
    assert summary["total_spend"] == 12.5
    assert summary["by_category"] == {"Transport": 12.5}
    assert summary["logged"] == {"count": 1, "total": 12.5}
    assert summary["reimbursements"]["outstanding"] == 0
    assert summary["reimbursements"]["items"] == []


def test_explicit_spend_is_the_same_as_no_kind():
    _log(_day(5), 12.5, "Transport", kind="spend")
    assert _summary([])["total_spend"] == 12.5


# ── Fronted ──────────────────────────────────────────────────────────────────

def test_fronted_share_comes_off_my_spending():
    _log(_day(5), 60, "Food & dining", "dinner, two others' share", kind="fronted")
    summary = _summary([_txn(_day(5), DINNER, debit=90)])
    assert summary["total_spend"] == 30
    assert summary["by_category"] == {"Food & dining": 30}
    assert summary["logged"] == {"count": 0, "total": 0.0}
    r = summary["reimbursements"]
    assert r["fronted_total"] == 60
    assert r["repaid_total"] == 0
    assert r["outstanding"] == 60
    assert r["not_deducted"] == 0
    assert r["items"] == [{"date": "2030-03-05", "kind": "fronted", "amount": 60.0,
                           "category": "Food & dining", "note": "dinner, two others' share"}]
    assert [(o["amount"], o["remaining"]) for o in r["open"]] == [(60.0, 60.0)]


def test_fronted_logged_spend_nets_to_my_share():
    # A transfer purchase: log the whole thing as a spend, the others' part as fronted.
    _log(_day(6), 120, "Entertainment", "tickets for three", kind="spend")
    _log(_day(6), 80, "Entertainment", "tickets, two others", kind="fronted")
    summary = _summary([])
    assert summary["total_spend"] == 40
    assert summary["by_category"] == {"Entertainment": 40}


def test_fronted_in_another_category_still_lowers_the_total():
    _log(_day(5), 20, "Shopping", kind="fronted")
    summary = _summary([_txn(_day(5), DINNER, debit=50)])
    assert summary["total_spend"] == 30
    assert summary["by_category"] == {"Food & dining": 30}


def test_fronted_with_nothing_to_offset_never_goes_negative():
    _log(_day(5), 40, "Food & dining", kind="fronted")
    summary = _summary([_txn(_day(6), DINNER, debit=15)])
    assert summary["total_spend"] == 0
    assert summary["by_category"] == {}
    assert summary["reimbursements"]["not_deducted"] == 25
    assert summary["reimbursements"]["outstanding"] == 40


# ── Repaid ───────────────────────────────────────────────────────────────────

def test_repaid_clears_the_outstanding_balance():
    _log(_day(5), 60, kind="fronted")
    _log(_day(7), 60, "", "paid back", kind="repaid")
    summary = _summary([_txn(_day(5), DINNER, debit=90),
                        _txn(_day(7), "Osko Deposit 07Mar Alex Example", credit=60)])
    assert summary["total_spend"] == 30
    r = summary["reimbursements"]
    assert (r["fronted_total"], r["repaid_total"], r["outstanding"]) == (60, 60, 0)
    assert r["open"] == []
    assert summary["income"] == []
    repaid = [t for t in summary["transfers"]["items"] if t.get("repaid")]
    assert [(t["amount"], t["direction"]) for t in repaid] == [(60.0, "in")]


def test_partial_repayment_leaves_the_rest_outstanding():
    _log(_day(5), 60, kind="fronted")
    _log(_day(6), 25, kind="repaid")
    r = _summary([])["reimbursements"]
    assert r["outstanding"] == 35
    assert [(o["amount"], o["remaining"]) for o in r["open"]] == [(60.0, 35.0)]


def test_repayment_pays_off_the_matching_fronted_amount_first():
    _log(_day(4), 30, kind="fronted")
    _log(_day(5), 45, kind="fronted")
    _log(_day(8), 45, kind="repaid")
    r = _summary([])["reimbursements"]
    assert r["outstanding"] == 30
    assert [(o["amount"], o["remaining"]) for o in r["open"]] == [(30.0, 30.0)]


def test_repayment_in_a_later_week_clears_an_earlier_fronted_amount():
    _log(_day(5), 60, kind="fronted")
    _log(_day(13), 60, kind="repaid")
    first = _summary([])["reimbursements"]
    assert first["outstanding"] == 60
    second = _summary([], start=_day(11), end=_day(17))["reimbursements"]
    assert (second["fronted_total"], second["repaid_total"], second["outstanding"]) == (0, 60, 0)
    assert [i["kind"] for i in second["items"]] == ["repaid"]


def test_logged_repayment_from_a_listed_payer_is_not_income(tmp_path):
    (tmp_path / "term_context.json").write_text(json.dumps({
        "spending": {"income_payers": ["Example Tutoring Pay"]},
    }))
    pay = "Osko Deposit 06Mar Example Tutoring Pay Wages"
    back = "Osko Deposit 08Mar Example Tutoring Pay Dinner Split"
    txns = [_txn(_day(6), pay, credit=500), _txn(_day(8), back, credit=60)]
    before = _summary(txns)
    assert sorted(row["amount"] for row in before["income"]) == [60.0, 500.0]

    _log(_day(9), 60, kind="repaid")  # logged a day after it landed
    after = _summary(txns)
    assert [row["amount"] for row in after["income"]] == [500.0]
    repaid = [t for t in after["transfers"]["items"] if t.get("repaid")]
    assert [(t["date"], t["amount"]) for t in repaid] == [("2030-03-08", 60.0)]
    assert after["total_spend"] == 0


def test_repayment_with_no_matching_transfer_leaves_income_alone(tmp_path):
    (tmp_path / "term_context.json").write_text(json.dumps({
        "spending": {"income_payers": ["Example Tutoring Pay"]},
    }))
    txns = [_txn(_day(6), "Osko Deposit 06Mar Example Tutoring Pay Wages", credit=500)]
    _log(_day(6), 60, kind="repaid")
    summary = _summary(txns)
    assert [row["amount"] for row in summary["income"]] == [500.0]
    assert summary["reimbursements"]["outstanding"] == -60


def test_analyse_spending_uses_the_same_fronted_rule():
    today = datetime.datetime.now(finance_tracker.TIMEZONE).date()
    _log(today, 25, kind="fronted")
    result = finance_tracker.analyse_spending([_txn(today, DINNER, debit=40)], days=7)
    assert result["total_spend"] == 15
    assert result["category_totals"] == {"Food & dining": 15}


# ── HTTP: POST /spend and the Back Tap server ───────────────────────────────

def _api(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_api, "AUDIT_FILE", tmp_path / "agent_api_audit.jsonl")
    return agent_api.create_app(token=TOKEN).test_client()


def test_post_spend_kinds_and_unchanged_spend_reply(tmp_path, monkeypatch):
    client = _api(tmp_path, monkeypatch)
    spend = client.post("/spend", headers=AUTH, json={"amount": 4, "category": "Other", "note": "bus"})
    assert spend.status_code == 200
    body = spend.get_json()
    assert body["logged"] == "$4.00 Other"
    assert body["week_total"] == "$4.00"
    assert "kind" not in body and "outstanding" not in body
    assert body["entry"]["kind"] == "spend"

    fronted = client.post("/spend", headers=AUTH, json={
        "amount": 60, "category": "Food & dining", "note": "dinner", "kind": "fronted"})
    assert fronted.status_code == 200
    body = fronted.get_json()
    assert body["logged"] == "$60.00 Food & dining (fronted)"
    assert body["week_total"] == "$4.00"
    assert (body["kind"], body["outstanding"]) == ("fronted", "$60.00")

    repaid = client.post("/spend", headers=AUTH, json={"amount": 60, "kind": "repaid"})
    assert repaid.status_code == 200
    assert repaid.get_json()["logged"] == "$60.00 paid back"
    assert repaid.get_json()["outstanding"] == "$0.00"

    lines = [json.loads(line) for line in live_spend.SPEND_FILE.read_text().splitlines()]
    assert [line["kind"] for line in lines] == ["spend", "fronted", "repaid"]
    audit = [json.loads(line) for line in (tmp_path / "agent_api_audit.jsonl").read_text().splitlines()]
    assert [row["target"]["kind"] for row in audit] == ["spend", "fronted", "repaid"]

    assert client.post("/spend", headers=AUTH, json={
        "amount": 5, "category": "Other", "kind": "income"}).status_code == 400
    assert client.post("/spend", headers=AUTH, json={
        "amount": 5, "category": "Other", "kind": None}).status_code == 400


def test_idempotency_key_covers_kind(tmp_path, monkeypatch):
    client = _api(tmp_path, monkeypatch)
    headers = {**AUTH, "Idempotency-Key": "split-001"}
    body = {"amount": 30, "category": "Food & dining", "kind": "fronted"}
    first = client.post("/spend", headers=headers, json=body)
    again = client.post("/spend", headers=headers, json=body)
    assert first.status_code == again.status_code == 200
    assert again.get_json() == first.get_json()
    clash = client.post("/spend", headers=headers, json={**body, "kind": "spend"})
    assert clash.status_code == 409
    assert len(live_spend.SPEND_FILE.read_text().splitlines()) == 1

    # Without a key, the same amount as a different kind is a different entry.
    client.post("/spend", headers=AUTH, json={"amount": 9, "category": "Other"})
    client.post("/spend", headers=AUTH, json={"amount": 9, "category": "Other", "kind": "fronted"})
    assert len(live_spend.SPEND_FILE.read_text().splitlines()) == 3


def test_old_audit_rows_without_kind_replay_as_spends(tmp_path, monkeypatch):
    client = _api(tmp_path, monkeypatch)
    old = {
        "ts": datetime.datetime.now(agent_api.TIMEZONE).isoformat(timespec="seconds"),
        "actor": "owner", "action": "spend",
        "target": {"amount": 4.0, "category": "Other", "note": "bus"},
        "idempotency_key": "old-001", "response": {"ok": True, "logged": "$4.00 Other"},
    }
    (tmp_path / "agent_api_audit.jsonl").write_text(json.dumps(old) + "\n")
    headers = {**AUTH, "Idempotency-Key": "old-001"}
    replay = client.post("/spend", headers=headers, json={"amount": 4, "category": "Other", "note": "bus"})
    assert replay.get_json() == {"ok": True, "logged": "$4.00 Other"}
    clash = client.post("/spend", headers=headers, json={
        "amount": 4, "category": "Other", "note": "bus", "kind": "fronted"})
    assert clash.status_code == 409


def test_back_tap_server_accepts_kind_and_old_payloads(tmp_path, monkeypatch):
    monkeypatch.setattr(live_spend, "TOKEN_FILE", tmp_path / ".spend_token")
    secret = "s" * 32
    (tmp_path / ".spend_token").write_text(secret + "\n")
    client = live_spend.create_app().test_client()
    headers = {"X-Jarvis-Token": secret}

    old = client.post("/spend", headers=headers, json={"amount": 14.5, "category": "Food & dining"})
    assert old.get_json() == {"ok": True, "logged": "$14.50 Food & dining", "week_total": "$14.50"}

    fronted = client.post("/spend", headers=headers, json={
        "amount": 20, "category": "Food & dining", "kind": "fronted"})
    assert fronted.get_json()["outstanding"] == "$20.00"
    assert fronted.get_json()["week_total"] == "$14.50"

    bad = client.post("/spend", headers=headers, json={"amount": 5, "category": "Other", "kind": "x"})
    assert bad.status_code == 400
