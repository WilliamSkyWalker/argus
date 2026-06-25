"""iOS Simulator control — the 'eyes' and 'hands' of the agent."""

import json
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Device:
    name: str
    udid: str
    state: str
    runtime: str


def _simctl(*args: str) -> str:
    result = subprocess.run(
        ["xcrun", "simctl", *args],
        capture_output=True, text=True, check=True,
    )
    return result.stdout


def list_devices() -> list[Device]:
    """List available iOS simulator devices."""
    raw = _simctl("list", "devices", "available", "--json")
    data = json.loads(raw)
    devices = []
    for runtime, devs in data["devices"].items():
        for d in devs:
            devices.append(Device(
                name=d["name"],
                udid=d["udid"],
                state=d["state"],
                runtime=runtime,
            ))
    return devices


def create_device(name: str = "Argus", device_type: str = "iPhone 16 Pro", runtime: str | None = None) -> str:
    """Create a simulator device. Returns the UDID."""
    if runtime is None:
        # Find the latest available iOS runtime
        raw = _simctl("list", "runtimes", "--json")
        runtimes = json.loads(raw)["runtimes"]
        ios_runtimes = [r for r in runtimes if r["isAvailable"] and "iOS" in r["name"]]
        if not ios_runtimes:
            raise RuntimeError("No available iOS runtime found. Install one via Xcode > Settings > Platforms.")
        runtime = ios_runtimes[-1]["identifier"]
    udid = _simctl("create", name, device_type, runtime).strip()
    return udid


def boot(udid: str = "booted") -> None:
    """Boot a simulator and open the Simulator app window."""
    _simctl("boot", udid)
    subprocess.run(["open", "-a", "Simulator"], check=True)


def shutdown(udid: str = "booted") -> None:
    _simctl("shutdown", udid)


def install_app(app_path: str, udid: str = "booted") -> None:
    _simctl("install", udid, app_path)


def launch_app(bundle_id: str, udid: str = "booted") -> None:
    _simctl("launch", udid, bundle_id)


def screenshot(output_path: str | None = None, udid: str = "booted") -> Path:
    """Take a screenshot. Returns path to the PNG file."""
    if output_path is None:
        output_path = tempfile.mktemp(suffix=".png")
    _simctl("io", udid, "screenshot", output_path)
    return Path(output_path)
