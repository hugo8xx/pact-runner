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


def test_pact_connect_adds_the_board_to_antigravity_and_keeps_the_rest(tmp_path: Path) -> None:
    agy = tmp_path / ".gemini" / "config" / "mcp_config.json"
    agy.parent.mkdir(parents=True)
    agy.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}, "x": 1}))
    legacy = tmp_path / ".gemini" / "settings.json"
    info = _info(agent_id="gemini-web", client="gemini", mcp_url="https://board.example/mcp/a/gemini-web")
    steps = connect.connect_gemini(info, "pact", config=tmp_path / "config", antigravity=agy, gemini_cli=legacy)
    data = json.loads(agy.read_text())
    assert data["x"] == 1 and data["mcpServers"]["other"] == {"command": "x"}
    assert data["mcpServers"]["pact"] == {
        "serverUrl": "https://board.example/mcp/a/gemini-web",
        "headers": {"Authorization": "Bearer pact_secret"},
        "disabled": False,
    }
    assert agy.stat().st_mode & 0o777 == 0o600 and (tmp_path / "config" / "gemini-web.env").exists()
    assert not legacy.exists() and any("agy" in s for s in steps)  # Gemini CLI only when asked


def test_pact_connect_configures_gemini_cli_when_asked(tmp_path: Path) -> None:
    agy, legacy = tmp_path / "agy.json", tmp_path / "settings.json"
    legacy.write_text(json.dumps({"theme": "Dracula"}))
    connect.connect_gemini(
        _info(client="gemini"), "board", config=tmp_path / "c", antigravity=agy, gemini_cli=legacy, legacy=True
    )
    data = json.loads(legacy.read_text())
    assert data["theme"] == "Dracula" and data["mcpServers"]["board"]["httpUrl"] == "https://board.example/mcp/a/runner-web"
    assert "board" in json.loads(agy.read_text())["mcpServers"]


def test_pact_connect_leaves_a_broken_config_alone(tmp_path: Path) -> None:
    agy = tmp_path / "mcp_config.json"
    agy.write_text("{ // a comment\n}")
    with pytest.raises(SystemExit, match="not plain JSON"):
        connect.connect_gemini(
            _info(client="gemini"), "pact", config=tmp_path / "c", antigravity=agy, gemini_cli=tmp_path / "s.json"
        )
    assert agy.read_text().startswith("{ //")


HOOK = "/opt/pact/bin/pact-hook"


def _code_info() -> dict[str, Any]:
    return _info(agent_id="code-web", client="code", mcp_url="https://board.example/mcp/a/code-web")


def _no_claude(monkeypatch: pytest.MonkeyPatch, hook: str = HOOK) -> None:
    monkeypatch.setattr(connect.shutil, "which", lambda name: hook if name == "pact-hook" else None)


def test_pact_connect_installs_the_hooks_into_a_fresh_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _no_claude(monkeypatch)
    steps = connect.connect_code(_code_info(), tmp_path, "pact", config=tmp_path / "config")
    settings = tmp_path / ".claude" / "settings.local.json"
    assert any(str(settings) in s for s in steps)
    text = settings.read_text()
    assert text.startswith('{\n  "hooks"') and text.endswith("}\n")
    hooks = json.loads(text)["hooks"]
    assert set(hooks) == {"SessionStart", "UserPromptSubmit", "PostToolUse", "Stop"}
    assert hooks["PostToolUse"] == [
        {
            "matcher": "Bash|Edit|MultiEdit|Write|NotebookEdit",
            "hooks": [{"type": "command", "command": f"{HOOK} code-web post-tool-use", "timeout": 10}],
        }
    ]
    assert hooks["SessionStart"] == [{"hooks": [{"type": "command", "command": f"{HOOK} code-web session-start", "timeout": 10}]}]
    assert hooks["Stop"][0]["hooks"][0]["command"] == f"{HOOK} code-web stop"
    assert "pact_secret" not in text


