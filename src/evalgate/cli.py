import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

from evalgate.adapters import load_adapter
from evalgate.errors import GateError, InfraError
from evalgate.gate import compare
from evalgate.github import post_or_update
from evalgate.report import render
from evalgate.runner import load_cases, run_cases, write_result


def _digest_of(base_url: str, model: str) -> str:
    with urllib.request.urlopen(f"{base_url}/api/tags", timeout=30) as resp:
        tags = json.loads(resp.read())["models"]
    for item in tags:
        if item.get("name") == model or item.get("model") == model:
            return item["digest"]
    raise InfraError(f"{model} is not pulled on {base_url}")


def cmd_check_model(args) -> int:
    try:
        got = _digest_of(args.base_url, args.model)
    except OSError as exc:
        raise InfraError(f"can't reach Ollama at {args.base_url}: {exc}") from exc
    if not got.startswith(args.digest):
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
    )
    right = sum(c["correct"] for c in data["cases"])
    print(f"{args.side}: {right}/{len(results)} correct in {data['wall_s']}s")
    return 0


def cmd_validate(args) -> int:
    cases = load_cases(args.dataset)
    adapter = load_adapter(args.adapter)
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


def cmd_verdict(args) -> int:
    verdict = json.loads(Path(args.file).read_text(encoding="utf-8"))["verdict"]
    print(f"verdict: {verdict}")
    return 1 if verdict == "FAIL" else 0


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
