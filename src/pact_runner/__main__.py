"""``pact-runner``: one process per runner agent. Exit 0 means the board told it to stop (kill switch,
paused agent, dead mandate) and a supervisor should leave it stopped; anything else is a crash."""

import asyncio
import logging
import sys

from .board import McpBoard
from .config import RunnerConfig
from .runner import Runner


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # Every poll is a few HTTP requests; at INFO they would bury the runner's own lines.
    for noisy in ("httpx", "httpx2", "mcp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    cfg = RunnerConfig.from_env()
    reason = asyncio.run(Runner(cfg, McpBoard(cfg.mcp_url, cfg.token)).run_forever())
    logging.getLogger("pact_runner").warning("stopped: %s", reason)
    sys.exit(0)


if __name__ == "__main__":
    main()
