"""Run every case through an adapter and grade it."""

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from evalgate.adapters import Outcome
from evalgate.compare import is_correct
from evalgate.errors import GateError


def load_cases(path: str | Path) -> list[dict]:
    cases = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        case = json.loads(line)
        is_rag = "question" in case and "file" in case  # file null = the documents don't answer
        if not is_rag and "gold_sql" not in case and case.get("expect") != "no_sql":
            raise ValueError(f"case {case.get('id')}: needs gold_sql or expect=no_sql")
        if is_rag and not case.get("id"):
            raise ValueError("every document case needs an id")
        cases.append(case)
    ids = [c["id"] for c in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate case ids")
    return cases


def grade(case: dict, outcome, gold_rows) -> bool:
    if case.get("expect") == "no_sql":
        return outcome.status == "no_sql"
    return outcome.status == "ok" and outcome.rows is not None and is_correct(
        outcome.rows, gold_rows, case["gold_sql"]
    )


def run_cases(cases: list[dict], adapter, log=print) -> list[dict]:
    """A model or SQL error on a case is a wrong answer. A GateError aborts the whole run."""
    if hasattr(adapter, "run_case"):
        return _run_graded_cases(cases, adapter, log)
    results = []
    for case in cases:
        gold = None if case.get("expect") == "no_sql" else adapter.gold_rows(case["gold_sql"])
        start = time.perf_counter()
        try:
            outcome = adapter.ask(case["question"])
        except GateError:
            raise
        except Exception as exc:
            outcome = Outcome("error", None, None, f"{type(exc).__name__}: {exc}")
        wall = time.perf_counter() - start
        correct = grade(case, outcome, gold)
        results.append(
            {
                "id": case["id"],
                "question": case["question"],
                "expect": "no_sql" if case.get("expect") == "no_sql" else "rows",
                "correct": correct,
                "status": outcome.status,
                "sql": outcome.sql,
                "detail": outcome.detail,
                "wall_s": round(wall, 2),
            }
        )
        log(f"{case['id']}: {outcome.status} correct={correct} {wall:.1f}s")
    return results


def _run_graded_cases(cases: list[dict], adapter, log) -> list[dict]:
    """For adapters that grade a case themselves (`run_case` returns the record, with `correct`)."""
    results = []
    for case in cases:
        start = time.perf_counter()
        record = adapter.run_case(case)  # a GateError from here aborts the run
        wall = time.perf_counter() - start
        results.append({"id": case["id"], "question": case["question"], **record,
                        "wall_s": round(wall, 2)})
        log(f"{case['id']}: {record['status']} correct={record['correct']} {wall:.1f}s")
    return results


def write_result(path, *, side, sha, model, digest, adapter_info, cases, wall_s, runner=None) -> dict:
    data = {
        "version": 1,
        "side": side,
        "sha": sha,
        "model": model,
        "model_digest": digest,
        "adapter": adapter_info,
        "runner": runner or {},
        "wall_s": round(wall_s, 1),
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "cases": cases,
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data

