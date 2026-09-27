---
description: Diagnose a connected Saygo session or platform-specific dependencies without requiring a mobile toolchain for browser/desktop work
argument-hint: "[--session NAME] [--platform browser|android|ios|desktop] [--profile device|full] [--json]"
allowed-tools: Bash(python3:*)
---

Run the check with the user's selected session/platform:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/doctor.py" $ARGUMENTS
```

Explain blocking failures and their relevant installation commands. Distinguish optional platform dependencies from requirements. With `--session`, report whether connection, capture and local image reading passed; input permissions remain untested unless the user explicitly runs `saygo doctor --probe-action` against a harmless target. Do not claim a business task succeeded from diagnostics alone.

An installed `saygo-agent-control` package works from the user's project without `SAYGO_HOME`; a checkout can use `SAYGO_HOME` or be installed with the appropriate extras. Only suggest the mobile toolchain when the selected platform is Android or iOS. Environment modifications should follow the user's task and existing authorization.
