"""pact-connect on the machine: it trades a setup code for a token and writes the runner's env and
LaunchAgent. Also the task prompt the Runner builds."""

import json
import plistlib
from pathlib import Path
from typing import Any

import httpx
import pytest

from pact_runner import connect
from pact_runner.runner import task_prompt


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
