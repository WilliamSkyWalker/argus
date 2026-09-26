"""Unified device entry points sharing persistent sessions with CLI and Runtime."""
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess

from argus.devices import mobile
from argus.platforms import device_session as ds


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
    result["browsers"]=[s for s in result["sessions"] if s.get("platform")=="browser"]
    return result


def _connect(args):
    serial=args.session
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}",serial):
        raise ValueError("session must contain letters, digits, dot, dash or underscore")
    kind=desktop_platform(args.platform)
    if getattr(args,"background",False) and kind != "windows":
        raise ValueError("--background currently requires Windows")
    if kind in {"android","ios"}:
        return mobile.connect(kind,args.device,serial,args.server_url,args.team_id)
    old=ds.load_state(serial)
    if old and (old.get("os") or old.get("kind")) != kind:
        raise ValueError("Session belongs to another platform; use a different --session")
    if kind in {"mac","windows"}:
        if not args.app: raise ValueError("Desktop connections require --app (window title on Windows, app name on macOS)")
        if old and old.get("app") != args.app:
            raise ValueError("Session belongs to another desktop app; use a different --session")
        if old and old.get("handoff"):
            return {"connected":False,"session":serial,**old["handoff"]}
        state={"kind":"desktop","os":kind,"app":args.app}
        if old and not getattr(args,"new_window",False):
            state.update({key:old[key] for key in ("process_id","process_name","launch","background") if key in old})
        if getattr(args,"new_window",False) and (not getattr(args,"launch",None) or not getattr(args,"new_window_args",None)):
            raise ValueError("--new-window requires --launch and explicit --new-window-arg values supported by the app")
        for key in ("launch", "process_name", "new_window", "new_window_args", "background"):
            value=getattr(args,key,None)
            if value: state[key]=value
        if kind != "windows" and any(state.get(k) for k in ("launch", "process_name", "new_window")):
            raise ValueError("Explicit desktop lifecycle options currently require Windows")
        from argus.platforms.desktop import DesktopHandoffRequired
        try:
            plat=ds.attach_desktop(state)
        except DesktopHandoffRequired as exc:
            state.pop("new_window",None); state.pop("new_window_args",None)
            state["handoff"]=exc.details
            ds.save_state(serial,state)
            return {"connected":False,"session":serial,**exc.details}
        try:
            size=list(plat.screen_size)
            connection=getattr(plat,"connection",{})
            if not isinstance(connection,dict): connection={}
            for key in ("process_id", "process_name", "launch"):
                if connection.get(key): state[key]=connection[key]
            # New-window intent is one-shot, never repeated by screenshots/actions.
            state.pop("new_window",None); state.pop("new_window_args",None)
            ds.save_state(serial,state)
            return {"connected":True,"session":serial,"platform":kind,"screen_size":size,
                    "connection":connection,"requires_observation":True}
        finally: ds.release_controller(plat, kind)
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
            if not args.bridge_directory:
                raise ValueError("Install the browser bridge first or provide --bridge-directory")
            from argus.integrations.browser_bridge import Client
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
        ds.release_controller(plat, "browser")


def _disconnect(serial):
    state=ds.load_state(serial)
    if state is None: raise ValueError("Unknown session")
    if state.get("kind") not in {"browser","desktop"}:
        ds.stop(serial)  # End Appium session, not the mobile device/emulator.
    state["disconnected"]=True
    ds.save_state(serial,state)  # Preserve browser endpoint and target for explicit reconnect.
    return {"session":serial,"disconnected":True}


def connect(args):
    from argus.runtime.locking import resource_guard, session_keys
    state = ds.load_state(args.session) or {}
    if state.get("handoff"):
        return {"connected": False, "session": args.session, **state["handoff"]}
    keys = session_keys(args.session, state)
    if args.platform == 'browser' and (args.backend or state.get('browser_backend')) == 'extension' and not args.bridge_directory:
        default = ds.STATE_DIR.parent / 'browser-bridge.json'
        if not state and default.is_file():
            args.bridge_directory = json.loads(default.read_text())['directory']
    if args.platform in {"desktop", "windows", "mac"}:
        keys.append("desktop:local")
    if getattr(args, "device", None):
        keys.append("mobile:" + args.device)
    if getattr(args, "bridge_directory", None):
        keys.append("browser-extension:" + str(Path(args.bridge_directory).expanduser().resolve()))
    with resource_guard(keys):
        current = ds.load_state(args.session) or {}
        if current.get("handoff"):
            return {"connected": False, "session": args.session, **current["handoff"]}
        return _connect(args)


