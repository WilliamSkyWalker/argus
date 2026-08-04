---
description: Check the argus device-driving dependency chain (Python packages / Node + Appium server + drivers / adb + devices / simulators) and print the exact fix for anything missing
argument-hint: "[--profile device|full] [--json]"
allowed-tools: Bash(python3:*)
---

Run the dependency check:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/doctor.py" $ARGUMENTS
```

Then report it back like this — don't just paste the raw output:

1. **Blocking items (✗)**: list each one as "what's missing → the exact command to run". Pass the commands through verbatim; do not rewrite them for a different package manager.
2. **Advisory items (!)**: say which platform or optional capability each one affects (e.g. a missing `appium driver:xcuitest` means iOS can't run, Android is unaffected). Do not present these as mandatory.
3. **All green**: confirm in one line and tell the user you can drive the device now (screenshots, taps and typing all go through the `argus-device` MCP tools).
4. If `argus package` says it was found via the current directory, point out that `ARGUS_HOME` should be set — otherwise the plugin stops working in any other project directory.

⚠️ Never install or switch Python/Node versions yourself and never repoint a package manager. Hand the commands to the user and let them choose the environment to run them in.
