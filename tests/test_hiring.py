"""Hiring agents from roles: the role decides scope, budget and term; a token agent connects with a
one-time setup code instead of a token; renewing and replacing keep the board tidy."""

import json
import plistlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from pact.auth import agent_for_token
from pact.board import get_agent
from pact.db import fetchall, fetchone, transaction
from pact.errors import PactError
from pact_runner import connect
from pact_runner.runner import task_prompt

from .conftest import World

pytestmark = pytest.mark.anyio


async def refused(coro: Any, code: str) -> PactError:
    with pytest.raises(PactError) as info:
        await coro
    assert info.value.code == code, f"expected {code}, got {info.value.code}: {info.value.message}"
    return info.value


async def mandate_of(w: World, agent_id: str) -> dict[str, Any]:
    async with transaction(w.pool) as conn:
        row = await fetchone(
            conn,
            """SELECT m.scope, m.limits, m.delegations_left, m.expires_at FROM mandates m JOIN agents a
               ON a.root_mandate_id = m.id WHERE a.id = %s""",
            (agent_id,),
        )
    assert row
    return dict(row)


async def test_the_board_starts_with_seven_roles(world: World) -> None:
    roles = {r["id"]: r for r in await world.admin.list_roles()}
    assert list(roles) == ["chat", "code", "runner", "worker", "secretary", "design", "cowork"]
    assert roles["runner"]["client"] == "runner" and roles["runner"]["limits"] == {"runs": 50, "turns": 2000}
    assert roles["secretary"]["actions"] == ["task.read", "task.post", "report.brief"]
    schedule = json.loads(roles["secretary"]["settings"]["PACT_RUNNER_SCHEDULE"])
    assert schedule[0]["action"] == "report.brief" and "{since}" in schedule[0]["body"]


async def test_hiring_chat_gives_a_connector_url_and_no_token(world: World) -> None:
    await world.project("web")
    out = await world.admin.hire("chat", "web", by="boss")
    assert out["agent_id"] == "chat-web" and out["connect"] == {"kind": "oauth", "url": "http://127.0.0.1:8787/mcp/a/chat-web"}
    m = await mandate_of(world, "chat-web")
    assert m["scope"] == [f"{a}@project:web" for a in ("task.read", "task.post", "task.work", "context.write")]
    assert m["delegations_left"] == 3
    async with transaction(world.pool) as conn:
        agent = await get_agent(conn, "chat-web")
        tokens = await fetchall(conn, "SELECT 1 FROM agent_tokens WHERE agent_id = 'chat-web'")
    assert agent and agent.client == "chat" and tokens == []


async def test_a_runner_connects_once_with_its_setup_code(world: World) -> None:
    await world.project("web")
    out = await world.admin.hire("runner", "web", by="boss", limits={"runs": 5, "turns": 100}, days=3)
    code = out["connect"]["code"]
    assert out["connect"]["kind"] == "setup_code" and out["connect"]["command"].endswith(code)
    m = await mandate_of(world, "runner-web")
    assert m["limits"] == {"runs": 5, "turns": 100}
    assert m["expires_at"] < datetime.now(UTC) + timedelta(days=3, minutes=1)

    info = await world.admin.redeem_setup_code(code)
    assert info["agent_id"] == "runner-web" and info["client"] == "runner" and info["project"] == "web"
    assert info["settings"]["PACT_RUNNER_WORKERS"] == "worker-web"  # {project} filled in
    async with transaction(world.pool) as conn:
        assert await agent_for_token(conn, "runner-web", info["token"])
    await refused(world.admin.redeem_setup_code(code), "forbidden")  # single use


async def test_an_expired_or_unknown_code_is_refused(world: World) -> None:
    await world.project("web")
    code = (await world.admin.hire("code", "web", by="boss"))["connect"]["code"]
    async with transaction(world.pool) as conn:
        await conn.execute("UPDATE setup_codes SET expires_at = now() - interval '1 second'")
    await refused(world.admin.redeem_setup_code(code), "forbidden")
    await refused(world.admin.redeem_setup_code("pcs_nope"), "forbidden")
    fresh = await world.admin.setup_code("code-web", by="boss")
    assert (await world.admin.redeem_setup_code(fresh["code"]))["agent_id"] == "code-web"


async def test_names_are_checked(world: World) -> None:
    await world.project("web")
    await world.admin.hire("chat", "web", by="boss")
    await refused(world.admin.hire("chat", "web", by="boss"), "invalid_request")  # chat-web exists
    await refused(world.admin.hire("chat", "web", by="boss", agent_id="Chat Web"), "invalid_request")
    assert (await world.admin.hire("chat", "web", by="boss", agent_id="chat-web-2"))["agent_id"] == "chat-web-2"
    await refused(world.admin.hire("nope", "web", by="boss"), "not_found")


async def test_roles_are_edited_and_checked(world: World) -> None:
    await world.project("web")
    base = {"name": "Marketer", "client": "runner", "actions": ["task.read", "task.work"], "limits": {"runs": 3}}
    await world.admin.save_role("marketer", base, by="boss")
    assert (await world.admin.hire("marketer", "web", by="boss"))["agent_id"] == "marketer-web"
    for bad in (
        {**base, "actions": ["task.work@project:web"]},
        {**base, "client": "robot"},
        {**base, "limits": {"runs": -1}},
        {**base, "settings": {"PATH": "/tmp"}},
        {**base, "delegations": 9},
    ):
        await refused(world.admin.save_role("marketer", bad, by="boss"), "invalid_request")
    await world.admin.archive_role("marketer", by="boss")
    assert "marketer" not in {r["id"] for r in await world.admin.list_roles()}
    await refused(world.admin.hire("marketer", "web", by="boss", agent_id="m2"), "invalid_request")


