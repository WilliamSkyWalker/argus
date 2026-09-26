"""Native messaging host and file IPC client (stdlib only, including Windows).

The private directory is the trust boundary: anyone who can write it can control
shared tabs. Requests are dispatched once, never retried after a timeout.
"""
import argparse
import json
import os
from pathlib import Path
import queue
import re
import struct
import sys
import threading
import time
import uuid

HOST = "com.argus.browser"
MAX_FRAME = 1024 * 1024


def atomic_json(path, value):
    path = Path(path)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with open(temp, "x", encoding="utf-8") as stream:
            if os.name != "nt":
                os.chmod(temp, 0o600)
            json.dump(value, stream, ensure_ascii=False)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def read_exact(stream, size):
    data = bytearray()
    while len(data) < size:
        block = stream.read(size - len(data))
        if not block:
            raise EOFError("native messaging disconnected")
        data.extend(block)
    return bytes(data)


def read_frame(stream):
    length = struct.unpack("=I", read_exact(stream, 4))[0]
    if not 0 < length <= MAX_FRAME:
        raise ValueError("invalid native message length")
    return json.loads(read_exact(stream, length))


def write_frame(stream, value):
    data = json.dumps(value, ensure_ascii=False).encode("utf-8")
    if len(data) > MAX_FRAME:
        raise ValueError("native message too large")
    stream.write(struct.pack("=I", len(data)) + data)
    stream.flush()


class Client:
    def __init__(self, directory, timeout=35):
        self.directory = Path(directory).expanduser().resolve()
        self.timeout = timeout

    def call(self, operation, **arguments):
        status = json.loads((self.directory / "status.json").read_text())
        if not status.get("connected"):
            raise RuntimeError("Connect the Argus browser extension first")
        rid = uuid.uuid4().hex
        request = self.directory / (rid + ".request")
        response = self.directory / (rid + ".response")
        atomic_json(request, {"id": rid, "epoch": status["epoch"], "operation": operation,
                              "arguments": arguments, "deadline": time.time() + min(self.timeout - 1, 25)})
        end = time.monotonic() + self.timeout
        try:
            while time.monotonic() < end:
                if response.exists():
                    result = json.loads(response.read_text(encoding="utf-8"))
                    if "error" in result:
                        raise RuntimeError(result["error"])
                    return result["result"]
                current = json.loads((self.directory / "status.json").read_text())
                if not current.get("connected") or current.get("epoch") != status["epoch"]:
                    raise RuntimeError("Browser bridge disconnected; dispatched action outcome may be unknown")
                time.sleep(.05)
            raise RuntimeError("Browser bridge timed out; outcome may be unknown. Do not retry an action blindly")
        finally:
            request.unlink(missing_ok=True)
            response.unlink(missing_ok=True)


def serve(directory, input_stream=None, output_stream=None):
    directory = Path(directory).resolve()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    # One native host per directory, across Chrome profiles and reconnects.
    lock = open(directory / "host.lock", "a+b")
    try:
        if os.name == "nt":
            import msvcrt
            lock.seek(0)
            lock.write(b"0")
            lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _serve_locked(directory, input_stream or sys.stdin.buffer,
                      output_stream or sys.stdout.buffer)
    finally:
        lock.close()


def _serve_locked(directory, source, sink):
    incoming = queue.Queue()
    epoch = uuid.uuid4().hex

    def reader():
        try:
            while True:
                incoming.put(read_frame(source))
        except (EOFError, ValueError, OSError) as exc:
            incoming.put(exc)

    threading.Thread(target=reader, daemon=True).start()
    status = directory / "status.json"
    atomic_json(status, {"connected": True, "epoch": epoch})
    try:
        while True:
            if not incoming.empty():
                item = incoming.get_nowait()
                if isinstance(item, Exception):
                    return
            for path in sorted(directory.glob("*.request")):
                if not re.fullmatch(r"[a-f0-9]{32}\.request", path.name):
                    continue
                response = path.with_suffix(".response")
                try:
                    if path.stat().st_size > MAX_FRAME - 1024:
                        raise ValueError("request too large")
                    request = json.loads(path.read_text(encoding="utf-8"))
                    path.unlink(missing_ok=True)  # claim before dispatch; never replay
                    if request["id"] != path.stem or request["epoch"] != epoch:
                        raise ValueError("stale bridge request; reconnect explicitly")
                    if request["deadline"] <= time.time():
                        raise ValueError("request expired before dispatch")
                    write_frame(sink, request)
                    chunks = []
                    end = time.monotonic() + 30
                    while True:
                        item = incoming.get(timeout=max(.001, end - time.monotonic()))
                        if isinstance(item, Exception):
                            raise RuntimeError("extension disconnected; action outcome may be unknown")
                        if item.get("id") != request["id"]:
                            raise RuntimeError("unexpected extension response")
                        chunks.append(item["chunk"])
                        if sum(map(len, chunks)) > 32 * MAX_FRAME:
                            raise RuntimeError("extension response exceeds 32 MiB")
                        if item.get("last"):
                            atomic_json(response, json.loads("".join(chunks)))
                            break
                except (OSError, ValueError, KeyError) as exc:
                    path.unlink(missing_ok=True)
                    atomic_json(response, {"error": str(exc)})
                except (queue.Empty, RuntimeError) as exc:
                    atomic_json(response, {"error": str(exc) or "extension timeout; outcome unknown"})
                    return  # terminate transport; no subsequent dispatch after ambiguity
            time.sleep(.05)
    finally:
        atomic_json(status, {"connected": False, "epoch": epoch})


