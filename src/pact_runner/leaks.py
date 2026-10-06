"""What must not leave the machine in a push or a PR: secrets, the owner's home path, and any words
the owner lists in a deny file (real hosts, names, emails). The guard runs this before ``git push``
and before ``gh`` writes a PR, issue or comment, because on a public repository those are public
for good.

A line that holds an obviously fake test value can say so with ``leak-ok`` anywhere on it, but only
in a test file: the marker is ignored in other files, in commit messages and in PR or issue text.
"""

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

ALLOW_MARK = "leak-ok"

SECRETS: list[tuple[str, re.Pattern[str]]] = [
    ("a private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY(?: BLOCK)?-----")),
    ("an API key (sk-…)", re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{16,}")),
    ("a Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}")),
    ("a GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("an AWS key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("a Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("a Slack webhook", re.compile(r"hooks\.slack\.com/services/[A-Za-z0-9/]{10,}")),
    ("a PACT token", re.compile(r"\bpact_[A-Za-z0-9_-]{20,}")),
    ("a JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("a password in a URL", re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s:/@]*:[^\s@/]+@", re.IGNORECASE)),
]

RANDOM_ONLY = {"an API key (sk-…)", "a PACT token"}
"""Patterns that also match ordinary names (``pact_board_settings``, a CSS class ``sk-skeleton``):
they count only when the match looks generated, with upper case letters and digits in it."""


def _looks_generated(text: str) -> bool:
    return any(c.isupper() for c in text) and any(c.isdigit() for c in text)


def is_test_file(path: str) -> bool:
    """Where ``leak-ok`` is honoured: files under a tests directory, or named like a test."""
    parts = Path(path).parts
    name = Path(path).name
    return any(p in ("tests", "test", "__tests__") for p in parts[:-1]) or name.startswith("test_") or ".test." in name


def without_mark(text: str) -> str:
    """The text with every ``leak-ok`` taken out, for places the marker does not count."""
    return text.replace(ALLOW_MARK, "")


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
        for what, pattern in SECRETS:
            hits = [m.group(0) for m in pattern.finditer(line)]
            if what in RANDOM_ONLY:
                hits = [h for h in hits if _looks_generated(h)]
            if hits:
                found.append(Leak(what, at))
        if home and home in line:
            found.append(Leak("this machine's home path", at))
        found += [
            Leak(f'the denied word "{w}"', at)
            for w in deny
            if re.search(rf"(?<![A-Za-z0-9]){re.escape(w)}(?![A-Za-z0-9])", line, re.IGNORECASE)
        ]
    return found


_FILE = re.compile(r"^\+\+\+ (?:b/(.*)|/dev/null)$")


def outgoing(cwd: str | None, sources: list[str]) -> list[tuple[str, str]]:
    """(commit, text) for every commit the push would send that no remote has yet: its message and
    the lines it adds, merge commits included (against each parent). ``leak-ok`` stays only on lines
    added to test files. Raises CalledProcessError when git cannot tell, so the caller can refuse."""
    log = subprocess.run(
        [
            "git",
            "log",
            "-p",
            "-m",
            "--no-color",
            "--no-ext-diff",
            "--format=%x00%h%n%B%x01",
            *sources,
            "--not",
            "--remotes",
            "--",
        ],
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
        added: list[str] = []
        path = ""
        for line in diff.splitlines():
            header = _FILE.match(line)
            if header:
                path = header.group(1) or ""
            elif line.startswith("+"):
                added.append(line[1:] if is_test_file(path) else without_mark(line[1:]))
        out.append((sha, without_mark(message.strip()) + "\n" + "\n".join(added)))
    return out
