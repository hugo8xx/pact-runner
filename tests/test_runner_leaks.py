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
from pact_runner.guard import leak_refusal, refusal

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


# ── findings of the outside review (gemini-pact, 2026-10-06) ──────────────────


def guarded(command: str, cwd: str, deny: list[str] | None = None) -> str | None:
    return refusal(command, "runner/t1") or leak_refusal(command, cwd, deny or [], HOME)


def test_a_push_must_run_on_its_own(clone: Path) -> None:
    """The guard reads the outgoing commits before the line runs, so a commit made earlier in the
    same line would slip past it."""
    assert guarded(f'git commit --allow-empty -m "{KEY}" && git push origin runner/t1', str(clone))
    assert guarded("git add . && git commit -m x; git push", str(clone))
    assert guarded("echo body > pr.md && gh pr create --title t --body-file pr.md", str(clone))
    assert guarded("cd somewhere && git push -u origin runner/t1", str(clone)) is None  # a leading cd is fine
    assert guarded("git status && git log --oneline -3", str(clone)) is None  # no push, nothing to read


@pytest.mark.parametrize(
    "command",
    [
        "(git push origin main)",
        "git push origin main &",
        "echo `git push origin main`",
        'eval "git push origin main"',
        'sh -c "git push origin main"',
        "echo origin main | xargs git push",
        'env X=1 bash -c "gh pr create --title t --body x"',
        'git -c alias.sp="push origin main" sp',
        "gh api -X PUT repos/o/r/pulls/1/merge",
        "gh api --method=DELETE repos/o/r/git/refs/heads/x",
        "gh api repos/o/r/issues/1/comments -f body=hello",
        "git push origin :runner/t1",
        "git push -uf origin runner/t1",
        "git push -ud origin runner/t1",
    ],
)
def test_ways_around_the_guard_are_refused(command: str) -> None:
    assert refusal(command, "runner/t1")


@pytest.mark.parametrize("command", ["gh api repos/o/r/pulls/1", "gh api -X GET repos/o/r", "git push -u origin runner/t1"])
def test_harmless_forms_still_run(command: str) -> None:
    assert refusal(command, "runner/t1") is None


@pytest.mark.parametrize(
    "command",
    [
        "gh pr create --title t --body-file=pr.md",
        "gh pr create --title t -Fpr.md",
        "gh pr create --title t -F pr.md",
        "GH_X=1 gh pr create --title t --body-file pr.md",
        "gh release create v1 --notes-file pr.md",
    ],
)
def test_every_way_of_naming_a_body_file_is_read(clone: Path, command: str) -> None:
    (clone / "pr.md").write_text(f"deployed with {KEY}\n")
    assert leak_refusal(command, str(clone), [], HOME)
    (clone / "pr.md").write_text("all checks pass\n")
    assert leak_refusal(command, str(clone), [], HOME) is None


def test_a_body_on_stdin_or_a_missing_file_is_refused(clone: Path) -> None:
    assert leak_refusal("gh pr create --title t < pr.md", str(clone), [], HOME)
    assert leak_refusal("gh pr create --title t --body-file -", str(clone), [], HOME)
    assert leak_refusal("gh pr create --title t --body-file nowhere.md", str(clone), [], HOME)


@pytest.mark.parametrize(
    "line",
    [
        "key = AIza" + "SyD-1234567890123456789012345678901",
        "redis://:" + "my_super_secret_pw@db:6379",
        "-----BEGIN PGP PRIVATE " + "KEY BLOCK-----",
    ],
)
def test_more_secrets_are_caught(line: str) -> None:
    assert leaks.scan(line, "x", [], HOME)


@pytest.mark.parametrize(
    "line",
    ["def pact_board_environment_variable(): ...", 'class="sk-placeholder-skeleton-loader"', "DATABASE=pact_runner_split_test"],
)
def test_ordinary_names_are_not_secrets(line: str) -> None:
    assert leaks.scan(line, "x", [], HOME) == []
    assert leaks.scan("token: pact_" + "Ab3dE5" * 7, "x", [], HOME)  # a generated one still is


def test_denied_words_match_whole_words() -> None:
    assert leaks.scan("the channel is open", "x", ["Ann"], HOME) == []
    assert leaks.scan("written by Ann Lee", "x", ["Ann"], HOME)
    assert leaks.scan("see board.internal.example/x", "x", ["board.internal.example"], HOME)


def test_leak_ok_counts_only_in_test_files(clone: Path) -> None:
    (clone / "tests").mkdir()
    (clone / "src").mkdir()
    commit(clone, "tests/test_x.py", f'FAKE = "{KEY}"  # leak-ok\n')
    assert leak_refusal("git push origin runner/t1", str(clone), [], HOME) is None
    commit(clone, "src/app.py", f'KEY = "{KEY}"  # leak-ok\n')
    assert leak_refusal("git push origin runner/t1", str(clone), [], HOME)


def test_leak_ok_does_not_count_in_messages_or_pr_text(clone: Path) -> None:
    commit(clone, "b.txt", "x\n", message=f"use {KEY} leak-ok")
    assert leak_refusal("git push origin runner/t1", str(clone), [], HOME)
    assert leak_refusal(f'gh pr comment 1 --body "{KEY} leak-ok"', str(clone), [], HOME)


def test_the_home_path_is_found_in_pr_text_even_when_it_is_the_cwd(clone: Path) -> None:
    assert leak_refusal(f'gh pr comment 1 --body "tested in {HOME}/wt/x"', f"{HOME}/wt", [], HOME)


def test_merge_commits_and_plus_lines_are_read(clone: Path) -> None:
    git(clone, "switch", "-qc", "side")
    commit(clone, "s.txt", "side\n")
    git(clone, "switch", "-q", "runner/t1")
    commit(clone, "m.txt", "main side\n")
    git(clone, "merge", "-q", "--no-ff", "--no-commit", "side")
    (clone / "resolved.txt").write_text(f"{KEY}\n")
    git(clone, "add", ".")
    git(clone, "commit", "-qm", "merge side")
    assert leak_refusal("git push origin runner/t1", str(clone), [], HOME)  # a secret added in the merge itself
    commit(clone, "c.txt", "++counter;\n")
    assert "++counter;" in "\n".join(t for _, t in leaks.outgoing(str(clone), ["runner/t1"]))
