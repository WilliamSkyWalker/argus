# Saygo 0.4.4 — Codex installation approval fix

- Codex setup now configures Saygo-scoped MCP tool approval, matching the existing continuous-operation setup for the other supported clients.
- Existing user approval policies and per-tool overrides are preserved. Repeated setup is idempotent; uninstall removes only the unchanged approval entry added by the installer.
- Respect `CODEX_HOME`, back up existing configuration, and support Python 3.10 hosts through the prepared runtime when a local TOML parser is unavailable.

## Install or upgrade from PyPI

```sh
python -m pip install --upgrade saygo-agent-control
saygo setup --client codex
```

For pipx installations:

```sh
pipx upgrade saygo-agent-control
saygo setup --client codex
```

Restart Codex after setup to load the updated approval policy. This approval covers Saygo MCP tools, including actions in connected applications; shell approval and other MCP servers are unchanged.

This release contains CLI and Agent integration packages. Desktop installers and Chrome Web Store publication are not included.