def disconnect(serial):
    from argus.runtime.locking import resource_guard, session_keys
    with resource_guard(session_keys(serial)):
        return _disconnect(serial)


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
    p.add_argument("--background",action="store_true",help="Windows background window messages and capture; never use global input")
    p.add_argument("--process",dest="process_name",help="Windows executable process name")
    p.add_argument("--launch",help="Windows executable; launched only if no matching process exists")
    p.add_argument("--new-window",action="store_true",help="Explicitly request another window")
    p.add_argument("--new-window-arg",dest="new_window_args",action="append",default=[],
                   help="App-specific argument, e.g. --new-window-arg=--new-window")
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
    p=sub.add_parser("handoff",help="Pause any session for login or other user interaction")
    p.add_argument("--session",required=True)
    p.add_argument("--reason",default="login")
    p.add_argument("--instructions",required=True)
    p=sub.add_parser("resume",help="Return a session to observation after user interaction")
    p.add_argument("--session",required=True)
    p.add_argument("--note",required=True)
    mobile.register_install_boot(sub)
    from .network import register as register_network
    register_network(sub)


def _handoff(args):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}",args.session): raise ValueError("Invalid session alias")
    state=ds.load_state(args.session)
    if not state:
        raise ValueError("Known session required")
    if not args.instructions.strip(): raise ValueError("Handoff instructions cannot be empty")
    state["handoff"]={"status":"waiting_for_human","reason":args.reason,"instructions":args.instructions}
    ds.save_state(args.session,state)
    return {"session":args.session,**state["handoff"]}


def _resume(args):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}",args.session): raise ValueError("Invalid session alias")
    state=ds.load_state(args.session)
    if not state or not state.get("handoff"): raise ValueError("Session is not waiting for human interaction")
    if not args.note.strip(): raise ValueError("A completion note is required")
    from argus.platforms.desktop import DesktopHandoffRequired
    from argus.devices import mobile
    candidate=dict(state); candidate.pop("handoff")
    plat=None
    try:
        if candidate.get("kind") == "desktop":
            plat=ds.attach_desktop(candidate, serial=args.session)
        elif candidate.get("kind") == "browser":
            plat=ds.attach_browser(args.session, state=candidate)
        else:
            ds.save_state(args.session,candidate)
            try:
                plat=ds.attach(args.session)
                if plat is None: raise RuntimeError("Session expired; reconnect required")
            finally:
                ds.save_state(args.session,state)
        # A fresh screenshot is required before returning control, not a login-success claim.
        from .observations import capture
        observation = capture(plat, args.session)
        candidate["resume_note"]=args.note
        if candidate.get("kind") == "browser" and hasattr(plat, "page_id"):
            candidate["page_id"] = plat.page_id
        ds.save_state(args.session,candidate)
        return {"session":args.session,"status":"needs_observation", "path":observation["path"],
                "observation":observation,"requires_observation":True}
    except DesktopHandoffRequired as exc:
        return {"session":args.session,**exc.details}
    finally: ds.release_controller(plat, state.get("os") or state.get("kind"))


def _human_event(args, function):
    from argus.runtime.locking import resource_guard, session_keys, reserve, release
    from .service import _record
    import uuid
    owner = "human:" + args.session
    keys = session_keys(args.session)
    with resource_guard(keys, owner=owner):
        if function is _handoff:
            reserve(owner, keys)
        try:
            result = function(args)
        except Exception:
            if function is _handoff:
                release(owner)
            raise
        if function is _resume and result.get("status") == "needs_observation":
            release(owner)
        root = ds.STATE_DIR.parent / "operations"
        root.mkdir(parents=True, exist_ok=True)
        _record(root / (uuid.uuid4().hex + ".jsonl"), function.__name__.lstrip("_"), result)
        return result


def handoff(args):
    return _human_event(args, _handoff)


def resume(args):
    return _human_event(args, _resume)


def dispatch(args):
    command=args.device_command
    try:
        if command in {"install","boot"}:
            mobile.dispatch(args)
            return
        if command=="handoff": result=handoff(args)
        elif command=="resume": result=resume(args)
        elif command=="network":
            from .network import execute
            result=execute(args)
        elif command=="list": result=discover(args.platform)
        elif command=="sessions": result={"sessions":sessions()}
        elif command=="connect": result=connect(args)
        else: result=disconnect(args.session)
        print(json.dumps(result,ensure_ascii=False))
    except (OSError,ValueError,RuntimeError,subprocess.SubprocessError) as exc:
        print(json.dumps({"ok":False,"error":str(exc)},ensure_ascii=False))
        raise SystemExit(2) from exc
