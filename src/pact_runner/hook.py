"""``pact-hook <agent-id> <event>``: the PACT Board hook for Claude Code, in Python.

It does what the board's ``hooks/pact-hook.sh`` does, so ``pact-connect`` can install hooks without
copying a shell script around. It forwards the hook's stdin JSON to
``POST {PACT_URL}/hooks/a/<agent-id>/<event>`` and prints the board's answer:

- ``session-start``: the open tasks when a session opens; never claims.
- ``user-prompt-submit``: may auto-claim one delegated task when ``PACT_AUTO_CLAIM=1``, the sender is
  in ``PACT_AUTO_CLAIM_FROM`` and the working tree is clean; hands Claude new messages.
- ``post-tool-use``: logs a shell command or file edit. Printed only when the answer carries
  ``hookSpecificOutput`` (new messages from other agents for Claude).
- ``stop``: tells the person about new tasks; never claims.

``PACT_URL`` and ``PACT_TOKEN`` (and ``PACT_AUTO_CLAIM``, ``PACT_AUTO_CLAIM_FROM``) come from
``${PACT_HOOK_DIR:-~/.config/pact}/<agent-id>.env``. Missing config or an unreachable board never
blocks Claude Code: it prints nothing and exits 0. Standard library only, so it starts fast.
"""

import os
import shlex
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

TIMEOUT = 5.0


def read_env(path: Path) -> dict[str, str]:
    """The ``KEY=value`` lines of a shell env file (values may be shell-quoted)."""
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        try:
            words = shlex.split(line, comments=True)
        except ValueError:
            continue
        if words[:1] == ["export"]:
            words = words[1:]
        for word in words:
            key, sep, value = word.partition("=")
            if sep and key.isidentifier():
                values[key] = value
    return values


def _status(*args: str) -> str:
    try:
        return subprocess.run(["git", *args, "status", "--porcelain"], capture_output=True, text=True).stdout
    except OSError:
        return ""


def git_clean(cwd: Path) -> bool:
    """No uncommitted or staged changes. A folder that is not a repo counts as clean only when it
    holds at least one repo and every one is clean."""
    try:
        inside = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=cwd, capture_output=True).returncode == 0
    except OSError:
        inside = False
    if inside:
        return _status("-C", str(cwd)) == ""
    repos = [d for d in sorted(cwd.iterdir()) if not d.name.startswith(".") and d.is_dir() and (d / ".git").is_dir()]
    return bool(repos) and all(_status("-C", str(d)) == "" for d in repos)


def run(agent: str, event: str, body: bytes, cwd: Path) -> str | None:
    """The text to print, or None to stay quiet."""
    conf = Path(os.environ.get("PACT_HOOK_DIR") or Path.home() / ".config" / "pact") / f"{agent}.env"
    if not agent or not event:
        return None
    try:
        env = {**os.environ, **read_env(conf)}
    except OSError:
        return None
    url, token = env.get("PACT_URL", ""), env.get("PACT_TOKEN", "")
    if not url or not token:
        return None
    clean = event == "user-prompt-submit" and git_clean(cwd)
    request = urllib.request.Request(
        f"{url}/hooks/a/{agent}/{event}",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-Pact-Auto-Claim": env.get("PACT_AUTO_CLAIM") or "0",
            "X-Pact-Auto-Claim-From": env.get("PACT_AUTO_CLAIM_FROM", ""),
            "X-Pact-Git-Clean": "1" if clean else "0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            raw: bytes = response.read()
    except urllib.error.HTTPError as err:  # curl without -f prints an error body too
        try:
            raw = err.read()
        except OSError:
            return None
    except (OSError, ValueError):
        return None
    out = raw.decode("utf-8", "replace").rstrip("\n")
    if event == "post-tool-use" and "hookSpecificOutput" not in out:
        return None
    return out


def main() -> None:
    try:
        agent, event = (sys.argv[1:] + ["", ""])[:2]
        body = sys.stdin.buffer.read() if not sys.stdin.isatty() else b""
        out = run(agent, event, body, Path.cwd())
        if out is not None:
            sys.stdout.write(out + "\n")
    except Exception:  # never block Claude Code
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
