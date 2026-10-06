"""PreToolUse guard for Runner sessions: no pushing to protected branches, no force pushes, no
merging PRs, and nothing that looks like a leak (see ``leaks``) in what a push or a ``gh`` write
would publish. Claude Code runs it before every Bash call; exit code 2 blocks the call and the
reason on stderr goes back to the model.

The allowlist decides which commands may run at all; this catches the dangerous forms of the
commands the allowlist lets through. Branch protection on the host is still the hard stop.
"""

import argparse
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

from . import leaks

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


GH_WRITES = {"pr", "issue", "release", "gist"}


def _push_sources(args: list[str]) -> list[str]:
    """What ``git push <args>`` sends: the source side of each refspec, HEAD when none is given."""
    refs = [a for a in args[1:] if not a.startswith("-")][1:]
    sources = [r.split(":", 1)[0].removeprefix("+") for r in refs]
    return [s for s in sources if s] if refs else ["HEAD"]


def _git_dir(words: list[str], cwd: str | None) -> str | None:
    if "-C" in words and words.index("-C") + 1 < len(words):
        return str(Path(cwd or ".") / words[words.index("-C") + 1])
    return cwd


def leak_refusal(command: str, cwd: str | None, deny: list[str], home: str | None = None) -> str | None:
    """Why ``command`` would publish something it must not, or None. Before a push this reads the
    outgoing commits; before ``gh pr|issue|release|gist`` it reads the command and any body file."""
    home = home if home is not None else str(Path.home())
    found: list[leaks.Leak] = []
    for words in _words(command):
        args = _git_args(words)
        if args and args[0] == "push":
            try:
                commits = leaks.outgoing(_git_dir(words, cwd), _push_sources(args))
            except (OSError, subprocess.SubprocessError) as err:
                return f"Could not read the commits this push would send, so it is refused: {err}"
            for sha, text in commits:
                found += leaks.scan(text, f"commit {sha}", deny, home)
        elif len(words) >= 2 and words[0] == "gh" and words[1] in GH_WRITES:
            # The text a person will read. Paths the command only works in (cd targets, the
            # worktree, the body file) are not published, so they are left out.
            text = command
            paths = [cwd or ""] + [w[i + 1] for w in _words(command) for i in range(len(w) - 1) if w[i] == "cd"]
            bodies = [words[i + 1] for i in range(len(words) - 1) if words[i] in ("-F", "--body-file")]
            for path in sorted(filter(None, paths + bodies), key=len, reverse=True):
                text = text.replace(path, "")
            for body in bodies:
                try:
                    text += "\n" + (Path(cwd or ".") / body).read_text()
                except OSError:
                    pass
            found += leaks.scan(text, f"the gh {words[1]} text", deny, home)
    if not found:
        return None
    listed = "; ".join(str(leak) for leak in dict.fromkeys(found))
    return (
        f"This would publish something that must stay private: {listed}. Remove it (rewrite the commit "
        f"if it is already committed) and try again. Use placeholders instead. If a line holds an "
        f"obviously fake test value, mark that line with `{leaks.ALLOW_MARK}`."
    )


def _branch(cwd: str | None) -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd or None, capture_output=True, text=True, timeout=5, check=True
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def main() -> None:
    parser = argparse.ArgumentParser(prog="pact-runner-guard")
    parser.add_argument("--deny-file", type=Path, help="words that must never be published, one per line")
    opts = parser.parse_args()
    try:
        event = json.load(sys.stdin)
    except ValueError:
        print("pact-runner-guard: unreadable hook input; refusing", file=sys.stderr)
        sys.exit(2)
    if event.get("tool_name") != "Bash":
        return
    command = str((event.get("tool_input") or {}).get("command", ""))
    cwd = event.get("cwd")
    reason = refusal(command, _branch(cwd)) or leak_refusal(command, cwd, leaks.deny_words(opts.deny_file))
    if reason:
        print(reason, file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
