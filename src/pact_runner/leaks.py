"""What must not leave the machine in a push or a PR: secrets, the owner's home path, and any words
the owner lists in a deny file (real hosts, names, emails). The guard runs this before ``git push``
and before ``gh`` writes a PR, issue or comment, because on a public repository those are public
for good.

A line that holds an obviously fake test value can say so with ``leak-ok`` anywhere on it.
"""

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

ALLOW_MARK = "leak-ok"

SECRETS: list[tuple[str, re.Pattern[str]]] = [
    ("a private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("an API key (sk-…)", re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{16,}")),
    ("a GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("an AWS key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("a Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("a Slack webhook", re.compile(r"hooks\.slack\.com/services/[A-Za-z0-9/]{10,}")),
    ("a PACT token", re.compile(r"\bpact_[A-Za-z0-9_-]{20,}")),
    ("a JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("a password in a URL", re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s:/@]+:[^\s@/]+@", re.IGNORECASE)),
]


@dataclass(frozen=True)
class Leak:
    what: str
    where: str

    def __str__(self) -> str:
        return f"{self.what} in {self.where}"


def deny_words(path: Path | None) -> list[str]:
    """One word or phrase per line; blank lines and ``#`` comments are skipped. Matched ignoring case."""
    if not path:
        return []
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    return [w.strip() for w in lines if w.strip() and not w.lstrip().startswith("#")]


def scan(text: str, where: str, deny: list[str], home: str | None = None) -> list[Leak]:
    found: list[Leak] = []
    for n, line in enumerate(text.splitlines(), 1):
        if ALLOW_MARK in line:
            continue
        at = f"{where} line {n}" if "\n" in text else where
        found += [Leak(what, at) for what, pattern in SECRETS if pattern.search(line)]
        if home and home in line:
            found.append(Leak("this machine's home path", at))
        lower = line.lower()
        found += [Leak(f'the denied word "{w}"', at) for w in deny if w.lower() in lower]
    return found


def outgoing(cwd: str | None, sources: list[str]) -> list[tuple[str, str]]:
    """(commit, text) for every commit the push would send that no remote has yet: its message and
    the lines it adds. Raises CalledProcessError when git cannot tell, so the caller can refuse."""
    log = subprocess.run(
        ["git", "log", "-p", "--no-color", "--no-ext-diff", "--format=%x00%h%n%B%x01", *sources, "--not", "--remotes", "--"],
        cwd=cwd or None,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    ).stdout
    out: list[tuple[str, str]] = []
    for chunk in log.split("\x00")[1:]:
        head, _, diff = chunk.partition("\x01")
        sha, _, message = head.partition("\n")
        added = [line[1:] for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++")]
        out.append((sha, message.strip() + "\n" + "\n".join(added)))
    return out
