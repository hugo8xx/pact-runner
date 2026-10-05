"""PACT Runner against the real board, with a fake `claude` standing in for Claude Code.

Done-criteria 13 (a task runs to the end with no person touching it), 14 (work beyond the mandate
is deferred, not attempted) and 20 (the kill switch stops a running task and the runner itself).
"""

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from pact.board import Agent, Board
from pact.db import fetchall, transaction
from pact.errors import PactError
from pact.scope import board_scope
from pact_runner.board import BoardRefusal
from pact_runner.config import RunnerConfig
from pact_runner.runner import Runner

from .conftest import World

pytestmark = pytest.mark.anyio

FAKE = Path(__file__).parent / "runner_fixtures" / "fake_claude.py"


class InProcessBoard:
    """The Runner's BoardClient, calling the board directly instead of over MCP."""

    def __init__(self, board: Board, agent: Agent) -> None:
        self.board = board
        self.agent = agent
        self.calls: list[str] = []

    async def call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(tool)
        fn = {
            "pact_whoami": self.board.whoami,
            "pact_list": self.board.list_tasks,
            "pact_claim": self.board.claim,
            "pact_report": self.board.report,
            "pact_defer": self.board.defer,
        }[tool]
        try:
            return await fn(self.agent, **args)  # type: ignore[operator]
        except PactError as err:
            raise BoardRefusal(err.code, err.message) from err


class Fake:
    def __init__(self, tmp: Path) -> None:
        self.control = tmp / "control.json"
        self.log = tmp / "claude.log"
        self.mode("completed")

    def mode(self, mode: str) -> None:
        self.control.write_text(json.dumps({"mode": mode, "log": str(self.log)}))

    def script(self, *modes: str) -> None:
        self.control.write_text(json.dumps({"script": list(modes), "log": str(self.log)}))

    def release(self) -> None:
        (self.control.parent / "release").write_text("")

    def calls(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Fake:
    f = Fake(tmp_path)
    monkeypatch.setenv("FAKE_CLAUDE_CONTROL", str(f.control))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-reach-the-child")
    return f


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    for args in (["init", "-q", "-b", "main"], ["commit", "-q", "--allow-empty", "-m", "init"]):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args], cwd=path, check=True)
    return path


async def setup(w: World, tmp: Path, repo: Path | None, **cfg: Any) -> Runner:
    await w.project("web")
    await w.agent("chat-boss", "chat", ["web"], limits={"runs": 5, "turns": 100})
    await w.agent("runner-web", "runner", ["web"])
    config = RunnerConfig(
        board_url="http://board.invalid",
        agent_id="runner-web",
        token="secret-token",
        state_dir=tmp / "state",
        repo=repo,
        claude=(sys.executable, str(FAKE)),
        **cfg,
    )
    runner = Runner(config, InProcessBoard(w.board, w.agents["runner-web"]))
    await runner.start()
    return runner


async def delegate(w: World, title: str = "fix it", limits: dict[str, float] | None = None, to: str = "runner-web") -> str:
    t = await w.board.post(
        w.agents["chat-boss"],
        project_id="web",
        title=title,
        body="Please fix the thing.",
        mandate_id=w.roots["chat-boss"],
        delegate_to=to,
        child_limits=limits if limits is not None else {"runs": 2, "turns": 30},
    )
    return str(t["task_id"])


async def task_row(w: World, task_id: str) -> dict[str, Any]:
    async with transaction(w.pool) as conn:
        [row] = await fetchall(
            conn, "SELECT status, deferred, result, needed_scope, assignee FROM tasks WHERE id = %s", (task_id,)
        )
    return dict(row)


async def once(runner: Runner) -> None:
    await runner.tick()
    await runner.drain()


# ── criterion 13 ──────────────────────────────────────────────────────────────


async def test_13_a_delegated_task_runs_to_the_end_unattended(world: World, tmp_path: Path, fake: Fake, repo: Path) -> None:
    runner = await setup(world, tmp_path, repo)
    tid = await delegate(world)
    await once(runner)

    row = await task_row(world, tid)
    assert row["status"] == "completed" and "## Handoff" in row["result"]
    [call] = fake.calls()
    argv = call["argv"]
    assert "--dangerously-skip-permissions" not in argv
    assert argv[argv.index("--permission-mode") + 1] == "default"
    assert argv[argv.index("--max-turns") + 1] == "30"  # the task's budget, below the runner's 40
    assert "--append-system-prompt" in argv and "--resume" not in argv
    assert "mcp__pact__pact_report" in argv[argv.index("--disallowedTools") + 1]
    assert "Please fix the thing." in call["stdin"]
    assert call["api_key"] is False  # subscription mode never hands the child an API key
    assert call["cwd"].endswith(tid[:8]) and not await asyncio.to_thread(Path(call["cwd"]).exists)  # worktree removed
    git = ["git", "branch", "--list", "runner/*"]
    branches = (await asyncio.to_thread(subprocess.run, git, cwd=repo, capture_output=True, text=True)).stdout
    assert f"runner/{tid[:8]}" in branches  # the branch stays for the PR

    async with transaction(world.pool) as conn:
        used = {
            r["limit_key"]: float(r["used"])
            for r in await fetchall(
                conn, "SELECT limit_key, used FROM limit_usage WHERE mandate_id = %s", (world.roots["chat-boss"],)
            )
        }
    assert used == {"runs": 1, "turns": 3}


