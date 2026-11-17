"""Adapter for text-to-sql-chat. It runs inside that app's environment, so `sqlchat` imports.

The adapter lives here and not in the app, so a pull request can't change how it is graded.
"""

import httpx
import psycopg

from evalgate.adapters import Outcome
from evalgate.errors import DatasetError, InfraError


def _chain(exc: BaseException):
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__ or exc.__context__


def is_infra(exc: BaseException) -> bool:
    """Down or unreachable services, as opposed to the model writing bad SQL."""
    for err in _chain(exc):
        if isinstance(err, psycopg.Error) and err.sqlstate is None:
            return True  # the pipeline re-raises connection failures, which carry no SQLSTATE
        if isinstance(err, (httpx.TransportError, ConnectionError)):
            return True
        status = getattr(err, "status_code", None)
        if type(err).__name__ == "ResponseError" and isinstance(status, int) and (
            status == 404 or status >= 500
        ):
            return True
    return False


class SqlChatAdapter:
    def __init__(self, model: str = "", base_url: str = "http://localhost:11434") -> None:
        self.model = model
        self.base_url = base_url
        self._pipeline = None
        self._db = None

    def _settings(self):
        from sqlchat.config import Settings

        return Settings(llm_provider="ollama", llm_model=self.model, ollama_base_url=self.base_url)

    def _database(self):
        if self._db is None:
            from sqlchat.db import Database

            settings = self._settings()
            self._db = Database(settings.database_url, settings.statement_timeout_ms)
        return self._db

    def preflight(self) -> None:
        """Fail fast if a service is down, and load the model so case 1 isn't billed for it."""
        try:
            httpx.get(f"{self.base_url}/api/version", timeout=10).raise_for_status()
            self._database().query("SELECT 1")
            from sqlchat.pipeline import build_pipeline

            self._pipeline = build_pipeline(self._settings())
            httpx.post(
                f"{self.base_url}/api/generate",
                json={"model": self.model, "prompt": "hi", "stream": False,
                      "options": {"num_predict": 1}},
                timeout=600,
            ).raise_for_status()
        except Exception as exc:
            raise InfraError(f"preflight failed: {type(exc).__name__}: {exc}") from exc

    def describe(self) -> dict:
        return {"name": "sqlchat", "model": self.model}

    def gold_rows(self, sql: str) -> list:
        try:
            return self._database().query(sql).rows
        except Exception as exc:
            if is_infra(exc):
                raise InfraError(f"database unreachable: {exc}") from exc
            raise DatasetError(f"gold SQL failed: {exc}\n{sql}") from exc

    def ask(self, question: str) -> Outcome:
        try:
            answer = self._pipeline.ask(question)
        except Exception as exc:
            if is_infra(exc):
                raise InfraError(f"{type(exc).__name__}: {exc}") from exc
            return Outcome("error", None, None, f"{type(exc).__name__}: {exc}")
        rows = answer.result.rows if answer.result is not None else None
        detail = None if answer.status == "ok" else answer.text
        return Outcome(answer.status, rows, answer.sql, detail)

    def lint_gold(self, sql: str) -> list[str]:
        """Problems with a gold query: fails, empty, or a tie at the LIMIT cut."""
        import sqlglot

        try:
            rows = self.gold_rows(sql)
        except DatasetError as exc:
            return [str(exc).splitlines()[0]]
        problems = []
        if not rows or all(v is None for v in rows[0]):
            problems.append("empty result")

        tree = sqlglot.parse_one(sql, read="postgres")
        limit, order = tree.args.get("limit"), tree.args.get("order")
        if limit is not None:
            if order is None:
                return [*problems, "LIMIT without ORDER BY"]
            n = int(limit.expression.name)
            key = order.expressions[0].this
            index = _projection_index(tree, key)
            if index is None:
                return [*problems, "put the first ORDER BY key in the select list to check ties"]
            full = tree.copy()
            full.set("limit", None)
            ranked = self.gold_rows(full.sql(dialect="postgres"))
            if len(ranked) > n and ranked[n - 1][index] == ranked[n][index]:
                problems.append(f"tie at the cut: rows {n} and {n + 1} share {ranked[n][index]}")
        return problems


def _projection_index(tree, key) -> int | None:
    if key.is_int:
        return int(key.name) - 1
    for i, item in enumerate(tree.expressions):
        if item.alias_or_name == key.sql() or item.unalias() == key or item.alias == key.name:
            return i
    return None
