# evalgate

evalgate is an eval gate for LLM apps. You write a small set of questions with known answers,
and every pull request has to hold its own against the base branch before it merges. The gate
runs the same cases on the base commit and on the PR commit, compares them, and posts one
comment on the PR.

I'm building it in small milestones. The first real user is
[text-to-sql-chat](https://github.com/ramaraihan12/text-to-sql-chat).

## M1: the gate

- Cases are lines in a `.jsonl` file: `id`, `question` and either `gold_sql` or
  `"expect": "no_sql"` for a question the data can't answer. An answer counts as right when the
  rows match the gold query's rows (row order only matters for top-N queries).
- The model is a small Ollama model on the GitHub runner, pinned by tag and digest. The job
  fails if the pulled digest isn't the pinned one.
- Base and PR run as two parallel jobs on separate runners, each with its own Postgres, its own
  `setup` from that commit, and its own Ollama, so they never share a prompt cache. The cases
  come from the PR head for both sides.
- Accuracy is shown per side with a Wilson 95% interval. The comparison is paired on the same
  cases: `b` = right on base and wrong on the PR, `c` = the other way round. The gate fails when
  the PR is less accurate and the exact one-sided McNemar p-value (a binomial on `b + c`) is
  below 0.05.
- A wrong SQL, a blocked query or a model crash counts as a wrong answer. Ollama or Postgres
  being down fails the job as an error, not as a regression.
- It is a reusable workflow, `.github/workflows/gate.yml`. Call it from a `pull_request`
  workflow (not `pull_request_target`). On fork PRs the token can't comment, so the comment is
  skipped and the job summary has the report.

Honest limits: with about 50 cases only large drops are detectable. Even if nothing improved,
at least 5 cases have to flip from right to wrong before the gate fails. A small local model
is also not the model an app runs in production, so this tests the prompt, the schema notes and
the pipeline, not the production model's quality.

## Use it

```yaml
jobs:
  gate:
    uses: ramaraihan12/evalgate/.github/workflows/gate.yml@main
    with:
      app_repo: ${{ github.repository }}
      head_repo: ${{ github.event.pull_request.head.repo.full_name }}
      base_sha: ${{ github.event.pull_request.base.sha }}
      head_sha: ${{ github.event.pull_request.head.sha }}
      pr_number: ${{ github.event.pull_request.number }}
      model: qwen2.5-coder:3b
      model_digest: f72c60cabf6237b07f6e632b2c48d533cef25eda2efbd34bed21c5e9c01e6225
```

The adapter in `src/evalgate/adapters/sqlchat.py` knows how to run text-to-sql-chat. A new app
needs an adapter with `preflight`, `ask`, `gold_rows` and `describe`.

## M0

Before building the gate I measured how fast small Ollama models run the pipeline on a plain
GitHub runner (4 vCPU, no GPU). `bench/m0.py` and the `m0-bench` workflow are still here.
For `qwen2.5-coder:3b`, 12 questions took about 95 seconds.

## Develop

```bash
uv sync
uv run pytest
```