def test_pact_connect_keeps_other_settings_and_hooks_and_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _no_claude(monkeypatch)
    settings = tmp_path / ".claude" / "settings.local.json"
    settings.parent.mkdir()
    other = {"type": "command", "command": "echo mine"}
    old_pact = {"type": "command", "command": "/old/place/pact-hook.sh code-web stop"}
    other_agent = {"type": "command", "command": f"{HOOK} code-api stop", "timeout": 10}
    settings.write_text(
        json.dumps(
            {
                "permissions": {"allow": ["Bash(ls:*)"]},
                "hooks": {
                    "Stop": [{"hooks": [other, old_pact]}, {"hooks": [other_agent]}],
                    "PreToolUse": [{"matcher": "Bash", "hooks": [other]}],
                },
            }
        )
    )
    connect.install_hooks(tmp_path, "code-web")
    first = settings.read_text()
    data = json.loads(first)
    assert data["permissions"] == {"allow": ["Bash(ls:*)"]}
    assert data["hooks"]["PreToolUse"] == [{"matcher": "Bash", "hooks": [other]}]
    assert data["hooks"]["Stop"] == [
        {"hooks": [other]},
        {"hooks": [other_agent]},
        {"hooks": [{"type": "command", "command": f"{HOOK} code-web stop", "timeout": 10}]},
    ]
    connect.install_hooks(tmp_path, "code-web")
    assert settings.read_text() == first


def test_pact_connect_refuses_a_settings_file_that_is_not_plain_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _no_claude(monkeypatch)
    settings = tmp_path / ".claude" / "settings.local.json"
    settings.parent.mkdir()
    settings.write_text('{ // mine\n  "hooks": {}\n}')
    with pytest.raises(SystemExit, match="not plain JSON"):
        connect.connect_code(_code_info(), tmp_path, "pact", config=tmp_path / "config")
    assert settings.read_text().startswith("{ // mine")


def test_no_hooks_leaves_the_settings_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _no_claude(monkeypatch)
    steps = connect.connect_code(_code_info(), tmp_path, "pact", config=tmp_path / "config", hooks=False)
    assert not (tmp_path / ".claude").exists() and not any("hooks:" in s for s in steps)
    assert (tmp_path / "config" / "code-web.env").exists()


def test_a_hook_path_with_spaces_is_quoted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _no_claude(monkeypatch, "/Users/you/Application Support/bin/pact-hook")
    connect.install_hooks(tmp_path, "code-web")
    connect.install_hooks(tmp_path, "code-web")  # the quoted command is recognised and replaced
    hooks = json.loads((tmp_path / ".claude" / "settings.local.json").read_text())["hooks"]
    assert len(hooks["Stop"]) == 1
    command = hooks["Stop"][0]["hooks"][0]["command"]
    assert command == "'/Users/you/Application Support/bin/pact-hook' code-web stop"
    import shlex

    assert shlex.split(command)[0] == "/Users/you/Application Support/bin/pact-hook"


def test_hook_command_falls_back_to_the_script_next_to_python(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(connect.shutil, "which", lambda name: None)
    import shlex
    import sys

    assert shlex.split(connect.hook_command())[0] == str(Path(sys.executable).parent / "pact-hook")


def test_hooks_only_needs_the_env_file_and_never_prints_the_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _no_claude(monkeypatch)
    config = tmp_path / "config"
    monkeypatch.setattr(connect, "CONFIG", config)
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(connect.sys, "argv", ["pact-connect", "hooks", "code-web", "--dir", str(project)])
    with pytest.raises(SystemExit, match="does not exist"):
        connect.main()
    assert not (project / ".claude").exists()
    token = "pact_" + "fake" * 4
    connect.write_secret(config / "code-web.env", connect.env_lines({"PACT_URL": "https://board.example", "PACT_TOKEN": token}))
    connect.main()
    out = capsys.readouterr().out
    assert token not in out and str(project / ".claude" / "settings.local.json") in out
    hooks = json.loads((project / ".claude" / "settings.local.json").read_text())["hooks"]
    assert hooks["UserPromptSubmit"][0]["hooks"][0]["command"] == f"{HOOK} code-web user-prompt-submit"
