import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from evalgate.adapters import load_adapter
from evalgate.errors import GateError, InfraError
from evalgate.gate import compare
from evalgate.github import post_or_update
from evalgate.report import dataset_changes, render
from evalgate.runner import load_cases, run_cases, write_result


def _digest_of(base_url: str, model: str) -> str:
    with urllib.request.urlopen(f"{base_url}/api/tags", timeout=30) as resp:
        tags = json.loads(resp.read())["models"]
    for item in tags:
        if item.get("name") == model or item.get("model") == model:
            return item["digest"]
    raise InfraError(f"{model} is not pulled on {base_url}")


def digest_matches(got: str, expected: str) -> bool:
    """Exact match of a full 64-hex digest. A prefix, an empty or a short value is an error."""
    if not re.fullmatch(r"[0-9a-f]{64}", expected or ""):
        raise InfraError("the pinned digest must be the full 64 hex characters")
    return got == expected


def _cpu_model() -> str:
    try:
        out = subprocess.run(["lscpu"], capture_output=True, text=True, timeout=10).stdout
        for line in out.splitlines():
            if line.startswith("Model name:"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return "unknown"


def runner_info(base_url: str) -> dict:
    try:
        with urllib.request.urlopen(f"{base_url}/api/version", timeout=10) as resp:
            ollama = json.loads(resp.read()).get("version", "unknown")
    except (OSError, ValueError):
        ollama = "unknown"
    return {"cpu_model": _cpu_model(), "vcpus": os.cpu_count(), "ollama": ollama}


def cmd_check_model(args) -> int:
    try:
        got = _digest_of(args.base_url, args.model)
    except OSError as exc:
        raise InfraError(f"can't reach Ollama at {args.base_url}: {exc}") from exc
    if not digest_matches(got, args.digest):
        raise InfraError(f"{args.model} digest is {got}, expected {args.digest}. Refusing to run.")
    print(f"{args.model} digest {got[:12]} matches")
    return 0


def cmd_run(args) -> int:
    cases = load_cases(args.dataset)
    adapter = load_adapter(args.adapter, model=args.model, base_url=args.base_url)
    adapter.preflight()
    digest = _digest_of(args.base_url, args.model)
    start = time.perf_counter()
    results = run_cases(cases, adapter)
    data = write_result(
        args.out,
        side=args.side,
        sha=args.sha,
        model=args.model,
        digest=digest,
        adapter_info=adapter.describe(),
        cases=results,
        wall_s=time.perf_counter() - start,
        runner=runner_info(args.base_url),
    )
    right = sum(c["correct"] for c in data["cases"])
    print(f"{args.side}: {right}/{len(results)} correct in {data['wall_s']}s")
    print(f"runner: {data['runner']}")
    return 0


def cmd_validate(args) -> int:
    cases = load_cases(args.dataset)
    adapter = load_adapter(args.adapter)
    if hasattr(adapter, "validate"):
        problems = adapter.validate(cases)
        for line in problems:
            print(line)
        print(f"{len(cases)} cases, {len(problems)} problems")
        return 1 if problems else 0
    bad = 0
    for case in cases:
        if case.get("expect") == "no_sql":
            print(f"{case['id']}: expects no_sql")
            continue
        problems = adapter.lint_gold(case["gold_sql"])
        print(f"{case['id']}: {'; '.join(problems) if problems else 'ok'}")
        bad += bool(problems)
    return 1 if bad else 0


def cmd_compare(args) -> int:
    base = json.loads(Path(args.base).read_text(encoding="utf-8"))
    head = json.loads(Path(args.head).read_text(encoding="utf-8"))
    if base.get("model_digest") != head.get("model_digest"):
        raise GateError("base and PR ran on different model digests")
    result = compare(base, head)
    Path(args.out_md).write_text(render(result, base, head), encoding="utf-8")
    summary = {k: v for k, v in result.items() if not k.endswith("_cases")}
    Path(args.out_json).write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(
        f"{result['verdict']}: base {result['base_correct']}/{result['n']}, "
        f"PR {result['head_correct']}/{result['n']}, b={result['b']} c={result['c']} "
        f"p={result['p_value']:.3f}"
    )
    return 0


def cmd_comment(args) -> int:
    body = Path(args.body_file).read_text(encoding="utf-8")
    outcome = post_or_update(args.repo, args.pr, body)
    if outcome == "skipped":
        print("::warning::no permission to comment on this PR, the job summary has the report")
    print(f"comment {outcome}")
    return 0


def cmd_dataset_diff(args) -> int:
    base = Path(args.base)
    base_text = base.read_text(encoding="utf-8") if base.is_file() and base.stat().st_size else None
    head_text = Path(args.head).read_text(encoding="utf-8")
    line = dataset_changes(base_text, head_text)
    print(line)
    with open(args.append_to, "a", encoding="utf-8") as fh:
        fh.write(chr(10) + line + chr(10))
    return 0


def cmd_verdict(args) -> int:
    verdict = json.loads(Path(args.file).read_text(encoding="utf-8"))["verdict"]
    print(f"verdict: {verdict}")
    return 1 if verdict == "FAIL" else 0


def cmd_harvest(args) -> int:
    from evalgate.harvest import env_credentials, run_harvest

    host, public_key, secret_key = env_credentials()
    result = run_harvest(
        args.out, host=host, public_key=public_key, secret_key=secret_key, days=args.days
    )
    print(
        f"{result['week']}: read {result['read']} observations, wrote {result['written']} rows "
        f"{result['per_app']}, dropped over the cap {result['dropped']}"
    )
    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as fh:
            fh.write(f"rows={result['written']}\nweek={result['week']}\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evalgate")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run the cases against one checkout of the app")
    run.add_argument("--dataset", required=True)
    run.add_argument("--adapter", required=True)
    run.add_argument("--model", required=True)
    run.add_argument("--base-url", default=os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434"))
    run.add_argument("--side", required=True, choices=["base", "head"])
    run.add_argument("--sha", required=True)
    run.add_argument("--out", required=True)
    run.set_defaults(func=cmd_run)

    val = sub.add_parser("validate", help="check the gold SQL of every case")
    val.add_argument("--dataset", required=True)
    val.add_argument("--adapter", required=True)
    val.set_defaults(func=cmd_validate)

    chk = sub.add_parser("check-model", help="fail if the pulled model digest differs")
    chk.add_argument("--model", required=True)
    chk.add_argument("--digest", required=True)
    chk.add_argument("--base-url", default="http://localhost:11434")
    chk.set_defaults(func=cmd_check_model)

    cmp_ = sub.add_parser("compare", help="compare the base run and the PR run")
    cmp_.add_argument("--base", required=True)
    cmp_.add_argument("--head", required=True)
    cmp_.add_argument("--out-md", required=True)
    cmp_.add_argument("--out-json", required=True)
    cmp_.set_defaults(func=cmd_compare)

    com = sub.add_parser("comment", help="post or update the sticky PR comment")
    com.add_argument("--repo", required=True)
    com.add_argument("--pr", required=True, type=int)
    com.add_argument("--body-file", required=True)
    com.set_defaults(func=cmd_comment)

    dd = sub.add_parser("dataset-diff", help="append the dataset changes between base and PR to the report")
    dd.add_argument("--base", required=True, help="the base dataset; a missing or empty file means none")
    dd.add_argument("--head", required=True)
    dd.add_argument("--append-to", required=True)
    dd.set_defaults(func=cmd_dataset_diff)

    hv = sub.add_parser("harvest", help="write last week's flagged production answers to the review queue")
    hv.add_argument("--out", default="review-queue")
    hv.add_argument("--days", type=int, default=7)
    hv.add_argument("--github-output", default=None, help="append rows= and week= to this file")
    hv.set_defaults(func=cmd_harvest)

    ver = sub.add_parser("verdict", help="exit 1 when the verdict file says FAIL")
    ver.add_argument("--file", required=True)
    ver.set_defaults(func=cmd_verdict)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except GateError as exc:
        print(f"::error::evalgate could not finish: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
