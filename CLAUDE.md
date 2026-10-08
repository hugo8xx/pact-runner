# pact-runner

Read by Claude Code sessions in this repository, including PACT Runner sessions.

## This repository is public

Everything here is public for good: files, commit messages, branch names, PR titles, bodies and
comments. A later commit does not take anything back. So never write any of these into them:

- tokens, keys, passwords, setup codes, webhook URLs or connection strings, even partial ones
- the production hosts and URLs of a real deployment, or its account, team or project names
- personal names, email addresses, home-directory paths (`/Users/<name>/…`) or machine details
- board task ids, mandate ids, note contents or anything else copied from a private board or repo

Use placeholders (`https://board.example`, `/Users/you`, `runner-myproject`) and fake secrets that
are obviously fake. If a task seems to need any of the above in the repo, stop and ask (`input_required`).
The Runner's guard (`src/pact_runner/guard.py`) refuses a push or a PR that holds any of this. Build fake secrets at run time
(`"sk-" + "ant-" + "x" * 24`) rather than writing them out, or mark the line `leak-ok`. CI runs
gitleaks on every PR as well.

## Layout

- `src/pact_runner/`: PACT Runner (headless `claude -p` per task), `pact-runner-guard`, `pact-connect` and `pact-hook`.
  It talks to the board over MCP only; never import `pact` (the board) from here.
- `tests/`: pytest. The Runner runs against a real board in-process (`pact-board`, a dev dependency
  pinned to a commit) and Postgres (`pact_test` by default, or `DATABASE_URL`).

## Checks

Run all four after every edit, even a one-line one, and before opening a PR:

```
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
```

A runner should use its own test database through `DATABASE_URL` so it never collides with another.

## Git

- Work on a branch and open a PR. Never push to `main`, never force-push a shared branch, never merge.
- Conventional Commits (`feat(runner): …`, `fix(guard): …`); the body says why.
- Machines install the Runner from `main`, so a PR must be complete: code, tests, README when behaviour changes.
