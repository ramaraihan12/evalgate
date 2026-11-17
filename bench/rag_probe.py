"""Speed probe for docs-rag-assistant on a CI runner (not part of the gate).

Run with the app's environment, from the app's folder:
    uv run python /path/to/rag_probe.py --model qwen2.5:3b --out probe.json
It ingests samples/ with the embedding model, then answers each answerable question in
evals/questions.jsonl at the app's top_k, and records how long each step takes.
"""

import argparse
import asyncio
import json
import os
import subprocess
import time
from pathlib import Path

from docs_rag.config import Settings
from docs_rag.factory import build_knowledge_base
from docs_rag.models import AnswerDelta, SourcesFound


def cpu_model() -> str:
    out = subprocess.run(["lscpu"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if line.startswith("Model name:"):
            return line.split(":", 1)[1].strip()
    return "unknown"


async def ask(kb, question):
    start = time.perf_counter()
    first = None
    sources, tokens = 0, 0
    text = []
    async for event in kb.astream_answer(question):
        if isinstance(event, SourcesFound):
            sources = len(event.sources)
        elif isinstance(event, AnswerDelta):
            if first is None:
                first = time.perf_counter() - start
            tokens += 1
            text.append(event.text)
    return {
        "question": question,
        "total_s": round(time.perf_counter() - start, 2),
        "first_token_s": round(first, 2) if first is not None else None,
        "sources": sources,
        "chunks_streamed": tokens,
        "answer_chars": len("".join(text)),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    settings = Settings(llm_provider="ollama", llm_model=args.model)
    kb = build_knowledge_base(settings, table_name="probe_chunks")

    start = time.perf_counter()
    chunks = 0
    for path in sorted(Path("samples").iterdir()):
        chunks += kb.ingest_file(path).chunks
    ingest_s = round(time.perf_counter() - start, 1)

    questions = [
        json.loads(line)["question"]
        for line in Path("evals/questions.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip() and json.loads(line)["file"]
    ]
    results = []
    with asyncio.Runner() as runner:
        for question in questions:
            results.append(runner.run(ask(kb, question)))
            print(results[-1], flush=True)

    times = [r["total_s"] for r in results]
    data = {
        "model": args.model,
        "cpu": cpu_model(),
        "vcpus": os.cpu_count(),
        "ingest_s": ingest_s,
        "chunks": chunks,
        "first_question_s": times[0],
        "mean_s": round(sum(times) / len(times), 2),
        "median_s": sorted(times)[len(times) // 2],
        "max_s": max(times),
        "total_s": round(sum(times), 1),
        "questions": results,
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"{args.model}: ingest {ingest_s}s, {len(results)} questions in {data['total_s']}s, "
          f"mean {data['mean_s']}s")


if __name__ == "__main__":
    main()
