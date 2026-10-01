# Python package release

Package: `saygo-agent-control`. CLI: `saygo`. Version: `0.4.5` (beta software).
Version 0.4.4 is published on PyPI. Its wheel passed installation and plugin preparation
checks on Linux, Windows and macOS before upload.

## Build and verify

```sh
python -m pip install build twine
python -m build --outdir dist/python
python -m twine check --strict dist/python/*
python scripts/check_python_package.py dist/python/saygo_agent_control-0.4.5-py3-none-any.whl
```

The default build produces an sdist and builds the wheel from that sdist, checking
that source releases contain all required build inputs. The wheel includes a
small allowlisted source ZIP for `saygo setup`. It contains the installer, plugin
manifests, Skill and extension files, with no local configuration or session data.
The setup command invokes the existing managed installer against that exact source
version. Python dependencies are downloaded as needed; this is not an offline bundle.

The smoke test installs into a disposable environment outside the checkout,
prepares both client bundles without registering them, imports MCP from the managed
runtime, and repeats setup to verify runtime reuse. It does not operate devices or
prove live client ingestion. CI runs the same check on Linux, Windows and macOS.

## Publisher configuration

The production PyPI Trusted Publisher is configured. Use these settings when configuring or restoring a publisher (TestPyPI is separate):

| Field | Value |
| --- | --- |
| Project | `saygo-agent-control` |
| Owner | `WilliamSkyWalker` |
| Repository | `saygo` |
| Workflow | `python-package.yml` |
| Environment | `pypi` (or `testpypi`) |

Create the matching GitHub environments. For production, configure a required
reviewer in the `pypi` environment. Trusted Publishing uses GitHub OIDC rather than
committed credentials or a long-lived API token.

Run the **Python package** workflow with `publish: none` first. Select `testpypi`
to upload to the test index. For production, select the matching `v0.4.5` tag and
`publish: pypi`. Build and all three smoke jobs must pass before upload. Only a
manual dispatch can publish; pull requests only build and test. The public PyPI
project endpoint may return 404 before first publication; this does not guarantee
that PyPI will permit the chosen name.

After publication, verify from outside the checkout:

```sh
pipx install 'saygo-agent-control[mcp]'
saygo setup --help
saygo setup --client codex
```

The website uses `installMode: "pypi"`. The pipx application and managed plugin runtime are separate:
`pipx upgrade saygo-agent-control` followed by `saygo setup` updates both. The
optional managed runtime auto-updater still uses GitHub releases.

References: [PyPA publishing guide](https://packaging.python.org/en/latest/guides/publishing-package-distribution-releases-using-github-actions-ci-cd-workflows/).
