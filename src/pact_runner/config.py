"""Runner settings, read from the environment (a launchd job passes them in, or an env file)."""

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

AuthMode = Literal["subscription", "api_key"]

DEFAULT_ALLOWED_TOOLS = (
    "Read",
    "Edit",
    "Write",
    "Glob",
    "Grep",
    "TodoWrite",
    "Bash(git status:*)",
    "Bash(git diff:*)",
    "Bash(git log:*)",
    "Bash(git show:*)",
    "Bash(git add:*)",
    "Bash(git commit:*)",
    "Bash(git switch -c:*)",
    "Bash(git checkout -b:*)",
    "Bash(git push:*)",
    "Bash(gh pr create:*)",
    "Bash(gh pr view:*)",
    "Bash(ls:*)",
    "Bash(uv run:*)",
    "Bash(uv sync:*)",
    "Bash(npm test:*)",
    "Bash(npm run:*)",
    "Bash(npx tsc:*)",
    "mcp__pact__pact_whoami",
    "mcp__pact__pact_list",
    "mcp__pact__pact_note",
    "mcp__pact__pact_post",
)
"""What a run may use without asking. In ``claude -p`` anything else is refused, never prompted."""

DISALLOWED_TOOLS = (
    "mcp__pact__pact_claim",
    "mcp__pact__pact_report",
    "mcp__pact__pact_defer",
    "mcp__pact__pact_revoke",
)
"""The Runner alone claims and closes tasks; the session hands its answer back as structured output."""


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _list(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = _env(name)
    return tuple(x.strip() for x in raw.split(",") if x.strip()) if raw else default


def _hours(raw: str) -> tuple[int, int] | None:
    """``"9-18"`` → (9, 18): local hours during which no new run starts."""
    if not raw:
        return None
    start, end = (int(x) for x in raw.split("-", 1))
    if not (0 <= start <= 23 and 0 <= end <= 24):
        raise ValueError(f"PACT_RUNNER_QUIET_HOURS={raw!r} must look like 9-18")
    return start, end


@dataclass
class RunnerConfig:
    board_url: str
    agent_id: str
    token: str
    state_dir: Path
    repo: Path | None = None
    """A clone the Runner owns; each task works in a worktree of it. None: a plain directory per task."""
    mandate_id: str | None = None
    """The runner's own (root) mandate; by default the first live one pact_whoami lists."""
    project_id: str | None = None
    take_open: bool = False
    """Also take open tasks delegated to nobody. Off: only tasks delegated to this runner."""
    poll_seconds: float = 30
    heartbeat_seconds: float = 600
    run_timeout_seconds: float = 1800
    kill_grace_seconds: float = 20
    max_turns: int = 40
    concurrency: int = 1
    max_runs_per_day: int = 20
    quiet_hours: tuple[int, int] | None = None
    reserve_five_hour: float = 0.7
    """Start no run once the 5-hour quota is this used, so the owner keeps the rest."""
    reserve_seven_day: float = 0.8
    auth: AuthMode = "subscription"
    api_key: str | None = None
    model: str | None = None
    claude: tuple[str, ...] = ("claude",)
    allowed_tools: tuple[str, ...] = DEFAULT_ALLOWED_TOOLS
    role_file: Path | None = None
    entry_hook: str | None = None
    """Shell command for a PostToolUse hook that logs entries to the board (hooks/pact-hook.sh)."""
    guard: tuple[str, ...] = field(default_factory=lambda: (sys.executable, "-m", "pact_runner.guard"))

    @property
    def mcp_url(self) -> str:
        return f"{self.board_url.rstrip('/')}/mcp/a/{self.agent_id}"

    @classmethod
    def from_env(cls) -> "RunnerConfig":
        agent = _env("PACT_RUNNER_AGENT")
        token = _env("PACT_RUNNER_TOKEN")
        url = _env("PACT_URL")
        missing = [n for n, v in (("PACT_URL", url), ("PACT_RUNNER_AGENT", agent), ("PACT_RUNNER_TOKEN", token)) if not v]
        if missing:
            raise SystemExit(f"pact-runner: set {', '.join(missing)}")
        auth = _env("PACT_RUNNER_AUTH", "subscription")
        if auth not in ("subscription", "api_key"):
            raise SystemExit("pact-runner: PACT_RUNNER_AUTH must be subscription or api_key")
        api_key = _env("PACT_RUNNER_ANTHROPIC_API_KEY") or None
        if auth == "api_key" and not api_key:
            raise SystemExit("pact-runner: PACT_RUNNER_AUTH=api_key needs PACT_RUNNER_ANTHROPIC_API_KEY")
        state = Path(_env("PACT_RUNNER_STATE_DIR") or Path.home() / ".local/state/pact-runner" / agent).expanduser()
        repo = _env("PACT_RUNNER_REPO")
        role = _env("PACT_RUNNER_ROLE_FILE")
        return cls(
            board_url=url,
            agent_id=agent,
            token=token,
            state_dir=state,
            repo=Path(repo).expanduser() if repo else None,
            mandate_id=_env("PACT_RUNNER_MANDATE") or None,
            project_id=_env("PACT_RUNNER_PROJECT") or None,
            take_open=_env("PACT_RUNNER_TAKE_OPEN") == "1",
            poll_seconds=float(_env("PACT_RUNNER_POLL_SECONDS", "30")),
            heartbeat_seconds=float(_env("PACT_RUNNER_HEARTBEAT_SECONDS", "600")),
            run_timeout_seconds=float(_env("PACT_RUNNER_RUN_TIMEOUT_MINUTES", "30")) * 60,
            max_turns=int(_env("PACT_RUNNER_MAX_TURNS", "40")),
            concurrency=max(int(_env("PACT_RUNNER_CONCURRENCY", "1")), 1),
            max_runs_per_day=int(_env("PACT_RUNNER_MAX_RUNS_PER_DAY", "20")),
            quiet_hours=_hours(_env("PACT_RUNNER_QUIET_HOURS")),
            reserve_five_hour=float(_env("PACT_RUNNER_RESERVE_FIVE_HOUR", "0.7")),
            reserve_seven_day=float(_env("PACT_RUNNER_RESERVE_SEVEN_DAY", "0.8")),
            auth=auth,  # type: ignore[arg-type]
            api_key=api_key,
            model=_env("PACT_RUNNER_MODEL") or None,
            claude=tuple(_env("PACT_RUNNER_CLAUDE", "claude").split()),
            allowed_tools=_list("PACT_RUNNER_ALLOWED_TOOLS", DEFAULT_ALLOWED_TOOLS),
            role_file=Path(role).expanduser() if role else None,
            entry_hook=_env("PACT_RUNNER_ENTRY_HOOK") or None,
        )
