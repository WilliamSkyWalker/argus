# Saygo 0.4.6 — Check updates at every MCP startup

- Check for updates in the background whenever an agent client starts the managed Saygo MCP server, regardless of the previous check time.
- Bypass the 24-hour cache for startup checks. Device tool calls continue to read the saved result without making update requests.
- Keep device connections nonblocking and preserve opt-in automatic installation and runtime activation safeguards.

## Install or upgrade

```sh
pipx upgrade saygo-agent-control
saygo setup --client codex
```

Alternatively, use the attached versioned installer to update a managed installation. Restart the agent client after setup to load the new launcher. The first device call can still precede completion of the background check.

This release contains CLI and Agent integration packages. Desktop installers and Chrome Web Store publication are not included.
