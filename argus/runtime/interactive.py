"""Incremental tasks using Runtime's resources, checkpoints and dispatch boundary."""
from contextlib import contextmanager
import copy
import json
from pathlib import Path
import uuid
import zipfile

from .engine import Runtime
from .resources import resource_key
from .locking import resource_guard, session_keys, reserve, release
from .store import Store


def default_store():
    import os
    from argus.platforms import device_session as ds
    return Store(os.environ.get("ARGUS_RUNTIME_DIR", str(ds.STATE_DIR.parent / "runtime")))


class InteractiveRuntime(Runtime):
    def create_task(self, bindings):
        from argus.platforms import device_session as ds
        if not isinstance(bindings, dict) or not bindings:
            raise ValueError("bindings must map resource names to connected session names")
        resources = {}
        for name, session in bindings.items():
            state = ds.load_state(session)
            if not state or state.get("disconnected"):
                raise ValueError(f"Session {session} is missing or disconnected")
            kind = state.get("os") or state.get("kind")
            resources[name] = {"kind": kind, "session": session}
            if kind == "browser":
                resources[name]["backend"] = state.get("browser_backend", "selenium")
                if state.get("page_id"):
                    resources[name]["page_id"] = state["page_id"]
        workflow = {"version": 1, "resources": resources,
                    "steps": [{"id": "initial", "kind": "observe", "resource": next(iter(resources))}]}
        # Reuse schema validation; incremental tasks have no preplanned steps.
        from .schema import validate
        workflow = validate(workflow)
        workflow["steps"] = []
        state = self.store.create(workflow, mode="interactive")
        state.update(mode="interactive", status="idle", requests={})
        keys = self._resource_keys(state)
        try:
            with resource_guard(keys, owner=state["id"]):
                reserve(state["id"], keys)
                self.store.save(state, "task_created", {"bindings": bindings})
        except Exception as exc:
            release(state["id"])
            state.update(status="failed", error=str(exc))
            self.store.save(state, "task_create_failed", {"error": str(exc)})
            raise
        return state

    @contextmanager
    def _task(self, run_id):
        with self.store.guard(run_id):
            state = self.store.get(run_id)
            if state.get("mode") != "interactive":
                raise ValueError("Use workflow commands for a planned workflow")
            keys = []
            for spec in state["workflow"]["resources"].values():
                keys.append(resource_key(spec))
                keys.extend(session_keys(spec["session"]))
            with resource_guard(keys, owner=run_id):
                if state["status"] not in {"succeeded", "cancelled", "failed"}:
                    reserve(run_id, keys)
                try:
                    yield state
                finally:
                    self._close()

    def observe(self, run_id, resource):
        with self._task(run_id) as state:
            if resource not in state["workflow"]["resources"]:
                raise ValueError("Unknown task resource")
            self._observe(state, resource)
            return state

    def submit(self, run_id, resource, action, observation_id, request_id, note=None):
        if not isinstance(request_id, str) or not request_id.strip():
            raise ValueError("request_id is required to prevent accidental duplicate input")
        with self._task(run_id) as state:
            request = {"resource": resource, "action": action, "observation_id": observation_id}
            previous = state["requests"].get(request_id)
            if previous:
                if previous["request"] != request:
                    raise ValueError("request_id was already used for different input")
                return state  # Never redispatch an accepted request, even after a crash.
            if state["status"] != "idle":
                raise ValueError(f"Task is {state['status']}; recover, resolve or resume before submitting")
            observation = state["observations"].get(resource)
            if not observation or observation["id"] != observation_id:
                raise ValueError("Use the latest observation ID of this resource")
            step = {"id": "step_" + uuid.uuid4().hex, "kind": "action", "resource": resource,
                    "action": copy.deepcopy(action), "observation": copy.deepcopy(observation)}
            state["workflow"]["steps"].append(step)
            state["requests"][request_id] = {"step": step["id"], "request": request}
            state["status"] = "running"
            self.store.save(state, "step_submitted", {"step": step["id"], "request_id": request_id})
            if note:
                self.store.save(state, "agent_annotation", {"step": step["id"], "note": note})
            try:
                self._action(state, step, {"steps": state["outputs"], "inputs": {}})
            except Exception as exc:
                state["error"] = f"{type(exc).__name__}: {exc}"
                if state["pending"]:
                    state["status"] = "needs_review"
                else:
                    state["cursor"] += 1
                    state["status"] = "idle"
                self.store.save(state, "action_failed", {"step": step["id"], "error": state["error"]})
                return state
            if state["status"] != "running":
                return state
            state["status"] = "idle"
            self.store.save(state, "awaiting_agent")
            return state

    def recover_task(self, run_id):
        with self._task(run_id) as state:
            if state["status"] in {"succeeded", "cancelled", "failed"}:
                release(run_id)
                return state
            if state["status"] == "running":
                state["status"] = "needs_review" if state["pending"] else "idle"
                # A preflight interruption is discarded, never automatically replayed.
                if not state["pending"]:
                    state["cursor"] = len(state["workflow"]["steps"])
                state["observations"] = {}
                self.store.save(state, "recovered", {"pending": state["pending"]})
            for name in state["workflow"]["resources"]:
                try:
                    self._observe(state, name)
                except Exception as exc:
                    self.store.save(state, "observation_failed", {"resource": name, "error": str(exc)})
            return state

    def resolve_task(self, run_id, outcome, note):
        if outcome not in {"completed", "not_executed"} or not isinstance(note, str) or not note.strip():
            raise ValueError("Resolve requires completed/not_executed and a note with evidence")
        with self._task(run_id) as state:
            if state["status"] != "needs_review" or not state["pending"]:
                raise ValueError("No uncertain input to resolve")
            pending = state["pending"]
            state.update(status="idle", pending=None, observations={}, error=None)
            state["cursor"] = len(state["workflow"]["steps"])
            self.store.save(state, "action_resolved", {"action_id": pending["id"], "outcome": outcome, "note": note})
            # not_executed also requires a new observation and a new explicit request.
            return state

    def handoff_task(self, run_id, instructions):
        if not isinstance(instructions, str) or not instructions.strip():
            raise ValueError("Human instructions required")
        from argus.platforms import device_session as ds
        with self._task(run_id) as state:
            if state["status"] != "idle":
                raise ValueError("Resolve/recover active input before handoff")
            state.update(status="waiting_for_human", human={"instructions": instructions}, observations={})
            self.store.save(state, "human_requested", state["human"])
            for spec in state["workflow"]["resources"].values():
                binding = ds.load_state(spec["session"])
                binding["handoff"] = {"status":"waiting_for_human", "instructions": instructions, "task": run_id}
                ds.save_state(spec["session"], binding)
            return state

    def resume_task(self, run_id, note):
        from argparse import Namespace
        from argus.devices.control import _resume
        if not isinstance(note, str) or not note.strip():
            raise ValueError("Completion note required")
        with self._task(run_id) as state:
            if state["status"] != "waiting_for_human":
                raise ValueError("Task is not waiting for human control")
            from argus.platforms import device_session as ds
            for spec in state["workflow"]["resources"].values():
                if (ds.load_state(spec["session"]) or {}).get("handoff"):
                    _resume(Namespace(session=spec["session"], note=note))
            state["observations"] = {}
            for name in state["workflow"]["resources"]:
                self._observe(state, name)
            state.update(status="idle", human=None)
            self.store.save(state, "human_returned", {"note": note, "business_success": None})
            return state

    def finish(self, run_id, note):
        with self._task(run_id) as state:
            if state["status"] != "idle" or not isinstance(note, str) or not note.strip():
                raise ValueError("Finish requires an idle task and result evidence in note")
            state["status"] = "succeeded"
            self.store.save(state, "agent_completed", {"note": note, "verified_by": "external_agent"}, release=True)
            release(run_id)
            return state

    def cancel_task(self, run_id, note):
        if not isinstance(note, str) or not note.strip():
            raise ValueError("Cancellation requires a note; uncertain actions remain in the history")
        with self._task(run_id) as state:
            from argus.platforms import device_session as ds
            # Cancellation stops the task; it does not undo input or certify an outcome.
            for spec in state["workflow"]["resources"].values():
                binding = ds.load_state(spec["session"]) or {}
                if binding.get("handoff", {}).get("task") == run_id:
                    binding.pop("handoff")
                    ds.save_state(spec["session"], binding)
            state["status"] = "cancelled"
            self.store.save(state, "task_cancelled", {"note": note, "unresolved_action": state["pending"]}, release=True)
            release(run_id)
            return state

    def timeline(self, run_id):
        rows = []
        for event in self.store.events(run_id):
            data = event["data"]
            row = {key: event[key] for key in ("seq", "at", "kind")}
            row.update({key: data[key] for key in ("step", "resource", "error", "note", "path", "outcome") if key in data})
            if event["kind"] == "action_dispatching":
                row["action"] = data["action"].get("type")
            rows.append(row)
        return {"task_id": run_id, "timeline": rows}

    def export(self, run_id, out):
        with self.store.guard(run_id):
            state = self.store.get(run_id)
            if state.get("mode") != "interactive":
                raise ValueError("Expected an interactive task")
            path = Path(out).expanduser().resolve()
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("task.json", json.dumps(state, ensure_ascii=False, indent=2))
                archive.writestr("events.json", json.dumps(self.store.events(run_id), ensure_ascii=False, indent=2))
                archive.writestr("timeline.json", json.dumps(self.timeline(run_id), ensure_ascii=False, indent=2))
                directory = self.store.root / "artifacts" / run_id
                if directory.exists():
                    for artifact in sorted(directory.glob("*.png")):
                        archive.write(artifact, "artifacts/" + artifact.name)
            return {"path": str(path)}


