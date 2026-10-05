"""Running headless Claude Code: the command line, the child's environment, and reading its
stream-json output into an outcome.

Never ``--dangerously-skip-permissions``: in ``-p`` mode a tool outside ``--allowedTools`` is
refused rather than prompted, and a PreToolUse guard blocks pushes to protected branches.
"""

import asyncio
import contextlib
import json
import os
import re
import signal
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import DISALLOWED_TOOLS, RunnerConfig

FINAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["completed", "failed", "input_required", "defer", "waiting"],
            "description": (
                "completed/failed close the task; input_required asks a person; defer = needs more authority; "
                "waiting = you posted subtasks and want to be resumed with their results."
            ),
        },
        "result": {
            "type": "string",
            "description": "Markdown. For completed/failed it must contain a '## Handoff' section.",
        },
        "question": {"type": "string", "description": "For input_required: what the person must answer."},
        "needed_scope": {
            "type": "array",
            "items": {"type": "string"},
            "description": "For defer: the scope the task would need, e.g. deploy.web@project:pact.",
        },
    },
    "required": ["status", "result"],
}

ROLE = """You run unattended for PACT Runner agent {agent}. Nobody watches this session and nobody can
answer a question mid-run. You work on exactly one board task, in the git worktree you start in
(branch {branch}).

Rules:
- Stay inside the task. Commit to your branch and push it; open a PR with `gh pr create` when there is
  code to review. Never push to main or another protected branch, never force-push, never merge.
- Read the project's notes with pact_note before you start, and follow the repository's own checks.
- {workers}
- You cannot claim or close tasks yourself. End by returning the structured result:
  completed or failed (result holds a '## Handoff' section: what was done; repo / branch / PR / commit;
  checks; what is left; what needs a person), input_required (a question only a person can answer),
  defer (the task needs authority your mandate lacks; give needed_scope), or waiting (see above).
"""

WORKERS = """To split off work that can run on its own, post a subtask: pact_post with project_id={project},
  mandate_id={mandate}, parent_task_id={task}, delegate_to one of {names}, a self-contained title and
  body (the worker sees nothing else), and child_limits such as {{"runs": 1, "turns": 20}} taken from
  your own budget. No person approves it while it stays within your budget. Then end with status
  waiting; you are resumed in this session with every subtask's result. Never wait or poll yourself.
  If pact_post is refused (scope_exceeded: this task was not given task.post; limit_exceeded: not
  enough budget), do the work yourself instead."""

NO_WORKERS = "You have no sub-agents: do the work yourself and never end with status waiting."

_QUOTA_TEXT = re.compile(
    r"usage limit|rate limit|hit your limit|limit reached|5-hour limit|weekly limit|out of extra usage|"
    r"\b429\b|overloaded",
    re.I,
)


@dataclass
class Run:
    task_id: str
    prompt: str
    workdir: Path
    branch: str
    mcp_config: Path
    max_turns: int
    resume: str | None = None
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class Outcome:
    session_id: str | None = None
    num_turns: int = 0
    structured: dict[str, Any] | None = None
    subtype: str | None = None
    is_error: bool = False
    text: str = ""
    exit_code: int | None = None
    timed_out: bool = False
    killed: bool = False
    quota_hit: bool = False
    rate_limit: dict[str, Any] | None = None
    stderr: str = ""

    @property
    def resume_failed(self) -> bool:
        return self.is_error and "no conversation found" in (self.text + self.stderr).lower()


def role_prompt(cfg: RunnerConfig, task_id: str, branch: str, mandate_id: str = "", project_id: str = "") -> str:
    extra = cfg.role_file.read_text() if cfg.role_file else ""
    workers = (
        WORKERS.format(project=project_id, mandate=mandate_id, task=task_id, names=", ".join(cfg.workers))
        if cfg.workers
        else NO_WORKERS
    )
    return ROLE.format(agent=cfg.agent_id, branch=branch, workers=workers) + ("\n" + extra if extra else "")


def settings(cfg: RunnerConfig) -> dict[str, Any]:
    hooks: dict[str, Any] = {
        "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": " ".join(cfg.guard)}]}],
    }
    if cfg.entry_hook:
        hooks["PostToolUse"] = [{"matcher": "", "hooks": [{"type": "command", "command": cfg.entry_hook}]}]
    return {"hooks": hooks}