def install(directory, extension_id, browser):
    if not re.fullmatch("[a-p]{32}", extension_id):
        raise ValueError("extension ID must be the 32-letter ID from chrome://extensions")
    directory = Path(directory).expanduser().resolve()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    script = str(Path(__file__).resolve())
    if os.name == "nt":
        launcher = directory / "host.cmd"
        # cmd expands percent even inside quotes; reject paths that cannot be represented safely.
        if any(c in str(directory) + script + sys.executable for c in '%\r\n"'):
            raise ValueError("installation paths contain unsupported shell characters")
        launcher.write_text('@echo off\nchcp 65001 >nul\nsetlocal DisableDelayedExpansion\n' +
                            " ".join('"' + arg + '"' for arg in
                                     [sys.executable, script, "host", "--directory", str(directory)]) + '\n',
                            encoding="utf-8")
    else:
        import shlex
        launcher = directory / "host.sh"
        launcher.write_text("#!/bin/sh\nexec " + shlex.join(
            [sys.executable, script, "host", "--directory", str(directory)]) + "\n")
        launcher.chmod(0o700)
    manifest = {"name": HOST, "description": "Argus existing browser bridge",
                "path": str(launcher), "type": "stdio",
                "allowed_origins": [f"chrome-extension://{extension_id}/"]}
    manifest_path = directory / (HOST + ".json")
    atomic_json(manifest_path, manifest)
    if os.name == "nt":
        import winreg
        vendor = "Google\\Chrome" if browser == "chrome" else "Microsoft\\Edge"
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"Software\{vendor}\NativeMessagingHosts\{HOST}") as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, str(manifest_path))
    else:
        if sys.platform == "darwin":
            root = Path.home() / "Library/Application Support" / ("Google/Chrome" if browser == "chrome" else "Microsoft Edge")
        else:
            root = Path.home() / ".config" / ("google-chrome" if browser == "chrome" else "microsoft-edge")
        target = root / "NativeMessagingHosts" / (HOST + ".json")
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(target, manifest)
    return {"installed": True, "directory": str(directory), "manifest": str(manifest_path)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("host", "install", "bind"):
        p = sub.add_parser(command)
        p.add_argument("--directory", required=True)
        if command == "install":
            p.add_argument("--extension-id", required=True)
            p.add_argument("--browser", choices=["chrome", "edge"], default="chrome")
        if command == "bind":
            p.add_argument("--serial", required=True)
    args, extra = parser.parse_known_args()  # Chrome appends the extension origin to host argv.
    if extra and args.command != "host":
        parser.error("unexpected arguments: " + " ".join(extra))
    if args.command == "host":
        if os.name == "nt":
            import msvcrt
            msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
            msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
        serve(args.directory)
    elif args.command == "install":
        print(json.dumps(install(args.directory, args.extension_id, args.browser)))
    else:
        from argus.platforms import device_session as ds
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", args.serial):
            raise ValueError("invalid session serial")
        pages = Client(args.directory).call("pages")
        if ds.load_state(args.serial):
            raise ValueError("session already exists; use a new serial")
        ds.save_state(args.serial, {"kind": "browser", "browser_backend": "extension",
                                   "bridge_directory": str(Path(args.directory).expanduser().resolve())})
        print(json.dumps({"serial": args.serial, "pages": pages}))


if __name__ == "__main__":
    main()
