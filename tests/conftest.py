import asyncio
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psycopg
import pytest
from pact.admin import Admin
from pact.board import Agent, Board, get_agent
from pact.db import create_pool, migrate, transaction


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


_load_env(Path(__file__).resolve().parents[1] / ".env.test")
os.environ.setdefault("DATABASE_URL", "postgres://localhost:5432/pact_test")
os.environ.setdefault("PACT_SIGNING_KEY", "test-signing-key-test-signing-key-0123456789")

TABLES = (
    "entries, entry_chain_heads, payloads, task_activity, hook_cursors, context_note_versions, context_notes, notifications, "
    "expiry_notices, setup_codes, "
    "credential_revocations, credential_revocation_version, credential_links, agent_keys, trusted_roots, limit_usage, tasks, "
    "agent_tokens, agent_projects, "
    "mandates, agents, projects, humans"
)


@pytest.fixture(scope="session", autouse=True)
def _schema() -> None:
    """Fresh schema once per run, migrated from the real migration files."""
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")

    async def run() -> None:
        pool = create_pool()
        await pool.open()
        await migrate(pool)
        await pool.close()

    asyncio.run(run())


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


HANDOFF = "## Handoff\n- Done: test\n- Repo / branch / PR / commit: no code"


@dataclass
class World:
    """A clean board with one owner (`boss`) and helpers to register things."""

    pool: Any
    admin: Admin
    board: Board
    agents: dict[str, Agent] = field(default_factory=dict)
    roots: dict[str, str] = field(default_factory=dict)
    tokens: dict[str, str] = field(default_factory=dict)

    async def project(self, pid: str, production: bool = False) -> str:
        await self.admin.add_project(pid, pid.title(), by="boss", production=production)
        return pid

    async def agent(self, aid: str, client: str, projects: list[str], **kw: Any) -> Agent:
        out = await self.admin.register_agent(aid, by="boss", client=client, projects=projects, **kw)  # type: ignore[arg-type]
        async with transaction(self.pool) as conn:
            agent = await get_agent(conn, aid)
        assert agent is not None
        self.agents[aid] = agent
        self.roots[aid] = out["root_mandate_id"]
        self.tokens[aid] = out["token"]
        return agent


@pytest.fixture
async def world() -> AsyncIterator[World]:
    pool = create_pool()
    await pool.open()
    async with transaction(pool) as conn:
        # entries is append-only by trigger; tests reset it with the trigger off.
        await conn.execute("ALTER TABLE entries DISABLE TRIGGER entries_no_update")
        await conn.execute(f"TRUNCATE {TABLES} CASCADE")
        await conn.execute("ALTER TABLE entries ENABLE TRIGGER entries_no_update")
        await conn.execute("UPDATE system_state SET halted = false")
    admin = Admin(pool)
    await admin.add_human("boss", "Boss", "owner")
    yield World(pool=pool, admin=admin, board=Board(pool))
    await pool.close()
