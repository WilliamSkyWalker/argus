# Phones and mobile simulators

`saygo device` discovers local devices, connects them through the existing Appium
visual driver, and provisions Android/iOS simulators. It does not reset the phone
or launch an app on connection. Screenshots, touch and typing remain Appium
operations; adb/simctl are used here for discovery and simulator lifecycle only.

## Discover and connect

```bash
python3 -m saygo.cli device list
python3 -m saygo.cli device list --platform android
python3 -m saygo.cli device list --platform ios

python3 -m saygo.cli device connect --platform android --device DEVICE_ID --session phone
python3 -m saygo.cli device connect --platform ios --device DEVICE_UDID --session iphone
python3 -m saygo.cli device screenshot --serial phone --out phone.png
```

Discovery returns JSON containing `devices`, installed `android_avds`, and
`diagnostics`. A missing platform tool is reported separately; it does not hide
results from another platform. Android unauthorized/offline devices are listed
but not automatically selected. iOS lists available simulators and paired
physical iPhones/iPads separately (physical discovery requires Xcode's devicectl).

Omit `--device` only when exactly one device is online. Shut-down simulators must
be booted first. Multiple candidates produce an explicit selection error.
`--session` is a persistent Saygo alias; it is distinct from the physical UDID.
An alias already bound to another device/server is rejected. The existing
`device start` entry point continues to work.

Android phones need USB debugging and the phone's trust authorization. iOS
phones need trust, Developer Mode, and a working Xcode signing configuration;
set `IOS_TEAM_ID`/`IOS_WDA_BUNDLE_ID` or pass `--team-id` when appropriate. Unlock
the device before screenshotting. These on-device permissions are not bypassed.
For an existing phone, install the automation tools first if necessary:

```bash
python3 -m saygo.cli mcp init --skip-ios   # Android
python3 -m saygo.cli mcp init              # iOS on macOS
```

The emulator installer below also installs Appium and its required drivers.
An Android SDK must be available to the local Appium server (`ANDROID_HOME` or
one of the detected SDK directories). The isolated platform-tools-only bootstrap
is not a complete Android SDK installation.

## One-command Android setup

```bash
python3 -m saygo.cli device install --platform android --host auto --dry-run
python3 -m saygo.cli device install --platform android --host auto --name Saygo --boot --connect --session phone
```

This prepares Java (reusing Java 17+ or downloading a private Temurin JDK 21),
Android command-line tools, platform-tools, SDK platform/build-tools, emulator, an API 35 Google APIs system
image, an AVD, and isolated Node/Appium/UiAutomator2. It then boots the AVD, waits
for Android boot completion, opens an Appium session, and saves a verification
screenshot under the caller's Saygo runtime directory. Download size is several
GB; SDK/package failures return an error instead of reporting success.

The plan shows the host, paths, components, available disk space and acceleration
blockers before installation. `--dry-run` only inspects; it does not download or
install anything. Provisioning requires at least 12 GiB free space. Installed
components and matching AVDs are reused; failed downloads can be retried by
rerunning the command. Partial SDK downloads are managed by Google's SDK manager.
Download counters and periodic SDK installation messages go to stderr; stdout
contains the final JSON result. Archives downloaded directly by Saygo are
SHA-256 checked (Node uses its official checksum manifest).

SDK license acceptance is requested interactively before downloads. For unattended
use after reviewing the [Android SDK terms](https://developer.android.com/studio/terms), explicitly
pass `--accept-licenses`. Use `--api 36` to request another published API image (installer: API 30+).
Use `--headless` to omit the simulator window. Omit `--boot --connect` to install
without starting a device; `--connect` alone implies boot.

The installer reuses `ANDROID_HOME`/`ANDROID_SDK_ROOT` or a detected SDK, otherwise
uses `~/.saygo/runtime/android-sdk`. Java, Node and Appium are private to Saygo;
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
python3 -m saygo.cli device install --platform ios --name Saygo --boot --connect
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
python3 -m saygo.cli device boot --platform android Saygo
python3 -m saygo.cli device boot --platform android --host windows Saygo --connect --session phone
python3 -m saygo.cli device boot --platform ios SIMULATOR_UDID
```

Android reuses the exact running AVD by name; iOS reuses an already booted
simulator. Successful output means boot readiness was observed. A timeout leaves
the emulator available for inspection, with Android startup logs in
`~/.saygo/runtime/emulator-NAME.log`. It does not silently choose another device.

## Windows, WSL and remote hosts

`install/boot --host auto` selects Windows when running in WSL without usable
`/dev/kvm`; otherwise it uses the current OS. `--host windows` explicitly selects
the Windows host from WSL, and `--host local` disables that fallback. This is local
WSL interop, not an installer for arbitrary remote machines. Windows host support
currently requires x64 Windows, WSL interop and PowerShell.

The Windows worker installs a private embedded Python and automation tools under
`%LOCALAPPDATA%\Saygo\mobile`. It reuses a Windows SDK from `ANDROID_HOME`,
`ANDROID_SDK_ROOT` or the standard Android SDK location, if available. Otherwise
the SDK is installed under that private directory. It never modifies the global
PATH. Only the stdlib worker source files are copied; project configs and secrets
are not copied to Windows.

Windows Hypervisor Platform must be enabled and the hypervisor running. The
preflight reports missing acceleration before downloading; enabling Windows
features, BIOS virtualization or rebooting remains a user action.

With `--connect`, Windows Appium listens only on Windows loopback. A tokenized
WSL loopback relay forwards requests through Windows Python stdio; no firewall
rule or mirrored-networking setting is needed. Failed requests are not replayed.
The worker uses a persistent private Windows adb port and supplies it to Appium,
avoiding WSL's forwarding of the default adb port without stopping other adb servers.
Background Windows tools use hidden consoles. Saygo's private Appium process also
preloads a small Node helper to hide adb/logcat child consoles; the emulator GUI
remains visible. Emulator metrics and crash-report prompts are disabled for this
automated launch; startup errors remain in the emulator log.
Use the resulting `--session` with screenshot/tap/input/open commands as usual.
Appium and the relay persist for later CLI commands. `device disconnect` ends the
Appium session but leaves the emulator and host services running. After a reboot,
run `device boot ... --connect` to establish a new session; use a fresh session
alias if the old one belongs to a previous server endpoint.

`device list` still discovers devices through the caller's local adb server;
Windows-hosted sessions appear under `device sessions`. Windows USB discovery
from WSL requires a separately configured adb/USB connection.

Saygo in WSL/Linux can connect to devices managed by a Mac or Windows Appium host:

```bash
python3 -m saygo.cli device connect --platform ios --device DEVICE_UDID \
  --server-url http://mac-host.example:4723 --session iphone
python3 -m saygo.cli device connect --platform android --device DEVICE_ID \
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
selection, host routing, license refusal, preflight blockers, installation
sequencing, session identity, screenshot verification and transport failure handling.
The WSL-to-Windows path has been exercised end to end: SDK/tool installation,
API 35 emulator boot, Appium connection, screenshot, opening Android Settings,
coordinate tapping and entering a search query. A 21-call Windows helper check
and a separate 45-second window observation found no visible helper consoles
after the console fix. Mac/iPhone operations remain unverified on real hardware;
mocked tests are not hardware validation.
