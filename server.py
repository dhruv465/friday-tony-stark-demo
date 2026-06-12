"""
Friday MCP Server — Entry Point
Run with: python server.py
"""

import logging
from contextlib import asynccontextmanager

from mcp.server.fastmcp import FastMCP
from friday.tools import register_all_tools
from friday.prompts import register_all_prompts
from friday.resources import register_all_resources
from friday.config import config


@asynccontextmanager
async def _lifespan(app):
    # Pick learning jobs left running by a previous server process back up.
    try:
        from friday.learning.engine import LearningRuntime

        resumed = LearningRuntime.instance().resume_in_progress()
        if resumed:
            logging.getLogger("friday").info("Resumed learning jobs: %s", resumed)
    except Exception as exc:
        logging.getLogger("friday").warning("Learning resume skipped: %s", exc)
    # Build/refresh the memory FTS index so first searches are warm.
    try:
        from friday.memory.index import reindex

        stats = reindex()
        logging.getLogger("friday").info("Memory index ready: %s", stats)
    except Exception as exc:
        logging.getLogger("friday").warning("Memory reindex skipped: %s", exc)
    # Start the subagent runtime so scheduled jobs and event alerts tick
    # even before any agent is deployed.
    try:
        from friday.agents.runtime import SubagentRuntime

        SubagentRuntime.instance().ensure_running()
    except Exception as exc:
        logging.getLogger("friday").warning("Runtime start skipped: %s", exc)
    yield {}


# Create the MCP server instance
mcp = FastMCP(
    name=config.SERVER_NAME,
    instructions=(
        "You are Friday, a Tony Stark-style AI assistant. "
        "You have access to a set of tools to help the user. "
        "Be concise, accurate, and a little witty."
    ),
    lifespan=_lifespan,
)

# Register tools, prompts, and resources
register_all_tools(mcp)
register_all_prompts(mcp)
register_all_resources(mcp)

def main():
    mcp.run(transport='sse')

if __name__ == "__main__":
    main()