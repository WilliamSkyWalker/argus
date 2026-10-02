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

HOST = "com.saygo.browser"
MAX_FRAME = 1024 * 1024
PROTOCOL = 1
VERSION = "0.4.9"
RESPONSE_TIMEOUT = 30
MAX_RESPONSE_BYTES = 32 * MAX_FRAME


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
        if status.get('protocol') != PROTOCOL:
            raise RuntimeError('Browser bridge protocol mismatch; update the local host and reload the extension')
        if not status.get("connected"):
            raise RuntimeError(status.get('error') or "Connect the Saygo browser extension first")
        rid = uuid.uuid4().hex
        request = self.directory / (rid + ".request")
        response = self.directory / (rid + ".response")
        atomic_json(request, {"id": rid, "epoch": status["epoch"], "operation": operation,
                              "arguments": arguments, "deadline": time.time() + min(self.timeout - 1, 25)})
        duration = arguments.get('duration', 0) if operation == 'long_press' else 0
        extra = duration if isinstance(duration, (int, float)) and 0 < duration <= 30 else 0
        end = time.monotonic() + self.timeout + extra
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
        while True:
            try:
                incoming.put(read_frame(source))
            except ValueError as exc:
                incoming.put(exc)
            except (EOFError, OSError) as exc:
                incoming.put(exc)
                return

    threading.Thread(target=reader, daemon=True).start()
    status = directory / "status.json"
    info = {"connected": False, "epoch": epoch, "protocol": PROTOCOL, "host_version": VERSION}
    atomic_json(status, info)
    try:
        write_frame(sink, {'type':'hello', 'protocol':PROTOCOL, 'version':VERSION})
        while True:
            try:
                hello = incoming.get(timeout=10)
            except queue.Empty:
                info['error'] = 'Waiting for extension handshake'
                atomic_json(status, info)
                continue
            if isinstance(hello, (EOFError, OSError)):
                return
            if isinstance(hello, dict) and hello.get('type') == 'hello' and hello.get('protocol') == PROTOCOL:
                break
            info['error'] = 'Extension protocol mismatch; update the extension and local host'
            atomic_json(status, info)
        info.pop('error', None)
        info.update(connected=True, extension_version=hello.get('version'))
        atomic_json(status, info)
        while True:
            if not incoming.empty():
                item = incoming.get_nowait()
                if isinstance(item, (EOFError, OSError)):
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
                    duration = request.get('arguments', {}).get('duration', 0) if request['operation'] == 'long_press' else 0
                    extra = duration if isinstance(duration, (int, float)) and 0 < duration <= 30 else 0
                    end = time.monotonic() + RESPONSE_TIMEOUT + extra
                    while True:
                        remaining = end - time.monotonic()
                        if remaining <= 0:
                            raise queue.Empty
                        item = incoming.get(timeout=remaining)
                        if isinstance(item, (EOFError, OSError)):
                            atomic_json(response, {"error": "extension disconnected; action outcome may be unknown"})
                            return
                        if isinstance(item, Exception):
                            raise ValueError(str(item))
                        if item.get("id") != request["id"]:
                            # A timed-out request can finish later. Never let its
                            # chunks satisfy a new request or extend its timeout.
                            continue
                        chunks.append(item["chunk"])
                        if sum(map(len, chunks)) > MAX_RESPONSE_BYTES:
                            raise RuntimeError("extension response exceeds 32 MiB")
                        if item.get("last"):
                            atomic_json(response, json.loads("".join(chunks)))
                            break
                except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError) as exc:
                    path.unlink(missing_ok=True)
                    atomic_json(response, {"error": str(exc)})
                except queue.Empty:
                    error = "extension timeout; outcome unknown"
                    atomic_json(response, {"error": error})
                    info['last_timeout'] = {'request_id': request['id'],
                                            'operation': request['operation'],
                                            'error': error, 'at': time.time()}
                    atomic_json(status, info)
                    # Report uncertainty without replaying input or closing the
                    # transport. The caller must observe before another action.
            time.sleep(.05)
    except RuntimeError as exc:
        info['error'] = str(exc)
    finally:
        atomic_json(status, {**info, "connected": False})


def host_command(directory):
    if getattr(sys, 'frozen', False):
        executable = Path(sys.executable)
        if sys.platform == 'win32':
            executable = executable.with_name('SaygoNativeHost.exe')
            if not executable.is_file():
                raise FileNotFoundError('SaygoNativeHost.exe is missing; extract the complete desktop ZIP')
        return [str(executable), '--native-host', '--directory', str(directory)]
    return [sys.executable, str(Path(__file__).resolve()), 'host', '--directory', str(directory)]


def install(directory, extension_id, browser):
    if not re.fullmatch("[a-p]{32}", extension_id):
        raise ValueError("extension ID must be the 32-letter ID from chrome://extensions")
    directory = Path(directory).expanduser().resolve()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    script = str(Path(__file__).resolve())
    command = host_command(directory)
    if os.name == "nt":
        launcher = directory / "host.cmd"
        # cmd expands percent even inside quotes; reject paths that cannot be represented safely.
        if any(c in str(directory) + script + sys.executable for c in '%\r\n"'):
            raise ValueError("installation paths contain unsupported shell characters")
        launcher.write_text('@echo off\nchcp 65001 >nul\nsetlocal DisableDelayedExpansion\n' +
                            " ".join('"' + arg + '"' for arg in
                                     command) + '\n',
                            encoding="utf-8")
    else:
        import shlex
        launcher = directory / "host.sh"
        launcher.write_text("#!/bin/sh\nexec " + shlex.join(
            command) + "\n")
        launcher.chmod(0o700)
    manifest = {"name": HOST, "description": "Saygo existing browser bridge",
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


def uninstall(directory, browser):
    """Unregister only this installation; preserve bridge data and executables."""
    directory = Path(directory).expanduser().resolve()
    manifest = directory / (HOST + '.json')
    if os.name == 'nt':
        import winreg
        vendor = 'Google\\Chrome' if browser == 'chrome' else 'Microsoft\\Edge'
        key_path = rf'Software\{vendor}\NativeMessagingHosts\{HOST}'
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                current = winreg.QueryValueEx(key, '')[0]
            if Path(current).resolve() != manifest:
                raise RuntimeError('Native host registration belongs to another installation; left unchanged')
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_path)
        except FileNotFoundError:
            pass
    else:
        if sys.platform == 'darwin':
            root = Path.home() / 'Library/Application Support' / ('Google/Chrome' if browser == 'chrome' else 'Microsoft Edge')
        else:
            root = Path.home() / '.config' / ('google-chrome' if browser == 'chrome' else 'microsoft-edge')
        target = root / 'NativeMessagingHosts' / (HOST + '.json')
        if target.exists():
            current = json.loads(target.read_text())
            if Path(current['path']).parent.resolve() != directory:
                raise RuntimeError('Native host registration belongs to another installation; left unchanged')
            target.unlink()
    return {'unregistered':True, 'directory':str(directory), 'data_preserved':True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("host", "install", "uninstall"):
        p = sub.add_parser(command)
        p.add_argument("--directory", required=True)
        if command == "install":
            p.add_argument("--extension-id", required=True)
        if command != 'host':
            p.add_argument("--browser", choices=["chrome", "edge"], default="chrome")
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
    elif args.command == 'uninstall':
        print(json.dumps(uninstall(args.directory, args.browser)))


if __name__ == "__main__":
    main()
