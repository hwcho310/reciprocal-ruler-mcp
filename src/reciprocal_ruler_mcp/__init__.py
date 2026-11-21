"""Reciprocal Ruler MCP server package (stdio transport, standard library only)."""

from .server import SERVER_NAME, SERVER_VERSION, main

__all__ = ["SERVER_NAME", "SERVER_VERSION", "main"]
__version__ = SERVER_VERSION
