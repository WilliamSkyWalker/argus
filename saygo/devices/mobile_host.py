"""Host selection and Windows provisioning from WSL; no install during planning."""
import base64
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile

from saygo.devices import mobile

PYTHON_URL = "https://www.python.org/ftp/python/3.13.13/python-3.13.13-embed-amd64.zip"
PYTHON_SHA256 = "8766a8775746235e23cf5aee5027ab1060bb981d93110577adcf3508aa0cbd55"
SDK_TERMS = "https://developer.android.com/studio/terms"
MIN_FREE = 12 * 1024**3


def is_wsl():
    return platform.system() == "Linux" and "microsoft" in platform.release().lower()


def has_kvm():
    return os.access("/dev/kvm", os.R_OK | os.W_OK)


def select_host(requested, target):
    if requested not in {"auto", "local", "windows"}:
        raise ValueError("Unknown host: " + requested)
    if target == "ios":
        if requested == "windows":
            raise ValueError("iOS simulators require a local Mac, not a Windows host")
        mobile.require_mac()
        return "local"
    if requested == "windows":
        if platform.system() == "Windows":
            return "local"
        if not is_wsl():
            raise ValueError("--host windows requires WSL or native Windows")
        return "windows"
    if requested == "auto" and is_wsl() and not has_kvm():
        return "windows"
    return "local"


def windows_info():
    shell = shutil.which("powershell.exe")
    if not shell:
        raise RuntimeError("Windows PowerShell interop is unavailable; enable WSL interop")
    # Static script only. Paths/data returned as JSON, never interpolated into shell code.
    script = r'''
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$root = Join-Path $env:LOCALAPPDATA 'Saygo\mobile'
$sdk = $env:ANDROID_HOME
if (-not $sdk) { $sdk = $env:ANDROID_SDK_ROOT }
if (-not $sdk) {
    $sdk = Join-Path $env:LOCALAPPDATA 'Android\Sdk'
    if (-not (Test-Path $sdk)) { $sdk = Join-Path $root 'runtime\android-sdk' }
}
$drive = [System.IO.DriveInfo]::new([System.IO.Path]::GetPathRoot($sdk))
$rootDrive = [System.IO.DriveInfo]::new([System.IO.Path]::GetPathRoot($root))
$feature = Get-CimInstance Win32_OptionalFeature -Filter "Name='HypervisorPlatform'" -ErrorAction SilentlyContinue
$computer = Get-CimInstance Win32_ComputerSystem
@{home=$root; sdk_root=$sdk; free_bytes=[Math]::Min($drive.AvailableFreeSpace,$rootDrive.AvailableFreeSpace);
  hypervisor=[bool]$computer.HypervisorPresent; whpx=($feature.InstallState -eq 1);
  architecture=$env:PROCESSOR_ARCHITECTURE} | ConvertTo-Json -Compress
'''
    encoded = base64.b64encode(script.encode("utf-16le")).decode()
    result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                            capture_output=True, text=True, encoding="utf-8", timeout=30)
    if result.returncode:
        raise RuntimeError("Windows preflight failed: " + result.stderr[-2000:])
    return json.loads(result.stdout.lstrip("\ufeff"))


def plan(args):
    host = select_host(args.host, args.platform)
    blockers = []
    if host == "windows":
        details = windows_info()
        if details["architecture"].lower() != "amd64":
            blockers.append("Windows-hosted Android provisioning currently requires x64 Windows")
        accelerated = details["hypervisor"] and details["whpx"]
    else:
        root = mobile.sdk_root() if args.platform == "android" else mobile.home()
        ancestor = root
        while not ancestor.exists():
            ancestor = ancestor.parent
        details = {"home": str(mobile.home().parent), "sdk_root": str(root),
                   "free_bytes": shutil.disk_usage(ancestor).free}
        accelerated = platform.system() != "Linux" or has_kvm()
        if platform.system() == "Windows":
            native = windows_info()
            accelerated = native["hypervisor"] and native["whpx"]
        emulator = mobile.sdk_root() / "emulator" / mobile.executable("emulator")
        if args.platform == "android" and emulator.is_file():
            try:
                mobile.run([emulator, "-accel-check"])
                accelerated = True
            except RuntimeError as exc:
                accelerated = False
                details["acceleration_error"] = str(exc)
    if details["free_bytes"] < MIN_FREE:
        blockers.append("At least 12 GiB free disk space is required for provisioning")
    if args.platform == "android" and not accelerated:
        blockers.append("Hardware acceleration unavailable. " + (
            "Enable Windows Hypervisor Platform in Windows Features, reboot, then retry."
            if host == "windows" or platform.system() == "Windows" else
            "Enable KVM and grant access to /dev/kvm; on WSL use --host windows."))
    return {"host": host, "platform": args.platform, **details,
            "hardware_acceleration": accelerated, "blockers": blockers,
            "components": ["Java", "Android SDK", "Android Emulator", "system image", "Node", "Appium", "UiAutomator2"]
            if args.platform == "android" else ["iOS Simulator runtime", "Node", "Appium", "XCUITest"],
            "download_note": "Several GB on first install; existing components are reused",
            "license_url": SDK_TERMS if args.platform == "android" else None}


