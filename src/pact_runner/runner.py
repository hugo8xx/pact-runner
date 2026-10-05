"""The Runner loop: poll the board, claim a delegated task with a run reserved from the budget, run
Claude in the task's worktree, report the outcome and the turns it spent.

Stops for good (exit 0) on the kill switch, a paused agent or a dead mandate. Never retries in a
loop: a quota hit, a timeout or a run without an answer goes to a person as input_required.
"""

import asyncio
import hashlib
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from pact.handoff import has_handoff

from . import worktree
from .board import FATAL, BoardClient, BoardRefusal
from .claude import Outcome, Run, execute, role_prompt
from .config import RunnerConfig
from .store import Session, Store, Waiting

log = logging.getLogger("pact_runner")

TERMINAL = frozenset({"completed", "failed", "canceled", "rejected"})

DROP = frozenset({"already_claimed", "wrong_agent", "invalid_request", "approval_pending", "not_found", "project_mismatch"})
"""Claim refusals that only mean "not this task": forget it and move on."""


def subtasks_prompt(subtasks: list[dict[str, Any]]) -> str:
    parts = ["Every subtask you posted is closed. Their results:"]
    for t in subtasks:
        result = t.get("result")
        text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
        parts += ["", f"### {t['title']} ({t['status']}, {t.get('assignee') or t.get('delegate_to')})", (text or "")[:4000]]
    parts += ["", "Continue the task with these results and finish it."]
    return "\n".join(parts)


def task_prompt(task: dict[str, Any]) -> str:
    parts = [f"# Task {task['id']}: {task['title']}", "", task.get("body") or "(no body)"]
    if task.get("answer"):
        parts += ["", "## A person answered an earlier question", "", str(task["answer"])]
    return "\n".join(parts)


