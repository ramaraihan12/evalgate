"""M0: run text-to-sql-chat's real pipeline on small Ollama models and time it.

Run with the app's environment, e.g. `uv run --project app python bench/m0.py --model qwen2.5-coder:3b`.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import httpx
from sqlchat.config import Settings
from sqlchat.db import Database
from sqlchat.pipeline import build_pipeline

DATASET = Path(__file__).resolve().parent.parent / "datasets" / "sql_m0.jsonl"
ORDERED_LIMIT = re.compile(r"order\s+by.*\blimit\b", re.IGNORECASE | re.DOTALL)


def load_dataset() -> list[dict]:
    return [json.loads(line) for line in DATASET.read_text(encoding="utf-8").splitlines() if line]


def _norm(value):
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        return round(float(value), 2)
    return str(value) if value is not None else None


def normalize(rows, ordered: bool):
    out = [tuple(_norm(v) for v in row) for row in rows]
    return out if ordered else Counter(out)


def is_correct(model_rows, gold_rows, gold_sql: str) -> bool:
    ordered = bool(ORDERED_LIMIT.search(gold_sql))
    return normalize(model_rows, ordered) == normalize(gold_rows, ordered)


class CountingModel:
    """Wraps the chat model so each call's Ollama timing metadata is kept."""

    def __init__(self, inner):
        self._inner = inner
        self.calls: list[dict] = []

    def invoke(self, *args, **kwargs):
        start = time.perf_counter()
        reply = self._inner.invoke(*args, **kwargs)
        meta = getattr(reply, "response_metadata", {}) or {}
        self.calls.append(
            {
                "wall_s": time.perf_counter() - start,
                "prompt_tokens": meta.get("prompt_eval_count", 0),
                "prompt_ns": meta.get("prompt_eval_duration", 0),
                "gen_tokens": meta.get("eval_count", 0),
                "gen_ns": meta.get("eval_duration", 0),
            }
        )
        return reply

    def __getattr__(self, name):
        return getattr(self._inner, name)


def validate_gold(db: Database) -> bool:
    ok = True
    for item in load_dataset():
        try:
            rows = db.query(item["gold_sql"]).rows
        except Exception as exc:
            print(f"{item['id']}: gold SQL failed: {exc}")
            ok = False
            continue
        empty = not rows or all(v is None for v in rows[0])
        print(f"{item['id']}: {len(rows)} rows{'  (EMPTY)' if empty else ''}")
        ok = ok and not empty
    return ok


def cpu_model() -> str:
    try:
        out = subprocess.run(["lscpu"], capture_output=True, text=True, check=True).stdout
        for line in out.splitlines():
            if line.startswith("Model name"):
                return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return "unknown"


def ollama_version(base_url: str) -> str:
    try:
        return httpx.get(f"{base_url}/api/version", timeout=10).json().get("version", "unknown")
    except Exception:
        return "unknown"


def warm_up(base_url: str, model: str) -> float:
    """Load the model into memory so the first question isn't billed for it."""
    start = time.perf_counter()
    httpx.post(
        f"{base_url}/api/generate",
        json={"model": model, "prompt": "hi", "stream": False, "options": {"num_predict": 1}},
        timeout=600,
    )
    return time.perf_counter() - start


def percentile(values: list[float], p: float) -> float:
    s = sorted(values)
    k = (len(s) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def run(model: str, out: Path) -> int:
    settings = Settings(llm_provider="ollama", llm_model=model)
    pipeline = build_pipeline(settings)
    counter = CountingModel(pipeline._model)
    pipeline._model = counter
    db = Database(settings.database_url, settings.statement_timeout_ms)

    load_s = warm_up(settings.ollama_base_url, model)
    results = []
    for item in load_dataset():
        gold_rows = db.query(item["gold_sql"]).rows
        counter.calls.clear()
        start = time.perf_counter()
        try:
            answer = pipeline.ask(item["question"])
            status, rows, sql, error = answer.status, None, answer.sql, None
            if answer.result is not None:
                rows = answer.result.rows
        except Exception as exc:  # a crash is a failed question, not a failed benchmark
            status, rows, sql, error = "error", None, None, f"{type(exc).__name__}: {exc}"
        wall = time.perf_counter() - start
        correct = status == "ok" and rows is not None and is_correct(rows, gold_rows, item["gold_sql"])
        results.append(
            {
                "id": item["id"],
                "wall_s": round(wall, 2),
                "model_calls": len(counter.calls),
                "status": status,
                "correct": correct,
                "sql": sql,
                "error": error,
                "prompt_tokens": sum(c["prompt_tokens"] for c in counter.calls),
                "gen_tokens": sum(c["gen_tokens"] for c in counter.calls),
                "prompt_ns": sum(c["prompt_ns"] for c in counter.calls),
                "gen_ns": sum(c["gen_ns"] for c in counter.calls),
            }
        )
        print(f"{item['id']}: {status} correct={correct} {wall:.1f}s calls={len(counter.calls)}")

    walls = [r["wall_s"] for r in results]
    p_ns, g_ns = sum(r["prompt_ns"] for r in results), sum(r["gen_ns"] for r in results)
    summary = {
        "model": model,
        "correct": sum(r["correct"] for r in results),
        "questions": len(results),
        "p50_s": round(percentile(walls, 0.5), 2),
        "p95_s": round(percentile(walls, 0.95), 2),
        "total_s": round(sum(walls), 2),
        "prefill_tps": round(sum(r["prompt_tokens"] for r in results) / (p_ns / 1e9), 1) if p_ns else None,
        "gen_tps": round(sum(r["gen_tokens"] for r in results) / (g_ns / 1e9), 1) if g_ns else None,
        "prompt_tokens_per_call": round(
            sum(r["prompt_tokens"] for r in results) / max(1, sum(r["model_calls"] for r in results))
        ),
        "model_load_s": round(load_s, 2),
        "pull_s": float(os.environ.get("PULL_SECONDS", "nan")),
        "cache_hit": os.environ.get("CACHE_HIT", "") == "true",
        "ollama_version": ollama_version(settings.ollama_base_url),
        "cpu": cpu_model(),
        "nproc": os.cpu_count(),
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"summary": summary, "questions": results}, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model")
    parser.add_argument("--out", default="results/m0.json")
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    if args.validate_only:
        db = Database(Settings().database_url)
        return 0 if validate_gold(db) else 1
    if not args.model:
        parser.error("--model is required")
    return run(args.model, Path(args.out))


if __name__ == "__main__":
    sys.exit(main())
