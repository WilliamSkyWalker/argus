Argus 0.4.0 beta distribution

- Versioned installer with source checksum verification; no Git required.
- Managed Claude/Codex MCP plugins and browser native-host setup, including WSL → Windows.
- Default Claude Argus permissions; Qoder/QoderCN MCP, shared Skill and scoped service trust.
- Stable development extension ID and a separate Web Store upload archive.
- Protocol negotiation, default bridge discovery and registration-aware uninstall.

Requires Python 3.10+ and an installed Claude/Codex CLI. Browser extension loading
still requires user confirmation. This draft does not imply Chrome Web Store
approval. See distribution/README.md for installation and verification limits.

Desktop development builds: native Windows EXE/ZIP, macOS app/DMG and Linux tar.gz packaging. DMG creation runs on macOS and verifies the image with hdiutil. Desktop builds bundle Python and Qt; pyenv and WSL are not runtime requirements.
