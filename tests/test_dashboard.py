import io
import json
import zipfile

import pytest

from evalgate.dashboard import build_record, merge_runs, parse_verdict, pr_state, verdict_from_zip

VERDICT = {
    "n": 50, "base_correct": 26, "head_correct": 17, "base_ci": [0.385, 0.652],
    "head_ci": [0.224, 0.478], "b": 10, "c": 1, "p_value": 0.0059, "alpha": 0.05,
    "verdict": "FAIL", "regressed": ["c01", "c02"], "fixed": ["c23"],
}
RUN = {"id": 7, "html_url": "https://x/runs/7", "created_at": "2026-11-10T10:00:00Z",
       "head_branch": "drop-revenue-rules", "pull_requests": [{"number": 2}]}


def test_parse_keeps_numbers_and_counts_only():
    out = parse_verdict(json.dumps(VERDICT))
    assert out["regressed_count"] == 2 and out["fixed_count"] == 1
    assert "regressed" not in out and "alpha" not in out
    assert out["base_ci"] == [0.385, 0.652]


def test_parse_rejects_incomplete():
    with pytest.raises(ValueError):
        parse_verdict('{"n": 1}')


def test_zip_roundtrip():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("report.md", "x")
        zf.writestr("verdict.json", json.dumps(VERDICT))
    assert verdict_from_zip(buf.getvalue())["verdict"] == "FAIL"


def test_pr_state():
    assert pr_state({"state": "open"}) == "open"
    assert pr_state({"state": "closed", "merged": True}) == "merged"
    assert pr_state({"state": "closed", "merged_at": None}) == "closed"


def rec(run_id, created, state="open", title="t", merged=False):
    run = {**RUN, "id": run_id, "created_at": created}
    return build_record("sql-chat", "o/r", parse_verdict(json.dumps(VERDICT)), run,
                        {"title": title, "state": state, "merged": merged})


def test_build_record_fields():
    r = rec(7, "2026-11-10T10:00:00Z", title="drop rules")
    assert (r["pr"], r["pr_title"], r["branch"], r["pr_state"]) == (2, "drop rules", "drop-revenue-rules", "open")


def test_merge_keeps_old_updates_state_and_sorts():
    old = [rec(2, "2026-11-12T00:00:00Z"), rec(1, "2026-11-10T00:00:00Z")]
    new = [rec(2, "2026-11-12T00:00:00Z", state="closed", title="new title", merged=True), rec(3, "2026-11-11T00:00:00Z")]
    out = merge_runs(old, new)
    assert [r["run_id"] for r in out] == [1, 3, 2]
    assert out[2]["pr_state"] == "merged" and out[2]["pr_title"] == "new title"


def test_merge_does_not_erase_expired():
    old = [rec(1, "2026-08-01T00:00:00Z")]
    assert merge_runs(old, []) == old