async def test_renewing_issues_a_fresh_mandate_from_the_role(world: World) -> None:
    await world.project("web")
    await world.admin.hire("worker", "web", by="boss", days=1)
    before = await mandate_of(world, "worker-web")
    out = await world.admin.renew("worker-web", by="boss")
    after = await mandate_of(world, "worker-web")
    assert after["expires_at"] > before["expires_at"] + timedelta(days=5)
    assert after["limits"] == {"runs": 50, "turns": 1000} and out["mandate_id"]
    await world.agent("old-web", "code", ["web"])
    await refused(world.admin.renew("old-web", by="boss"), "invalid_request")  # not hired into a role


async def test_a_replaced_agent_points_to_its_successor(world: World) -> None:
    await world.project("web")
    await world.admin.hire("chat", "web", by="boss", agent_id="pact-chat")
    await world.admin.hire("chat", "web", by="boss", replaces="pact-chat")
    async with transaction(world.pool) as conn:
        old = await get_agent(conn, "pact-chat")
    assert old and old.status == "banned"
    err = await refused(world.board.whoami(old), "agent_paused")
    assert "replaced by chat-web" in err.message and "/mcp/a/chat-web" in err.message


async def test_an_agent_can_be_hired_for_another_person(world: World) -> None:
    await world.project("web")
    await world.admin.add_human("vic", "Vic", "viewer", by="boss")
    await world.admin.hire("chat", "web", by="boss", owner="vic", agent_id="chat-vic")
    async with transaction(world.pool) as conn:
        agent = await get_agent(conn, "chat-vic")
    assert agent and agent.owner == "vic"
    await refused(world.admin.hire("chat", "web", by="boss", owner="ghost", agent_id="chat-ghost"), "not_found")
    await refused(world.admin.hire("chat", "web", by="vic", agent_id="chat-x"), "forbidden")  # viewers cannot hire


# ── pact-connect on the machine ───────────────────────────────────────────────


def _info(**kw: Any) -> dict[str, Any]:
    base = {
        "agent_id": "runner-web",
        "client": "runner",
        "project": "web",
        "board_url": "https://board.example",
        "mcp_url": "https://board.example/mcp/a/runner-web",
        "token": "pact_secret",
        "settings": {"PACT_RUNNER_SCHEDULE": '[{"at": "07:30", "body": "it\'s {date}"}]'},
        "instructions": "Be brief.",
    }
    return {**base, **kw}


def test_pact_connect_writes_a_runner_env_that_a_shell_reads_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    monkeypatch.setattr(connect, "AGENTS_DIR", tmp_path / "LaunchAgents")
    monkeypatch.setattr(connect, "LOGS", tmp_path / "Logs")
    steps = connect.connect_runner(_info(), str(tmp_path), launch=False, config=tmp_path / "config")
    env = tmp_path / "config" / "runner-web.env"
    assert env.stat().st_mode & 0o777 == 0o600 and any("runner env" in s for s in steps)
    read = subprocess.run(
        [
            "sh",
            "-c",
            f'set -a; . "{env}"; printf "%s\\n%s\\n%s" "$PACT_RUNNER_TOKEN" "$PACT_RUNNER_SCHEDULE" "$PACT_RUNNER_ROLE_FILE"',
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\n")
    assert read[0] == "pact_secret" and json.loads(read[1])[0]["body"] == "it's {date}"
    assert Path(read[2]).read_text() == "Be brief."


def test_the_launch_agent_carries_a_path_with_the_tools(tmp_path: Path) -> None:
    label, body = connect.launch_agent("runner-web", tmp_path / "x.env", "/opt/bin/pact-runner")
    plist = plistlib.loads(body)
    assert label == "com.pact.runner.runner-web" and plist["KeepAlive"] == {"SuccessfulExit": False}
    assert "/usr/bin" in plist["EnvironmentVariables"]["PATH"] and "exec /opt/bin/pact-runner" in plist["ProgramArguments"][2]


def test_redeem_reports_the_boards_refusal() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {"code": "pcs_x"}
        return httpx.Response(403, json={"error": "forbidden", "message": "this setup code is unknown, used or expired"})

    with pytest.raises(SystemExit) as info:
        connect.redeem("https://board.example", "pcs_x", httpx.Client(transport=httpx.MockTransport(handler)))
    assert "used or expired" in str(info.value)


def test_a_runner_prompt_shows_the_budget_left() -> None:
    prompt = task_prompt({"id": "t1", "title": "fix", "body": "b", "_budget": {"runs": 2.0, "turns": 40.0}})
    assert "runs 2, turns 40" in prompt


async def test_agents_from_before_roles_can_take_their_role(world: World) -> None:
    """The migration's backfill rule: name prefix and client must both match a role."""
    await world.project("web")
    await world.agent("runner-web", "runner", ["web"])
    await world.agent("runner-odd", "code", ["web"])
    async with transaction(world.pool) as conn:
        await conn.execute(
            """UPDATE agents a SET role_id = r.id FROM agent_roles r
               WHERE a.role_id IS NULL AND r.id = split_part(a.id, '-', 1) AND r.client = a.client"""
        )
        rows = {r["id"]: r["role_id"] for r in await fetchall(conn, "SELECT id, role_id FROM agents")}
    assert rows == {"runner-web": "runner", "runner-odd": None}
    assert (await world.admin.renew("runner-web", by="boss"))["agent_id"] == "runner-web"
