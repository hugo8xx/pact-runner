"""``pact-connect <board-url> <setup-code>``: connect this machine as the agent a person just hired.

The Admin UI hands out a one-time setup code instead of a token. This trades it for the token and
writes it straight into the agent's config, so the token is never shown or pasted:

- Claude Code (client ``code``): adds the board as an MCP server for the project directory, writes
  the hooks env file and installs the ``pact-hook`` hooks into the project's
  ``.claude/settings.local.json`` (``--no-hooks`` skips that). ``pact-connect hooks <agent-id>``
  installs them later for an agent that is already connected.
- Gemini (client ``gemini``): adds the board as an MCP server to Antigravity CLI's
  ``~/.gemini/config/mcp_config.json`` (and with ``--gemini-cli`` to Gemini CLI's
  ``~/.gemini/settings.json``), outside any repository, mode 600, keeping everything else.
- Runner (client ``runner``): writes the runner's env file (role settings, role instructions), clones
  its repository if asked, and on macOS installs and starts a LaunchAgent whose PATH holds the tools
  it needs (claude, uv, gh, git) as found on this machine.
"""

import argparse
import json
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx

CONFIG = Path.home() / ".config" / "pact"
LOGS = Path.home() / "Library" / "Logs"
AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
REPOS = Path.home() / "pact-runner" / "repos"
TOOLS = ("claude", "uv", "gh", "git", "pact-runner")
GEMINI_SETTINGS = Path.home() / ".gemini" / "settings.json"
ANTIGRAVITY_MCP = Path.home() / ".gemini" / "config" / "mcp_config.json"


def redeem(board: str, code: str, client: httpx.Client | None = None) -> dict[str, Any]:
    http = client or httpx.Client(timeout=30)
    response = http.post(f"{board.rstrip('/')}/connect", json={"code": code})
    body = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
    if response.status_code != 200:
        raise SystemExit(f"pact-connect: {body.get('message') or response.text[:200]}")
    return dict(body)


def write_secret(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(path, 0o600)


def env_lines(values: dict[str, str]) -> str:
    return "".join(f"{k}={shlex.quote(v)}\n" for k, v in values.items())


HOOK_EVENTS = (
    ("SessionStart", "session-start", None),
    ("UserPromptSubmit", "user-prompt-submit", None),
    ("PostToolUse", "post-tool-use", "Bash|Edit|MultiEdit|Write|NotebookEdit"),
    ("Stop", "stop", None),
)


def hook_command() -> str:
    """The absolute path of the installed ``pact-hook``, shell-quoted when it needs it."""
    found = shutil.which("pact-hook")
    path = Path(found).absolute() if found else Path(sys.executable).parent / "pact-hook"
    return shlex.quote(str(path))


def _is_pact_hook(command: Any, agent: str) -> bool:
    return (
        isinstance(command, str)
        and re.search(rf"pact-hook(\.sh)?['\"]?\s+['\"]?{re.escape(agent)}['\"]?(\s|$)", command) is not None
    )


def install_hooks(project_dir: Path, agent: str, command: str | None = None) -> Path:
    """Put this agent's PACT hooks into ``<project>/.claude/settings.local.json`` (Claude Code's
    per-user settings, not committed), keeping every other key and hook. Earlier PACT hooks for the
    same agent are replaced, so running it again changes nothing. A file that is not plain JSON is
    left alone rather than rewritten."""
    path = project_dir / ".claude" / "settings.local.json"
    current: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text() or "{}")
        except ValueError as err:
            raise SystemExit(f"pact-connect: {path} is not plain JSON ({err}); fix it or add the hooks yourself.") from err
        if not isinstance(loaded, dict):
            raise SystemExit(f"pact-connect: {path} does not hold a JSON object; fix it or add the hooks yourself.")
        current = loaded
    hooks = current.get("hooks")
    if hooks is None:
        hooks = current["hooks"] = {}
    if not isinstance(hooks, dict):
        raise SystemExit(f"pact-connect: 'hooks' in {path} is not a JSON object; fix it or add the hooks yourself.")
    command = command or hook_command()
    for name, event, matcher in HOOK_EVENTS:
        groups = hooks.get(name) or []
        if not isinstance(groups, list):
            raise SystemExit(f"pact-connect: 'hooks.{name}' in {path} is not a list; fix it or add the hooks yourself.")
        kept = []
        for group in groups:
            if isinstance(group, dict) and isinstance(group.get("hooks"), list):
                rest = [h for h in group["hooks"] if not (isinstance(h, dict) and _is_pact_hook(h.get("command"), agent))]
                if not rest:
                    continue
                group = {**group, "hooks": rest}
            kept.append(group)
        entry: dict[str, Any] = {"matcher": matcher} if matcher else {}
        entry["hooks"] = [{"type": "command", "command": f"{command} {shlex.quote(agent)} {event}", "timeout": 10}]
        hooks[name] = [*kept, entry]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(current, indent=2) + "\n")
    return path


