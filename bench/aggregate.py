"""Merge per-model M0 result files into one JSON and one markdown table."""

import json
import sys
from pathlib import Path


def main(results_dir: str, out_json: str, out_md: str) -> int:
    runs = [json.loads(p.read_text()) for p in sorted(Path(results_dir).rglob("*.json"))]
    summaries = [r["summary"] for r in runs]
    Path(out_json).write_text(json.dumps(runs, indent=2), encoding="utf-8")
    lines = [
        "| model | correct | p50 s | p95 s | total s | prefill tok/s | gen tok/s | prompt tok | pull s |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for s in summaries:
        pull = "cached" if s["cache_hit"] else s["pull_s"]
        lines.append(
            f"| {s['model']} | {s['correct']}/{s['questions']} | {s['p50_s']} | {s['p95_s']} "
            f"| {s['total_s']} | {s['prefill_tps']} | {s['gen_tps']} "
            f"| {s['prompt_tokens_per_call']} | {pull} |"
        )
    if summaries:
        lines += ["", f"CPU: {summaries[0]['cpu']} ({summaries[0]['nproc']} vCPU), "
                  f"Ollama {summaries[0]['ollama_version']}"]
    Path(out_md).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:4]))
