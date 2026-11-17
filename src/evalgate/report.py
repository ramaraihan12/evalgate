"""Markdown for the job summary and the sticky PR comment."""

from evalgate.stats import ALPHA, min_regressions

MARKER = "<!-- evalgate-gate -->"
SQL_LIMIT = 500


def _pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def _row(label: str, sha: str, correct: int, n: int, ci: tuple[float, float]) -> str:
    return f"| {label} `{sha[:7]}` | {correct}/{n} | {_pct(correct / n)} | {_pct(ci[0])} to {_pct(ci[1])} |"


def _clip(text: str | None) -> str:
    text = (text or "").strip().replace("```", "'''")
    return text if len(text) <= SQL_LIMIT else text[:SQL_LIMIT] + " ..."


def _why(case: dict) -> str:
    if case["status"] == "ok":
        return "returned different rows"
    return {
        "no_sql": "said it can't answer from the data",
        "blocked": "SQL blocked by the guardrails",
        "failed": "SQL failed",
        "error": "crashed",
    }.get(case["status"], case["status"])


def _runner_text(info: dict) -> str:
    if not info:
        return "not recorded"
    return f"{info.get('cpu_model', '?')}, {info.get('vcpus', '?')} vCPU, Ollama {info.get('ollama', '?')}"


def runner_warning(base: dict, head: dict) -> str | None:
    """One line when the two sides ran on different hardware or a different Ollama."""
    a, b = base.get("runner") or {}, head.get("runner") or {}
    if not a or not b:
        return None
    if all(a.get(k) == b.get(k) for k in ("cpu_model", "vcpus", "ollama")):
        return None
    return (
        "Warning: the two sides ran on different runners (CPU, vCPU count or Ollama version), "
        "so a few differences may be noise from the hardware. Re-run the check if the result is close."
    )


def render(result: dict, base: dict, head: dict) -> str:
    n = result["n"]
    verdict = result["verdict"]
    model = head.get("model", "?")
    digest = (head.get("model_digest") or "")[:12]
    lines = [
        MARKER,
        f"### Eval gate: {verdict}",
        "",
        "| | correct | accuracy | 95% interval |",
        "|---|---|---|---|",
        _row("base", base["sha"], result["base_correct"], n, result["base_ci"]),
        _row("this PR", head["sha"], result["head_correct"], n, result["head_ci"]),
        "",
        f"On the same {n} cases, {result['b']} went from right to wrong (b) and "
        f"{result['c']} went from wrong to right (c). One-sided exact McNemar p = "
        f"{result['p_value']:.3f}. The gate fails when the PR is less accurate than the base "
        f"and p < {ALPHA}.",
    ]
    warning = runner_warning(base, head)
    if warning:
        lines += ["", warning]
    if result["verdict"] == "FAIL":
        lines += ["", "This PR answers fewer of the cases correctly than its base, by more than noise explains."]

    lines += [
        "",
        f"Runner, base: {_runner_text(base.get('runner'))}.  ",
        f"Runner, this PR: {_runner_text(head.get('runner'))}.",
    ]

    flipped = [("regressed", i) for i in result["regressed"]] + [("fixed", i) for i in result["fixed"]]
    if flipped:
        lines += ["", "<details><summary>Cases that changed</summary>", ""]
        for kind, case_id in flipped:
            hc, bc = result["head_cases"][case_id], result["base_cases"][case_id]
            if kind == "regressed":
                lines.append(f"**{case_id}**, now wrong ({_why(hc)}): {hc['question']}")
                if hc.get("sql"):
                    lines += ["", "```sql", _clip(hc["sql"]), "```"]
            else:
                lines.append(f"**{case_id}**, now right (base {_why(bc)}): {hc['question']}")
            lines.append("")
        lines.append("</details>")

    lines += [
        "",
        f"With {n} cases only large drops can be detected: even if nothing improved, at least "
        f"{min_regressions()} cases would have to flip from right to wrong before this fails. "
        "A pass means no large drop, not that nothing changed. The model is also small "
        f"(`{model}`, digest `{digest}`, on a CPU runner) and is not the model the live app "
        "uses, so it shows how the prompt, schema comments and pipeline behave, not how the "
        "production model would score.",
    ]
    return "\n".join(lines) + "\n"
