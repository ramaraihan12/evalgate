"""One sticky PR comment, found by a hidden marker."""

import json
import os
import urllib.error
import urllib.request

from evalgate.report import MARKER

API = "https://api.github.com"


def _request(method: str, url: str, token: str, body: dict | None = None):
    req = urllib.request.Request(
        url,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "evalgate",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read() or "null")


def find_sticky(comments: list[dict]) -> dict | None:
    for comment in comments:
        if MARKER in (comment.get("body") or "") and (comment.get("user") or {}).get("type") == "Bot":
            return comment
    return None


def post_or_update(repo: str, pr: int, body: str, token: str | None = None) -> str:
    """Returns 'created', 'updated' or 'skipped'. Never raises on a missing permission."""
    token = token or os.environ.get("GITHUB_TOKEN", "")
    if not token:
        return "skipped"
    base = f"{API}/repos/{repo}/issues"
    try:
        comments, page = [], 1
        while True:
            chunk = _request("GET", f"{base}/{pr}/comments?per_page=100&page={page}", token)
            comments += chunk
            if len(chunk) < 100:
                break
            page += 1
        existing = find_sticky(comments)
        if existing:
            _request("PATCH", f"{base}/comments/{existing['id']}", token, {"body": body})
            return "updated"
        _request("POST", f"{base}/{pr}/comments", token, {"body": body})
        return "created"
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403, 404):  # fork PRs get a read-only token
            return "skipped"
        raise
