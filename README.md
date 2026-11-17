# evalgate

evalgate is going to be an eval gate and tracing tool for LLM apps. You write a small set of
questions with known answers, and every pull request has to pass them before it merges.
When a run fails, you get a trace that shows which call went wrong.

This repo is at milestone M0. Before building anything, I want to know how fast small Ollama
models run a real app on a plain GitHub runner (4 vCPU, no GPU), and how often they get it right.
M0 runs the pipeline from [text-to-sql-chat](https://github.com/ramaraihan12/text-to-sql-chat)
on a few models and writes down the numbers. Start it from the Actions tab: `m0-bench`.