def call(command, task_id=None, **options):
    runtime = InteractiveRuntime(default_store())
    if command == "list":
        with runtime.store.connect() as db:
            states = [json.loads(row[0]) for row in db.execute("SELECT snapshot FROM runs")]
        return {"tasks": [{key: state.get(key) for key in ("id", "status", "cursor", "error", "created_at")}
                          for state in states if state.get("mode") == "interactive"]}
    if command == "create":
        return runtime.create_task(options["bindings"])
    if command == "status":
        return runtime.store.get(task_id)
    if command == "timeline":
        return runtime.timeline(task_id)
    if command == "events":
        return {"events": runtime.store.events(task_id)}
    if command == "observe":
        return runtime.observe(task_id, options["resource"])
    if command == "submit":
        return runtime.submit(task_id, **options)
    if command == "recover":
        return runtime.recover_task(task_id)
    if command == "resolve":
        return runtime.resolve_task(task_id, options["outcome"], options["note"])
    if command == "handoff":
        return runtime.handoff_task(task_id, options["instructions"])
    if command == "resume":
        return runtime.resume_task(task_id, options["note"])
    if command == "cancel":
        return runtime.cancel_task(task_id, options["note"])
    if command == "finish":
        return runtime.finish(task_id, options["note"])
    if command == "export":
        return runtime.export(task_id, options["out"])
    raise ValueError("Unknown task command")
