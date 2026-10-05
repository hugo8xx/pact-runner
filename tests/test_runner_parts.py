"""Runner pieces that need no board: the push guard, the stream-json reader, the child environment."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from pact_runner.claude import StreamReader, child_env
from pact_runner.config import RunnerConfig
from pact_runner.guard import refusal


@pytest.mark.parametrize(
    "command",
    [
        "git push origin main",
        "git push origin HEAD:main",
        "git push origin feat:refs/heads/main",
        "git push -u origin stage",
        "git push --force origin runner/abc",
        "git push -f",
        "git push origin +runner/abc",
        "git push --force-with-lease",
        "git -C /tmp/x push origin main",
        "cd repo && git push origin master",
        "gh pr merge 12 --squash",
        "git push origin --delete runner/abc",
    ],
)
def test_guard_blocks(command: str) -> None:
    assert refusal(command, current_branch="runner/abc")


@pytest.mark.parametrize(
    "command",
    [
        "git push -u origin runner/abc",
        "git push origin HEAD",
        "git push",
        "git status && git log --oneline -3",
        "gh pr create --fill",
        "echo main",
    ],
)
def test_guard_allows(command: str) -> None:
    assert refusal(command, current_branch="runner/abc") is None


def test_guard_checks_what_a_bare_push_would_push() -> None:
    assert refusal("git push", current_branch="main")
    assert refusal("git push origin HEAD", current_branch="stage")


def test_guard_as_a_hook(tmp_path: Path) -> None:
    def run(event: dict[str, object]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "pact_runner.guard"], input=json.dumps(event), capture_output=True, text=True
        )

    blocked = run({"tool_name": "Bash", "tool_input": {"command": "git push origin main"}, "cwd": str(tmp_path)})
    assert blocked.returncode == 2 and "never push to main" in blocked.stderr
    assert run({"tool_name": "Bash", "tool_input": {"command": "git status"}}).returncode == 0
    assert run({"tool_name": "Read", "tool_input": {"file_path": "x"}}).returncode == 0


def test_stream_reader_takes_session_turns_and_structured_output() -> None:
    r = StreamReader()
    r.feed(json.dumps({"type": "system", "subtype": "init", "session_id": "abc"}))
    r.feed("not json")
    info = {"status": "allowed", "unifiedWindows": {"five_hour": {"utilization": 0.14, "resetsAt": 1}}}
    r.feed(json.dumps({"type": "rate_limit_event", "rate_limit_info": info, "session_id": "abc"}))
    r.feed(
        json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "num_turns": 2,
                "result": "{}",
                "structured_output": {"status": "completed", "result": "ok"},
                "session_id": "abc",
            }
        )
    )
    out = r.out
    assert (out.session_id, out.num_turns, out.structured, out.quota_hit) == (
        "abc",
        2,
        {"status": "completed", "result": "ok"},
        False,
    )
    assert out.rate_limit == info


@pytest.mark.parametrize(
    "event",
    [
        {"type": "rate_limit_event", "rate_limit_info": {"status": "rejected"}},
        {"type": "result", "is_error": True, "api_error_status": 429, "result": ""},
        {"type": "result", "is_error": True, "result": "You've hit your limit · resets 5pm"},
    ],
)
def test_stream_reader_spots_a_usage_limit(event: dict[str, object]) -> None:
    r = StreamReader()
    r.feed(json.dumps(event))
    assert r.out.quota_hit


def cfg(**kw: object) -> RunnerConfig:
    return RunnerConfig(board_url="http://b", agent_id="r", token="t", state_dir=Path("/tmp/x"), **kw)  # type: ignore[arg-type]


def test_subscription_mode_drops_every_api_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "PACT_RUNNER_TOKEN"):
        monkeypatch.setenv(k, "secret")
    env = child_env(cfg(), {"PACT_TASK_ID": "1"})
    assert not {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "PACT_RUNNER_TOKEN"} & set(env)
    assert env["PACT_TASK_ID"] == "1"


def test_api_key_mode_hands_over_only_the_configured_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "someone-elses")
    env = child_env(cfg(auth="api_key", api_key="sk-runner"), {})
    assert env["ANTHROPIC_API_KEY"] == "sk-runner"


def test_config_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PACT_URL", "https://board.example")
    monkeypatch.setenv("PACT_RUNNER_AGENT", "runner-web")
    monkeypatch.setenv("PACT_RUNNER_TOKEN", "pact_x")
    monkeypatch.setenv("PACT_RUNNER_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("PACT_RUNNER_QUIET_HOURS", "9-18")
    c = RunnerConfig.from_env()
    assert c.mcp_url == "https://board.example/mcp/a/runner-web" and c.quiet_hours == (9, 18) and c.max_turns == 40
    monkeypatch.setenv("PACT_RUNNER_AUTH", "api_key")
    with pytest.raises(SystemExit):
        RunnerConfig.from_env()
