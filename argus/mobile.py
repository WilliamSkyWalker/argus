"""Local mobile discovery, Appium attachment and simulator provisioning.

Device actions stay in Appium. adb/simctl here are discovery and lifecycle tools.
"""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

from . import toolchain


def home():
    return Path(os.environ.get("ARGUS_HOME_DIR", Path.home() / ".argus")) / "runtime"


def sdk_root():
    configured = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
    if configured:
        return Path(configured).expanduser()
    candidates = [home()/"android-sdk", Path.home()/"Library/Android/sdk", Path.home()/"Android/Sdk"]
    if os.name == "nt":
        candidates.append(Path(os.environ.get("LOCALAPPDATA", Path.home()))/"Android/Sdk")
    return next((p for p in candidates if p.is_dir()), candidates[0])


def executable(name):
    return name + (".exe" if os.name == "nt" else "")


def environment():
    env = os.environ.copy()
    root = sdk_root()
    env.update(ANDROID_HOME=str(root), ANDROID_SDK_ROOT=str(root))
    jdk = home()/"jdk"
    if (jdk/"bin"/executable("java")).is_file():
        env["JAVA_HOME"] = str(jdk)
    paths = [str(root/"platform-tools"), str(root/"emulator")]
    if env.get("JAVA_HOME"):
        paths.append(str(Path(env["JAVA_HOME"])/"bin"))
    env["PATH"] = os.pathsep.join(paths + [env.get("PATH", "")])
    return env


