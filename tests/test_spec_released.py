"""Spec-released gate: the planner only schedules an assessment inside its lead window.

A 20% assignment is inside the window at 14 days, not at 20. Until spec_released
is true, an assignment is not a boost, a due note, a fallback task, or an alert.
Older term records that omit the field still load.
"""

import datetime
import json
import os

os.environ.setdefault("YOUR_EMAIL", "test@example.com")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("HEVY_API_KEY", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")

import day_plan
import study_tracker as st
import term_context

TODAY = datetime.date(2026, 10, 22)  # 20 days before 2026-11-11


def _install(tmp_path, monkeypatch, assessments):
    """Point term context at a temp file and keep the task writer offline."""
    path = tmp_path / "term_context.json"
    path.write_text(json.dumps({"subjects": [{
        "code": "MATH2931",
        "name": "Higher Linear Models",
        "assessments": assessments,
    }]}))
    monkeypatch.setattr(term_context, "CONTEXT_FILE", path)
    import course_schedule
    monkeypatch.setattr(course_schedule, "_current_week", lambda: 5)
    monkeypatch.setattr(
        course_schedule, "get_week_topics",
        lambda week, code=None: [{"topics": ["Categorical Features"], "activities": []}],
    )
    monkeypatch.setattr(day_plan, "ANTHROPIC_AVAILABLE", False)
    return path


def _row(due, spec="omit", name="Assignment 2", weight=20):
    row = {"name": name, "due": due, "weight": weight, "status": "pending"}
    if spec != "omit":
        row["spec_released"] = spec
    return row


def _study_task(today):
    start = datetime.datetime.combine(today, datetime.time(9, 0))
    end = start + datetime.timedelta(hours=1)
    ctx = day_plan.course_context(today, ["MATH2931"])
    tasks = day_plan.write_tasks([("MATH2931", "study", start, end)], ctx, [])
    return ctx, tasks


def test_twenty_percent_twenty_days_out_does_not_drive_the_task(tmp_path, monkeypatch):
    # Spec is out, and the date is still outside the 14-day lead window.
    _install(tmp_path, monkeypatch, [_row("2026-11-11", spec=True)])
    ctx, tasks = _study_task(TODAY)
    assert ctx["MATH2931"]["urgent"] is False
    assert ctx["MATH2931"]["urgent_assessments"] == []
    assert not any(" due " in note for note in ctx["MATH2931"]["notes"])
    assert tasks == ["W5 problems"]
    assert day_plan.assessment_boosts(TODAY, ["MATH2931"]) == {}
    assert st.get_assessment_alerts(TODAY) == []


def test_ten_days_out_with_spec_released_drives_the_task(tmp_path, monkeypatch):
    _install(tmp_path, monkeypatch, [_row("2026-11-01", spec=True)])
    ctx, tasks = _study_task(TODAY)
    assert ctx["MATH2931"]["urgent"] is True
    assert ctx["MATH2931"]["urgent_assessments"] == ["Assignment 2"]
    assert any(note.startswith("Assignment 2 due ") for note in ctx["MATH2931"]["notes"])
    assert tasks == ["Assignment 2"]
    assert day_plan.assessment_boosts(TODAY, ["MATH2931"]) == {"MATH2931": day_plan.BOOST_LEAD}
    assert any("Assignment 2" in alert for alert in st.get_assessment_alerts(TODAY))


def test_ten_days_out_without_a_spec_does_not_drive_the_task(tmp_path, monkeypatch):
    _install(tmp_path, monkeypatch, [_row("2026-11-01")])
    ctx, tasks = _study_task(TODAY)
    assert ctx["MATH2931"]["urgent"] is False
    assert not any(" due " in note for note in ctx["MATH2931"]["notes"])
    assert tasks == ["W5 problems"]
    assert "Assignment" not in tasks[0]
    assert day_plan.assessment_boosts(TODAY, ["MATH2931"]) == {}
    assert st.get_assessment_alerts(TODAY) == []


def test_close_window_without_a_spec_does_not_boost(tmp_path, monkeypatch):
    due = (TODAY + datetime.timedelta(days=2)).isoformat()
    _install(tmp_path, monkeypatch, [_row(due, spec=False)])
    assert day_plan.assessment_boosts(TODAY, ["MATH2931"]) == {}
    _ctx, tasks = _study_task(TODAY)
    assert tasks == ["W5 problems"]


def test_old_records_without_the_field_still_load(tmp_path, monkeypatch):
    path = _install(tmp_path, monkeypatch, [
        _row("2026-11-11", name="Assignment 2"),
        {"name": "Final exam", "due": "2026-11-28", "weight": 60},
        {"name": "Quiz 3", "due": "2026-10-28", "weight": 5},
        {"name": "In-class Test", "due": "2026-10-25", "weight": 20},
        {"name": "Assessment 3", "type": "exam", "due": "2026-11-20", "weight": 40},
        {"name": "Milestone", "type": "project", "due": "2026-11-05", "weight": 15},
        {"name": "Quiz 1", "due": "2026-10-30", "weight": 5, "spec_released": False},
    ])
    before = path.read_text()
    loaded = {row["name"]: row["spec_released"] for row in st._load_assessments(TODAY)}
    assert path.read_text() == before
    assert "spec_released" not in json.loads(before)["subjects"][0]["assessments"][0]
    assert loaded["Assignment 2"] is False
    assert loaded["Final exam"] is True
    assert loaded["Quiz 3"] is True
    assert loaded["In-class Test"] is True
    assert loaded["Assessment 3"] is True
    assert loaded["Milestone"] is False
    assert loaded["Quiz 1"] is False
    assert term_context.spec_is_released({"name": "Assignment 2", "spec_released": "true"}) is False


def test_unreleased_assignment_is_not_a_start_flag(tmp_path, monkeypatch):
    due = (datetime.date.today() + datetime.timedelta(days=3)).isoformat()
    path = tmp_path / "term_context.json"
    path.write_text(json.dumps({"subjects": [{
        "code": "MATH2931",
        "assessments": [{"name": "Assignment 2", "due": due, "weight": 20, "status": "pending"}],
    }]}))
    monkeypatch.setattr(term_context, "CONTEXT_FILE", path)
    assert not any("Assignment 2" in flag for flag in term_context.get_flags())


def test_prompt_rule_uses_the_urgent_flag():
    start = datetime.datetime(2026, 10, 22, 9, 0)
    end = datetime.datetime(2026, 10, 22, 10, 0)
    prompt = day_plan._task_prompt(
        [("MATH2931", "study", start, end)],
        {"MATH2931": {
            "week": 5, "notes": [], "study_style": "notes",
            "urgent": True, "urgent_assessments": ["Assignment 2"],
        }},
        [],
        {},
        {"MATH2931": "notes"},
    )
    assert "urgent_assessments" in prompt
    assert "A due assessment comes before" not in prompt