def connect_code(
    info: dict[str, Any], project_dir: Path, mcp_name: str, config: Path = CONFIG, *, hooks: bool = True
) -> list[str]:
    agent = info["agent_id"]
    done = []
    env = config / f"{agent}.env"
    write_secret(env, env_lines({"PACT_URL": info["board_url"], "PACT_TOKEN": info["token"]}))
    done.append(f"hooks env: {env}")
    claude = shutil.which("claude")
    if claude:
        subprocess.run([claude, "mcp", "remove", mcp_name, "-s", "local"], cwd=project_dir, capture_output=True)
        subprocess.run(
            [
                claude,
                "mcp",
                "add",
                "--transport",
                "http",
                "-s",
                "local",
                mcp_name,
                info["mcp_url"],
                "--header",
                f"Authorization: Bearer {info['token']}",
            ],
            cwd=project_dir,
            capture_output=True,
            check=True,
        )
        done.append(f"Claude Code MCP server '{mcp_name}' for {project_dir} (restart the session to load it)")
    else:
        done.append("claude not found: add the MCP server yourself; the token is in the hooks env file")
    if hooks:
        done.append(f"Claude Code hooks: {install_hooks(project_dir, agent)} (restart the session to load them)")
    return done


def hooks_only(agent: str, project_dir: Path, config: Path | None = None) -> list[str]:
    """Install the hooks for an agent that is already connected: no setup code, and the token is
    neither read nor printed. Only the env file has to be there."""
    env = (config or CONFIG) / f"{agent}.env"
    if not env.is_file():
        raise SystemExit(f"pact-connect: {env} does not exist; connect {agent} first with a setup code.")
    return [f"Claude Code hooks: {install_hooks(project_dir, agent)} (restart the session to load them)"]


def hooks_main(argv: list[str]) -> None:
    p = argparse.ArgumentParser(
        prog="pact-connect hooks", description="Install the PACT hooks for a Claude Code agent that is already connected."
    )
    p.add_argument("agent", help="the agent id (its env file must be in ~/.config/pact)")
    p.add_argument("--dir", default=".", help="the project directory whose .claude/settings.local.json gets the hooks")
    args = p.parse_args(argv)
    for step in hooks_only(args.agent, Path(args.dir).resolve()):
        print(f"  - {step}")


def _add_mcp_server(path: Path, name: str, entry: dict[str, Any]) -> None:
    """Put one server into a JSON file's ``mcpServers``, keeping everything else in it. A file that
    is not plain JSON is left alone rather than rewritten."""
    current: dict[str, Any] = {}
    if path.exists():
        try:
            current = json.loads(path.read_text() or "{}")
        except ValueError as err:
            raise SystemExit(
                f"pact-connect: {path} is not plain JSON ({err}); fix it or add the server yourself. The code is used up."
            ) from err
        if not isinstance(current, dict):
            raise SystemExit(f"pact-connect: {path} does not hold a JSON object. The code is used up.")
    current.setdefault("mcpServers", {})[name] = entry
    write_secret(path, json.dumps(current, indent=2) + "\n")


def connect_gemini(
    info: dict[str, Any],
    mcp_name: str,
    config: Path = CONFIG,
    *,
    antigravity: Path | None = None,
    gemini_cli: Path | None = None,
    legacy: bool = False,
) -> list[str]:
    """A ``gemini`` agent runs in Antigravity CLI (``agy``), which replaced Gemini CLI for people
    signing in with a Google account. Its user-level MCP config gets the board (``serverUrl`` and a
    bearer header). With ``legacy``, Gemini CLI's ``settings.json`` gets it too (``httpUrl``), for
    people still on Gemini CLI with an API key. User-level files only: a project's config lives in
    the repository and could be committed with the token in it."""
    antigravity = antigravity or ANTIGRAVITY_MCP
    gemini_cli = gemini_cli or GEMINI_SETTINGS
    agent = info["agent_id"]
    bearer = {"Authorization": f"Bearer {info['token']}"}
    _add_mcp_server(antigravity, mcp_name, {"serverUrl": info["mcp_url"], "headers": bearer, "disabled": False})
    done = [f"Antigravity CLI MCP server '{mcp_name}' in {antigravity} (start agy again to load it; `agy mcp list` lists it)"]
    if legacy:
        _add_mcp_server(gemini_cli, mcp_name, {"httpUrl": info["mcp_url"], "headers": bearer, "trust": False})
        done.append(f"Gemini CLI MCP server '{mcp_name}' in {gemini_cli} (trust the folder you run gemini in)")
    env = config / f"{agent}.env"
    write_secret(env, env_lines({"PACT_URL": info["board_url"], "PACT_TOKEN": info["token"]}))
    return [*done, f"hooks env: {env}"]


def runner_env(info: dict[str, Any], repo: Path | None, role_file: Path | None) -> dict[str, str]:
    values = {
        "PACT_URL": info["board_url"],
        "PACT_RUNNER_AGENT": info["agent_id"],
        "PACT_RUNNER_TOKEN": info["token"],
        "PACT_RUNNER_PROJECT": info["project"],
        "PACT_RUNNER_AUTH": "subscription",
    }
    if repo:
        values["PACT_RUNNER_REPO"] = str(repo)
    if role_file:
        values["PACT_RUNNER_ROLE_FILE"] = str(role_file)
    values.update(info.get("settings") or {})
    return values