async def test_a_result_without_a_handoff_gets_one(world: World, tmp_path: Path, fake: Fake) -> None:
    runner = await setup(world, tmp_path, None)
    fake.mode("no_handoff")
    tid = await delegate(world)
    await once(runner)
    row = await task_row(world, tid)
    assert row["status"] == "completed" and "## Handoff" in row["result"] and "runner-web" in row["result"]


async def test_only_tasks_delegated_to_the_runner(world: World, tmp_path: Path, fake: Fake) -> None:
    runner = await setup(world, tmp_path, None)
    t = await world.board.post(world.agents["chat-boss"], project_id="web", title="anyone", mandate_id=world.roots["chat-boss"])
    await once(runner)
    assert (await task_row(world, t["task_id"]))["status"] == "submitted" and fake.calls() == []


async def test_no_run_left_in_the_budget_means_no_claim(world: World, tmp_path: Path, fake: Fake) -> None:
    runner = await setup(world, tmp_path, None)
    tid = await delegate(world, limits={"runs": 0})
    await once(runner)
    assert (await task_row(world, tid))["status"] == "submitted" and fake.calls() == []


# ── criterion 14 ──────────────────────────────────────────────────────────────


async def test_14_work_beyond_the_mandate_is_deferred(world: World, tmp_path: Path, fake: Fake) -> None:
    runner = await setup(world, tmp_path, None)
    fake.mode("defer")
    tid = await delegate(world)
    await once(runner)
    row = await task_row(world, tid)
    assert row["deferred"] and row["needed_scope"] == ["deploy.web@project:web"]
    assert runner.stopped is None


# ── criterion 20 ──────────────────────────────────────────────────────────────


async def test_20_the_kill_switch_stops_the_run_and_the_runner(world: World, tmp_path: Path, fake: Fake) -> None:
    runner = await setup(world, tmp_path, None, heartbeat_seconds=0.2, kill_grace_seconds=1)
    fake.mode("sleep")
    await delegate(world)
    started = time.monotonic()
    await runner.tick()
    while not fake.calls():  # noqa: ASYNC110 — the fake runs in another process
        await asyncio.sleep(0.05)
    await world.admin.set_halted(True, by="boss")
    await asyncio.wait_for(runner.drain(), timeout=10)
    assert runner.stopped == "system_halted"
    assert time.monotonic() - started < 10  # the 60-second fake was killed, not waited for


async def test_a_paused_agent_stops_the_runner(world: World, tmp_path: Path, fake: Fake) -> None:
    runner = await setup(world, tmp_path, None)
    await world.admin.set_agent_status("runner-web", "paused", by="boss")
    with pytest.raises(BoardRefusal):
        await runner.tick()
    await runner._refused(BoardRefusal("agent_paused"))
    assert runner.stopped == "agent_paused"


# ── questions, sessions, quota ────────────────────────────────────────────────


async def test_a_question_waits_for_a_person_then_resumes_the_same_session(
    world: World, tmp_path: Path, fake: Fake, repo: Path
) -> None:
    runner = await setup(world, tmp_path, repo)
    fake.mode("input_required")
    tid = await delegate(world)
    await once(runner)
    row = await task_row(world, tid)
    assert row["status"] == "input_required" and row["deferred"]

    await world.admin.resume_task(tid, by="boss", answer="Blue.")
    fake.mode("completed")
    await once(runner)
    first, second = fake.calls()
    session = second["argv"][second["argv"].index("--resume") + 1]
    assert session.startswith("sess-") and "--append-system-prompt" not in second["argv"]
    assert "Blue." in second["stdin"] and second["cwd"] == first["cwd"]
    assert (await task_row(world, tid))["status"] == "completed"


