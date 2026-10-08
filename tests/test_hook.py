"""``pact-hook``, run as Claude Code runs it: a subprocess with the hook JSON on stdin, against a
small local HTTP stub of the board."""

import json
import os
import shlex
import subprocess
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

TOKEN = "pact_" + "fake" * 4


class Stub:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.answer = b'{"systemMessage": "2 open tasks"}'
        self.status = 200


@pytest.fixture
def board() -> Iterator[tuple[str, Stub]]:
    stub = Stub()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            stub.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
            self.send_response(stub.status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(stub.answer)

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}", stub
    server.shutdown()


def _conf(tmp_path: Path, url: str, **extra: str) -> Path:
    conf = tmp_path / "conf"
    conf.mkdir(exist_ok=True)
    values = {"PACT_URL": url, "PACT_TOKEN": TOKEN, **extra}
    (conf / "code-web.env").write_text("".join(f"{k}={shlex.quote(v)}\n" for k, v in values.items()))
    return conf


def _hook(conf: Path, event: str, cwd: Path, stdin: str = '{"session_id": "s1"}') -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("PACT_")}
    env.update(PACT_HOOK_DIR=str(conf), NO_PROXY="*", no_proxy="*")
    return subprocess.run(
        [sys.executable, "-m", "pact_runner.hook", "code-web", event],
        input=stdin,
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
        timeout=30,
    )


def test_no_config_is_silent(tmp_path: Path) -> None:
    done = _hook(tmp_path / "nowhere", "session-start", tmp_path)
    assert done.returncode == 0 and done.stdout == "" and done.stderr == ""


def test_a_board_that_is_down_is_silent(tmp_path: Path) -> None:
    import socket

    with socket.socket() as s:  # a port nobody listens on
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    done = _hook(_conf(tmp_path, f"http://127.0.0.1:{port}"), "user-prompt-submit", tmp_path)
    assert done.returncode == 0 and done.stdout == "" and done.stderr == ""


def test_other_events_print_the_answer_and_forward_stdin(tmp_path: Path, board: tuple[str, Stub]) -> None:
    url, stub = board
    for event in ("session-start", "stop", "user-prompt-submit"):
        done = _hook(_conf(tmp_path, url), event, tmp_path)
        assert done.returncode == 0 and done.stdout == stub.answer.decode() + "\n"
    assert [r["path"] for r in stub.requests] == [
        "/hooks/a/code-web/session-start",
        "/hooks/a/code-web/stop",
        "/hooks/a/code-web/user-prompt-submit",
    ]
    first = stub.requests[0]
    assert json.loads(first["body"]) == {"session_id": "s1"}
    assert first["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert first["headers"]["X-Pact-Auto-Claim"] == "0" and first["headers"]["X-Pact-Auto-Claim-From"] == ""
    assert first["headers"]["X-Pact-Git-Clean"] == "0"


def test_post_tool_use_is_quiet_unless_it_carries_messages(tmp_path: Path, board: tuple[str, Stub]) -> None:
    url, stub = board
    conf = _conf(tmp_path, url)
    stub.answer = b'{"ok": true}'
    done = _hook(conf, "post-tool-use", tmp_path)
    assert done.returncode == 0 and done.stdout == "" and len(stub.requests) == 1
    stub.answer = b'{"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "a message"}}'
    done = _hook(conf, "post-tool-use", tmp_path)
    assert done.returncode == 0 and json.loads(done.stdout)["hookSpecificOutput"]["additionalContext"] == "a message"


def test_auto_claim_flags_and_a_clean_repo_are_sent(tmp_path: Path, board: tuple[str, Stub]) -> None:
    url, stub = board
    conf = _conf(tmp_path, url, PACT_AUTO_CLAIM="1", PACT_AUTO_CLAIM_FROM="chat-a,chat b")
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _hook(conf, "user-prompt-submit", repo)
    (repo / "dirty.txt").write_text("x")
    _hook(conf, "user-prompt-submit", repo)
    _hook(conf, "stop", repo.parent)  # computed for user-prompt-submit only
    clean = [r["headers"]["X-Pact-Git-Clean"] for r in stub.requests]
    assert clean == ["1", "0", "0"]
    assert stub.requests[0]["headers"]["X-Pact-Auto-Claim"] == "1"
    assert stub.requests[0]["headers"]["X-Pact-Auto-Claim-From"] == "chat-a,chat b"


def test_a_folder_of_repos_is_clean_only_when_every_repo_is(tmp_path: Path, board: tuple[str, Stub]) -> None:
    url, stub = board
    conf = _conf(tmp_path, url)
    folder = tmp_path / "work"
    folder.mkdir()
    _hook(conf, "user-prompt-submit", folder)  # no repo at all
    for name in ("a", "b"):
        subprocess.run(["git", "init", "-q", str(folder / name)], check=True)
    _hook(conf, "user-prompt-submit", folder)
    (folder / "b" / "dirty.txt").write_text("x")
    _hook(conf, "user-prompt-submit", folder)
    assert [r["headers"]["X-Pact-Git-Clean"] for r in stub.requests] == ["0", "1", "0"]