def launch_path() -> str:
    dirs: list[str] = []
    for tool in TOOLS:
        found = shutil.which(tool)
        if found and str(Path(found).parent) not in dirs:
            dirs.append(str(Path(found).parent))
    for d in ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"):
        if d not in dirs:
            dirs.append(d)
    return ":".join(dirs)


def launch_agent(agent: str, env: Path, runner_bin: str) -> tuple[str, bytes]:
    label = f"com.pact.runner.{agent}"
    log = str(LOGS / f"pact-runner-{agent}.log")
    plist = {
        "Label": label,
        "ProgramArguments": ["/bin/sh", "-c", f"set -a; . {shlex.quote(str(env))}; exec {shlex.quote(runner_bin)}"],
        "EnvironmentVariables": {"PATH": launch_path()},
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 60,
        "StandardOutPath": log,
        "StandardErrorPath": log,
    }
    return label, plistlib.dumps(plist)


def connect_runner(info: dict[str, Any], repo_arg: str | None, launch: bool, config: Path = CONFIG) -> list[str]:
    agent = info["agent_id"]
    done = []
    repo: Path | None = None
    if repo_arg:
        if Path(repo_arg).expanduser().is_dir():
            repo = Path(repo_arg).expanduser().resolve()
        else:
            repo = REPOS / agent
            if not repo.exists():
                subprocess.run(["git", "clone", "-q", repo_arg, str(repo)], check=True)
            done.append(f"repository: {repo}")
    role_file = None
    if (info.get("instructions") or "").strip():
        role_file = config / f"{agent}-role.md"
        write_secret(role_file, info["instructions"])
        done.append(f"role instructions: {role_file}")
    db = (info.get("settings") or {}).get("DATABASE_URL", "")
    if db.startswith("postgres://localhost") and shutil.which("createdb"):
        subprocess.run(["createdb", db.rsplit("/", 1)[-1]], capture_output=True)
    env = config / f"{agent}.env"
    write_secret(env, env_lines(runner_env(info, repo, role_file)))
    done.append(f"runner env: {env}")
    runner_bin = shutil.which("pact-runner") or "pact-runner"
    if sys.platform == "darwin":
        label, body = launch_agent(agent, env, runner_bin)
        plist = AGENTS_DIR / f"{label}.plist"
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_bytes(body)
        done.append(f"LaunchAgent: {plist}")
        if launch:
            domain = f"gui/{os.getuid()}"
            subprocess.run(["launchctl", "bootout", f"{domain}/{label}"], capture_output=True)
            for _ in range(3):
                if subprocess.run(["launchctl", "bootstrap", domain, str(plist)], capture_output=True).returncode == 0:
                    done.append(f"started: log at {LOGS / f'pact-runner-{agent}.log'}")
                    break
                subprocess.run(["sleep", "2"])
            else:
                done.append(f"could not start it: run `launchctl bootstrap {domain} {plist}`")
    else:
        done.append(f"start it with: set -a; . {env}; {runner_bin}")
    return done


def main() -> None:
    if sys.argv[1:2] == ["hooks"]:
        hooks_main(sys.argv[2:])
        return
    p = argparse.ArgumentParser(
        prog="pact-connect",
        description="Connect this machine as a newly hired PACT agent.",
        epilog="pact-connect hooks <agent-id> [--dir DIR] installs the Claude Code hooks for an agent already connected.",
    )
    p.add_argument("board", help="the board URL shown with the setup code")
    p.add_argument("code", help="the one-time setup code (pcs_...)")
    p.add_argument("--dir", default=".", help="Claude Code: the project directory to add the MCP server to")
    p.add_argument("--mcp-name", default="pact", help="Claude Code and Gemini: the MCP server name (default: pact)")
    p.add_argument(
        "--gemini-cli", action="store_true", help="Gemini: also configure Gemini CLI (API-key users), not only Antigravity CLI"
    )
    p.add_argument("--no-hooks", action="store_true", help="Claude Code: do not install the PACT hooks")
    p.add_argument("--repo", help="Runner: a local clone, or a git URL to clone for it")
    p.add_argument("--no-launch", action="store_true", help="Runner: write the LaunchAgent but do not start it")
    p.add_argument("--force", action="store_true", help="overwrite an existing env file for this agent")
    args = p.parse_args()

    info = redeem(args.board, args.code)
    env = CONFIG / f"{info['agent_id']}.env"
    if env.exists() and not args.force:
        raise SystemExit(f"pact-connect: {env} already exists. The code is used up; get a new one and run again with --force.")
    print(f"connected as {info['agent_id']} ({info['client']}, project {info['project']})")
    if info["client"] == "runner":
        steps = connect_runner(info, args.repo, not args.no_launch)
    elif info["client"] == "gemini":
        steps = connect_gemini(info, args.mcp_name, legacy=args.gemini_cli)
    else:
        steps = connect_code(info, Path(args.dir).resolve(), args.mcp_name, hooks=not args.no_hooks)
    for step in steps:
        print(f"  - {step}")


if __name__ == "__main__":
    main()
