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
GH_WRITES = {"pr", "issue", "release", "gist"}
BODY_FILE_FLAGS = ("-F", "--body-file", "--notes-file")
WRAPPERS = {"eval", "sh", "bash", "zsh", "dash", "xargs", "exec", "env", "nohup", "timeout", "command", "builtin", "time", "sudo"}
"""Commands that run another command the guard would not see as itself."""
_HIDDEN_PUBLISH = re.compile(r"\bgit\b.*\bpush\b|\bgh\s+(?:pr|issue|release|gist|api)\b", re.S)


def _words(command: str) -> list[list[str]]:
    """Split a shell line into simple commands on ;, &&, ||, |, &, newlines, subshell parentheses
    and backticks. Unparseable lines come back as one raw command so they are judged rather than
    skipped."""
    out: list[list[str]] = []
    for part in re.split(r"&&|\|\||;|\||\n|&|\(|\)|`", command):
        try:
            words = shlex.split(part)
        except ValueError:
            words = part.split()
        if words:
            out.append(words)
    return out


def _strip_env(words: list[str]) -> list[str]:
    """The command without leading ``NAME=value`` assignments."""
    i = 0
    while i < len(words) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[i]):
        i += 1
    return words[i:]


def _git_args(words: list[str]) -> list[str] | None:
    """The arguments after ``git``, skipping env assignments and git's own options (``-C dir``)."""
    words = _strip_env(words)
    i = 0
    if i >= len(words) or words[i].rsplit("/", 1)[-1] != "git":
        return None
    i += 1
    while i < len(words) and words[i].startswith("-"):
        i += 2 if words[i] in ("-C", "-c", "--git-dir", "--work-tree") else 1
    return words[i:]


def _gh_write(words: list[str]) -> bool:
    """``gh pr|issue|release|gist …``: a command that publishes text."""
    words = _strip_env(words)
    return len(words) >= 2 and words[0] == "gh" and words[1] in GH_WRITES


def _is_push(words: list[str]) -> bool:
    args = _git_args(words)
    return args is not None and args[:1] == ["push"]


def _api_writes(args: list[str]) -> bool:
    """Whether ``gh api <args>`` changes something: a method other than GET, or fields (which make
    it a POST)."""
    for i, a in enumerate(args):
        method = None
        if a in ("-X", "--method") and i + 1 < len(args):
            method = args[i + 1]
        elif a.startswith("--method="):
            method = a.split("=", 1)[1]
        elif a.startswith("-X") and len(a) > 2:
            method = a[2:]
        if method is not None and method.upper() != "GET":
            return True
        if a in ("-f", "-F", "--field", "--raw-field", "--input") or a.startswith(("--field=", "--raw-field=", "--input=")):
            return True
    return False


def _short_flags(opts: list[str]) -> str:
    """The letters of single-dash options, so ``-uf`` counts as ``-u -f``."""
    return "".join(o[1:] for o in opts if o.startswith("-") and not o.startswith("--"))


def refusal(command: str, current_branch: str | None = None) -> str | None:
    """Why ``command`` must not run, or None. ``current_branch`` is what a push without a target
    (``git push``, ``git push origin HEAD``) would push."""
    commands = _words(command)
    publishing = [w for w in commands if _is_push(w) or _gh_write(w)]
    others = [w for w in commands if w not in publishing and _strip_env(w)[:1] != ["cd"]]
    if publishing and others:
        # The guard reads what will be published before anything in the line runs, so a commit or a
        # body file made earlier in the same line would not be read.
        return (
            "Run git push and gh pr/issue/release/gist writes in a Bash call of their own (a leading cd is fine), "
            "after the commit or the body file exists, so the guard can read what they publish."
        )
    for words in commands:
        bare = _strip_env(words)
        if bare and bare[0].rsplit("/", 1)[-1] in WRAPPERS and _HIDDEN_PUBLISH.search(" ".join(bare[1:])):
            return "Run git push and gh writes directly, not through eval, sh -c, xargs or a similar wrapper."
        if len(bare) >= 3 and bare[0] == "gh" and bare[1] == "pr" and bare[2] == "merge":
            return "Runner sessions never merge pull requests; a person merges."
        if bare[:2] == ["gh", "api"] and _api_writes(bare[2:]):
            return "Runner sessions never write through gh api; use gh pr create and push a branch."
        if bare[:1] == ["git"] and any(v.startswith("alias.") for v in bare[1:]):
            return "Runner sessions never define git aliases."
        args = _git_args(words)
        if not args or args[0] != "push":
            continue
        opts = [a for a in args[1:] if a.startswith("-")]
        if any(
            o in ("--force", "--force-with-lease", "--mirror", "--all") or o.startswith("--force") for o in opts
        ) or "f" in _short_flags(opts):
            return "Runner sessions never force-push or push every branch."
        if any(o.startswith("+") for o in args[1:]):
            return "Runner sessions never force-push (a + refspec)."
        refs = [a for a in args[1:] if not a.startswith("-")][1:]
        if "--delete" in opts or "d" in _short_flags(opts) or any(r.startswith(":") for r in refs):
            return "Runner sessions never delete remote branches."
        targets = [r.split(":", 1)[-1].removeprefix("refs/heads/") for r in refs] or ["HEAD"]
        for target in targets:
            if target == "HEAD":
                target = current_branch or ""
            if target in PROTECTED:
                return f"Runner sessions never push to {target}; push a feature branch and open a PR."
    return None


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
        elif _gh_write(words):
            bare = _strip_env(words)
            files: list[str] = []
            published: list[str] = []
            skip = False
            for i, w in enumerate(bare):
                if skip:
                    skip = False
                    continue
                if w in BODY_FILE_FLAGS:
                    files.append(bare[i + 1] if i + 1 < len(bare) else "-")
                    skip = True
                elif any(w.startswith(f + "=") for f in BODY_FILE_FLAGS if f.startswith("--")):
                    files.append(w.split("=", 1)[1])
                elif w.startswith("-F") and len(w) > 2:
                    files.append(w[2:])
                elif w.startswith("<"):
                    return "Pass a gh body with --body or --body-file, not on stdin, so the guard can read it."
                else:
                    published.append(w)
            text = leaks.without_mark("\n".join(published))
            for body in files:
                if body == "-":
                    return "Pass a gh body with --body or --body-file, not on stdin, so the guard can read it."
                try:
                    text += "\n" + leaks.without_mark((Path(cwd or ".") / body).read_text())
                except OSError as err:
                    return f"Could not read the body file {body}, so this is refused: {err}"
            found += leaks.scan(text, f"the gh {bare[1]} text", deny, home)
    if not found:
        return None
    listed = "; ".join(str(leak) for leak in dict.fromkeys(found))
    return (
        f"This would publish something that must stay private: {listed}. Remove it (rewrite the commit "
        f"if it is already committed) and try again with placeholders. Never mark real values to get "
        f"past this; `{leaks.ALLOW_MARK}` only counts on an obviously fake value inside a test file."
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