class Runner:
    def __init__(self, cfg: RunnerConfig, board: BoardClient, store: Store | None = None) -> None:
        self.cfg = cfg
        self.board = board
        self.store = store or Store(cfg.state_dir / "runner.sqlite3")
        self.mandate_id = cfg.mandate_id or ""
        self.project_id = cfg.project_id
        self.since: int | None = None
        self.backlog: dict[str, dict[str, Any]] = {}
        self.running: dict[str, asyncio.Task[None]] = {}
        self.beats: dict[str, float] = {}
        """When each waiting task last sent a heartbeat."""
        self.stopped: str | None = None
        """Why the runner stopped for good; None while it runs."""

    # ── lifecycle ─────────────────────────────────────────────────────────

    async def start(self) -> None:
        me = await self._call("pact_whoami", {})
        if me["agent"]["client"] != "runner":
            raise SystemExit(f"pact-runner: agent {self.cfg.agent_id} is a {me['agent']['client']} agent, not a runner")
        if not self.mandate_id:
            roots = [m for m in me["mandates"] if m["parent_id"] is None] or me["mandates"]
            if not roots:
                raise SystemExit(f"pact-runner: agent {self.cfg.agent_id} holds no live mandate")
            self.mandate_id = roots[0]["id"]
        if self.project_id is None and len(me["projects"]) == 1:
            self.project_id = me["projects"][0]["id"]
        self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        self._write_mcp_config()
        log.info("runner %s started under mandate %s", self.cfg.agent_id, self.mandate_id)

    async def run_forever(self) -> str:
        await self.start()
        while not self.stopped:
            try:
                await self.tick()
            except BoardRefusal as err:
                await self._refused(err)
            except Exception:  # the board or network may be down; try again next poll
                log.exception("poll failed")
            await asyncio.sleep(self.cfg.poll_seconds)
        await self.drain()
        return self.stopped

    async def drain(self) -> None:
        if self.running:
            await asyncio.gather(*self.running.values(), return_exceptions=True)

    def stop(self, reason: str) -> None:
        if not self.stopped:
            log.warning("runner stopping: %s", reason)
            self.stopped = reason

    # ── one poll ──────────────────────────────────────────────────────────

    async def tick(self) -> None:
        listed = await self._call(
            "pact_list", {"mandate_id": self.mandate_id, "filter": "open", "since": self.since, "project_id": self.project_id}
        )
        for task in listed["tasks"]:
            mine = task.get("delegate_to") == self.cfg.agent_id
            if (mine or (self.cfg.take_open and not task.get("delegate_to"))) and task["status"] == "submitted":
                self.backlog[task["id"]] = task
            else:
                self.backlog.pop(task["id"], None)
        self.since = listed["next_since"]
        await self._check_waiting()
        for task_id in list(self.backlog):
            if self.stopped or not self._may_start():
                break
            if task_id in self.running:
                continue
            await self._claim_and_start(self.backlog.pop(task_id))

    def _may_start(self) -> bool:
        if len(self.running) >= self.cfg.concurrency:
            return False
        if self.store.runs_today() >= self.cfg.max_runs_per_day:
            return False
        if self.cfg.quiet_hours:
            start, end = self.cfg.quiet_hours
            hour = datetime.now().hour
            if (start <= hour < end) if start <= end else (hour >= start or hour < end):
                return False
        return self._quota_allows()

    def _quota_allows(self) -> bool:
        """Leave the owner their share of the subscription: no new run while a quota window is past
        its reserve line or a rate limit is in force, until that window resets."""
        now = time.time()
        paused = self.store.get("paused_until")
        if paused and now < float(paused):
            return False
        info = self.store.get("rate_limit") or {}
        windows = info.get("unifiedWindows") or {}
        for name, line in (("five_hour", self.cfg.reserve_five_hour), ("seven_day", self.cfg.reserve_seven_day)):
            w = windows.get(name) or {}
            if float(w.get("utilization") or 0) >= line and now < float(w.get("resetsAt") or 0):
                return False
        return True

    async def _claim_and_start(self, task: dict[str, Any]) -> None:
        mandate = task.get("delegated_mandate_id") if task.get("delegate_to") == self.cfg.agent_id else None
        mandate = mandate or self.mandate_id
        try:
            claimed = await self._call("pact_claim", {"task_id": task["id"], "mandate_id": mandate, "reserve": {"runs": 1}})
        except BoardRefusal as err:
            if err.code == "limit_exceeded":
                log.warning("task %s: no run left in the budget (%s)", task["id"], err.message)
            elif err.code not in DROP:
                await self._refused(err)
            return
        self.store.count_run()
        budget = claimed.get("budget") or {}
        turns = self.cfg.max_turns if "turns" not in budget else min(self.cfg.max_turns, int(budget["turns"]))
        self.running[task["id"]] = asyncio.create_task(self._run_task(task, mandate, turns))

    # ── one task ──────────────────────────────────────────────────────────

    async def _run_task(self, task: dict[str, Any], mandate: str, max_turns: int) -> None:
        task_id = task["id"]
        try:
            if max_turns < 1:
                await self._report(task_id, mandate, "input_required", "The turns budget for this task is used up.")
                return
            await self._work(task, mandate, max_turns)
        except Exception:
            log.exception("task %s failed inside the runner", task_id)
        finally:
            self.running.pop(task_id, None)

    async def _work(
        self, task: dict[str, Any], mandate: str, max_turns: int, subtasks: list[dict[str, Any]] | None = None
    ) -> None:
        task_id = task["id"]
        workdir = await worktree.prepare(self.cfg.repo, self.cfg.state_dir / "work", task_id)
        branch = worktree.branch_for(task_id)
        role = role_prompt(self.cfg, task_id, branch, mandate, task.get("project_id") or self.project_id or "")
        role_hash = hashlib.sha256(role.encode()).hexdigest()
        wake = "subtasks" if subtasks is not None else "answer" if task.get("answer") else "assignment"
        prior = self.store.session(task_id)
        resume = prior.session_id if prior and prior.role_hash == role_hash and wake != "assignment" else None
        lost = False

        async def heartbeat() -> None:
            nonlocal lost
            try:
                await self._call("pact_report", {"task_id": task_id, "status": "working", "mandate_id": mandate})
            except BoardRefusal as err:
                lost = True
                if err.code != "claim_lost":
                    await self._refused(err)

        def run_for(resume_id: str | None) -> Run:
            if wake == "subtasks":
                follow_up = subtasks_prompt(subtasks or [])
                prompt = follow_up if resume_id else f"{task_prompt(task)}\n\n{follow_up}"
            elif resume_id:
                prompt = f"A person answered your question:\n\n{task['answer']}\n\nContinue the task."
            else:
                prompt = task_prompt(task)
            return Run(
                task_id=task_id,
                prompt=prompt,
                workdir=workdir,
                branch=branch,
                mcp_config=self.cfg.state_dir / "mcp.json",
                max_turns=max_turns,
                resume=resume_id,
                env={
                    "PACT_TASK_ID": task_id,
                    "PACT_MANDATE_ID": mandate,
                    "PACT_WAKE_REASON": wake,
                },
            )

        def halt() -> bool:
            return lost or self.stopped is not None

        out = await execute(
            self.cfg, run_for(resume), role, should_stop=halt, tick=heartbeat, tick_seconds=self.cfg.heartbeat_seconds
        )
        if out.resume_failed and resume:
            log.info("task %s: session %s is gone; starting a fresh one", task_id, resume)
            out = await execute(
                self.cfg, run_for(None), role, should_stop=halt, tick=heartbeat, tick_seconds=self.cfg.heartbeat_seconds
            )
        self._remember_quota(out)
        if out.session_id:
            self.store.save_session(Session(task_id, out.session_id, str(workdir), role_hash))
        if halt():
            return  # the claim is gone or the runner is stopping; nobody to report to
        await self._finish(task, mandate, workdir, branch, out, max_turns)

    async def _finish(self, task: dict[str, Any], mandate: str, workdir: Path, branch: str, out: Outcome, max_turns: int) -> None:
        task_id = task["id"]
        usage = {"turns": out.num_turns} if out.num_turns else None
        where = f"branch `{branch}`, session `{out.session_id}`, {out.num_turns} turns"
        so = out.structured or {}
        status = so.get("status")
        if out.quota_hit:
            resets = self._resets_at(out)
            await self._report(
                task_id,
                mandate,
                "input_required",
                f"Claude's usage limit stopped this run ({where}). New runs wait until {resets}. "
                "Resume the task to continue from the saved session.",
                usage,
            )
        elif out.timed_out:
            await self._report(
                task_id,
                mandate,
                "input_required",
                f"The run hit the {self.cfg.run_timeout_seconds / 60:g}-minute limit ({where}). "
                "Resume the task to continue from the saved session, or cancel it.",
                usage,
            )
        elif status in ("completed", "failed"):
            result = str(so.get("result") or "")
            if not has_handoff(result):
                result += f"\n\n## Handoff\n- Runner {self.cfg.agent_id}: {where}\n- The session returned no handoff of its own."
            if await self._report(task_id, mandate, status, result, usage):
                await worktree.remove(self.cfg.repo, workdir)
                self.store.forget_session(task_id)
        elif status == "input_required":
            await self._report(task_id, mandate, "input_required", so.get("question") or so.get("result") or "", usage)
        elif status == "waiting":
            if not await self._subtasks(task_id):
                await self._report(
                    task_id,
                    mandate,
                    "input_required",
                    f"The session ended with status waiting but posted no subtasks ({where}).\n\n{so.get('result') or ''}",
                    usage,
                )
            elif not usage or await self._report(task_id, mandate, "working", None, usage):
                self.store.wait(task, mandate, max_turns, time.time())
                self.beats[task_id] = time.time()
                log.info("task %s waits for its subtasks", task_id)
        elif status == "defer":
            if usage:
                await self._report(task_id, mandate, "working", None, usage)
            try:
                await self._call(
                    "pact_defer",
                    {
                        "task_id": task_id,
                        "mandate_id": mandate,
                        "reason": str(so.get("result") or "needs more authority"),
                        "needed_scope": so.get("needed_scope") or None,
                    },
                )
            except BoardRefusal as err:
                if err.code != "claim_lost":
                    await self._refused(err)
        else:
            why = (
                "ran out of turns"
                if out.subtype == "error_max_turns"
                else f"ended without a result ({out.subtype or out.exit_code})"
            )
            detail = (out.text or out.stderr).strip()[-1500:]
            await self._report(
                task_id,
                mandate,
                "input_required",
                f"The run {why} ({where}).\n\n{detail}\n\nResume to continue from the saved session, or cancel.",
                usage,
            )

    # ── tasks waiting for sub-agents ──────────────────────────────────────

    async def _subtasks(self, task_id: str) -> list[dict[str, Any]]:
        listed = await self._call(
            "pact_list", {"mandate_id": self.mandate_id, "filter": "all", "parent_task_id": task_id, "limit": 200}
        )
        deferred = [t for t in listed.get("deferred", []) if t.get("parent_task_id") == task_id]
        return [*listed["tasks"], *deferred]

    async def _check_waiting(self) -> None:
        """Keep the claim of every waiting task alive, and resume a task once all its subtasks are
        closed. A task that waits too long goes to a person."""
        now = time.time()
        for w in self.store.waiting():
            task_id = w.task["id"]
            if task_id in self.running or self.stopped:
                continue
            if now - self.beats.get(task_id, 0) >= self.cfg.heartbeat_seconds:
                if not await self._report(task_id, w.mandate_id, "working", None):
                    self.store.unwait(task_id)
                    continue
                self.beats[task_id] = now
            subtasks = await self._subtasks(task_id)
            still_open = [t for t in subtasks if t["status"] not in TERMINAL]
            if not still_open:
                if self._may_start():
                    self.store.unwait(task_id)
                    self.running[task_id] = asyncio.create_task(self._resume_after_subtasks(w, subtasks))
            elif now - w.since > self.cfg.max_wait_seconds:
                self.store.unwait(task_id)
                titles = ", ".join(f"{t['title']} ({t['status']})" for t in still_open)
                await self._report(
                    task_id,
                    w.mandate_id,
                    "input_required",
                    f"Waited {self.cfg.max_wait_seconds / 3600:g} hours for subtasks that are still open: {titles}. "
                    "Resume the task to continue without them, or cancel it.",
                )

    async def _resume_after_subtasks(self, w: Waiting, subtasks: list[dict[str, Any]]) -> None:
        """Wake the waiting session with its subtasks' results. That is another run, taken from the
        task's budget like the first."""
        task_id = w.task["id"]
        try:
            out = await self._call(
                "pact_report", {"task_id": task_id, "status": "working", "mandate_id": w.mandate_id, "usage": {"runs": 1}}
            )
            budget = out.get("budget") or {}
            turns = min(self.cfg.max_turns, int(budget.get("turns", self.cfg.max_turns)))
            if "runs" in (out.get("budget_exceeded") or []) or turns < 1:
                await self._report(
                    task_id,
                    w.mandate_id,
                    "input_required",
                    "The subtasks are done, but the budget has no run or turns left to finish the task.",
                )
                return
            self.store.count_run()
            await self._work(w.task, w.mandate_id, turns, subtasks=subtasks)
        except BoardRefusal as err:
            if err.code != "claim_lost":
                await self._refused(err)
        except Exception:
            log.exception("task %s failed inside the runner", task_id)
        finally:
            self.running.pop(task_id, None)

    # ── helpers ───────────────────────────────────────────────────────────

    async def _report(self, task_id: str, mandate: str, status: str, result: Any, usage: dict[str, int] | None = None) -> bool:
        try:
            out = await self._call(
                "pact_report", {"task_id": task_id, "status": status, "mandate_id": mandate, "result": result, "usage": usage}
            )
        except BoardRefusal as err:
            if err.code != "claim_lost":
                await self._refused(err)
            return False
        if out.get("budget_exceeded"):
            log.warning("task %s: budget used up for %s", task_id, ", ".join(out["budget_exceeded"]))
        return True

    async def _refused(self, err: BoardRefusal) -> None:
        """A refusal on one task's mandate may only concern that task; stop only if the runner's own
        mandate, the agent or the whole board is refused."""
        log.warning("board refused: %s", err)
        if err.code not in FATAL:
            return
        if err.code in ("system_halted", "agent_paused", "agent_unknown"):
            self.stop(err.code)
            return
        try:
            await self._call("pact_list", {"mandate_id": self.mandate_id, "filter": "mine", "limit": 1})
        except BoardRefusal as again:
            if again.code in FATAL:
                self.stop(again.code)

    def _remember_quota(self, out: Outcome) -> None:
        if out.rate_limit:
            self.store.put("rate_limit", out.rate_limit)
        if out.quota_hit:
            reset = float((out.rate_limit or {}).get("resetsAt") or 0)
            self.store.put("paused_until", reset if reset > time.time() else time.time() + 3600)

    def _resets_at(self, out: Outcome) -> str:
        until = self.store.get("paused_until") or time.time() + 3600
        return datetime.fromtimestamp(float(until)).strftime("%Y-%m-%d %H:%M")

    async def _call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        return await self.board.call(tool, args)

    def _write_mcp_config(self) -> None:
        """The board as an MCP server for the session, under the runner's own token. The file holds
        a secret, so only the owner may read it."""
        path = self.cfg.state_dir / "mcp.json"
        body = {
            "mcpServers": {
                "pact": {"type": "http", "url": self.cfg.mcp_url, "headers": {"Authorization": f"Bearer {self.cfg.token}"}}
            }
        }
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(body, f)
        os.chmod(path, 0o600)
