"""Collect gate results into one json file for the static dashboard.

Artifacts expire after 90 days, so every run is copied into docs/data/runs.json and old
records are never dropped. Only numbers and PR metadata are kept, no case questions or answers.
"""

import io
import json
import subprocess
import zipfile
from pathlib import Path

from evalgate.errors import InfraError

ARTIFACT = "gate-report"


def gh_api(path: str, binary: bool = False):
    """Read-only GET through the gh CLI. The token comes from GH_TOKEN or GITHUB_TOKEN."""
    proc = subprocess.run(["gh", "api", path], capture_output=True, timeout=120)
    if proc.returncode != 0:
        msg = proc.stderr.decode(errors="replace").strip().splitlines()[:1]
        raise InfraError(f"gh api {path} failed: {msg[0] if msg else proc.returncode}")
    return proc.stdout if binary else json.loads(proc.stdout or "null")


def pr_state(pr: dict) -> str:
    if pr.get("merged") or pr.get("merged_at"):
        return "merged"
    return "open" if pr.get("state") == "open" else "closed"


def parse_verdict(raw: bytes | str) -> dict:
    data = json.loads(raw)
    for key in ("n", "base_correct", "head_correct", "b", "c", "p_value", "verdict"):
        if key not in data:
            raise ValueError(f"verdict.json has no {key}")
    return {
        "n": data["n"],
        "base_correct": data["base_correct"],
        "head_correct": data["head_correct"],
        "base_ci": list(data.get("base_ci") or []),
        "head_ci": list(data.get("head_ci") or []),
        "b": data["b"],
        "c": data["c"],
        "p_value": data["p_value"],
        "verdict": data["verdict"],
        "regressed_count": len(data.get("regressed") or []),
        "fixed_count": len(data.get("fixed") or []),
    }


def verdict_from_zip(blob: bytes) -> dict:
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        return parse_verdict(zf.read("verdict.json"))


def build_record(app: str, repo: str, verdict: dict, run: dict, pr: dict | None) -> dict:
    number = (pr or {}).get("number")
    if number is None:
        prs = run.get("pull_requests") or []
        number = prs[0]["number"] if prs else None
    return {
        "app": app,
        "repo": repo,
        "run_id": run["id"],
        "run_url": run["html_url"],
        "created_at": run["created_at"],
        "pr": number,
        "pr_title": (pr or {}).get("title", ""),
        "pr_state": pr_state(pr) if pr else "none",
        "branch": run.get("head_branch", ""),
        **verdict,
    }


def merge_runs(old: list[dict], new: list[dict]) -> list[dict]:
    """Keep every old record. A known (repo, run_id) only takes the fresh PR title and state."""
    by_key = {(r["repo"], r["run_id"]): dict(r) for r in old}
    for rec in new:
        key = (rec["repo"], rec["run_id"])
        if key in by_key:
            by_key[key]["pr_title"] = rec["pr_title"] or by_key[key]["pr_title"]
            by_key[key]["pr_state"] = rec["pr_state"]
        else:
            by_key[key] = dict(rec)
    return sorted(by_key.values(), key=lambda r: (r["created_at"], r["repo"], r["run_id"]))


def list_artifacts(repo: str) -> list[dict]:
    found, page = [], 1
    while True:
        data = gh_api(f"repos/{repo}/actions/artifacts?name={ARTIFACT}&per_page=100&page={page}")
        chunk = data.get("artifacts", [])
        found += [a for a in chunk if not a.get("expired")]
        if len(chunk) < 100:
            return found
        page += 1


def find_pr_number(repo: str, run: dict) -> int | None:
    """Runs of merged PRs often lose pull_requests; look the PR up by head commit, then branch."""
    paths = []
    if run.get("head_sha"):
        paths.append(f"repos/{repo}/commits/{run['head_sha']}/pulls")
    if run.get("head_branch"):
        owner = repo.split("/")[0]
        paths.append(f"repos/{repo}/pulls?state=all&head={owner}:{run['head_branch']}")
    for path in paths:
        try:
            prs = gh_api(path)
        except InfraError:
            continue
        if prs:
            return prs[0]["number"]
    return None


def collect_repo(app: str, repo: str, known: set[tuple[str, int]]) -> list[dict]:
    records, pr_cache = [], {}
    for art in list_artifacts(repo):
        run_id = (art.get("workflow_run") or {}).get("id")
        if run_id is None:
            continue
        run = gh_api(f"repos/{repo}/actions/runs/{run_id}")
        prs = run.get("pull_requests") or []
        number = prs[0]["number"] if prs else None
        if number is None:
            number = find_pr_number(repo, run)
        if number is not None and number not in pr_cache:
            pr_cache[number] = gh_api(f"repos/{repo}/pulls/{number}")
        pr = pr_cache.get(number)
        if (repo, run_id) in known:
            verdict = {}  # only the PR title and state are refreshed for known runs
        else:
            try:
                blob = gh_api(f"repos/{repo}/actions/artifacts/{art['id']}/zip", binary=True)
                verdict = verdict_from_zip(blob)
            except (ValueError, KeyError, zipfile.BadZipFile) as exc:
                print(f"skipping {repo} run {run_id}: {exc}")
                continue
        records.append(build_record(app, repo, verdict, run, pr))
    return records


def collect(repos: list[tuple[str, str]], out: str) -> dict:
    path = Path(out)
    old = json.loads(path.read_text(encoding="utf-8")).get("runs", []) if path.exists() else []
    known = {(r["repo"], r["run_id"]) for r in old}
    fresh = []
    for app, repo in repos:
        fresh += collect_repo(app, repo, known)
    runs = merge_runs(old, fresh)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"runs": runs}, indent=1) + "\n", encoding="utf-8")
    counts = {}
    for r in runs:
        counts[r["app"]] = counts.get(r["app"], 0) + 1
    return {"total": len(runs), "new": len(runs) - len(old), "per_app": counts}
