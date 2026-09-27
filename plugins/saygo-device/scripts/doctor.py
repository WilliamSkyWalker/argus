#!/usr/bin/env python3
"""Dependency check for the saygo-device plugin.

A plugin cannot install system dependencies (Node, the Appium server and its drivers,
Xcode, ANDROID_HOME, Python packages), so it is far better to state everything that is
missing up front than to fail halfway through a run as a "can't tap / can't connect"
false failure.

Usage:
    python3 doctor.py                 # device-driving profile (saygo-device)
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

REPO_URL = "https://github.com/WilliamSkyWalker/saygo"

OK, WARN, MISS = "ok", "warn", "miss"

results: list[dict] = []


def add(status: str, name: str, detail: str = "", hint: str = "") -> None:
    results.append({"status": status, "name": name, "detail": detail, "hint": hint})


def run(cmd: list[str], timeout: int = 20,
        env: dict[str, str] | None = None) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except FileNotFoundError:
        return 127, "not found"
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    except OSError as e:
        return 1, str(e)


def _is_root(p: Path) -> bool:
    return (p / "saygo" / "__init__.py").is_file()


def saygo_root() -> tuple[Path | None, str]:
    """Locate the saygo checkout: SAYGO_HOME, then the cwd chain. Returns (path, origin)."""
    raw = (os.environ.get("SAYGO_HOME") or "").strip()
    if raw:
        home = Path(raw).expanduser()
        for cand in (home, *home.parents):
            if _is_root(cand):
                return cand, "SAYGO_HOME"
    cwd = Path.cwd()
    for cand in (cwd, *cwd.parents):
        if _is_root(cand):
            return cand, "cwd"
    return None, ""


# ── checks ────────────────────────────────────────────────────────

def check_python() -> None:
    v = sys.version_info
    if (v.major, v.minor) >= (3, 10):
        add(OK, "python", f"{platform.python_version()} ({sys.executable})")
    else:
        add(MISS, "python", f"{platform.python_version()} < 3.10",
            "saygo needs Python >= 3.10 — run Claude Code against a 3.10+ interpreter")


def check_saygo() -> Path | None:
    root, origin = saygo_root()
    if root:
        add(OK, "saygo package", f"{root} ({origin})")
        if origin == "cwd":
            add(WARN, "SAYGO_HOME", "unset — found via the current directory",
                f"set SAYGO_HOME={root} so the plugin also works from other project dirs")
        return root
    if importlib.util.find_spec("saygo"):
        add(OK, "saygo package", "pip-installed")
        return None
    add(MISS, "saygo package", "not found",
        f"git clone {REPO_URL}.git && set SAYGO_HOME to the cloned repository")
    return None


def check_py_deps(target="browser", profile="device") -> None:
    deps = [("mcp", "mcp", True, "MCP server"), ("PIL", "Pillow", True, "screenshot handling")]
    if target in {"android", "ios"}:
        deps.extend([("appium", "Appium-Python-Client", True, "mobile driver"),
                     ("selenium", "selenium", True, "Appium client dependency")])
    elif target == "browser":
        deps.append(("playwright", "playwright", False, "Playwright browser backend; extension backend does not require this"))
    if profile == "full":
        deps.append(("openai", "openai", True, "autonomous QA runner"))
    for mod, pip_name, blocking, why in deps:
        if importlib.util.find_spec(mod):
            add(OK, f"py:{pip_name}", why)
        else:
            add(MISS if blocking else WARN, f"py:{pip_name}", f"missing ({why})", f"pip3 install {pip_name}")


def check_appium() -> None:
    runtime = Path(os.environ.get("SAYGO_HOME_DIR", Path.home() / ".saygo")) / "runtime"
    sandbox_appium = runtime / "node_modules" / ".bin" / "appium"
    sandbox_node = runtime / "node" / "bin" / "node"
    node_marker = runtime / "node_path.txt"

    appium = str(sandbox_appium) if sandbox_appium.is_file() else shutil.which("appium")
    node = str(sandbox_node) if sandbox_node.is_file() else shutil.which("node")
    if not sandbox_node.is_file() and node_marker.is_file():
        recorded = node_marker.read_text(errors="ignore").strip()
        if recorded and Path(recorded).is_file():
            node = recorded

    if not node:
        add(MISS, "node", "not on PATH", "install Node LTS (the Appium server runs on Node)")
    else:
        _, out = run([node, "-v"])
        add(OK, "node", out.strip().splitlines()[0] if out.strip() else "")

    if not appium:
        add(MISS, "appium server", "not found on PATH or in ~/.saygo/runtime",
            "python3 -m saygo.cli mcp init --skip-ios   # omit --skip-ios when iOS is needed")
        return

    appium_env = os.environ.copy()
    if node:
        appium_env["PATH"] = str(Path(node).parent) + os.pathsep + appium_env.get("PATH", "")
    sandbox_home = runtime / "appium_home"
    if sandbox_appium.is_file():
        appium_env["APPIUM_HOME"] = str(sandbox_home)

    code, out = run([appium, "-v"], env=appium_env)
    add(OK if code == 0 else WARN, "appium server",
        (out.strip().splitlines()[0] if out.strip() else "")
        + (" (~/.saygo/runtime)" if sandbox_appium.is_file() else " (PATH)"))

    code, out = run([appium, "driver", "list", "--installed"],
                    timeout=60, env=appium_env)
    low = out.lower()
    for drv, plat_name, install in (
        ("uiautomator2", "Android", "python3 -m saygo.cli mcp init --skip-ios"),
        ("xcuitest", "iOS", "python3 -m saygo.cli mcp init"),
    ):
        if drv in low:
            add(OK, f"appium driver:{drv}", plat_name)
        else:
            add(WARN, f"appium driver:{drv}", f"not installed ({plat_name} cannot run)", install)


def check_android() -> None:
    adb = shutil.which("adb")
    sandbox_adb = (Path(os.environ.get("SAYGO_HOME_DIR", Path.home() / ".saygo"))
                   / "runtime" / "platform-tools" / "adb")
    if not adb and sandbox_adb.is_file():
        adb = str(sandbox_adb)
    home = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
    if not adb and home:
        cand = Path(home) / "platform-tools" / "adb"
        adb = str(cand) if cand.exists() else None
    if not adb:
        add(WARN, "adb", "not on PATH, in ANDROID_HOME, or in ~/.saygo/runtime",
            "python3 -m saygo.cli mcp init --skip-ios")
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
            f"set LLM_API_KEY=… in {env_file} (python3 -m saygo.cli init writes a template)")

    tests_dir = (root / "tests") if root else Path("tests")
    if tests_dir.is_dir():
        targets = [d.name for d in sorted(tests_dir.iterdir())
                   if d.is_dir() and not d.name.startswith((".", "_"))]
        add(OK, "tests/", f"{len(targets)} target(s)"
            + (f": {', '.join(targets[:5])}" if targets else ""))
    else:
        add(WARN, "tests/", f"{tests_dir} does not exist",
            "python3 -m saygo.cli new <target> --platform android --package com.example.app")


# ── main ──────────────────────────────────────────────────────────

def main() -> int:
    managed = Path(__file__).resolve().parents[1] / 'managed_runtime.json'
    if managed.is_file():
        python = json.loads(managed.read_text())['python']
        if os.path.abspath(sys.executable) != os.path.abspath(python):
            os.execv(python, [python, '-I', str(Path(__file__).resolve()), *sys.argv[1:]])
    ap = argparse.ArgumentParser(prog="saygo-doctor")
    ap.add_argument("--profile", choices=("device", "full"), default="device",
                    help="device = device driving only; full = also check .env / tests/")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--session", help="Check connection, screenshot and image reading for a named session")
    ap.add_argument("--platform", choices=("browser", "android", "ios", "desktop"), default="browser")
    args = ap.parse_args()

    check_python()
    root = check_saygo()
    if root:
        sys.path.insert(0, str(root))
    if args.session:
        try:
            from saygo.commands.doctor import diagnose
            report = diagnose(args.session)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report.get("ok") else 1
        except (ImportError, RuntimeError, OSError, ValueError) as exc:
            print(json.dumps({"ok": False, "error": str(exc)}))
            return 1
    check_py_deps(args.platform, args.profile)
    if args.platform in {"android", "ios"}:
        check_appium()
    if args.platform == "android":
        check_android()
    if args.platform == "ios":
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
    print(f"saygo doctor — profile: {args.profile}\n")
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
