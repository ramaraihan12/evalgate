"""Adapter for docs-rag-assistant. It runs inside that app's environment, so `docs_rag` imports.

The adapter lives here and not in the app, so a pull request can't change how it is graded.
The gold is the expected file and page or section in the dataset, so the only way a PR can
change the grading is by editing the dataset. Ingestion, chunking, retrieval and the prompt all
come from the checkout under test: `preflight` indexes that checkout's `samples/` into a
scratch table with its own code.
"""

import asyncio
import os
from pathlib import Path

from evalgate import ragmetrics
from evalgate.errors import DatasetError, InfraError

TABLE = "evalgate_chunks"
ANSWER_CLIP = 600


def is_infra(exc: BaseException) -> bool:
    """Down or unreachable services, as opposed to the app misbehaving on one question."""
    import httpx

    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        name = type(exc).__name__
        status = getattr(exc, "status_code", None)
        if isinstance(exc, (httpx.TransportError, ConnectionError, TimeoutError)):
            return True
        if name == "ResponseError" and isinstance(status, int) and (status == 404 or status >= 500):
            return True
        if name in ("OperationalError", "InterfaceError", "CannotConnectNowError"):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


def location_problems(cases: list[dict], documents: list[dict]) -> list[str]:
    """Every expected file, page and section must exist in the loaded sample documents."""
    problems = []
    by_file: dict[str, list[dict]] = {}
    for doc in documents:
        by_file.setdefault(doc["file_name"], []).append(doc)
    for case in cases:
        file = case.get("file")
        if not file:
            if case.get("page") is not None or case.get("section"):
                problems.append(f"{case['id']}: an unanswerable case has no page or section")
            continue
        parts = by_file.get(file)
        if parts is None:
            problems.append(f"{case['id']}: no sample document named {file}")
        elif case.get("page") is None and not case.get("section"):
            problems.append(f"{case['id']}: needs a page or a section")
        elif not any(ragmetrics.matches(case, {**p, "file_name": file}) for p in parts):
            where = case.get("page") if case.get("page") is not None else case.get("section")
            problems.append(f"{case['id']}: {file} has nothing at {where!r}")
    return problems


class DocsRagAdapter:
    def __init__(self, model: str = "", base_url: str = "http://localhost:11434") -> None:
        self.model = model
        self.base_url = base_url
        self.samples = Path(os.environ.get("EVALGATE_SAMPLES", "samples"))
        self._runner = None
        self._kb = None
        self._ingest: dict = {}

    def _settings(self):
        from docs_rag.config import Settings

        return Settings(llm_provider="ollama", llm_model=self.model, ollama_base_url=self.base_url)

    def _run(self, coro):
        if self._runner is None:
            self._runner = asyncio.Runner()
        return self._runner.run(coro)

    def preflight(self) -> None:
        """Index this checkout's samples, then load the chat model so case 1 isn't billed for it."""
        import time

        import httpx

        try:
            httpx.get(f"{self.base_url}/api/version", timeout=10).raise_for_status()
            from docs_rag.factory import build_knowledge_base

            settings = self._settings()
            self._kb = build_knowledge_base(settings, table_name=TABLE)
            if not self.samples.is_dir():
                raise DatasetError(f"no samples folder at {self.samples.resolve()}")
            start = time.perf_counter()
            files = chunks = 0
            for path in sorted(self.samples.iterdir()):
                if path.is_file():
                    result = self._kb.ingest_file(path)
                    files, chunks = files + 1, chunks + result.chunks
            self._ingest = {
                "files": files,
                "chunks": chunks,
                "seconds": round(time.perf_counter() - start, 1),
                "chunk_size": settings.chunk_size,
                "top_k": settings.top_k,
                "similarity_cutoff": settings.similarity_cutoff,
            }
            print(f"ingested {files} files, {chunks} chunks in {self._ingest['seconds']}s")
            httpx.post(
                f"{self.base_url}/api/generate",
                json={"model": self.model, "prompt": "hi", "stream": False,
                      "options": {"num_predict": 1}},
                timeout=600,
            ).raise_for_status()
        except DatasetError:
            raise
        except Exception as exc:
            raise InfraError(f"preflight failed: {type(exc).__name__}: {exc}") from exc

    def describe(self) -> dict:
        return {"name": "docsrag", "model": self.model, **self._ingest}

    async def _answer(self, question: str):
        from docs_rag.models import AnswerDelta, SourcesFound

        sources, parts = [], []
        async for event in self._kb.astream_answer(question):
            if isinstance(event, SourcesFound):
                sources = [
                    {"file_name": s.file_name, "page": s.page, "section": s.section,
                     "score": s.score}
                    for s in event.sources
                ]
            elif isinstance(event, AnswerDelta):
                parts.append(event.text)
        return sources, "".join(parts)

    def run_case(self, case: dict) -> dict:
        """Ask one question the way the app does and grade it. Returns the per-case record."""
        try:
            sources, answer = self._run(self._answer(case["question"]))
        except Exception as exc:
            if is_infra(exc):
                raise InfraError(f"{type(exc).__name__}: {exc}") from exc
            record = ragmetrics.grade_case(case, "", [])
            record.update(correct=False, status="error", detail=f"{type(exc).__name__}: {exc}")
            return record
        record = ragmetrics.grade_case(case, answer, sources)
        record["status"] = "ok"
        record["answer"] = answer.strip()[:ANSWER_CLIP]
        record["sources"] = [
            f"{s['file_name']}, {('page ' + str(s['page'])) if s['page'] is not None else s['section']}"
            for s in sources
        ]
        return record

    def validate(self, cases: list[dict]) -> list[str]:
        """Dataset problems, found without a database or a model: it loads the sample
        documents with the app's own loader and checks every expected location against them."""
        from docs_rag.loaders import load_file

        if not self.samples.is_dir():
            return [f"no samples folder at {self.samples.resolve()}"]
        documents = []
        for path in sorted(self.samples.iterdir()):
            if path.is_file():
                documents += [dict(d.metadata) for d in load_file(path)]
        problems = location_problems(cases, documents)
        langs = [c.get("lang") for c in cases]
        if any(lang not in ("en", "id") for lang in langs):
            problems.append("every case needs lang en or id")
        return problems
