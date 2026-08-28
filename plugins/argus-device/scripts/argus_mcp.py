#!/usr/bin/env python3
"""Launcher for the argus MCP server used by the argus-device plugin.

Why this shim exists: on install, Claude Code copies only the *plugin directory* into
``~/.claude/plugins/cache``. The argus Python package is not in there, and a plugin
cannot reference files outside its own directory (``../..`` won't be copied). So before
starting the MCP server we have to locate argus and put it on ``sys.path``:

  1. ``ARGUS_HOME=/path/to/argus`` — repo cloned but not pip-installed (argus's normal usage)
  2. the current working directory (or a parent) being an argus checkout
  3. an installed ``argus`` package

If none of those hold, print a copy-pasteable install hint to stderr and exit; the MCP
client will show the server as failed and surface this text in its logs.

stdout belongs to JSON-RPC, so every diagnostic here goes to stderr.
"""

from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path

REPO_URL = "https://github.com/WilliamSkyWalker/argus"

_HINT = f"""[argus-mcp] Cannot find the argus package — the MCP server cannot start. Pick one:

  A. Clone the repo and point at it (recommended; argus itself needs no pip install):
       git clone {REPO_URL}.git
       # then, in your shell profile or the plugin's env:
       export ARGUS_HOME=/absolute/path/to/argus
       pip3 install openai Pillow uiautomator2 selenium Appium-Python-Client mcp

  B. Install it as a package:
       pip3 install "argus[mobile,mcp] @ git+{REPO_URL}.git"

Afterwards run /argus-device:doctor in Claude Code for a full check (Appium server, drivers and
connected devices included).
"""


def _looks_like_argus_root(p: Path) -> bool:
    """Is `p` an argus checkout (i.e. does it contain the argus/ package)?"""
    return (p / "argus" / "__init__.py").is_file()


def _resolve_argus_root() -> Path | None:
    """Locate the argus checkout: ARGUS_HOME first, then the cwd chain."""
    raw = (os.environ.get("ARGUS_HOME") or "").strip()
    if raw:
        home = Path(raw).expanduser()
        # Tolerate ARGUS_HOME pointing at the inner package dir (.../argus/argus)
        for cand in (home, *home.parents):
            if _looks_like_argus_root(cand):
                return cand
        print(f"[argus-mcp] ARGUS_HOME={raw!r} has no argus/ package; ignoring it.",
              file=sys.stderr)

    cwd = Path.cwd()
    for cand in (cwd, *cwd.parents):
        if _looks_like_argus_root(cand):
            return cand
    return None


def main() -> int:
    root = _resolve_argus_root()
    if root is not None:
        sys.path.insert(0, str(root))
        # Some argus paths resolve against the cwd (test target discovery, reports),
        # so for the runner profile we need to sit in the repo root.
        try:
            os.chdir(root)
        except OSError as e:
            print(f"[argus-mcp] chdir({root}) failed: {e}", file=sys.stderr)

    try:
        import argus  # noqa: F401
    except ImportError:
        print(_HINT, file=sys.stderr)
        return 1

    try:
        import mcp  # noqa: F401
    except ImportError:
        print("[argus-mcp] Missing the MCP SDK: pip3 install mcp", file=sys.stderr)
        return 1

    # The profile (device / full) comes from plugin.json's env; server.py reads it.
    sys.argv = [sys.argv[0]]
    runpy.run_module("argus.mcp.server", run_name="__main__")
    return 0


if __name__ == "__main__":
    sys.exit(main())
