"""PreToolUse guard for Runner sessions: no pushing to protected branches, no force pushes, no
merging PRs. Claude Code runs it before every Bash call; exit code 2 blocks the call and the reason
on stderr goes back to the model.

The allowlist decides which commands may run at all; this catches the dangerous forms of the
commands the allowlist lets through. Branch protection on the host is still the hard stop.
"""

import json
import re
import shlex
import subprocess
import sys

PROTECTED = ("main", "master", "stage", "staging", "release")


def _words(command: str) -> list[list[str]]:
    """Split a shell line into simple commands on ;, &&, || and |. Unparseable lines come back
    as one raw command so they are judged rather than skipped."""
    out: list[list[str]] = []
    for part in re.split(r"&&|\|\||;|\||\n", command):
        try:
            words = shlex.split(part)
        except ValueError:
            words = part.split()
        if words:
            out.append(words)
    return out


def _git_args(words: list[str]) -> list[str] | None:
    """The arguments after ``git``, skipping env assignments and git's own options (``-C dir``)."""
    i = 0
    while i < len(words) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[i]):
        i += 1
    if i >= len(words) or words[i].rsplit("/", 1)[-1] != "git":
        return None
    i += 1
    while i < len(words) and words[i].startswith("-"):
        i += 2 if words[i] in ("-C", "-c", "--git-dir", "--work-tree") else 1
    return words[i:]


def refusal(command: str, current_branch: str | None = None) -> str | None:
    """Why ``command`` must not run, or None. ``current_branch`` is what a push without a target
    (``git push``, ``git push origin HEAD``) would push."""
    for words in _words(command):
        if len(words) >= 3 and words[0] == "gh" and words[1] == "pr" and words[2] == "merge":
            return "Runner sessions never merge pull requests; a person merges."
        args = _git_args(words)
        if not args or args[0] != "push":
            continue
        opts = [a for a in args[1:] if a.startswith("-")]
        if any(o in ("-f", "--force", "--force-with-lease", "--mirror", "--all") or o.startswith("--force") for o in opts):
            return "Runner sessions never force-push or push every branch."
        if any(o.startswith("+") for o in args[1:]):
            return "Runner sessions never force-push (a + refspec)."
        refs = [a for a in args[1:] if not a.startswith("-")][1:]
        targets = [r.split(":", 1)[-1].removeprefix("refs/heads/") for r in refs] or ["HEAD"]
        for target in targets:
            if target == "HEAD":
                target = current_branch or ""
            if target in PROTECTED:
                return f"Runner sessions never push to {target}; push a feature branch and open a PR."
        if "--delete" in opts or "-d" in opts:
            return "Runner sessions never delete remote branches."
    return None


def _branch(cwd: str | None) -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd or None, capture_output=True, text=True, timeout=5, check=True
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def main() -> None:
    try:
        event = json.load(sys.stdin)
    except ValueError:
        print("pact-runner-guard: unreadable hook input; refusing", file=sys.stderr)
        sys.exit(2)
    if event.get("tool_name") != "Bash":
        return
    reason = refusal(str((event.get("tool_input") or {}).get("command", "")), _branch(event.get("cwd")))
    if reason:
        print(reason, file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