def command(args):
    args = list(map(str, args))
    if os.name == "nt" and args[0].lower().endswith((".bat", ".cmd")):
        if any(any(c in a for c in '%!"\r\n') for a in args):
            raise ValueError("Unsupported characters in Windows SDK command arguments")
        # Pass a raw Windows command line: list2cmdline would backslash-escape
        # the /c payload's quotes, which cmd.exe does not interpret as escapes.
        prefix = subprocess.list2cmdline([os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c"])
        return prefix + ' "' + " ".join('"' + a + '"' for a in args) + '"'
    return args


def run(args, *, timeout=30, input=None, progress=False):
    if progress:
        process = subprocess.Popen(command(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   stdin=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                                   env=environment(), **toolchain.background_options())
        started = time.monotonic()
        try:
            while True:
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(args, timeout)
                try:
                    stdout, stderr = process.communicate(input=input, timeout=min(15, remaining))
                    result = subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
                    break
                except subprocess.TimeoutExpired:
                    input = None  # communicate continues buffering; never send input twice.
                    print(f"Waiting for {Path(str(args[0])).name} ({int(time.monotonic()-started)}s)",
                          file=sys.stderr, flush=True)
        except BaseException:
            process.kill()
            process.communicate()
            raise
    else:
        result = subprocess.run(command(args), capture_output=True, text=True,
                                encoding="utf-8", errors="replace", env=environment(),
                                timeout=timeout, input=input, **toolchain.background_options())
    if result.returncode:
        raise RuntimeError(f"{Path(str(args[0])).name} failed ({result.returncode}): " +
                           (result.stderr or result.stdout)[-3000:])
    return result.stdout.strip()


def adb():
    candidates = [sdk_root()/"platform-tools"/executable("adb")]
    other = toolchain.detect_adb()
    if other:
        candidates.append(Path(other))
    return str(next((p for p in candidates if p.is_file()), executable("adb")))


def android_devices():
    rows = []
    for line in run([adb(), "devices", "-l"]).splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[0] in {"List", "*"}:
            continue
        serial, state = parts[:2]
        info = dict(p.split(":", 1) for p in parts[2:] if ":" in p)
        rows.append({"platform":"android", "id":serial, "name":info.get("model",serial),
                     "type":"emulator" if serial.startswith("emulator-") else "physical",
                     "state":state, "connectable":state == "device"})
    return rows


def require_mac():
    if platform.system() != "Darwin":
        raise RuntimeError("Local iOS devices/simulators require macOS with full Xcode. On Windows/WSL/Linux use --server-url to an Appium server on a Mac")
    version = run(["xcodebuild", "-version"])
    if "Xcode" not in version:
        raise RuntimeError("Install full Xcode and select it with xcode-select; Command Line Tools alone are insufficient")


def ios_simulators():
    raw = json.loads(run(["xcrun", "simctl", "list", "devices", "available", "--json"]))
    return [{"platform":"ios", "id":d["udid"], "name":d["name"], "type":"simulator",
             "state":d["state"], "runtime":runtime, "connectable":d["state"] == "Booted"}
            for runtime, devices in raw["devices"].items() if ".iOS-" in runtime
            for d in devices if d.get("isAvailable", True)]


def ios_physical():
    with tempfile.TemporaryDirectory() as tmp:
        output = Path(tmp)/"devices.json"
        run(["xcrun", "devicectl", "list", "devices", "--json-output", output])
        raw = json.loads(output.read_text())
    rows = []
    for d in raw.get("result", {}).get("devices", []):
        hardware = d.get("hardwareProperties", {})
        details = d.get("deviceProperties", {})
        connection = d.get("connectionProperties", {})
        udid = hardware.get("udid")
        if not udid or hardware.get("platform") not in {"iOS", "iPadOS"}:
            continue
        paired = connection.get("pairingState") == "paired"
        online = connection.get("tunnelState") == "connected"
        rows.append({"platform":"ios", "id":udid, "name":details.get("name",udid),
                     "type":"physical", "state":"connected" if online else "unavailable",
                     "paired":paired, "connectable":online and paired,
                     "developer_mode":details.get("developerModeStatus", "unknown")})
    return rows


def discover(which="all"):
    devices, diagnostics = [], []
    sources = []
    if which in {"all", "android"}:
        sources.append(("android", android_devices))
    if which in {"all", "ios"}:
        if platform.system() == "Darwin":
            sources += [("ios_simulators",ios_simulators),("ios_physical",ios_physical)]
        else:
            diagnostics.append({"source":"ios","message":"Local iOS discovery requires macOS/Xcode; a remote Mac Appium server can be used"})
    for source, fn in sources:
        try:
            devices.extend(fn())
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
            diagnostics.append({"source":source,"message":str(exc)})
    avds = []
    emulator = sdk_root()/"emulator"/executable("emulator")
    if which in {"all", "android"} and emulator.is_file():
        try:
            avds = run([emulator,"-list-avds"]).splitlines()
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            diagnostics.append({"source":"android_avds","message":str(exc)})
    return {"devices":devices,"android_avds":avds,"diagnostics":diagnostics}


def connect(which, device=None, session=None, server_url=None, team_id=None, adb_port=None):
    from .platforms import device_session as ds
    if not server_url:
        if which == "ios":
            require_mac()
        result = discover(which)
        choices = [d for d in result["devices"] if d["connectable"] and (not device or d["id"] == device)]
        if len(choices) != 1:
            raise RuntimeError("Select exactly one online device with --device; boot a simulator or authorize/unlock the phone first. Discovery: " + json.dumps(result))
        device = choices[0]["id"]
    elif not device:
        raise ValueError("--device is required with a remote Appium server")
    serial = session or device
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", serial):
        raise ValueError("Use --session with a safe session name")
    cfg = {"device":device, "os":which}
    if adb_port is not None:
        cfg["adb_port"] = adb_port
    if server_url:
        cfg.update(server_url=server_url,auto_start=False)
    if team_id or os.environ.get("IOS_TEAM_ID"):
        cfg["team_id"] = team_id or os.environ["IOS_TEAM_ID"]
    if os.environ.get("IOS_WDA_BUNDLE_ID"):
        cfg["wda_bundle_id"] = os.environ["IOS_WDA_BUNDLE_ID"]
    expected_server = server_url or os.environ.get("APPIUM_SERVER_URL", "http://127.0.0.1:4723")
    existing = ds.load_state(serial)
    if existing and (existing.get("os") != which or existing.get("device_id",existing.get("serial")) != device or
                     existing.get("server_url", "").rstrip("/") != expected_server.rstrip("/")):
        raise ValueError("Session belongs to a different device/server; use another --session")
    plat = ds.start(serial, which, appium_config=cfg)
    return {"connected":True,"platform":which,"device":device,"session":serial,
            "screen_size":list(plat.screen_size)}


# Pinned official command-line archives and SHA-256 from developer.android.com/studio.
CMD_TOOLS = {
    "linux":("linux", "4e4c464f145a7512b57d088ac6c278c03c9eea610886b35a5e0804e74eedf583"),
    "windows":("win", "90ae805d20434428bffcb699c290860f19bb5f66a67e6b330067e3de801fb04a"),
    "mac-x64":("mac_x86_64", "c5a6378ab5cf7e0d5701921405115befff13e9ff7417fb588389338f8bd050f3"),
    "mac-arm64":("mac_arm64", "835b62a26162b229b441d1f6d4680383815a270809eb33522c0d480fa5002c4e")}


def fetch(url, timeout=30):
    """Small vendor metadata, with the same proxy fallback as archive downloads."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        curl = shutil.which("curl.exe" if os.name == "nt" else "curl")
        if exc.code != 403 or not curl:
            raise
        return subprocess.run([curl, "--fail", "--silent", "--show-error", "--location",
                               "--max-time", str(timeout), url], check=True,
                              stdout=subprocess.PIPE, **toolchain.background_options()).stdout


def download(url, target, sha256):
    print(f"Downloading {url}",file=sys.stderr,flush=True)
    try:
        with urllib.request.urlopen(url,timeout=60) as response, open(target,"wb") as out:
            downloaded = 0
            last = time.monotonic()
            while chunk := response.read(1024 * 1024):
                out.write(chunk)
                downloaded += len(chunk)
                if time.monotonic() - last >= 5:
                    print(f"  {downloaded // (1024 * 1024)} MiB downloaded", file=sys.stderr, flush=True)
                    last = time.monotonic()
    except urllib.error.HTTPError as exc:
        # Some corporate proxies reject urllib but support the system curl client.
        curl = shutil.which("curl.exe" if os.name == "nt" else "curl")
        if exc.code != 403 or not curl:
            raise
        subprocess.run([curl, "--fail", "--location", "--max-time", "600", "--output", str(target), url], check=True,
                       **toolchain.background_options())
    with open(target,"rb") as stream:
        digest=hashlib.file_digest(stream,"sha256").hexdigest()
    if digest != sha256:
        raise RuntimeError("Download checksum mismatch: " + url)


def unpack(archive, dest):
    dest=Path(dest).resolve()
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as z:
            for info in z.infolist():
                if not (dest/info.filename).resolve().is_relative_to(dest):
                    raise ValueError("Unsafe archive path")
                z.extract(info,dest)
                if os.name != "nt" and not info.is_dir():
                    mode=(info.external_attr >> 16) & 0o777
                    if mode:
                        (dest/info.filename).chmod(mode)
    else:
        with tarfile.open(archive) as t:
            t.extractall(dest,filter="data")


def ensure_java():
    env=environment()
    java=shutil.which(executable("java"),path=env["PATH"])
    if java:
        version=subprocess.run([java,"-version"],capture_output=True,text=True,timeout=10,
                               **toolchain.background_options())
        match=re.search(r'version "(\d+)',version.stderr+version.stdout)
        if version.returncode==0 and match and int(match[1])>=17:
            return
    host={"Darwin":"mac","Linux":"linux","Windows":"windows"}[platform.system()]
    arch="aarch64" if platform.machine().lower() in {"arm64","aarch64"} else "x64"
    url=f"https://api.adoptium.net/v3/assets/latest/21/hotspot?architecture={arch}&image_type=jdk&os={host}"
    package=json.loads(fetch(url))[0]["binary"]["package"]
    with tempfile.TemporaryDirectory() as tmp:
        archive=Path(tmp)/"jdk.archive"
        download(package["link"],archive,package["checksum"])
        unpack(archive,Path(tmp)/"unpacked")
        roots=list((Path(tmp)/"unpacked").iterdir())
        source=roots[0]/"Contents/Home" if host == "mac" else roots[0]
        target=home()/"jdk"
        target.parent.mkdir(parents=True,exist_ok=True)
        if target.exists():
            raise RuntimeError("Incomplete private JDK exists; inspect " + str(target))
        shutil.move(str(source),target)


def sdk_tool(name):
    binary=sdk_root()/"cmdline-tools/latest/bin"/(name+(".bat" if os.name == "nt" else ""))
    if not binary.is_file():
        raise RuntimeError("SDK command-line tools missing; run argus device install --platform android")
    return str(binary)


def install_android(name, api=35, accept_licenses=False):
    safe_name(name)
    if not 30 <= api <= 99:
        raise ValueError("Installer supports Android API 30 or newer (up to 99)")
    system=platform.system()
    arm=platform.machine().lower() in {"arm64","aarch64"}
    if system not in {"Linux","Darwin","Windows"} or (arm and system != "Darwin"):
        raise RuntimeError("Android emulator installer supports x64 Linux/Windows and Intel/Apple silicon macOS")
    root=sdk_root()
    ensure_java()
    binary=root/"cmdline-tools/latest/bin"/("sdkmanager.bat" if os.name == "nt" else "sdkmanager")
    if not binary.is_file():
        key="mac-arm64" if arm else "mac-x64"
        if system != "Darwin": key=system.lower()
        slug,digest=CMD_TOOLS[key]
        with tempfile.TemporaryDirectory() as tmp:
            archive=Path(tmp)/"tools.zip"
            download(f"https://dl.google.com/android/repository/commandlinetools-{slug}-15859902_latest.zip",archive,digest)
            unpack(archive,Path(tmp)/"unpacked")
            target=root/"cmdline-tools/latest"
            target.parent.mkdir(parents=True,exist_ok=True)
            if target.exists():
                raise RuntimeError("Incomplete command-line tools directory exists: " + str(target))
            shutil.move(str(Path(tmp)/"unpacked/cmdline-tools"),target)
    licenses=[sdk_tool("sdkmanager"),f"--sdk_root={root}","--licenses"]
    if accept_licenses:
        run(licenses,timeout=600,input="y\n"*200)
    else:
        # Preserve the vendor's interactive license prompts.
        subprocess.run(command(licenses),env=environment(),check=True)
    abi="arm64-v8a" if arm else "x86_64"
    image=f"system-images;android-{api};google_apis;{abi}"
    print("Installing emulator and system image (may take several minutes)",file=sys.stderr,flush=True)
    run([sdk_tool("sdkmanager"),f"--sdk_root={root}","platform-tools","emulator",f"platforms;android-{api}","build-tools;35.0.0",image],timeout=3600,input="y\n"*200 if accept_licenses else "n\n",progress=True)
    existing=run([root/"emulator"/executable("emulator"),"-list-avds"]).splitlines()
    if name in existing:
        avd_home=Path(os.environ.get("ANDROID_AVD_HOME", str(Path(os.environ.get("ANDROID_USER_HOME",Path.home()/".android"))/"avd")))
        import configparser
        ini=configparser.ConfigParser()
        ini.read_string("[avd]\n"+(avd_home/(name+".ini")).read_text())
        config=Path(ini["avd"]["path"])/"config.ini"
        ini.read_string("[image]\n"+config.read_text())
        actual=ini["image"].get("image.sysdir.1", "").replace("\\", "/").rstrip("/")
        if not actual.endswith(image.replace(";", "/")):
            raise RuntimeError("Existing AVD uses another system image; choose a new --name (existing AVD preserved)")
    if name not in existing:
        run([sdk_tool("avdmanager"),"create","avd","-n",name,"-k",image],timeout=180,input="no\n")
    return {"platform":"android","name":name,"sdk_root":str(root),"image":image,"created":name not in existing}


def safe_name(name):
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}",name):
        raise ValueError("Simulator name must start with a letter and contain only letters, digits, _ or -")


def install_ios(name):
    safe_name(name)
    require_mac()
    print("Downloading iOS Simulator runtime through Xcode",file=sys.stderr,flush=True)
    run(["xcodebuild","-runFirstLaunch"],timeout=600)
    run(["xcodebuild","-downloadPlatform","iOS"],timeout=7200)
    runtimes=json.loads(run(["xcrun","simctl","list","runtimes","--json"]))["runtimes"]
    candidates=[r for r in runtimes if r.get("isAvailable") and r.get("identifier", "").startswith("com.apple.CoreSimulator.SimRuntime.iOS-")]
    if not candidates:
        raise RuntimeError("Xcode did not install an available iOS runtime")
    runtime=max(candidates,key=lambda r:tuple(int(x) for x in r["version"].split(".")))
    matches=[d for d in ios_simulators() if d["name"] == name and d["runtime"] == runtime["identifier"]]
    if len(matches)>1:
        raise RuntimeError("Multiple matching simulators; choose an existing UDID with argus device boot")
    if matches:
        udid=matches[0]["id"]
    else:
        types=runtime.get("supportedDeviceTypes",[])
        phones=[t for t in types if t.get("productFamily") == "iPhone" or t.get("name", "").startswith("iPhone")]
        if not phones:
            raise RuntimeError("Runtime did not advertise a supported iPhone device type")
        udid=run(["xcrun","simctl","create",name,phones[-1]["identifier"],runtime["identifier"]])
    return {"platform":"ios","name":name,"device":udid,"runtime":runtime["identifier"],"created":not bool(matches)}


def boot_device(which, device, timeout=240, headless=False):
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    if which == "ios":
        require_mac()
        rows=[d for d in ios_simulators() if d["id"] == device or d["name"] == device]
        if len(rows)!=1:
            raise ValueError("Choose an unambiguous simulator name or UDID")
        udid=rows[0]["id"]
        if rows[0]["state"] != "Booted":
            run(["xcrun","simctl","boot",udid],timeout=120)
        run(["xcrun","simctl","bootstatus",udid,"-b"],timeout=timeout)
        if not headless:
            run(["open","-a","Simulator"])
        return {"platform":which,"device":udid,"state":"ready"}
    safe_name(device)
    emulator=sdk_root()/"emulator"/executable("emulator")
    if device not in run([emulator,"-list-avds"]).splitlines():
        raise ValueError("AVD missing; run argus device install first")
    # Reuse the exact running AVD, never another emulator.
    serial=None
    # A newly started adb server needs a moment to discover existing emulators.
    # Do not race that discovery by launching the same AVD a second time.
    for attempt in range(6):
        for row in android_devices():
            if row["type"]=="emulator" and row["connectable"]:
                avd=run([adb(),"-s",row["id"],"emu","avd","name"]).splitlines()[0]
                if avd == device:
                    serial=row["id"]
                    break
        if serial is not None:
            break
        if attempt < 5:
            time.sleep(1)
    proc=None
    if serial is None:
        run([emulator,"-accel-check"])
        used={r["id"] for r in android_devices()}
        port=next((p for p in range(5554,5683,2) if f"emulator-{p}" not in used),None)
        if port is None:
            raise RuntimeError("No emulator console port available")
        serial=f"emulator-{port}"
        log=home()/f"emulator-{device}.log"
        log.parent.mkdir(parents=True,exist_ok=True)
        with log.open("ab") as stream:
            proc=subprocess.Popen([str(emulator),"-avd",device,"-port",str(port),
                "-no-metrics","-crash-report-mode","disabled",*( ["-no-window"] if headless else [])],
                env=environment(),stdout=stream,stderr=stream,stdin=subprocess.DEVNULL,
                start_new_session=os.name!="nt")
    deadline=time.monotonic()+timeout
    last_progress=deadline-timeout
    while time.monotonic()<deadline:
        if time.monotonic()-last_progress >= 15:
            print(f"Waiting for Android boot completion: {serial}",file=sys.stderr,flush=True)
            last_progress=time.monotonic()
        if proc and proc.poll() is not None:
            raise RuntimeError("Emulator exited; inspect " + str(log))
        try:
            if run([adb(),"-s",serial,"shell","getprop","sys.boot_completed"],timeout=5)=="1":
                actual=run([adb(),"-s",serial,"emu","avd","name"]).splitlines()[0]
                if actual != device:
                    raise ValueError("Emulator port belongs to another AVD; refused to attach")
                return {"platform":which,"device":serial,"name":device,"state":"ready"}
        except (OSError,RuntimeError,subprocess.TimeoutExpired):
            pass
        time.sleep(2)
    raise RuntimeError(f"Boot timed out for {serial}; emulator left running for inspection")


def register_install_boot(commands):
    p=commands.add_parser("install")
    p.add_argument("--platform",choices=["android","ios"],required=True)
    p.add_argument("--name",default="Argus")
    p.add_argument("--host",choices=["auto","local","windows"],default="auto")
    p.add_argument("--dry-run",action="store_true",help="Inspect the host and installation plan without installing")
    p.add_argument("--session",help="Session alias when using --connect")
    p.add_argument("--api",type=int,default=35)
    p.add_argument("--accept-licenses",action="store_true")
    p.add_argument("--boot",action="store_true")
    p.add_argument("--connect",action="store_true",help="Boot and connect through Appium after installation")
    p.add_argument("--headless",action="store_true")
    p=commands.add_parser("boot")
    p.add_argument("--platform",choices=["android","ios"],required=True)
    p.add_argument("device",help="Android AVD name or iOS simulator name/UDID")
    p.add_argument("--host",choices=["auto","local","windows"],default="auto")
    p.add_argument("--connect",action="store_true")
    p.add_argument("--session",help="Session alias when using --connect")
    p.add_argument("--headless",action="store_true")
    p.add_argument("--timeout",type=int,default=240)


def provision(args):
    """Native-host install after preflight and license confirmation."""
    result=install_android(args.name,args.api,True) if args.platform=="android" else install_ios(args.name)
    with contextlib.redirect_stdout(sys.stderr):
        node=toolchain.ensure_node()
        appium=toolchain.install_appium(node)
        toolchain.install_drivers(node,appium,ios=args.platform=="ios")
    if args.boot or args.connect:
        result["boot"]=boot_device(args.platform,result.get("device",args.name),headless=args.headless)
        if args.connect:
            result["connection"]=connect(args.platform,result["boot"]["device"],args.session)
    return result


def dispatch(args):
    from .mobile_host import execute
    try:
        print(json.dumps(execute(args),ensure_ascii=False))
    except Exception as exc:
        # CLI boundary also normalizes Appium/Selenium connection failures.
        print(json.dumps({"error":str(exc)},ensure_ascii=False))
        raise SystemExit(2) from exc
