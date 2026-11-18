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
  fails if the pulled digest isn't the full pinned one.
- Base and PR run as two parallel jobs on separate runners, each with its own Postgres, its own
  `setup` from that commit, and its own Ollama, so they never share a prompt cache. The cases
  come from the PR merge commit for both sides.
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

## M2: documents

The second app is [docs-rag-assistant](https://github.com/ramaraihan12/docs-rag-assistant), a
question answering app over PDF, Markdown and DOCX files. Its gate is the reusable workflow
`.github/workflows/gate-rag.yml` and the adapter `src/evalgate/adapters/docsrag.py`.

- Cases are lines in a `.jsonl` file: `id`, `question`, `file` and `page` or `section`, which is
  where the answer is, or `"file": null` for a question the documents don't answer.
- Each side gets its own runner with pgvector, Ollama and the app checked out at that commit. The
  samples are indexed from that checkout with that checkout's code, so chunking, retrieval and the
  prompt are all part of what is tested. Then the app answers every question the way its
  `/api/ask` does.
- No model grades the answers. For each case the gated result is:
  - answerable: the expected passage is among the retrieved sources, and the answer cites at
    least one source number, every cited number exists, and at least one cited source is the
    expected passage;
  - unanswerable: the answer says the documents don't cover it and cites no retrieved source. A
    clause counts when it refers to the documents, has a negation and doesn't attribute the claim
    to them ("according to the documents, ..." does not count). A short list of English and
    Indonesian phrases, in `ragmetrics.py`, with tests.
- The comment also shows, per side and not gated: retrieval hit rate, MRR, citation rate,
  citations per answer, the share of citations pointing at a file other than the expected one,
  citations to sources that don't exist, and the abstain rate. The comment also lists dataset
  cases the PR removed, changed or added.
- The comparison is the same as M1: paired on the cases, exact one-sided McNemar, fail when the
  PR is less accurate and p < 0.05.
- The gold is the expected file and page in the dataset, read from the merge commit for both
  sides, and the grading code lives here. The gate guards against honest regressions. The PR's
  own code runs on the head side, so changes to `.github/workflows/` and `evals/` need a human
  review.

The chat model is small on purpose so one side finishes in about 7 to 12 minutes on a 4 vCPU runner, install included
(`qwen3:1.7b` for answers and `qwen3-embedding:0.6b` for retrieval, the app's own embedding
model, both pinned by digest). I picked it by running the real 40 cases on a few candidates:

| chat model | passed | citation rate | abstain rate | one side, whole job |
|---|---|---|---|---|
| qwen2.5:1.5b | 8/40 | 12% | 4/6 | 7 min |
| qwen2.5:3b | 27/40 | 71% | 3/6 | 8 min |
| llama3.2:3b | 27/40 | 62% | 6/6 | 11 min |
| qwen3:1.7b | 37/40 | 91% | 6/6 | 7 min |

(Retrieval hit rate was 100% for all of them. The 1.5B model answered correctly but almost never
cited a source, so it would have measured its habits and not the app.) With about 40 cases only
large drops are detectable, and a 1.7B model is not the model the live app uses, so this watches
chunking, retrieval and the prompt, not the quality of the production model's answers.

Runners are not all the same: in my timing runs the same job landed on an AMD EPYC 7763, an EPYC
9V74 and a Xeon 6975P, and the 3B model took 26 s per question on the first and about 9 s on the
last. Both sides record the CPU and the comment warns when they differ.

## Use it

```yaml
jobs:
  gate:
    uses: ramaraihan12/evalgate/.github/workflows/gate.yml@v0.1.0
    permissions:
      contents: read
      pull-requests: write
    with:
      app_repo: ${{ github.repository }}
      pr_number: ${{ github.event.pull_request.number }}
      model: qwen2.5-coder:3b
      model_digest: f72c60cabf6237b07f6e632b2c48d533cef25eda2efbd34bed21c5e9c01e6225
      evalgate_ref: v0.1.0
```

The PR side is the merge commit GitHub builds for the PR and the base side is its first parent,
so new commits on the base branch don't count against the PR. Both sides record the CPU, the
vCPU count and the Ollama version (pinned, 0.40.16 by default); the comment warns when they differ.
The gold rows come from the adapter's own Postgres connection, not from the app under test.

For documents, call `gate-rag.yml` the same way and also pass `embed_model` and
`embed_model_digest`. See docs-rag-assistant's `.github/workflows/eval-gate.yml`.

The adapter in `src/evalgate/adapters/sqlchat.py` knows how to run text-to-sql-chat. A new app
needs an adapter with `preflight`, `describe` and either `ask` and `gold_rows` (SQL) or
`run_case` (the adapter grades a case itself, like `docsrag.py`).

## M0

Before building the gate I measured how fast small Ollama models run the pipeline on a plain
GitHub runner (4 vCPU, no GPU). `bench/m0.py` and the `m0-bench` workflow are still here.
For `qwen2.5-coder:3b`, 12 questions took about 95 seconds.

## Develop

```bash
uv sync
uv run pytest
```
