---
name: device
description: "Drive Android devices/emulators and iOS devices/simulators purely by sight — screenshot the screen, locate elements visually, then tap/swipe/type/press keys, install APKs, launch apps. Use for any request to operate, verify or reproduce something on a phone: manual walkthroughs, bug reproduction, UI review, filling forms, repetitive tapping. Runs on Appium primitives; never shells out to adb for driving and never reads the UI tree."
---

# device — drive a phone by sight

**You are the brain: look at the screenshot → decide → issue one action → look again.** The eyes and hands come from this plugin's `device_*` MCP tools (the same capability is also reachable as `python3 -m argus.cli device …` in Bash, for non-MCP agents).

## Before you start (confirm, don't guess mid-run)
- On first use or on any failure, run `/argus-device:doctor` — it settles the whole dependency chain in one shot.
- **The device must be unlocked with the screen on.** A sleeping or locked screen cannot be captured (FLAG_SECURE) and comes back as an all-black screenshot. An all-black frame is a device-state problem, not a rendering one: send `device_key wakeup` and capture again. If it stays black, or a PIN/pattern lock screen appears, stop and ask the user to unlock it — do not attempt to unlock it yourself.
- With more than one device, pass `serial` on every call. No `serial` = default device.

## Main loop (one action at a time — never fire two taps back to back)
1. `device_screenshot` → returns `{path, screen_size, scale}`
2. **Read that image** and locate the target element visually
3. Convert coordinates using `scale`: if `scale == 1`, use the pixel coordinates as seen; if `≠ 1`, the screenshot was resized, so multiply by `1/scale`
4. `device_tap` / `device_swipe` / `device_input` / `device_key`
5. Back to step 1 — see the result before deciding the next move

**The number one cause of missed taps is a wrong `scale`/resolution calibration, not your visual aim.** If taps keep landing nowhere, re-check the `scale` and `screen_size` from `screenshot` instead of nudging coordinates over and over.

## Tools
| tool | purpose | notes |
|---|---|---|
| `device_screenshot` | capture to disk | returns `path` + `screen_size` + **`scale`**; re-capture after every action |
| `device_tap` | click | **device pixel coordinates** (not percentages, not raw screenshot pixels if scaled) |
| `device_swipe` | swipe/scroll | raise the duration for inertial scrolling |
| `device_input` | type text | goes through the IME; handles CJK and Flutter-drawn fields; **tap to focus first**; does not submit |
| `device_type_send` | fill + submit + wait + screenshot | one round trip instead of four; ideal for "type it and show me the result" |
| `device_key` | key event | `enter` `delete` `tab` `space` `escape` `back` `home` `recent` `wakeup` `power` `sleep` `menu`, or a raw Android keycode as digits. An unsupported name returns `ok: false` instead of pretending it worked. ⚠️ in some apps (especially Flutter) `back` exits the app — prefer the on-screen close control for dismissing overlays, and note some search fields need two `back`s (clear query, then leave) |
| `device_launch` | launch/relaunch an app | `force_stop=true` kills first; uses Appium activate, no adb |
| `list_devices` | list available devices | to get serials |
| `install_apk` | install a build | parallel across devices |
| `setup_simulator` | create/boot an iOS simulator | macOS only |

## Anti-patterns (all learned the hard way)
- ❌ **Blind retry on the same coordinates.** After two taps with no visible change, change strategy — verify `scale`, scroll the element into view, or back out of an unexpected state. Never a third identical tap.
- ❌ **Chained taps.** `tap A` then `tap B` without looking: a permission dialog or onboarding overlay often lands in between, so B hits something else entirely.
- ❌ **Ignoring incidental dialogs.** Notification permission prompts, rating nags and dark-mode intros all cover the target — dismiss them first.
- ❌ **Dumping the UI tree.** Flutter, games and custom-drawn UIs are a single canvas with nothing useful in the tree. This flow is built for pure vision — just read the image.
- ❌ **Typing without focus.** `device_input` into an unfocused screen does nothing. If the previous step left the keyboard up, dismiss it (enter, or tap neutral space) before the next tap — otherwise that tap lands on the IME.

## Reporting back
After each user-visible step, say in one line: what you saw → what you did → what is on screen now. Any claim that something succeeded must be based on what you **actually see** in a screenshot. If you cannot see it, say so — never infer the outcome with "it should have…".
