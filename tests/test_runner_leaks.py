"""The guard keeps secrets, the owner's home path and denied words out of pushes and gh writes.

Fake secrets are built at run time, so this file never holds one a scanner would flag.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from pact_runner import leaks
from pact_runner.claude import guard_command
from pact_runner.config import RunnerConfig
from pact_runner.guard import leak_refusal

KEY = "sk-" + "ant-" + "A1b2" * 6
HOME = "/Users/someone"


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    """A clone with one pushed commit and a remote to push to."""
    remote, work = tmp_path / "remote.git", tmp_path / "work"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    subprocess.run(["git", "clone", "-q", str(remote), str(work)], check=True, capture_output=True)
    git(work, "config", "user.email", "dev@example.com")
    git(work, "config", "user.name", "Dev")
    (work / "a.txt").write_text("hello\n")
    git(work, "add", ".")
    git(work, "commit", "-qm", "first")
    git(work, "push", "-q", "origin", "HEAD:main")
    git(work, "switch", "-qc", "runner/t1")
    return work


def commit(work: Path, name: str, body: str, message: str = "change") -> None:
    (work / name).write_text(body)
    git(work, "add", ".")
    git(work, "commit", "-qm", message)


@pytest.mark.parametrize(
    ("line", "what"),
    [
        (f"key = {KEY}", "an API key"),
        ("token " + "ghp_" + "x" * 30, "a GitHub token"),
        ("postgres://app:" + "hunter2@db/prod", "a password in a URL"),
        (f"open {HOME}/notes.md", "home path"),
        ("see https://Board.Internal.Example", 'denied word "board.internal.example"'),
    ],
)
def test_scan_finds(line: str, what: str) -> None:
    [leak] = leaks.scan(line, "x", ["board.internal.example"], HOME)
    assert what in leak.what


def test_scan_passes_clean_text_and_marked_fakes() -> None:
    assert leaks.scan("https://board.example and /Users/you/x", "x", ["internal"], HOME) == []
    assert leaks.scan(f'SECRET = "{KEY}"  # leak-ok', "x", [], HOME) == []


def test_deny_words_skip_comments_and_a_missing_file(tmp_path: Path) -> None:
    f = tmp_path / "deny.txt"
    f.write_text("# real hosts\nboard.internal.example\n\n  Jane Doe \n")
    assert leaks.deny_words(f) == ["board.internal.example", "Jane Doe"]
    assert leaks.deny_words(tmp_path / "missing") == [] and leaks.deny_words(None) == []


def test_a_clean_push_goes_through(clone: Path) -> None:
    commit(clone, "b.txt", "nothing to see\n")
    assert leak_refusal("git push -u origin runner/t1", str(clone), [], HOME) is None


def test_a_secret_in_an_outgoing_commit_stops_the_push(clone: Path) -> None:
    commit(clone, "b.txt", f"KEY={KEY}\n")
    commit(clone, "c.txt", "fine\n")
    reason = leak_refusal("git push -u origin runner/t1", str(clone), [], HOME)
    assert reason and "an API key" in reason and "leak-ok" in reason
    assert leak_refusal("git push", str(clone), [], HOME)  # a bare push sends HEAD
    assert leak_refusal(f"git -C {clone} push origin HEAD:runner/t1", None, [], HOME)


def test_the_commit_message_counts_too(clone: Path) -> None:
    commit(clone, "b.txt", "x\n", message="fix: as seen on board.internal.example")
    assert leak_refusal("git push origin runner/t1", str(clone), ["board.internal.example"], HOME)


def test_what_a_remote_already_has_is_not_judged_again(clone: Path) -> None:
    """A removed line is not a leak, and commits already on a remote are old news."""
    commit(clone, "b.txt", f"KEY={KEY}\n")
    git(clone, "push", "-q", "origin", "runner/t1")
    commit(clone, "b.txt", "KEY=placeholder\n")
    assert leak_refusal("git push origin runner/t1", str(clone), [], HOME) is None


def test_a_push_git_cannot_read_is_refused(tmp_path: Path) -> None:
    reason = leak_refusal("git push origin runner/t1", str(tmp_path), [], HOME)  # not a repository
    assert reason and "Could not read" in reason


def test_gh_text_and_body_files_are_read(tmp_path: Path) -> None:
    body = tmp_path / "pr.md"
    body.write_text(f"Deployed with {KEY}\n")
    cwd = f"{HOME}/.local/state/runner/wt"
    assert leak_refusal(f"gh pr create --title t --body-file {body}", cwd, [], HOME)
    assert leak_refusal('gh issue comment 3 --body "see board.internal.example"', cwd, ["board.internal.example"], HOME)
    body.write_text("All checks pass.\n")
    # where the command runs is not published: the cd target, the worktree and the body file path
    assert leak_refusal(f"cd {cwd} && gh pr create --title t --body-file {body}", cwd, [], HOME) is None
    assert leak_refusal(f"gh pr comment 3 --body 'logs in {HOME}/x'", cwd, [], HOME)
    assert leak_refusal("gh repo view", cwd, ["anything"], HOME) is None  # not a write


def test_the_hook_reads_the_deny_file(clone: Path, tmp_path: Path) -> None:
    deny = tmp_path / "deny.txt"
    deny.write_text("board.internal.example\n")
    commit(clone, "b.txt", "url = https://board.internal.example\n")
    cfg = RunnerConfig(board_url="http://b", agent_id="r", token="t", state_dir=tmp_path, deny_file=deny)
    assert guard_command(cfg).endswith(f"--deny-file {deny}")
    event = {"tool_name": "Bash", "tool_input": {"command": "git push origin runner/t1"}, "cwd": str(clone)}
    run = subprocess.run(
        [sys.executable, "-m", "pact_runner.guard", "--deny-file", str(deny)],
        input=json.dumps(event),
        capture_output=True,
        text=True,
    )
    assert run.returncode == 2 and "board.internal.example" in run.stderr