def confirm_licenses(accepted):
    if accepted:
        return
    if not sys.stdin.isatty():
        raise RuntimeError("Android SDK license acceptance required. Review " + SDK_TERMS +
                           " and rerun with --accept-licenses, or run interactively")
    print("Review Android SDK terms: " + SDK_TERMS, file=sys.stderr)
    print("Download/install these components and accept the SDK licenses? [y/N] ", end="", file=sys.stderr, flush=True)
    if input().strip().lower() not in {"y", "yes"}:
        raise RuntimeError("Installation cancelled; no components were downloaded")


def wsl_path(path, windows=False):
    result = subprocess.run(["wslpath", "-w" if windows else "-u", str(path)],
                            capture_output=True, text=True, check=True, timeout=10)
    return result.stdout.strip()


def prepare_windows(details, install=False):
    """Copy only our stdlib host implementation; never copy configs or credentials."""
    root = Path(wsl_path(details["home"]))
    python = root / "python" / "python.exe"
    if not python.is_file():
        if not install:
            raise RuntimeError("Windows host is not installed; run device install --platform android --host windows")
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as tmp:
            archive = Path(tmp) / "python.zip"
            mobile.download(PYTHON_URL, archive, PYTHON_SHA256)
            mobile.unpack(archive, Path(tmp) / "python")
            if (root / "python").exists():
                raise RuntimeError("Incomplete Windows Python directory; inspect " + str(root / "python"))
            shutil.move(str(Path(tmp) / "python"), root / "python")
    package = Path(__file__).resolve().parent.parent
    sources = {name: (package / name).read_bytes() for name in
               ("__init__.py", "logger.py", "devices/__init__.py", "devices/mobile.py",
                "devices/toolchain.py", "devices/mobile_host_worker.py", "devices/windows_no_console.cjs")}
    digest = hashlib.sha256(b"".join(sources.values())).hexdigest()[:20]
    bundle = root / "workers" / digest / "saygo"
    bundle.mkdir(parents=True, exist_ok=True)
    for name, content in sources.items():
        target = bundle / name
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
    return {"python": str(python), "script": wsl_path(bundle / "devices" / "mobile_host_worker.py", windows=True),
            "home": details["home"], "sdk_root": details["sdk_root"]}


def call_windows(host, operation, **params):
    payload = {"operation": operation, "home": host["home"], "sdk_root": host["sdk_root"], **params}
    result = subprocess.run([host["python"], host["script"]], input=json.dumps(payload),
                            stdout=subprocess.PIPE, text=True, encoding="utf-8",
                            cwd=str(Path(host["python"]).parent),
                            timeout=200 if operation == "http" else 7500)
    try:
        value = json.loads(result.stdout)
    except ValueError as exc:
        raise RuntimeError("Invalid Windows worker response: " + result.stdout[-1000:]) from exc
    if result.returncode or "error" in value:
        raise RuntimeError(value.get("error", "Windows worker failed"))
    return value


def connect_windows(host, device, session):
    from saygo.devices.mobile_relay import ensure_relay
    server = call_windows(host, "server")
    url = ensure_relay(host, server["port"], server["base_path"])
    return mobile.connect("android", device, session, url, adb_port=server["adb_port"])


def execute(args):
    if args.session and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", args.session):
        raise ValueError("Session alias must contain letters, digits, dot, dash or underscore")
    if args.device_command == "boot":
        host = select_host(args.host, args.platform)
        if host == "windows":
            worker = prepare_windows(windows_info())
            result = call_windows(worker, "boot", device=args.device, timeout=args.timeout, headless=args.headless)
        else:
            result = mobile.boot_device(args.platform, args.device, args.timeout, args.headless)
        result["host"] = host
        if args.connect:
            result["connection"] = (connect_windows(worker, result["device"], args.session) if host == "windows"
                                    else mobile.connect(args.platform, result["device"], args.session))
        return result

    mobile.safe_name(args.name)
    if args.platform == "android" and not 30 <= args.api <= 99:
        raise ValueError("Android API must be between 30 and 99")
    details = plan(args)
    if details["host"] == "windows" and "components" in details:
        details["components"].insert(0, "Python (private Windows host)")
    if args.dry_run:
        return details
    print(json.dumps(details, ensure_ascii=False, indent=2), file=sys.stderr, flush=True)
    if details["blockers"]:
        raise RuntimeError("; ".join(details["blockers"]))
    if args.platform == "android":
        confirm_licenses(args.accept_licenses)
    if details["host"] == "windows":
        worker = prepare_windows(details, install=True)
        result = call_windows(worker, "install", name=args.name, api=args.api, accept_licenses=True,
                              boot=args.boot or args.connect, headless=args.headless)
        if args.connect:
            result["connection"] = connect_windows(worker, result["boot"]["device"], args.session)
    else:
        result = mobile.provision(args)
    result["host"] = details["host"]
    if args.connect:
        from saygo.platforms import device_session as ds
        connection = result["connection"]
        plat = ds.attach(connection["session"])
        if plat is None:
            raise RuntimeError("Session could not be reattached for screenshot verification")
        target = mobile.home() / "verification" / (connection["session"] + ".png")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(plat.screenshot_raw())
        result["verification_screenshot"] = str(target)
    return result