async def test_a_lost_session_starts_fresh_once(world: World, tmp_path: Path, fake: Fake) -> None:
    runner = await setup(world, tmp_path, None)
    fake.mode("input_required")
    tid = await delegate(world)
    await once(runner)
    await world.admin.resume_task(tid, by="boss", answer="Go on.")
    fake.mode("resume_gone")
    await once(runner)
    calls = fake.calls()
    assert len(calls) == 3 and "--resume" in calls[1]["argv"] and "--resume" not in calls[2]["argv"]
    assert "Go on." in calls[2]["stdin"]  # the fresh session still gets the answer
    assert (await task_row(world, tid))["status"] == "completed"


async def test_a_usage_limit_asks_a_person_and_pauses_new_runs(world: World, tmp_path: Path, fake: Fake) -> None:
    runner = await setup(world, tmp_path, None)
    fake.mode("quota")
    first = await delegate(world, "one")
    await once(runner)
    row = await task_row(world, first)
    assert row["status"] == "input_required" and "usage limit" in row["result"]

    fake.mode("completed")
    second = await delegate(world, "two")
    await once(runner)
    assert (await task_row(world, second))["status"] == "submitted"  # no retry loop, no new run
    assert len(fake.calls()) == 1


async def test_the_owners_share_of_the_quota_is_kept(world: World, tmp_path: Path, fake: Fake) -> None:
    runner = await setup(world, tmp_path, None)
    runner.store.put("rate_limit", {"unifiedWindows": {"five_hour": {"utilization": 0.75, "resetsAt": time.time() + 600}}})
    tid = await delegate(world)
    await once(runner)
    assert (await task_row(world, tid))["status"] == "submitted" and fake.calls() == []


async def test_running_out_of_turns_asks_a_person(world: World, tmp_path: Path, fake: Fake) -> None:
    runner = await setup(world, tmp_path, None)
    fake.mode("max_turns")
    tid = await delegate(world)
    await once(runner)
    row = await task_row(world, tid)
    assert row["status"] == "input_required" and "ran out of turns" in row["result"]


async def test_the_mcp_config_holds_the_token_for_the_owner_only(world: World, tmp_path: Path, fake: Fake) -> None:
    runner = await setup(world, tmp_path, None)
    path = runner.cfg.state_dir / "mcp.json"
    assert path.stat().st_mode & 0o777 == 0o600
    server = json.loads(path.read_text())["mcpServers"]["pact"]
    assert (
        server["url"] == "http://board.invalid/mcp/a/runner-web" and server["headers"]["Authorization"] == "Bearer secret-token"
    )


# ── sub-agents (R2) ───────────────────────────────────────────────────────────


SPLITTABLE = [board_scope(a, "web") for a in ("task.read", "task.work", "task.post")]
"""A task's mandate must carry task.post for its runner to hand part of it to a worker."""


async def worker_for(w: World, tmp: Path, runner: Runner) -> Runner:
    """A second runner agent, the one the first hands subtasks to."""
    await w.agent("worker-web", "runner", ["web"])
    config = RunnerConfig(
        board_url="http://board.invalid",
        agent_id="worker-web",
        token="worker-token",
        state_dir=tmp / "worker-state",
        claude=runner.cfg.claude,
    )
    worker = Runner(config, InProcessBoard(w.board, w.agents["worker-web"]))
    await worker.start()
    return worker


async def post_subtask(w: World, parent: str, parent_mandate: str, limits: dict[str, float]) -> str:
    """What the parent's session does through its pact tools: hand part of the work to the worker."""
    t = await w.board.post(
        w.agents["runner-web"],
        project_id="web",
        title="write the docs",
        body="Write the docs for the fix.",
        mandate_id=parent_mandate,
        parent_task_id=parent,
        delegate_to="worker-web",
        child_limits=limits,
    )
    return str(t["task_id"])


async def started(fake: Fake, calls: int) -> None:
    while len(fake.calls()) < calls:  # noqa: ASYNC110 — the fake runs in another process
        await asyncio.sleep(0.05)


