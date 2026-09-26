# Argus distribution

One versioned Python installer prepares an isolated MCP runtime, registers the native
Claude/Codex plugins, configures Qoder/QoderCN, and installs the Chrome/Edge native messaging host. Git is not
needed. Run it in the environment where the client CLI runs (including WSL).
Python 3.10+ and a supported Agent client are prerequisites. This is currently a
beta installer, not a signed Windows EXE or a published PyPI/Web Store release.

## Install a released version

Download `install-argus-0.4.0.py` from the project's GitHub Release, then:

```sh
python3 install-argus-0.4.0.py
```

The installer auto-detects installed Claude, Codex, Qoder and QoderCN clients. Optional flags: `--client codex`,
`--client claude`, `--client qoder`, `--client qodercn`, `--client all`,
`--client both` (Claude + Codex), `--mobile`, `--browser edge`, or
`--browser none`. MCP browser control uses the extension, never Playwright.
`--install-browser` is an independent CLI-only Playwright option.

The release installer verifies the downloaded source ZIP against its embedded
SHA256 before extraction. For a locally downloaded archive:

```sh
python3 install-argus-0.4.0.py --archive argus-0.4.0.zip
```

Python dependencies are fetched from PyPI; this is not an offline bundle or a
fully locked dependency environment. WSL also downloads a checksum-pinned private
Windows Python for the browser host. It does not require Git or a user-managed
Python installation on Windows. Native Windows users run `py -3` instead of
`python3`. In WSL the extension files are copied onto the Windows filesystem.

Until the store listing is available, open `chrome://extensions`, enable Developer
mode, choose **Load unpacked**, and select the path printed by the installer.
The development extension ID is `oifpojkdkggpmdfbbochlclhjahkkmpc` (public key in
manifest); it is not an official Web Store ID. Open its popup and click **Connect
local bridge**. All website tabs in that profile become accessible to Argus.
Restart the Agent client. Ask it to list browser pages and connect a named session.
The bridge path is saved in `~/.argus/browser-bridge.json` (or `ARGUS_HOME_DIR`), so
new MCP extension connections no longer require a manually supplied path.

## Update and uninstall

Close active Argus tasks. Run the newer release installer using the same root,
reload the unpacked extension in Chrome, reconnect it, then start a new Agent
session. Old runtimes are preserved for rollback; existing sessions are retained.
The extension and host negotiate protocol version before any actions are sent.
Protocol mismatch instructs the user to update both sides; it is not reported as
successful connection. Versions 0.3.x lack this handshake and need both updated.

```sh
python3 install-argus-0.4.0.py --uninstall
```

Uninstall removes native client plugins and this installation's browser host
registration. It preserves runtimes, extension files and task records, including
screenshots. Remove the extension through Chrome/Edge. Marketplace definitions
remain reusable. Another installation's host registration is never deleted.
Use the same `--root` if a custom root was used for installation.

## Maintainer build and store upload

```sh
python3 scripts/build_release.py --out dist/release
```

Outputs: hash-pinned installer, source ZIP, development extension ZIP, store upload
ZIP (without development key), SHA256SUMS and release metadata. Source bundling
uses a narrow allowlist; no user session state, credentials or device captures are
included. `--prepare-only --client both --root /tmp/argus-package-check` builds
client bundles without registering clients/hosts. Running from the checkout is
also supported via `python3 scripts/install_agent_plugin.py`.

Tag `v0.4.0` triggers artifact generation and a **draft** GitHub Release. Manual
workflow runs only generate artifacts. Review it before publishing. For the first
Web Store upload, upload the `-store.zip`, obtain its ID, set `store_extension_id`
in `release.json`, and rebuild the release installer (or use
`--store-extension-id ID`). The store identity differs from the development one.
The installer then prints the store link and registers that exact origin. Do not
load the development ZIP when configured for the store ID. Test installation from
the actual store before calling it verified.

Complete the listing and privacy fields in `STORE_LISTING.md`; publish the privacy
policy on a public HTTPS page. Take actual screenshots from the final extension
build for the listing. Account registration, store review, screenshots and public
publication remain release-owner steps, not automated claims of completion.

## Verification boundary

