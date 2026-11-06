"""Local development shim for the northwind-ops tool server.

The real server is not part of this repository. This shim lets MCP clients
start the configured command without error; it exposes no tools, reads no
files, runs no commands, and exits immediately.
"""

from __future__ import annotations

import sys


def main() -> int:
    sys.stderr.write("northwind-ops dev shim: no tools are implemented locally; exiting.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