async def test_a_task_hands_work_to_a_worker_and_resumes_with_its_result(world: World, tmp_path: Path, fake: Fake) -> None:
    runner = await setup(world, tmp_path, None, workers=("worker-web",))
    worker = await worker_for(world, tmp_path, runner)
    fake.script("waiting", "child", "completed")
    t = await world.board.post(
        world.agents["chat-boss"],
        project_id="web",
        title="fix and document",
        mandate_id=world.roots["chat-boss"],
        delegate_to="runner-web",
        child_limits={"runs": 3, "turns": 60},
        child_scope=SPLITTABLE,
    )
    parent, parent_mandate = str(t["task_id"]), str(t["delegated_mandate_id"])

    await runner.tick()
    await started(fake, 1)
    [first] = fake.calls()
    assert "worker-web" in first["argv"][first["argv"].index("--append-system-prompt") + 1]  # the role names the worker
    child = await post_subtask(world, parent, parent_mandate, {"runs": 1, "turns": 20})
    fake.release()
    await runner.drain()
    assert (await task_row(world, parent))["status"] == "working"  # held, not closed, not handed to a person

    await once(worker)
    assert (await task_row(world, child))["status"] == "completed"

    # A fresh Runner on the same state picks the wait up again, as after a restart.
    again = Runner(runner.cfg, runner.board, runner.store)
    await again.start()
    await once(again)
    calls = fake.calls()
    assert len(calls) == 3
    resumed = calls[2]
    assert resumed["argv"][resumed["argv"].index("--resume") + 1].startswith("sess-")
    assert "Docs written." in resumed["stdin"] and resumed["env"]["PACT_WAKE_REASON"] == "subtasks"
    assert (await task_row(world, parent))["status"] == "completed"

    async with transaction(world.pool) as conn:
        used = {
            r["limit_key"]: float(r["used"])
            for r in await fetchall(conn, "SELECT limit_key, used FROM limit_usage WHERE mandate_id = %s", (parent_mandate,))
        }
    assert used == {"runs": 3, "turns": 9}  # first run, the worker's run, the resumed run; 3 turns each


async def test_waiting_without_subtasks_asks_a_person(world: World, tmp_path: Path, fake: Fake) -> None:
    runner = await setup(world, tmp_path, None, workers=("worker-web",))
    fake.mode("waiting")
    fake.release()
    tid = await delegate(world)
    await once(runner)
    row = await task_row(world, tid)
    assert row["status"] == "input_required" and "posted no subtasks" in row["result"]


async def test_a_wait_that_runs_too_long_asks_a_person(world: World, tmp_path: Path, fake: Fake) -> None:
    runner = await setup(world, tmp_path, None, workers=("worker-web",), max_wait_seconds=0.5)
    await worker_for(world, tmp_path, runner)
    fake.mode("waiting")
    t = await world.board.post(
        world.agents["chat-boss"],
        project_id="web",
        title="fix and document",
        mandate_id=world.roots["chat-boss"],
        delegate_to="runner-web",
        child_limits={"runs": 3, "turns": 60},
        child_scope=SPLITTABLE,
    )
    await runner.tick()
    await started(fake, 1)
    await post_subtask(world, str(t["task_id"]), str(t["delegated_mandate_id"]), {"runs": 1, "turns": 20})
    fake.release()
    await runner.drain()
    await asyncio.sleep(0.6)
    await once(runner)
    row = await task_row(world, str(t["task_id"]))
    assert row["status"] == "input_required" and "write the docs (submitted)" in row["result"]


async def test_no_run_left_to_resume_asks_a_person(world: World, tmp_path: Path, fake: Fake) -> None:
    runner = await setup(world, tmp_path, None, workers=("worker-web",))
    worker = await worker_for(world, tmp_path, runner)
    fake.script("waiting", "child")
    t = await world.board.post(
        world.agents["chat-boss"],
        project_id="web",
        title="fix and document",
        mandate_id=world.roots["chat-boss"],
        delegate_to="runner-web",
        child_limits={"runs": 2, "turns": 60},
        child_scope=SPLITTABLE,
    )
    parent = str(t["task_id"])
    await runner.tick()
    await started(fake, 1)
    await post_subtask(world, parent, str(t["delegated_mandate_id"]), {"runs": 1, "turns": 20})
    fake.release()
    await runner.drain()
    await once(worker)
    await once(runner)
    row = await task_row(world, parent)
    assert row["status"] == "input_required" and "no run or turns left" in row["result"]
    assert len(fake.calls()) == 2


async def test_without_workers_the_role_says_so(world: World, tmp_path: Path, fake: Fake) -> None:
    runner = await setup(world, tmp_path, None)
    await delegate(world)
    await once(runner)
    [call] = fake.calls()
    role = call["argv"][call["argv"].index("--append-system-prompt") + 1]
    assert "no sub-agents" in role and "parent_task_id=" not in role


async def test_without_task_post_the_subtask_is_refused(world: World, tmp_path: Path, fake: Fake) -> None:
    """Splitting work is authority too: a task delegated without task.post cannot be split."""
    runner = await setup(world, tmp_path, None, workers=("worker-web",))
    await worker_for(world, tmp_path, runner)
    t = await world.board.post(
        world.agents["chat-boss"],
        project_id="web",
        title="just do it",
        mandate_id=world.roots["chat-boss"],
        delegate_to="runner-web",
    )
    with pytest.raises(PactError) as info:
        await post_subtask(world, str(t["task_id"]), str(t["delegated_mandate_id"]), {"runs": 1})
    assert info.value.code == "scope_exceeded"
