#!/usr/bin/env python3
"""Dependency check for the argus-device plugin.

A plugin cannot install system dependencies (Node, the Appium server and its drivers,
Xcode, ANDROID_HOME, Python packages), so it is far better to state everything that is
missing up front than to fail halfway through a run as a "can't tap / can't connect"
false failure.

Usage:
    python3 doctor.py                 # device-driving profile (argus-device)
    python3 doctor.py --profile full  # also check .env / LLM key / tests dir (running suites)
    python3 doctor.py --json

Exit code: 0 = nothing blocking; 1 = at least one blocking item (warnings don't count).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

REPO_URL = "https://github.com/WilliamSkyWalker/argus"

OK, WARN, MISS = "ok", "warn", "miss"

results: list[dict] = []


def add(status: str, name: str, detail: str = "", hint: str = "") -> None:
    results.append({"status": status, "name": name, "detail": detail, "hint": hint})


def run(cmd: list[str], timeout: int = 20) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except FileNotFoundError:
        return 127, "not found"
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    except OSError as e:
        return 1, str(e)


def _is_root(p: Path) -> bool:
    return (p / "argus" / "__init__.py").is_file()


def argus_root() -> tuple[Path | None, str]:
    """Locate the argus checkout: ARGUS_HOME, then the cwd chain. Returns (path, origin)."""
    raw = (os.environ.get("ARGUS_HOME") or "").strip()
    if raw:
        home = Path(raw).expanduser()
        for cand in (home, *home.parents):
            if _is_root(cand):
                return cand, "ARGUS_HOME"
    cwd = Path.cwd()
    for cand in (cwd, *cwd.parents):
        if _is_root(cand):
            return cand, "cwd"
    return None, ""


# ── checks ────────────────────────────────────────────────────────

def check_python() -> None:
    v = sys.version_info
    if (v.major, v.minor) >= (3, 11):
        add(OK, "python", f"{platform.python_version()} ({sys.executable})")
    else:
        add(MISS, "python", f"{platform.python_version()} < 3.11",
            "argus needs Python >= 3.11 — run Claude Code against a 3.11+ interpreter")


def check_argus() -> Path | None:
    root, origin = argus_root()
    if root:
        add(OK, "argus package", f"{root} ({origin})")
        if origin == "cwd":
            add(WARN, "ARGUS_HOME", "unset — found via the current directory",
                f"set ARGUS_HOME={root} so the plugin also works from other project dirs")
        return root
    if importlib.util.find_spec("argus"):
        add(OK, "argus package", "pip-installed")
        return None
    add(MISS, "argus package", "not found",
        f'set ARGUS_HOME=/path/to/argus, or: pip3 install "argus[mobile,mcp] @ git+{REPO_URL}.git"')
    return None


def check_py_deps() -> None:
    # (import name, pip name, blocking, what it's for)
    deps = [
        ("mcp", "mcp", True, "MCP server"),
        ("PIL", "Pillow", True, "screenshot handling"),
        ("appium", "Appium-Python-Client", True, "mobile driver"),
        ("selenium", "selenium", True, "Appium client dependency / browser platform"),
        ("openai", "openai", True, "argus.cli import chain (only really used when running suites)"),
        ("uiautomator2", "uiautomator2", False, "optional Android helpers"),
    ]
    for mod, pip_name, blocking, why in deps:
        if importlib.util.find_spec(mod):
            add(OK, f"py:{pip_name}", why)
        else:
            add(MISS if blocking else WARN, f"py:{pip_name}", f"missing ({why})",
                f"pip3 install {pip_name}")


def check_appium() -> None:
    if not shutil.which("node"):
        add(MISS, "node", "not on PATH", "install Node LTS (the Appium server runs on Node)")
    else:
        _, out = run(["node", "-v"])
        add(OK, "node", out.strip().splitlines()[0] if out.strip() else "")

    if not shutil.which("appium"):
        add(MISS, "appium server", "not on PATH",
            "npm i -g appium@3   # argus starts the server itself, but the binary must exist")
        return
    code, out = run(["appium", "-v"])
    add(OK if code == 0 else WARN, "appium server",
        out.strip().splitlines()[0] if out.strip() else "")

    code, out = run(["appium", "driver", "list", "--installed"], timeout=60)
    low = out.lower()
    for drv, plat_name, install in (
        ("uiautomator2", "Android", "appium driver install uiautomator2"),
        ("xcuitest", "iOS", "appium driver install xcuitest@latest"),
    ):
        if drv in low:
            add(OK, f"appium driver:{drv}", plat_name)
        else:
            add(WARN, f"appium driver:{drv}", f"not installed ({plat_name} cannot run)", install)


def check_android() -> None:
    adb = shutil.which("adb")
    home = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
    if not adb and home:
        cand = Path(home) / "platform-tools" / "adb"
        adb = str(cand) if cand.exists() else None
    if not adb:
        add(WARN, "adb", "not on PATH and ANDROID_HOME has no usable SDK",
            "install Android platform-tools and set ANDROID_HOME (argus never drives via adb, "
            "but APK installs and device discovery need it)")
        return
    add(OK, "adb", adb)
    code, out = run([adb, "devices"])
    serials = [ln.split("\t")[0] for ln in out.splitlines()[1:] if "\tdevice" in ln]
    if serials:
        add(OK, "android devices", ", ".join(serials))
    else:
        add(WARN, "android devices", "none connected/authorized",
            "plug in a device and accept the USB debugging prompt, or boot an emulator")


def check_ios() -> None:
    if platform.system() != "Darwin":
        return
    if not shutil.which("xcrun"):
        add(WARN, "xcode/xcrun", "not on PATH",
            "install Xcode + Command Line Tools (iOS only)")
        return
    code, out = run(["xcrun", "simctl", "list", "devices", "booted"])
    booted = [ln.strip() for ln in out.splitlines() if "Booted" in ln]
    add(OK, "ios simulators",
        f"{len(booted)} booted" + (f": {booted[0]}" if booted else ""))
    if not os.environ.get("IOS_TEAM_ID"):
        add(WARN, "IOS_TEAM_ID", "unset (physical iOS devices only — used to sign WDA)",
            "set IOS_TEAM_ID=<your team id> in .env")


def check_runner(root: Path | None) -> None:
    """Extra requirements for running suites: an LLM key (.env) and a tests/ directory."""
    key = (os.environ.get("LLM_API_KEY") or "").strip()
    env_file = (root / ".env") if root else Path(".env")
    if not key and env_file.is_file():
        for line in env_file.read_text(errors="ignore").splitlines():
            if line.strip().startswith("LLM_API_KEY="):
                key = line.split("=", 1)[1].strip()
                break
    # Report presence only — never print the key itself
    if key:
        add(OK, "LLM_API_KEY", f"configured ({env_file if env_file.is_file() else 'env'})")
    else:
        add(MISS, "LLM_API_KEY", "not configured",
            f"set LLM_API_KEY=… in {env_file} (python3 -m argus.cli init writes a template)")

    tests_dir = (root / "tests") if root else Path("tests")
    if tests_dir.is_dir():
        targets = [d.name for d in sorted(tests_dir.iterdir())
                   if d.is_dir() and not d.name.startswith((".", "_"))]
        add(OK, "tests/", f"{len(targets)} target(s)"
            + (f": {', '.join(targets[:5])}" if targets else ""))
    else:
        add(WARN, "tests/", f"{tests_dir} does not exist",
            "python3 -m argus.cli new <target> --platform android --package com.example.app")


# ── main ──────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(prog="argus-doctor")
    ap.add_argument("--profile", choices=("device", "full"), default="device",
                    help="device = device driving only; full = also check .env / tests/")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    check_python()
    root = check_argus()
    check_py_deps()
    check_appium()
    check_android()
    check_ios()
    if args.profile == "full":
        check_runner(root)

    blocking = [r for r in results if r["status"] == MISS]
    warns = [r for r in results if r["status"] == WARN]

    if args.json:
        print(json.dumps({"profile": args.profile, "blocking": len(blocking),
                          "warnings": len(warns), "checks": results},
                         ensure_ascii=False, indent=2))
        return 1 if blocking else 0

    icon = {OK: "✓", WARN: "!", MISS: "✗"}
    width = max(len(r["name"]) for r in results)
    print(f"argus doctor — profile: {args.profile}\n")
    for r in results:
        print(f" {icon[r['status']]} {r['name']:<{width}}  {r['detail']}")
        if r["hint"] and r["status"] != OK:
            print(f"   {'':<{width}}   → {r['hint']}")
    print()
    if blocking:
        print(f"{len(blocking)} blocking item(s) must be fixed: "
              + ", ".join(r["name"] for r in blocking))
    else:
        print("Nothing blocking."
              + (f" {len(warns)} advisory item(s) (each affects one platform or optional capability)."
                 if warns else ""))
    return 1 if blocking else 0


if __name__ == "__main__":
    sys.exit(main())
