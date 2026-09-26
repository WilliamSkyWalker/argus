"""Unified device entry points sharing persistent sessions with CLI and Runtime."""
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess

from . import mobile
from .platforms import device_session as ds


def desktop_platform(value):
    if value != "desktop":
        return value
    system = platform.system()
    if system == "Darwin": return "mac"
    if system == "Windows" or "microsoft" in platform.release().lower(): return "windows"
    raise ValueError("Desktop control currently supports macOS and Windows (including WSL)")


def sessions():
    rows=[]
    for path in sorted(ds.STATE_DIR.glob("*.json")):
        try:
            state=json.loads(path.read_text())
            kind=state.get("os") or state.get("kind")
            rows.append({"session":path.stem,"platform":kind,
                         "backend":state.get("browser_backend"),"device":state.get("device_id"),
                         "app":state.get("app"),"page_id":state.get("page_id"),
                         "disconnected":state.get("disconnected",False)})
        except (OSError,ValueError) as exc:
            rows.append({"session":path.stem,"error":str(exc)})
    return rows


def desktop_windows():
    kind=desktop_platform("desktop")
    if kind == "windows":
        shell=shutil.which("powershell.exe")
        if not shell: raise RuntimeError("PowerShell is unavailable")
        script='[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; @(Get-Process | Where-Object MainWindowTitle | Select-Object ProcessName,MainWindowTitle,Id) | ConvertTo-Json -Compress'
        result=subprocess.run([shell,"-NoProfile","-Command",script],capture_output=True,text=True,encoding="utf-8",timeout=15,check=True)
        data=json.loads(result.stdout or "[]")
        if isinstance(data,dict): data=[data]
        return [{"platform":"windows","app":d["MainWindowTitle"],"process":d["ProcessName"],"pid":d["Id"]} for d in data]
    import Quartz
    windows=Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly,Quartz.kCGNullWindowID)
    return [{"platform":"mac","app":w.get(Quartz.kCGWindowOwnerName),"title":w.get(Quartz.kCGWindowName,""),"pid":w.get(Quartz.kCGWindowOwnerPID)}
            for w in windows if w.get(Quartz.kCGWindowLayer)==0 and w.get(Quartz.kCGWindowOwnerName)]


def discover(which):
    result={"devices":[],"windows":[],"sessions":sessions(),"diagnostics":[]}
    if which in {"all","android","ios"}:
        result.update(mobile.discover(which if which!="all" else "all"))
    if which in {"all","desktop","windows","mac"}:
        try:
            result["windows"]=desktop_windows()
            if which in {"windows","mac"}:
                result["windows"]=[w for w in result["windows"] if w["platform"]==which]
        except (ImportError,OSError,ValueError,RuntimeError,subprocess.SubprocessError) as exc:
            result["diagnostics"].append({"source":"desktop","message":str(exc)})
    # Browser discovery lists saved bindings, without reconnecting or changing focus.
    result["browsers"]=[s for s in result["sessions"] if s["platform"]=="browser"]
    return result


