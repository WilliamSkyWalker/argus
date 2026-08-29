# argus-device

Gives Claude Code **eyes and hands on a phone**: screenshot the screen → locate visually → tap / swipe / type / press keys, on Android devices and emulators and on iOS devices and simulators.

This is the device layer of [argus](https://github.com/WilliamSkyWalker/argus) (a vision-driven AI QA agent), split out so any Claude Code session can use it — **no LLM API key, no test files required**. You are the decision maker.

## Install
```
/plugin marketplace add WilliamSkyWalker/argus
/plugin install argus-device@argus-plugins
```

Then set up argus itself plus the system dependencies (a plugin cannot install these):
```bash
git clone https://github.com/WilliamSkyWalker/argus.git
export ARGUS_HOME=/absolute/path/to/argus          # put this in your shell profile
pip3 install -r "$ARGUS_HOME/requirements.txt"
python3 -m argus.cli mcp init                       # sandboxed Appium + drivers + adb
# Android-only setup can use: python3 -m argus.cli mcp init --skip-ios
```
Then run **`/argus-device:doctor`** in Claude Code — it checks every link in the chain and prints the exact fix for whatever is missing.

## Use
Just ask in plain language — the skill walks Claude through the screenshot→decide loop:
- "Open Settings and turn dark mode off"
- "Reproduce the blank screen on the XXX page on this device"
- "Fill in this form and submit it, showing me a screenshot after each step"

The device must be **unlocked with the screen on** (a locked screen can't be captured). With several devices, say which one to use (`list_devices` gives you the serials).

## What's inside
- **skill `device`** — the vision-driven loop plus the anti-patterns that actually bite (scale calibration, no blind retries, no chained taps, IME focus, no UI tree on Flutter)
- **command `/argus-device:doctor`** — dependency check (Python packages / Node + Appium + drivers / adb + devices / simulators)
- **MCP server `argus`** — `device_screenshot` `device_tap` `device_swipe` `device_input` `device_type_send` `device_key` `device_launch` `list_devices` `install_apk` `adb_reconnect` `setup_simulator`

A single long-lived Appium session is reused across processes, so turn-by-turn driving does not rebuild the connection each time. Everything runs on Appium primitives: **no adb for driving** (cloud device farms don't expose adb) and **no UI tree** (Flutter and custom-drawn UIs have nothing in it).

## What this plugin is not
It does not run test suites. Feeding `.feature`/`.md` cases to Argus's own agent loop — batch regression, multi-device parallelism, HTML reports — needs an LLM API key and a `tests/` directory, so it lives in the [Argus repo](https://github.com/WilliamSkyWalker/argus) itself (`argus run`), not here. This plugin is deliberately the zero-config half: **you** are the brain.
