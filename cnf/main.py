"""cnf — Cluster Nova Federation agent entry point."""
from __future__ import annotations

import asyncio
import sys


def main() -> None:
    from cnf.agent.agent import CNFAgent
    agent = CNFAgent()
    try:
        asyncio.run(agent.run_forever())
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
