"""Weekly harvest: flagged production answers from Langfuse into a review queue.

Reads the last N days of root "answer" generations (environment production) with a read-only
key, keeps the ones the app flagged, drops questions we already have, and writes one JSONL
file per app and ISO week. The text was redacted at the source, so rows hold nothing else.
"""

import base64
import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from evalgate.errors import InfraError

APPS = ("docs-rag", "sql-chat")
ENVIRONMENT = "production"
CAP_PER_APP = 50
EXCERPT_CAP = 300
PAGE_LIMIT = 100
MAX_PAGES = 100
EVAL_SETS = (
    "https://raw.githubusercontent.com/ramaraihan12/docs-rag-assistant/main/evals/rag_cases.jsonl",
    "https://raw.githubusercontent.com/ramaraihan12/text-to-sql-chat/main/evals/sql_cases.jsonl",
)


def normalize(question: str) -> str:
    return re.sub(r"\s+", " ", question.strip().lower()).strip(" ?!.")


def question_hash(question: str) -> str:
    return hashlib.sha256(normalize(question).encode()).hexdigest()


def _json(value):
    """Input and output arrive as JSON strings, metadata as an object; be forgiving."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def parse_observation(obs: dict) -> dict | None:
    """A harvest candidate, or None when this is not a flagged root answer from a known app."""
    if obs.get("type") != "GENERATION" or obs.get("name") != "answer":
        return None
    if obs.get("environment") != ENVIRONMENT:
        return None
    if obs.get("parentObservationId") or obs.get("isRootObservation") is False:
        return None
    meta = _json(obs.get("metadata"))
    if not isinstance(meta, dict):
        return None
    flags = _json(meta.get("flags"))
    if not isinstance(flags, list) or not flags:
        return None
    app = meta.get("app")
    if app not in APPS:
        return None
    data_in = _json(obs.get("input"))
    question = data_in.get("question") if isinstance(data_in, dict) else data_in
    if not isinstance(question, str) or not question.strip():
        return None
    if question.strip().lower().startswith("synthetic:"):
        return None
    data_out = _json(obs.get("output"))
    answer = data_out.get("answer") if isinstance(data_out, dict) else data_out
    if answer is None:
        answer = ""
    elif not isinstance(answer, str):
        answer = json.dumps(answer, ensure_ascii=False)
    return {
        "trace_id": obs.get("traceId") or "",
        "observation_id": obs.get("id") or "",
        "app": app,
        "question": question.strip(),
        "answer_excerpt": answer[:EXCERPT_CAP],
        "flags": [str(f) for f in flags],
        "label": None,
        "expected": None,
        "note": "",
        "_start": obs.get("startTime") or "",
    }


def _get(url: str, headers: dict) -> bytes:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        # Status only: never echo a header or body that could carry a key.
        raise InfraError(f"GET {urllib.parse.urlsplit(url).path} answered {exc.code}") from None
    except OSError as exc:
        raise InfraError(f"GET {urllib.parse.urlsplit(url).path} failed: {type(exc).__name__}") from None


def fetch_observations(host, public_key, secret_key, since, until, fetch=_get) -> list[dict]:
    if not host.startswith("https://"):
        raise InfraError("LANGFUSE_HOST must be an https URL")
    token = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
    headers = {"Authorization": f"Basic {token}", "User-Agent": "evalgate-harvest"}
    stamp = "%Y-%m-%dT%H:%M:%SZ"
    params = {
        "fromStartTime": since.strftime(stamp),
        "toStartTime": until.strftime(stamp),
        "environment": ENVIRONMENT,
        "limit": str(PAGE_LIMIT),
        "fields": "core,basic,io,metadata",
    }
    out: list[dict] = []
    cursor = None
    for _ in range(MAX_PAGES):
        page = dict(params, **({"cursor": cursor} if cursor else {}))
        url = f"{host.rstrip('/')}/api/public/v2/observations?{urllib.parse.urlencode(page)}"
        body = json.loads(fetch(url, headers))
        out.extend(body.get("data") or [])
        cursor = (body.get("meta") or {}).get("cursor")
        if not cursor:
            return out
    raise InfraError("too many pages from Langfuse, stopping")


def _questions_of(text: str) -> set[str]:
    hashes = set()
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and isinstance(row.get("question"), str):
            hashes.add(question_hash(row["question"]))
    return hashes


def known_hashes(queue_dir: Path, eval_texts: list[str]) -> set[str]:
    seen: set[str] = set()
    for text in eval_texts:
        seen |= _questions_of(text)
    for path in sorted(queue_dir.glob("*/*.jsonl")):
        seen |= _questions_of(path.read_text(encoding="utf-8"))
    return seen


def fetch_eval_texts(fetch=_get) -> list[str]:
    return [fetch(url, {"User-Agent": "evalgate-harvest"}).decode("utf-8") for url in EVAL_SETS]


def week_label(moment: datetime) -> str:
    year, week, _ = moment.isocalendar()
    return f"{year}-W{week:02d}"


def select_rows(observations: list[dict], seen: set[str], cap: int = CAP_PER_APP):
    """Returns ({app: rows}, {app: dropped}). Newest first, deduped, capped per app."""
    candidates = [c for c in (parse_observation(o) for o in observations) if c]
    candidates.sort(key=lambda c: c["_start"], reverse=True)
    seen = set(seen)
    rows: dict[str, list[dict]] = {}
    dropped: dict[str, int] = {}
    for cand in candidates:
        digest = question_hash(cand["question"])
        if digest in seen:
            continue
        seen.add(digest)
        app = cand["app"]
        if len(rows.setdefault(app, [])) >= cap:
            dropped[app] = dropped.get(app, 0) + 1
            continue
        rows[app].append({k: v for k, v in cand.items() if not k.startswith("_")})
    return rows, dropped


def write_rows(queue_dir: Path, week: str, rows: dict[str, list[dict]]) -> int:
    written = 0
    for app, items in rows.items():
        if not items:
            continue
        path = queue_dir / app / f"{week}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8", newline="\n") as fh:
            for row in items:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        written += len(items)
    return written


def run_harvest(queue_dir, *, host, public_key, secret_key, now=None, days=7, fetch=_get) -> dict:
    now = now or datetime.now(timezone.utc)
    queue_dir = Path(queue_dir)
    observations = fetch_observations(host, public_key, secret_key, now - timedelta(days=days), now, fetch)
    seen = known_hashes(queue_dir, fetch_eval_texts(fetch))
    rows, dropped = select_rows(observations, seen)
    week = week_label(now)
    return {
        "week": week,
        "written": write_rows(queue_dir, week, rows),
        "per_app": {app: len(items) for app, items in rows.items()},
        "dropped": dropped,
        "read": len(observations),
    }


def env_credentials() -> tuple[str, str, str]:
    names = ("LANGFUSE_HOST", "LANGFUSE_READ_PUBLIC_KEY", "LANGFUSE_READ_SECRET_KEY")
    values = [os.environ.get(n, "") for n in names]
    missing = [n for n, v in zip(names, values) if not v]
    if missing:
        raise InfraError("missing environment: " + ", ".join(missing))
    return values[0], values[1], values[2]
