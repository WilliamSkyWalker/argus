"""Loopback-only WSL HTTP relay to Windows Appium via stdio (NAT or mirrored)."""
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
import urllib.request

from . import mobile


def relay_request(config, method, path, body):
    from .mobile_host import call_windows
    return call_windows(config["host"], "http", port=config["port"], base_path=config["base_path"],
                        method=method, path=path, body=base64.b64encode(body).decode() if body else None)


def handler(config):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(200)

        def log_message(self, *args):
            pass  # Session tokens and request bodies are not logged.

        def forward(self):
            prefix = "/" + config["token"]
            if not self.path.startswith(prefix + "/"):
                self.send_error(403)
                return
            path = self.path[len(prefix):]
            if path == "/__argus_health" and self.command == "GET":
                self.send_response(200)
                self.end_headers()
                self.wfile.write(config["identity"].encode())
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 <= size <= 32 * 1024**2 or self.headers.get("Transfer-Encoding"):
                    raise ValueError("Unsupported request body")
                result = relay_request(config, self.command, path, self.rfile.read(size))
                data = base64.b64decode(result["body"], validate=True)
                status = result["status"]
            except Exception as exc:
                status = 502
                data = json.dumps({"value": {"error": "unknown error", "message":
                    "Windows transport failed; action outcome may be unknown. No retry performed. " + str(exc)}}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = do_DELETE = do_PUT = forward
    return Handler


def healthy(state, identity):
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(state["url"] + "/__argus_health", timeout=2) as response:
            return response.read().decode() == identity
    except (OSError, ValueError, KeyError):
        return False


def ensure_relay(host, port, base_path):
    import fcntl
    root = mobile.home() / "windows-relay"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    # One relay per host; port+base path also identify the Windows server instance.
    identity = hashlib.sha256(json.dumps([host["home"], port, base_path]).encode()).hexdigest()
    state_path = root / (identity + ".json")
    with (root / "start.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        old = json.loads(state_path.read_text()) if state_path.exists() else {}
        if old and healthy(old, identity):
            return old["url"]
        config = {"host": host, "port": port, "base_path": base_path, "identity": identity,
                  "token": old.get("token", secrets.token_urlsafe(32)), "listen_port": old.get("listen_port", 0),
                  "state_path": str(state_path)}
        config_path = root / (identity + "-config.json")
        config_path.write_text(json.dumps(config))
        config_path.chmod(0o600)
        log_path = root / (identity + ".log")
        with log_path.open("ab") as output:
            process = subprocess.Popen([sys.executable, "-m", "argus.mobile_relay", str(config_path)],
                                       cwd=str(Path(__file__).resolve().parent.parent), stdin=subprocess.DEVNULL,
                                       stdout=output, stderr=output, start_new_session=True)
        for _ in range(50):
            if process.poll() is not None:
                raise RuntimeError("Windows relay exited; inspect " + str(log_path))
            if state_path.exists():
                state = json.loads(state_path.read_text())
                if healthy(state, identity):
                    return state["url"]
            time.sleep(.1)
        process.terminate()
        raise RuntimeError("Windows relay failed to become ready; inspect " + str(log_path))


def serve(config):
    server = ThreadingHTTPServer(("127.0.0.1", config["listen_port"]), handler(config))
    server.daemon_threads = True
    port = server.server_address[1]
    state = {"url": f"http://127.0.0.1:{port}/{config['token']}", "listen_port": port,
             "token": config["token"], "pid": os.getpid()}
    path = Path(config["state_path"])
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state))
    temporary.chmod(0o600)
    temporary.replace(path)
    server.serve_forever()


if __name__ == "__main__":
    serve(json.loads(Path(sys.argv[1]).read_text()))
