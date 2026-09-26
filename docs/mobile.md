# Phones and mobile simulators

`argus mobile` discovers local devices, connects them through the existing Appium
visual driver, and provisions Android/iOS simulators. It does not reset the phone
or launch an app on connection. Screenshots, touch and typing remain Appium
operations; adb/simctl are used here for discovery and simulator lifecycle only.

## Discover and connect

```bash
python3 -m argus.cli mobile devices
python3 -m argus.cli mobile devices --platform android
python3 -m argus.cli mobile devices --platform ios

python3 -m argus.cli mobile connect --platform android --device DEVICE_ID --session phone
python3 -m argus.cli mobile connect --platform ios --device DEVICE_UDID --session iphone
python3 -m argus.cli device screenshot --serial phone --out phone.png
```

Discovery returns JSON containing `devices`, installed `android_avds`, and
`diagnostics`. A missing platform tool is reported separately; it does not hide
results from another platform. Android unauthorized/offline devices are listed
but not automatically selected. iOS lists available simulators and paired
physical iPhones/iPads separately (physical discovery requires Xcode's devicectl).

Omit `--device` only when exactly one device is online. Shut-down simulators must
be booted first. Multiple candidates produce an explicit selection error.
`--session` is a persistent Argus alias; it is distinct from the physical UDID.
An alias already bound to another device/server is rejected. The existing
`device start` entry point continues to work.

Android phones need USB debugging and the phone's trust authorization. iOS
phones need trust, Developer Mode, and a working Xcode signing configuration;
set `IOS_TEAM_ID`/`IOS_WDA_BUNDLE_ID` or pass `--team-id` when appropriate. Unlock
the device before screenshotting. These on-device permissions are not bypassed.
For an existing phone, install the automation tools first if necessary:

```bash
python3 -m argus.cli mcp init --skip-ios   # Android
python3 -m argus.cli mcp init              # iOS on macOS
```

The emulator installer below also installs Appium and its required drivers.
An Android SDK must be available to the local Appium server (`ANDROID_HOME` or
one of the detected SDK directories). The isolated platform-tools-only bootstrap
is not a complete Android SDK installation.

## One-command Android setup

```bash
python3 -m argus.cli mobile install --platform android --name Argus --boot --connect
```

This prepares Java (reusing Java 17+ or downloading a private Temurin JDK 21),
Android command-line tools, platform-tools, SDK platform/build-tools, emulator, an API 35 Google APIs system
image, an AVD, and isolated Node/Appium/UiAutomator2. It then boots the AVD, waits
for Android boot completion and opens an Appium session. Download size is several
GB; SDK/package failures return an error instead of reporting success.

SDK licenses are presented interactively. For unattended use after reviewing the
[Android SDK terms](https://developer.android.com/studio#downloads), explicitly
pass `--accept-licenses`. Use `--api 36` to request another published API image (installer: API 30+).
Use `--headless` to omit the simulator window. Omit `--boot --connect` to install
without starting a device; `--connect` alone implies boot.

The installer reuses `ANDROID_HOME`/`ANDROID_SDK_ROOT` or a detected SDK, otherwise
uses `~/.argus/runtime/android-sdk`. Java, Node and Appium are private to Argus;
system Node/npm are not replaced. AVDs use Android's configured/default AVD
location. Reinstalling the same name/image preserves it. An existing name with a
different image is rejected; use another `--name`. No AVD is force-overwritten.

Supported installer hosts: x64 Linux/Windows, Intel/Apple silicon macOS. Image ABI
follows the host architecture. Virtualization must already be enabled (KVM,
Windows Hypervisor Platform, or Apple's hypervisor). The boot command checks
acceleration and reports failure; it does not change BIOS settings or reboot the
host. See [Android acceleration requirements](https://developer.android.com/studio/run/emulator-acceleration).

## One-command iOS simulator setup

On a Mac with full Xcode installed and selected:

```bash
python3 -m argus.cli mobile install --platform ios --name Argus --boot --connect
```

This runs Xcode first-launch setup, downloads an iOS runtime with
`xcodebuild -downloadPlatform iOS`, creates/reuses an iPhone simulator on the latest
available iOS runtime, installs Appium/XCUITest, boots the simulator and connects.
It picks a device type advertised as supported by the runtime. Simulator use does
not require physical-device WDA signing.

Full Xcode installation, Apple account access, accepting Xcode's license and any
administrator-required first-launch components may need user action. Errors from
Xcode are surfaced. This command does not claim to install Xcode itself.
**iOS simulators cannot be installed or run locally on Windows/Linux/WSL.**
See [Apple's component installation guide](https://developer.apple.com/documentation/xcode/downloading-and-installing-additional-xcode-components).

## Boot an existing simulator

```bash
python3 -m argus.cli mobile boot --platform android Argus
python3 -m argus.cli mobile boot --platform ios SIMULATOR_UDID
```

Android reuses the exact running AVD by name; iOS reuses an already booted
simulator. Successful output means boot readiness was observed. A timeout leaves
the emulator available for inspection, with Android startup logs in
`~/.argus/runtime/emulator-NAME.log`. It does not silently choose another device.

## Windows, WSL and remote hosts

For Windows USB devices or the Windows Android Emulator, run discovery/install
and Appium natively on Windows. WSL discovery sees only devices exposed to its
own adb server (for example USB devices explicitly forwarded to WSL). Installing
inside WSL does not install an emulator on the Windows host. WSL acceleration
may be unavailable; a native Windows emulator avoids that dependency.

Argus in WSL/Linux can connect to devices managed by a Mac or Windows Appium host:

```bash
python3 -m argus.cli mobile connect --platform ios --device DEVICE_UDID \
  --server-url http://mac-host.example:4723 --session iphone
python3 -m argus.cli mobile connect --platform android --device DEVICE_ID \
  --server-url http://windows-host.example:4723 --session phone
```

Start Appium on that host first and use a reachable address. Remote connections
require an explicit ID; local discovery is not used to guess a remote device.
The remote server manages USB access, simulator state and signing. This command
does not install tools on a remote host or expose an Appium server to the network.

Runtime resources reference the same aliases:

```json
{"phone": {"kind": "android", "session": "phone"},
 "iphone": {"kind": "ios", "session": "iphone"}}
```

## Verification

```bash
python3 -m unittest discover -s tests/mobile_demo -v
```

Tests use fake command responses and Appium adapters to verify discovery,
selection, identity/signing propagation, installation sequencing, reuse and
failure handling. Local WSL discovery was exercised, but no device was visible.
Full emulator downloads/boots, Windows SDK execution and Mac/iPhone operations
have not been verified on real hardware in this development session.