Automated checks cover ZIP/hash integrity, native host IPC, protocol negotiation,
registration ownership and plugin configuration. The opt-in Chromium test covers
real extension/host operation in an isolated Linux browser profile. Production
Chrome/Edge store installation, macOS and fresh Windows machines need acceptance
runs; existing Windows/WSL driver testing does not prove this new distribution flow.

## Private configuration (.env)

The package never includes the developer's `.env`. External Agent device control
does not require an Argus model API key or database. Optional QA/model settings
load in this order: defaults → `~/.argus/config.env` → working-project `.env` →
process environment. `ARGUS_HOME_DIR` changes the user-config directory. Explicit
`ARGUS_CONFIG_FILE=/absolute/path/.env` replaces project-file discovery; a missing
explicit file is an error. Package installation paths are never searched for `.env`.

To keep an existing configuration without copying credentials:

```sh
python3 install-argus-0.4.0.py --config-file /absolute/path/to/private.env
```

The managed plugin stores only `ARGUS_CONFIG_FILE` and preserves it on upgrades;
it never embeds the file's contents. Both clients then resolve the same file even
when launched from different directories. For a portable user configuration, use
`argus init --user` to create a blank template at `~/.argus/config.env`, or `argus
init` for the working project's `.env`. Templates are created with owner-only
permissions on POSIX and never overwrite existing files. Keep secrets out of source
control. Database settings belonging to another application are not automatically
consumed by Argus.

## Authorize continuous operation in Codex

Codex controls MCP tool approvals. Selecting **Always allow** on one tool does not
authorize all other Argus tools. To authorize the managed Argus plugin once, put
this service-scoped setting in the user's `~/.codex/config.toml`:

```toml
[plugins."argus-device@argus-managed".mcp_servers.argus]
default_tools_approval_mode = "approve"
```

Merge into that table if it exists; do not create a duplicate TOML table. Start a
new Codex session after changing configuration. This applies to Argus tools,
including actions that change connected applications, and does not change shell
sandboxing or other MCP services. Existing per-tool overrides and managed policies
can still take precedence. Restore `"prompt"` to require confirmation again.
For a directly configured MCP server instead of the managed plugin, use the
corresponding `[mcp_servers.argus]` table. The installer does not silently grant
this trust for new users.

Reference: https://developers.openai.com/plugins/build/plugins


## Default Claude, Qoder and QoderCN setup

Selecting these clients automatically configures Argus-only continuous operation;
there is no separate trust flag or per-tool setup step. Installation updates
existing settings without replacing unrelated entries and saves a private
`settings.json.before-argus` backup. `--prepare-only` never changes client settings.

- Claude: after native plugin installation, add `mcp__plugin_argus-device_argus__*`
  and `mcp__argus__*` to `permissions.allow` in the user settings. These cover the
  plugin and direct Argus server naming conventions.
- Qoder: install the `argus` MCP entry with `trust: true` in
  `~/.qoder/settings.json` and copy the shared operation Skill to
  `~/.qoder/skills/argus-device/SKILL.md`.
- QoderCN: the same setup in `~/.qoder-cn`, selectable as `--client qodercn`.
  This is the QoderCN CLI configuration used by the tested local installation.

Qoder clients use the same isolated Python interpreter and device-only MCP profile
as Claude/Codex. An explicitly selected Qoder client can be configured before its
CLI is on PATH. Auto detection also recognizes existing user configuration and
private CLI installation directories. `CLAUDE_CONFIG_DIR` and `QODER_CONFIG_DIR`
are respected; the installer accepts `QODERCN_CONFIG_DIR` for a custom domestic
client directory (launch that client with its matching configuration directory).

Restart client sessions after installation. Managed deny/ask policies or higher
priority project settings may still require approval. Argus authorization does
not change shell approval or authorize other MCP servers. Uninstall removes only
permissions it added and unchanged Argus server/Skill entries that it owns;
user-customized entries are preserved. Native Qoder IDE configuration is not
claimed verified by the CLI integration.

References: [Claude permissions](https://code.claude.com/docs/en/permissions),
[Qoder MCP configuration](https://docs.qoder.com/cli/mcp-reference),
[Qoder Skills](https://docs.qoder.com/cli/Skills).
