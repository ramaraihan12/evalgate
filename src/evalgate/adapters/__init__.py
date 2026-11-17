"""An adapter wraps one app. It needs: preflight(), ask(question), gold_rows(sql), describe()."""

import importlib
from dataclasses import dataclass


@dataclass
class Outcome:
    status: str  # ok, no_sql, blocked, failed or error
    rows: list | None = None
    sql: str | None = None
    detail: str | None = None


def load_adapter(spec: str, **kwargs):
    module_name, _, class_name = spec.partition(":")
    if not module_name or not class_name:
        raise ValueError(f"adapter must look like 'package.module:ClassName', got {spec!r}")
    return getattr(importlib.import_module(module_name), class_name)(**kwargs)
