import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from evalgate import harvest
from evalgate.errors import InfraError

FIXTURE = Path(__file__).parent / "fixtures" / "langfuse_v2_observations_test.json"
NOW = datetime(2026, 11, 23, 3, 0, tzinfo=timezone.utc)


def fixture_rows():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["data"]


def prod(obs: dict, question: str | None = None) -> dict:
    obs = json.loads(json.dumps(obs))
    obs["environment"] = "production"
    if question is not None:
        obs["input"] = json.dumps({"question": question})
    return obs


def test_fixture_is_the_real_shape():
    rows = fixture_rows()
    assert len(rows) == 2
    assert {r["environment"] for r in rows} == {"test"}
    assert all(isinstance(r["input"], str) and isinstance(r["metadata"], dict) for r in rows)


def test_test_environment_and_synthetic_are_never_candidates():
    for obs in fixture_rows():
        assert harvest.parse_observation(obs) is None
        assert harvest.parse_observation(prod(obs)) is None  # synthetic: prefix


def test_parse_flagged_production_row():
    obs = prod(fixture_rows()[0], "How many orders last week?")
    row = harvest.parse_observation(obs)
    assert row["app"] == "sql-chat"
    assert row["question"] == "How many orders last week?"
    assert row["flags"] == ["error"]
    assert row["answer_excerpt"] == "SELECT 1"
    assert row["label"] is None and row["expected"] is None and row["note"] == ""


def test_parse_is_defensive():
    base = prod(fixture_rows()[0], "q one")
    for change in (
        {"type": "SPAN"},
        {"name": "other"},
        {"parentObservationId": "abc"},
        {"input": "   "},
        {"input": "{}"},
        {"metadata": None},
        {"metadata": "{broken"},
    ):
        assert harvest.parse_observation({**base, **change}) is None
    no_flags = {**base, "metadata": {**base["metadata"], "flags": []}}
    assert harvest.parse_observation(no_flags) is None
    other_app = {**base, "metadata": {**base["metadata"], "app": "x"}}
    assert harvest.parse_observation(other_app) is None
    str_meta = {**base, "metadata": json.dumps({"app": "docs-rag", "flags": '["slow"]'})}
    assert harvest.parse_observation(str_meta)["flags"] == ["slow"]
    bare = {**base, "input": "a bare question"}
    assert harvest.parse_observation(bare)["question"] == "a bare question"
    odd_out = {**base, "output": "plain text"}
    assert harvest.parse_observation(odd_out)["answer_excerpt"] == "plain text"


def test_excerpt_is_capped():
    obs = prod(fixture_rows()[0], "long one")
    obs["output"] = json.dumps({"answer": "x" * 1000})
    assert len(harvest.parse_observation(obs)["answer_excerpt"]) == 300


def test_normalize_hash():
    assert harvest.question_hash("  How MANY   orders?? ") == harvest.question_hash("how many orders")


def test_select_dedupes_caps_and_counts_drops():
    base = fixture_rows()[1]
    obs = []
    for i in range(55):
        o = prod(base, f"question number {i}")
        o["startTime"] = f"2026-11-20T00:00:{i:02d}.000Z"
        obs.append(o)
    obs.append(prod(base, "Question  Number 3?"))  # duplicate of 3
    obs.append(prod(base, "already in the evals"))
    seen = {harvest.question_hash("already in the evals")}
    rows, dropped = harvest.select_rows(obs, seen)
    assert len(rows["docs-rag"]) == 50
    assert dropped == {"docs-rag": 5}
    questions = [r["question"] for r in rows["docs-rag"]]
    assert "already in the evals" not in questions
    assert len({harvest.normalize(q) for q in questions}) == 50


def test_known_hashes_reads_queue_and_evals(tmp_path):
    (tmp_path / "docs-rag").mkdir()
    (tmp_path / "docs-rag" / "2026-W46.jsonl").write_text(
        json.dumps({"question": "In the queue"}) + "\n\n", encoding="utf-8"
    )
    eval_text = json.dumps({"id": "r1", "question": "In the evals"}) + "\nnot json\n"
    seen = harvest.known_hashes(tmp_path, [eval_text])
    assert harvest.question_hash("in the queue?") in seen
    assert harvest.question_hash("in the evals") in seen


def test_week_label_is_iso():
    assert harvest.week_label(datetime(2026, 11, 23, tzinfo=timezone.utc)) == "2026-W48"
    assert harvest.week_label(datetime(2027, 1, 1, tzinfo=timezone.utc)) == "2026-W53"


class FakeNet:
    def __init__(self, pages, evals=("", "")):
        self.pages = pages
        self.evals = list(evals)
        self.urls = []

    def __call__(self, url, headers):
        self.urls.append((url, headers))
        if "raw.githubusercontent.com" in url:
            return self.evals[0 if "rag_cases" in url else 1].encode()
        return json.dumps(self.pages.pop(0)).encode()


def test_fetch_follows_cursor_and_sends_params():
    net = FakeNet([{"data": [{"id": "a"}], "meta": {"cursor": "c1"}}, {"data": [{"id": "b"}], "meta": {}}])
    got = harvest.fetch_observations("https://lf.example", "pk", "sk", NOW, NOW, net)
    assert [o["id"] for o in got] == ["a", "b"]
    first, second = net.urls[0][0], net.urls[1][0]
    assert "environment=production" in first and "fields=core%2Cbasic%2Cio%2Cmetadata" in first
    assert "cursor=" not in first and "cursor=c1" in second
    assert net.urls[0][1]["Authorization"].startswith("Basic ")


def test_fetch_refuses_plain_http():
    with pytest.raises(InfraError):
        harvest.fetch_observations("http://lf.example", "pk", "sk", NOW, NOW, FakeNet([]))


def test_run_harvest_writes_once_and_is_idempotent(tmp_path):
    good = prod(fixture_rows()[1], "Where is the travel policy?")
    dup = prod(fixture_rows()[0], "In the evals")
    pages = lambda: [{"data": [good, dup, fixture_rows()[0]], "meta": {}}]
    evals = (json.dumps({"question": "in the evals"}), "")
    kwargs = dict(host="https://lf.example", public_key="pk", secret_key="sk", now=NOW)

    result = harvest.run_harvest(tmp_path, fetch=FakeNet(pages(), evals), **kwargs)
    assert result["week"] == "2026-W48" and result["written"] == 1
    path = tmp_path / "docs-rag" / "2026-W48.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1 and rows[0]["question"] == "Where is the travel policy?"
    assert set(rows[0]) == {
        "trace_id", "observation_id", "app", "question", "answer_excerpt",
        "flags", "label", "expected", "note",
    }
    assert not (tmp_path / "sql-chat").exists()

    again = harvest.run_harvest(tmp_path, fetch=FakeNet(pages(), evals), **kwargs)
    assert again["written"] == 0
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1


def test_env_credentials_names_missing(monkeypatch):
    for name in ("LANGFUSE_HOST", "LANGFUSE_READ_PUBLIC_KEY", "LANGFUSE_READ_SECRET_KEY"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(InfraError, match="LANGFUSE_HOST"):
        harvest.env_credentials()
