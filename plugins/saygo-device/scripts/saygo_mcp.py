#!/usr/bin/env python3
"""Launcher for the saygo MCP server used by the saygo-device plugin.

Why this shim exists: on install, Claude Code copies only the *plugin directory* into
``~/.claude/plugins/cache``. The saygo Python package is not in there, and a plugin
cannot reference files outside its own directory (``../..`` won't be copied). So before
starting the MCP server we have to locate saygo and put it on ``sys.path``:

  1. ``SAYGO_HOME=/path/to/saygo`` — repo cloned but not pip-installed (saygo's normal usage)
  2. the current working directory (or a parent) being an saygo checkout

If none of those hold, print a copy-pasteable install hint to stderr and exit; the MCP
client will show the server as failed and surface this text in its logs.

stdout belongs to JSON-RPC, so every diagnostic here goes to stderr.
"""

from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path

REPO_URL = "https://github.com/WilliamSkyWalker/saygo"

_HINT = f"""[saygo-mcp] Cannot import the saygo package — the MCP server cannot start:

       pip install "/path/to/saygo[browser,mcp]"
       # A built saygo_agent_control wheel can also be installed with these extras.

For phones add the mobile extra; SAYGO_HOME is optional when using a checkout.

Afterwards run /saygo-device:doctor in Claude Code for a full check (Appium server, drivers and
connected devices included).
"""


def _looks_like_saygo_root(p: Path) -> bool:
    """Is `p` an saygo checkout (i.e. does it contain the saygo/ package)?"""
    return (p / "saygo" / "__init__.py").is_file()


def _resolve_saygo_root() -> Path | None:
    """Locate the saygo checkout: SAYGO_HOME first, then the cwd chain."""
    raw = (os.environ.get("SAYGO_HOME") or "").strip()
    if raw:
        home = Path(raw).expanduser()
        # Tolerate SAYGO_HOME pointing at the inner package dir (.../saygo/saygo)
        for cand in (home, *home.parents):
            if _looks_like_saygo_root(cand):
                return cand
        print(f"[saygo-mcp] SAYGO_HOME={raw!r} has no saygo/ package; ignoring it.",
              file=sys.stderr)

    cwd = Path.cwd()
    for cand in (cwd, *cwd.parents):
        if _looks_like_saygo_root(cand):
            return cand
    return None


def main() -> int:
    root = _resolve_saygo_root()
    if root is not None:
        sys.path.insert(0, str(root))
        # Some saygo paths resolve against the cwd (test target discovery, reports),
        # so for the runner profile we need to sit in the repo root.
        if os.environ.get("SAYGO_MCP_PROFILE", "device") != "device":
            try:
                os.chdir(root)
            except OSError as e:
                print(f"[saygo-mcp] chdir({root}) failed: {e}", file=sys.stderr)

    try:
        import saygo  # noqa: F401
    except ImportError:
        print(_HINT, file=sys.stderr)
        return 1

    try:
        import mcp  # noqa: F401
    except ImportError:
        print("[saygo-mcp] Missing the MCP SDK: pip3 install mcp", file=sys.stderr)
        return 1

    # The profile (device / full) comes from plugin.json's env; server.py reads it.
    sys.argv = [sys.argv[0]]
    runpy.run_module("saygo.mcp.server", run_name="__main__")
    return 0


if __name__ == "__main__":
    sys.exit(main())