def connect(args):
    serial=args.session
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}",serial):
        raise ValueError("session must contain letters, digits, dot, dash or underscore")
    kind=desktop_platform(args.platform)
    if kind in {"android","ios"}:
        return mobile.connect(kind,args.device,serial,args.server_url,args.team_id)
    old=ds.load_state(serial)
    if old and (old.get("os") or old.get("kind")) != kind:
        raise ValueError("Session belongs to another platform; use a different --session")
    if kind in {"mac","windows"}:
        if not args.app: raise ValueError("Desktop connections require --app (window title on Windows, app name on macOS)")
        if old and old.get("app") != args.app:
            raise ValueError("Session belongs to another desktop app; use a different --session")
        state={"kind":"desktop","os":kind,"app":args.app}
        plat=ds.attach_desktop(state)
        try:
            size=list(plat.screen_size)
            ds.save_state(serial,state)
            return {"connected":True,"session":serial,"platform":kind,"screen_size":size}
        finally: plat.teardown()
    backend=args.backend or (old or {}).get("browser_backend","playwright")
    plat=None
    try:
        if old:
            if backend != old.get("browser_backend","selenium"):
                raise ValueError("Changing a session backend requires a new --session")
            if args.bridge_directory and str(Path(args.bridge_directory).expanduser().resolve()) != old.get("bridge_directory"):
                raise ValueError("Session belongs to another bridge; use a different --session")
            state=dict(old); state.pop("disconnected",None)
            ds.save_state(serial,state)
            plat=ds.attach_browser(serial,manage_pages=True)
        elif backend == "extension":
            if not args.bridge_directory: raise ValueError("Extension connections require --bridge-directory")
            from .browser_bridge import Client
            directory=str(Path(args.bridge_directory).expanduser().resolve())
            Client(directory).call("pages")
            ds.save_state(serial,{"kind":"browser","browser_backend":"extension","bridge_directory":directory})
            plat=ds.attach_browser(serial,manage_pages=True)
        else:
            plat=ds.start(serial,"browser",browser_backend=backend)
        if args.page_id:
            plat.select_page(args.page_id)
        result={"connected":True,"session":serial,"platform":"browser","backend":backend}
        if hasattr(plat,"list_pages"):
            result.update(pages=plat.list_pages(),page_id=plat.page_id)
        return result
    except Exception:
        if old: ds.save_state(serial,old)
        raise
    finally:
        release(plat)


def release(plat):
    if plat is None: return
    if hasattr(plat,"disconnect"): plat.disconnect()
    elif hasattr(plat,"_driver"):
        service=getattr(plat._driver,"service",None)
        if service: service.stop()


def disconnect(serial):
    state=ds.load_state(serial)
    if state is None: raise ValueError("Unknown session")
    if state.get("kind") not in {"browser","desktop"}:
        ds.stop(serial)  # End Appium session, not the mobile device/emulator.
    state["disconnected"]=True
    ds.save_state(serial,state)  # Preserve browser endpoint and target for explicit reconnect.
    return {"session":serial,"disconnected":True}


def register(sub):
    p=sub.add_parser("list",help="Discover phones, simulators, desktop windows and browser bindings")
    p.add_argument("--platform",choices=["all","android","ios","browser","desktop","windows","mac"],default="all")
    sub.add_parser("sessions",help="List saved sessions (does not probe liveness)")
    p=sub.add_parser("connect",help="Connect any platform to a named session")
    p.add_argument("--platform",choices=["android","ios","browser","desktop","windows","mac"],required=True)
    p.add_argument("--session","--serial",dest="session",required=True)
    p.add_argument("--device")
    p.add_argument("--server-url")
    p.add_argument("--team-id")
    p.add_argument("--app")
    p.add_argument("--backend",choices=["playwright","selenium","extension"])
    p.add_argument("--bridge-directory")
    p.add_argument("--page-id")
    p=sub.add_parser("disconnect",help="Detach a session, preserving reconnect configuration")
    p.add_argument("--session","--serial",dest="session",required=True)
    for name in ("open","scroll"):
        p=sub.add_parser(name)
        p.add_argument("--session","--serial",dest="serial",required=True)
        if name=="open": p.add_argument("target")
        else: p.add_argument("direction",choices=["up","down"])
    mobile.register_install_boot(sub)


def dispatch(args):
    command=args.device_command
    try:
        if command in {"install","boot"}:
            args.mobile_command=command
            mobile.dispatch(args)
            return
        if command=="list": result=discover(args.platform)
        elif command=="sessions": result={"sessions":sessions()}
        elif command=="connect": result=connect(args)
        else: result=disconnect(args.session)
        print(json.dumps(result,ensure_ascii=False))
    except (OSError,ValueError,RuntimeError,subprocess.SubprocessError) as exc:
        print(json.dumps({"ok":False,"error":str(exc)},ensure_ascii=False))
        raise SystemExit(2) from exc
