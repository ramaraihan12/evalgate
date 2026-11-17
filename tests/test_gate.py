import pytest

from evalgate.adapters import Outcome
from evalgate.errors import GateError, InfraError
from evalgate.gate import compare
from evalgate.github import find_sticky
from evalgate.report import MARKER, render
from evalgate.runner import grade, run_cases


def side(sha, flags, model="m", digest="d"):
    cases = [
        {"id": f"c{i:02d}", "question": f"q{i}", "correct": ok, "status": "ok" if ok else "failed",
         "sql": "SELECT 1", "detail": None}
        for i, ok in enumerate(flags)
    ]
    return {"sha": sha, "model": model, "model_digest": digest, "cases": cases}


def test_identical_runs_pass():
    base = side("a" * 40, [True] * 30 + [False] * 20)
    result = compare(base, side("b" * 40, [True] * 30 + [False] * 20))
    assert (result["b"], result["c"], result["verdict"]) == (0, 0, "PASS")


def test_large_drop_fails():
    base = side("a" * 40, [True] * 30 + [False] * 20)
    head = side("b" * 40, [False] * 8 + [True] * 22 + [False] * 20)
    result = compare(base, head)
    assert (result["b"], result["c"]) == (8, 0)
    assert result["verdict"] == "FAIL"
    assert result["regressed"][0] == "c00"


def test_small_drop_passes_because_it_is_noise():
    base = side("a" * 40, [True] * 30 + [False] * 20)
    head = side("b" * 40, [False] * 3 + [True] * 27 + [False] * 20)
    assert compare(base, head)["verdict"] == "PASS"


def test_drop_offset_by_fixes_passes():
    base = side("a" * 40, [True] * 6 + [False] * 6)
    head = side("b" * 40, [False] * 6 + [True] * 6)
    result = compare(base, head)
    assert (result["b"], result["c"], result["verdict"]) == (6, 6, "PASS")


def test_improvement_passes():
    base = side("a" * 40, [False] * 10)
    assert compare(base, side("b" * 40, [True] * 10))["verdict"] == "PASS"


def test_different_cases_cannot_be_paired():
    with pytest.raises(GateError):
        compare(side("a", [True, True]), side("b", [True]))


def test_report_has_marker_numbers_and_limits():
    base = side("a" * 40, [True] * 30 + [False] * 20)
    head = side("b" * 40, [False] * 8 + [True] * 22 + [False] * 20)
    text = render(compare(base, head), base, head)
    assert text.startswith(MARKER)
    assert "Eval gate: FAIL" in text
    assert "30/50" in text and "22/50" in text
    assert "only large drops" in text


def test_find_sticky_only_matches_bot_comments_with_marker():
    comments = [
        {"id": 1, "body": f"{MARKER} fake", "user": {"type": "User"}},
        {"id": 2, "body": "hello", "user": {"type": "Bot"}},
        {"id": 3, "body": f"{MARKER}\nreal", "user": {"type": "Bot"}},
    ]
    assert find_sticky(comments)["id"] == 3
    assert find_sticky(comments[:2]) is None


class FakeAdapter:
    def __init__(self, behaviour):
        self.behaviour = behaviour

    def gold_rows(self, sql):
        return [(1,)]

    def ask(self, question):
        return self.behaviour(question)


CASES = [
    {"id": "a", "question": "one", "gold_sql": "SELECT 1"},
    {"id": "b", "question": "two", "expect": "no_sql"},
]


def test_run_grades_cases_and_counts_model_errors_as_wrong():
    def behaviour(q):
        if q == "one":
            raise ValueError("model produced garbage")
        return Outcome("no_sql")

    results = run_cases(CASES, FakeAdapter(behaviour), log=lambda *_: None)
    assert [r["correct"] for r in results] == [False, True]
    assert results[0]["status"] == "error"


def test_infrastructure_failure_aborts_the_run():
    def behaviour(q):
        raise InfraError("ollama is down")

    with pytest.raises(InfraError):
        run_cases(CASES, FakeAdapter(behaviour), log=lambda *_: None)


def test_grade_no_sql_and_rows():
    assert grade(CASES[1], Outcome("no_sql"), None)
    assert not grade(CASES[1], Outcome("ok", [(1,)], "SELECT 1"), None)
    assert grade(CASES[0], Outcome("ok", [(1,)], "SELECT 1"), [(1,)])
    assert not grade(CASES[0], Outcome("no_sql"), [(1,)])
    assert not grade(CASES[0], Outcome("blocked", None, "x"), [(1,)])
