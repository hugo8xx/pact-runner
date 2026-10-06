# PACT Runner

Headless Claude Code for [PACT Board](https://github.com/hugo8xx/pact-board). A board cannot push
work to anyone, so `pact-runner` polls it for tasks delegated to a `runner` agent, claims one, runs
`claude -p` on it in a git worktree of its own and reports back with the turns it spent.

This package holds three commands:

| Command | What |
| --- | --- |
| `pact-runner` | the Runner loop |
| `pact-runner-guard` | the PreToolUse guard every Runner session runs before a Bash call |
| `pact-connect` | trades a board's one-time setup code for an agent token and sets the machine up |

## Install

```bash
uv tool install "pact-runner @ git+https://github.com/hugo8xx/pact-runner"
```

## Connect a machine

Hire the agent on the board (Admin UI, or `POST /admin/api/agents/hire`). An agent that uses a token
gets a one-time setup code, valid for 15 minutes. On the machine the agent will use, run:

```bash
pact-connect <board-url> <setup-code> [--repo <git-url>]
```

It posts the code to the board's `POST /connect` and gets the token back once. It never prints the
token; it writes it straight into place:

- Claude Code: the `pact` MCP server for the project directory, plus the hooks env file.
- Gemini CLI: the `pact` MCP server in `~/.gemini/settings.json` (user level, mode 600, everything
  else in the file kept). Not a project's `.gemini/settings.json`, which could be committed.
- Runner: its env file (mode 600) and role instructions, a clone (`--repo`), and on macOS a
  LaunchAgent whose `PATH` holds the `claude`, `uv`, `gh` and `git` found on that machine.

## How a task runs

One process per runner agent, on a machine that has a logged-in `claude`:

1. Every 30 s it calls `pact_list(since=…)` and keeps the tasks delegated to it (`PACT_RUNNER_TAKE_OPEN=1`
   also takes undelegated ones).
2. It claims one with `reserve={"runs": 1}`, so a task whose budget has no run left is never
   started. `--max-turns` is the smaller of `PACT_RUNNER_MAX_TURNS` and the turns left in the budget.
3. It runs `claude -p --output-format stream-json` in a git worktree on branch `runner/<task>`. The
   process gets its own process group. These limits apply:
   - Only the tools in the allowlist, with `--permission-mode default`; never
     `--dangerously-skip-permissions`.
   - Only project settings (`--setting-sources project`), so a person's own allow rules never reach it.
   - The owner's CLAUDE.md does reach it: that setting would otherwise hide `~/.claude/CLAUDE.md`, so
     the Runner reads the files in `PACT_RUNNER_CONTEXT_FILES` (default `~/.claude/CLAUDE.md`, `none`
     to turn it off) for every new session and puts them into the role. Only the text goes in, no
     settings, permissions or hooks. A missing file is skipped.
   - A PreToolUse guard (`pact-runner-guard`) refuses pushes to `main`/`master`/`stage`/`staging`/`release`,
     force pushes and `gh pr merge`. Branch protection on the host is still the hard stop.
   - The same guard reads what a push or a `gh pr|issue|release|gist` write would publish: every
     outgoing commit (message and added lines) and the PR or comment text, including a body file.
     It refuses secrets (API keys, tokens, private keys, passwords in URLs), this machine's home path
     and any word in `PACT_RUNNER_DENY_FILE` (one per line: real hosts, names, emails). On a public
     repository these would be public for good. A line with an obviously fake test value can carry
     `leak-ok`.
   - The board as an MCP server, minus `pact_claim`/`pact_report`/`pact_defer`/`pact_revoke`. The session
     answers through `--json-schema` structured output (`completed`, `failed`, `input_required` or
     `defer`), and the Runner reports for it with `usage={"turns": n}`.
4. It sends a heartbeat every 10 minutes. On `claim_lost` it kills that run.
5. On `system_halted`, `agent_paused` or a dead runner mandate it kills every run and exits 0. A
   supervisor should leave it stopped (done-criterion 20). One exception: when its own mandate dies but
   a person has issued it a newer root mandate (`pact-admin mandate-issue`), it carries on under that
   one, so renewing a runner's mandate needs no restart. A new token still does.
6. A question, a timeout, running out of turns or a usage limit becomes `input_required` with the
   session saved, never a retry loop. When a person answers and resumes the task, the Runner continues
   the same session (`--resume`, without resending the role). If that session is gone, it starts a
   fresh one once.
7. **Sub-agents.** With `PACT_RUNNER_WORKERS=worker-a,…` (other runner agents), the role tells the
   session how to split off work: `pact_post(delegate_to=<worker>, parent_task_id=<task>,
   child_limits=…)` taken from its own budget, then end with status `waiting`. No person approves
   this while it stays within budget. The Runner keeps the task claimed (heartbeats) until every
   subtask is closed. It then takes another run from the task's budget and resumes the same session
   with the subtasks' results. A task waits at most `PACT_RUNNER_MAX_WAIT_HOURS` (24) before a
   person is asked. Splitting needs authority too: whoever delegates the task must include
   `task.post@project:<p>` in `child_scope`, otherwise the split is refused with `scope_exceeded`.
   `pact_list(filter="all", parent_task_id=…)` lists a task's subtasks.
8. **Scheduled tasks.** `PACT_RUNNER_SCHEDULE` is a JSON list of jobs, for example
   `{"at": "07:30", "title": "Daily brief", "action": "report.brief", "body": "… since {since} …"}`.
   Once a day, from that local time on, the runner posts each job under its own mandate and claims it
   itself. A sleeping Mac catches up the same day. `{date}` and `{since}` in the body are replaced:
   `{since}` is the board cursor from when the previous run of that job was posted. `pact_list` answers
   with `head`, the newest change, for this.
9. Each run's `rate_limit_event` reports how much of the 5-hour and 7-day quota is used. While a
   window is past `PACT_RUNNER_RESERVE_FIVE_HOUR` (0.7) or `PACT_RUNNER_RESERVE_SEVEN_DAY` (0.8), or a
   limit was hit, no new run starts until that window resets, so the owner keeps the rest. There is
   also a daily run cap and optional quiet hours.

`PACT_RUNNER_AUTH=subscription` strips every Anthropic credential from the child's environment so the
login is used; `api_key` passes `PACT_RUNNER_ANTHROPIC_API_KEY` instead.

Settings are in `examples/runner/runner.env.example`, and a macOS LaunchAgent is in
`examples/runner/com.example.pact-runner.plist`. Register the agent with a budget, e.g.
`pact-admin agent-register runner-web --client runner --projects web --limits '{"runs": 50, "turns": 2000}' --days 7 --by <you>`.
For a task, the budget a Runner follows is the one on the mandate delegated with that task.

## Checks

The tests run the Runner against a real board in-process (`pact-board` is a dev dependency pinned
to a commit), so they need Postgres:

```bash
createdb pact_test               # once, or point DATABASE_URL at another database
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
```

The test suite drops and recreates the `public` schema of the test database on every run.

## Layout

| Path | What |
| --- | --- |
| `src/pact_runner/runner.py` | the loop: poll, claim with a reserved run, run, report |
| `src/pact_runner/claude.py` | the `claude -p` command line, the role, the child's environment, the stream-json reader |
| `src/pact_runner/guard.py` | the push guard: protected branches, force pushes, merges, leaks |
| `src/pact_runner/leaks.py` | what must not leave the machine in a push or a PR |
| `src/pact_runner/connect.py` | `pact-connect` |
| `src/pact_runner/board.py` | the board as an MCP client |
| `src/pact_runner/store.py` | sessions, waits and schedules in a local sqlite file |
| `examples/runner/` | settings, a macOS LaunchAgent and a secretary role |
| `tests/` | the Runner against a real board with a fake `claude` |

## License

Apache-2.0, see `LICENSE`.
