"""A working directory per task. With a repo, a git worktree on branch ``runner/<task>`` cut from
the remote's default branch, so parallel runs never share a checkout and each starts clean."""

import asyncio
import shutil
from pathlib import Path


async def _git(repo: Path, *args: str, check: bool = True) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(repo), *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {err.decode().strip()}")
    return out.decode().strip()


def branch_for(task_id: str) -> str:
    return f"runner/{task_id[:8]}"


def _ensure_parent(path: Path) -> bool:
    """Create ``path``'s parent; True if ``path`` already exists."""
    if path.exists():
        return True
    path.parent.mkdir(parents=True, exist_ok=True)
    return False


async def prepare(repo: Path | None, root: Path, task_id: str) -> Path:
    """The task's directory, created on first use and reused when the task comes back."""
    path = root / task_id[:8]
    if await asyncio.to_thread(_ensure_parent, path):
        return path
    if repo is None:
        await asyncio.to_thread(path.mkdir)
        return path
    base = "HEAD"
    if await _git(repo, "remote", check=False):
        await _git(repo, "fetch", "--quiet", "origin", check=False)
        head = await _git(repo, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD", check=False)
        base = head.removeprefix("refs/remotes/") if head else "origin/main"
    await _git(repo, "worktree", "add", "-B", branch_for(task_id), str(path), base)
    return path


async def remove(repo: Path | None, path: Path) -> None:
    """Drop a finished task's directory. The branch stays: it may hold a pushed PR."""
    if repo is None:
        await asyncio.to_thread(shutil.rmtree, path, True)
        return
    await _git(repo, "worktree", "remove", "--force", str(path), check=False)
