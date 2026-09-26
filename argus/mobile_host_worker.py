"""Private stdlib Windows worker, called with a JSON request on stdin."""
import contextlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid


@contextlib.contextmanager
def lifecycle_lock(home):
    import msvcrt
    root = Path(home) / "runtime"
    root.mkdir(parents=True, exist_ok=True)
    with (root / "lifecycle.lock").open("a+b") as stream:
        if stream.seek(0, 2) == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise RuntimeError("Another Argus Windows install/boot is running; retry when it finishes") from exc
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def request_appium(port, base_path, method, path, body=None):
    if not isinstance(port, int) or not 1024 <= port <= 65535:
        raise ValueError("Invalid Appium port")
    if not path.startswith("/") or path.startswith("//") or "\r" in path or "\n" in path:
        raise ValueError("Invalid Appium request path")
    if not base_path.startswith("/argus-") or not base_path[7:].isalnum():
        raise ValueError("Invalid Appium base path")
    import base64
    data = base64.b64decode(body, validate=True) if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{port}{base_path}{path}", data=data,
                                 method=method, headers={"Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        response = opener.open(req, timeout=180)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        raw = response.read(32 * 1024**2 + 1)
        if len(raw) > 32 * 1024**2:
            raise RuntimeError("Appium response exceeds 32 MiB")
        return {"status": response.status, "body": base64.b64encode(raw).decode(),
                "content_type": response.headers.get("Content-Type", "application/json")}


def configure_adb(home):
    """Use a private Windows adb server; WSL may own the default port 5037."""
    state = Path(home) / "runtime" / "host-adb.json"
    if state.exists():
        port = json.loads(state.read_text())["port"]
    else:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        state.parent.mkdir(parents=True, exist_ok=True)
        temporary = state.with_suffix(".tmp")
        temporary.write_text(json.dumps({"port": port}))
        temporary.replace(state)
    if not isinstance(port, int) or not 1024 <= port <= 65535:
        raise ValueError("Invalid private adb port")
    os.environ["ANDROID_ADB_SERVER_PORT"] = str(port)
    # Hostless tcp:PORT means a local daemon that adb may start itself.
    # tcp:127.0.0.1:PORT is treated as a remote server and will not autostart.
    os.environ["ADB_SERVER_SOCKET"] = f"tcp:{port}"
    return port


def ensure_server():
    from argus import mobile, toolchain
    root = mobile.home()
    state = root / "host-appium.json"
    if state.is_file():
        existing = json.loads(state.read_text())
        try:
            if request_appium(existing["port"], existing["base_path"], "GET", "/status")["status"] == 200:
                return existing
        except (OSError, ValueError):
            pass
    paths = toolchain.sandbox_paths()
    if not paths.get("node_bin") or not paths.get("appium_bin"):
        raise RuntimeError("Windows Appium is not installed; rerun device install")
    env = mobile.environment()
    env["APPIUM_HOME"] = str(toolchain.APPIUM_HOME)
    env["PATH"] = str(Path(paths["node_bin"]).parent) + os.pathsep + env["PATH"]
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    base_path = "/argus-" + uuid.uuid4().hex
    log = root / "host-appium.log"
    with log.open("ab") as output:
        preload = str(Path(__file__).with_name("windows_no_console.cjs"))
        process = subprocess.Popen([paths["node_bin"], "--require", preload, paths["appium_bin"], "--address", "127.0.0.1",
                                    "--port", str(port), "--base-path", base_path], env=env,
                                   stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                                   creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW)
    for _ in range(60):
        if process.poll() is not None:
            raise RuntimeError("Windows Appium exited; inspect " + str(log))
        try:
            if request_appium(port, base_path, "GET", "/status")["status"] == 200:
                value = {"port": port, "base_path": base_path, "pid": process.pid,
                         "adb_port": int(os.environ["ANDROID_ADB_SERVER_PORT"])}
                temporary = state.with_suffix(".tmp")
                temporary.write_text(json.dumps(value))
                temporary.replace(state)
                return value
        except OSError:
            pass
        time.sleep(1)
    process.terminate()
    raise RuntimeError("Windows Appium readiness timed out; inspect " + str(log))


def execute(payload):
    os.environ["ARGUS_HOME_DIR"] = payload["home"]
    os.environ["ANDROID_HOME"] = payload["sdk_root"]
    os.environ["ANDROID_SDK_ROOT"] = payload["sdk_root"]
    # Embeddable Python ignores PYTHONPATH; add only this immutable code bundle.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from argus import mobile, toolchain
    operation = payload["operation"]
    if operation == "http":
        return request_appium(payload["port"], payload["base_path"], payload["method"],
                              payload["path"], payload.get("body"))
    configure_adb(payload["home"])
    if operation == "server":
        return ensure_server()
    if operation == "boot":
        return mobile.boot_device("android", payload["device"], payload.get("timeout", 240), payload.get("headless", False))
    if operation != "install" or payload.get("accept_licenses") is not True:
        raise ValueError("Installation requires explicit SDK license acceptance")
    result = mobile.install_android(payload["name"], payload["api"], True)
    node = toolchain.ensure_node()
    appium = toolchain.install_appium(node)
    toolchain.install_drivers(node, appium, ios=False)
    if payload.get("boot"):
        result["boot"] = mobile.boot_device("android", payload["name"], headless=payload.get("headless", False))
    return result


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    try:
        payload = json.load(sys.stdin)
        lock = contextlib.nullcontext() if payload.get("operation") == "http" else lifecycle_lock(payload["home"])
        with lock, contextlib.redirect_stdout(sys.stderr):
            result = execute(payload)
        print(json.dumps(result, ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        raise SystemExit(2)
