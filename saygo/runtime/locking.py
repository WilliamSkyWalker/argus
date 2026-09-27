"""Global resource ownership and OS locks shared by all execution entrances."""
from contextlib import contextmanager, ExitStack
import hashlib
from saygo.platforms import device_session as ds
from .store import Store, BusyError


def registry():
    return Store(ds.STATE_DIR.parent / "control-locks")


def reserve(owner, keys):
    registry().reserve(owner, keys)


def release(owner):
    with registry().connect() as db:
        db.execute("DELETE FROM resources WHERE run_id=?", (owner,))


@contextmanager
def resource_guard(keys, owner=None):
    locks = registry()
    keys = sorted(set(keys))
    with ExitStack() as stack:
        for key in keys:
            stack.enter_context(locks.guard(hashlib.sha256(key.encode()).hexdigest()[:32]))
        with locks.connect() as db:
            for key in keys:
                row = db.execute("SELECT run_id FROM resources WHERE key=?", (key,)).fetchone()
                if row and row[0] != owner:
                    if row[0].startswith("human:"):
                        raise BusyError(f"resource {key} is waiting for human control; resume session {row[0][6:]}")
                    raise BusyError(f"resource {key} is owned by task/workflow {row[0]}; use that task or finish/cancel it first")
        yield


def session_keys(session, state=None):
    state = state if state is not None else ds.load_state(session) or {}
    keys = ["device-session:" + ds._key(session)]
    if state.get("kind") == "desktop":
        keys.append("desktop:local")
    if state.get("device_id"):
        keys.append("mobile:" + state["device_id"])
    if state.get("browser_backend") == "extension":
        from pathlib import Path
        keys.append("browser-extension:" + str(Path(state["bridge_directory"]).resolve()))
    if state.get("debugger_address"):
        keys.append("browser:" + state["debugger_address"])
    return keys
