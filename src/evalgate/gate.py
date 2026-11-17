"""Paired comparison of a base run and a PR run on the same cases."""

from evalgate.errors import GateError
from evalgate.stats import ALPHA, mcnemar_one_sided, verdict, wilson


def compare(base: dict, head: dict) -> dict:
    base_cases = {c["id"]: c for c in base["cases"]}
    head_cases = {c["id"]: c for c in head["cases"]}
    if set(base_cases) != set(head_cases):
        raise GateError("base and PR ran different cases, so they can't be paired")
    if not base_cases:
        raise GateError("no cases to compare")

    ids = sorted(base_cases)
    regressed = [i for i in ids if base_cases[i]["correct"] and not head_cases[i]["correct"]]
    fixed = [i for i in ids if not base_cases[i]["correct"] and head_cases[i]["correct"]]
    n = len(ids)
    base_ok = sum(base_cases[i]["correct"] for i in ids)
    head_ok = sum(head_cases[i]["correct"] for i in ids)
    b, c = len(regressed), len(fixed)
    p = mcnemar_one_sided(b, c)
    return {
        "n": n,
        "base_correct": base_ok,
        "head_correct": head_ok,
        "base_ci": wilson(base_ok, n),
        "head_ci": wilson(head_ok, n),
        "b": b,
        "c": c,
        "p_value": p,
        "alpha": ALPHA,
        "verdict": verdict(base_ok, head_ok, p),
        "regressed": regressed,
        "fixed": fixed,
        "base_cases": base_cases,
        "head_cases": head_cases,
    }