def command(cfg: RunnerConfig, run: Run, role: str) -> list[str]:
    argv = [*cfg.claude, "-p", "--output-format", "stream-json", "--verbose"]
    if run.resume:
        # The session already holds the role; sending it again only costs tokens.
        argv += ["--resume", run.resume]
    else:
        argv += ["--append-system-prompt", role]
    argv += [
        "--mcp-config",
        str(run.mcp_config),
        "--strict-mcp-config",
        "--setting-sources",
        "project",
        "--settings",
        json.dumps(settings(cfg)),
        "--permission-mode",
        "default",
        "--allowedTools",
        ",".join(cfg.allowed_tools),
        "--disallowedTools",
        ",".join(DISALLOWED_TOOLS),
        "--max-turns",
        str(run.max_turns),
        "--json-schema",
        json.dumps(FINAL_SCHEMA),
    ]
    if cfg.model:
        argv += ["--model", cfg.model]
    return argv


def child_env(cfg: RunnerConfig, extra: dict[str, str]) -> dict[str, str]:
    """The parent's environment minus anything that would switch Claude's billing by accident.
    subscription: the logged-in plan, so any API key is dropped (a key would override the login).
    api_key: the configured key only."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("PACT_RUNNER_")}
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
        env.pop(k, None)
    if cfg.auth == "api_key" and cfg.api_key:
        env["ANTHROPIC_API_KEY"] = cfg.api_key
    return {**env, **extra}


class StreamReader:
    """Folds stream-json events into an Outcome as they arrive."""

    def __init__(self) -> None:
        self.out = Outcome()

    def feed(self, line: str) -> None:
        try:
            event = json.loads(line)
        except ValueError:
            return
        if not isinstance(event, dict):
            return
        sid = event.get("session_id")
        if isinstance(sid, str) and sid:
            self.out.session_id = sid
        kind = event.get("type")
        if kind == "rate_limit_event" and isinstance(event.get("rate_limit_info"), dict):
            info = event["rate_limit_info"]
            self.out.rate_limit = info
            if info.get("status") not in (None, "allowed", "allowed_warning"):
                self.out.quota_hit = True
        elif kind == "result":
            self.out.subtype = event.get("subtype")
            self.out.is_error = bool(event.get("is_error"))
            self.out.num_turns = int(event.get("num_turns") or 0)
            self.out.text = str(event.get("result") or "")
            so = event.get("structured_output")
            self.out.structured = so if isinstance(so, dict) else None
            if self.out.is_error and (event.get("api_error_status") == 429 or _QUOTA_TEXT.search(self.out.text)):
                self.out.quota_hit = True


async def execute(
    cfg: RunnerConfig,
    run: Run,
    role: str,
    *,
    should_stop: Callable[[], bool] = lambda: False,
    tick: Callable[[], Awaitable[None]] | None = None,
    tick_seconds: float = 600,
) -> Outcome:
    """Run one ``claude -p`` to the end, a timeout, or ``should_stop``. The child gets its own
    process group, so stopping it takes everything it started: SIGTERM, a grace period, SIGKILL.
    ``tick`` runs every ``tick_seconds`` while the child runs (the board heartbeat)."""
    reader = StreamReader()
    proc = await asyncio.create_subprocess_exec(
        *command(cfg, run, role),
        cwd=run.workdir,
        env=child_env(cfg, run.env),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
        limit=16 * 1024 * 1024,
    )
    assert proc.stdin and proc.stdout and proc.stderr
    proc.stdin.write(run.prompt.encode())
    await proc.stdin.drain()
    proc.stdin.close()

    async def pump() -> None:
        assert proc.stdout
        async for raw in proc.stdout:
            reader.feed(raw.decode(errors="replace"))

    async def drain_err() -> str:
        assert proc.stderr
        return (await proc.stderr.read()).decode(errors="replace")[-4000:]

    pumping = asyncio.create_task(pump())
    err_task = asyncio.create_task(drain_err())
    deadline = time.monotonic() + cfg.run_timeout_seconds
    next_tick = time.monotonic() + tick_seconds
    while proc.returncode is None:
        try:
            await asyncio.wait_for(proc.wait(), timeout=1)
            break
        except TimeoutError:
            pass
        if should_stop():
            reader.out.killed = True
            break
        if time.monotonic() >= deadline:
            reader.out.timed_out = True
            break
        if tick and time.monotonic() >= next_tick:
            next_tick = time.monotonic() + tick_seconds
            await tick()
    if proc.returncode is None:
        await _terminate(proc, cfg.kill_grace_seconds)
    with contextlib.suppress(Exception):
        await asyncio.wait_for(pumping, timeout=5)
    reader.out.stderr = await err_task
    reader.out.exit_code = proc.returncode
    return reader.out


async def _terminate(proc: asyncio.subprocess.Process, grace: float) -> None:
    for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 5.0)):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, sig)
        try:
            await asyncio.wait_for(proc.wait(), timeout=wait)
            return
        except TimeoutError:
            continue
