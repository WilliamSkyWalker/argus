"""Sequential durable workflow runtime. All mutations cross one dispatch boundary."""

from pathlib import Path
import time
import json
import copy
import uuid

from .resources import PreconditionError, make_resource, resource_key
from .schema import resolve, validate

TERMINAL = {"succeeded", "failed", "cancelled"}


class Runtime:
    def __init__(self, store, factory=make_resource):
        self.store = store
        self.factory = factory
        self.resources = {}

    def create(self, workflow, base_dir=None):
        workflow = validate(workflow)
        for spec in workflow["resources"].values():
            if spec["kind"] == "sqlite":
                path = Path(spec["path"]).expanduser()
                spec["path"] = str((Path(base_dir or ".") / path).resolve())
        return self.store.create(workflow)

    def _resource(self, state, name):
        if name not in self.resources:
            spec = copy.deepcopy(state["workflow"]["resources"][name])
            binding = state.get("bindings", {}).get(name)
            if binding:
                spec["page_id"] = binding["page_id"]
            self.resources[name] = self.factory(spec)
        return self.resources[name]

    def _close(self):
        for resource in self.resources.values():
            try:
                resource.close()
            except Exception:
                pass
        self.resources = {}

    def _observe(self, state, name):
        png, metadata = self._resource(state, name).observe()
        oid = uuid.uuid4().hex
        artifact_dir = self.store.root / "artifacts" / state["id"]
        artifact_dir.mkdir(parents=True, exist_ok=True)
        path = artifact_dir / f"{oid}.png"
        path.write_bytes(png)
        observation = dict(metadata, id=oid, resource=name, at=time.time(), path=str(path))
        state["observations"][name] = observation
        if metadata.get("page_id"):
            state.setdefault("bindings", {})[name] = {"page_id": metadata["page_id"]}
        self.store.save(state, "observed", observation)
        return observation

    def _complete(self, state, step, output):
        json.dumps(output, allow_nan=False)
        # Keep the dispatch intent in memory as well as on disk until commit succeeds.
        completed = copy.deepcopy(state)
        completed["outputs"][step["id"]] = output
        if step.get("resource") and output.get("page_id"):
            completed.setdefault("bindings", {})[step["resource"]] = {"page_id": output["page_id"]}
        completed["cursor"] += 1
        completed["pending"] = None
        completed["error"] = None
        self.store.save(completed, "step_completed", {"step": step["id"], "output": output})
        state.clear()
        state.update(completed)

    def run(self, run_id):
        with self.store.guard(run_id):
            state = self.store.get(run_id)
            if state["status"] != "queued":
                raise ValueError(f"run is {state['status']}; use resume or recover, not run")
            try:
                return self._drive(state)
            finally:
                self._close()

    def _drive(self, state):
        self.store.reserve(state["id"], [resource_key(s) for s in state["workflow"]["resources"].values()])
        state["status"] = "running"
        self.store.save(state, "running")
        steps = state["workflow"]["steps"]
        while True:
            if self._control(state):
                return state
            if state["cursor"] >= len(steps):
                state["status"] = "succeeded"
                self.store.save(state, "succeeded", release=True)
                return state
            step = steps[state["cursor"]]
            context = {"inputs": state["workflow"].get("inputs", {}), "steps": state["outputs"]}
            try:
                if step["kind"] == "observe":
                    self._complete(state, step, self._observe(state, step["resource"]))
                elif step["kind"] == "pages":
                    self._complete(state, step, self._resource(state, step["resource"]).pages())
                elif step["kind"] == "tool":
                    arguments = resolve(step.get("arguments", {}), context)
                    output = self._resource(state, step["resource"]).call(step["operation"], arguments)
                    self._complete(state, step, output)
                elif step["kind"] == "check":
                    actual, expected = resolve(step["actual"], context), resolve(step["equals"], context)
                    if type(actual) is not type(expected) or actual != expected:
                        raise PreconditionError(f"check {step['id']} did not match expected value")
                    self._complete(state, step, {"verified": True, "actual": actual})
                elif step["kind"] == "human":
                    # No resource input after this checkpoint until an explicit resume.
                    state["status"] = "waiting_for_human"
                    state["human"] = dict(step)
                    observation = self._observe(state, step["resource"])
                    state["human"]["observation"] = observation
                    self.store.save(state, "human_requested", state["human"])
                    return state
                elif step["kind"] == "action":
                    self._action(state, step, context)
                    if state["status"] != "running":
                        return state
            except Exception as exc:
                state["error"] = f"{type(exc).__name__}: {exc}"
                # Once dispatch intent exists, we cannot prove an exception means no effect.
                state["status"] = "needs_review" if state["pending"] else "failed"
                self.store.save(state, state["status"], {"step": step["id"], "error": state["error"]},
                                release=state["status"] == "failed")
                return state

    def _control(self, state):
        command = self.store.control(state["id"])
        if command == "cancel":
            state["status"] = "cancelled"
            self.store.save(state, "cancelled", release=True, clear_control="cancel")
            return True
        if command == "pause":
            state["status"] = "waiting_for_human"
            state["human"] = {"kind": "pause", "instructions": "Return control with resume after completing manual work."}
            self.store.save(state, "human_requested", state["human"], clear_control="pause")
            return True
        return False

    def _action(self, state, step, context):
        name = step["resource"]
        observation = resolve(step["observation"], context)
        if state.get("revalidate_step") == step["id"]:
            fresh = self._observe(state, name)
            if (not isinstance(observation, dict) or observation.get("resource") != name or
                    any(observation.get(k) != fresh.get(k) for k in ("image_sha256", "screen_size", "page_id", "url"))):
                raise PreconditionError("screen changed during interruption; old action cannot be replayed")
            observation = fresh
            state.pop("revalidate_step", None)
        latest = state["observations"].get(name)
        if (not isinstance(observation, dict) or not latest or observation != latest
                or observation.get("resource") != name or time.time() - observation["at"] > 30):
            raise PreconditionError("action needs the latest observation of its resource (maximum age 30s)")
        action = resolve(step["action"], context)
        resource = self._resource(state, name)
        prepared = resource.prepare(action, observation)
        if self._control(state):
            return
        state["pending"] = {"id": uuid.uuid4().hex, "step": step["id"],
                            "resource": name, "action": prepared}
        state["observations"].pop(name, None)
        self.store.save(state, "action_dispatching", state["pending"])
        result = resource.execute(prepared)
        self._complete(state, step, result)

    def resume(self, run_id, note, data=None):
        if not note.strip():
            raise ValueError("resume requires a note describing what the user completed")
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise ValueError("human data must be a JSON object")
        json.dumps(data, allow_nan=False)
        with self.store.guard(run_id):
            state = self.store.get(run_id)
            if state["status"] != "waiting_for_human":
                raise ValueError("only waiting_for_human runs can resume")
            try:
                if self.store.control(run_id) == "cancel":
                    self._control(state)
                    return state
                # Invalidate all pre-handoff observations; reconnect and see current state.
                state["observations"] = {}
                for name, spec in state["workflow"]["resources"].items():
                    if spec["kind"] != "sqlite":
                        self._observe(state, name)
                human = state["human"]
                state["human"] = None
                state["status"] = "queued"
                if state["cursor"] < len(state["workflow"]["steps"]):
                    state["revalidate_step"] = state["workflow"]["steps"][state["cursor"]]["id"]
                if human.get("kind") == "human":
                    step = state["workflow"]["steps"][state["cursor"]]
                    self._complete(state, step, {"acknowledged": True, "note": note, "data": data,
                                              "verification_pending": human["verify_step"]})
                self.store.save(state, "human_returned", {"note": note, "data": data})
                return self._drive(state)
            finally:
                self._close()

    def recover(self, run_id):
        """Only possible when the OS executor lock is free; never replays input."""
        with self.store.guard(run_id):
            state = self.store.get(run_id)
            if state["status"] != "running":
                raise ValueError("recover requires an interrupted running task")
            state["observations"] = {}
            state["status"] = "needs_review" if state["pending"] else "queued"
            if state["cursor"] < len(state["workflow"]["steps"]):
                state["revalidate_step"] = state["workflow"]["steps"][state["cursor"]]["id"]
            self.store.save(state, "recovered", {"pending": state["pending"]})
            return state

    def resolve_action(self, run_id, outcome, note):
        if outcome not in {"completed", "not_executed"} or not note.strip():
            raise ValueError("resolution requires completed/not_executed and evidence in --note")
        with self.store.guard(run_id):
            state = self.store.get(run_id)
            if state["status"] != "needs_review" or not state["pending"]:
                raise ValueError("no uncertain action to resolve")
            step = state["workflow"]["steps"][state["cursor"]]
            action_id = state["pending"]["id"]
            state["status"] = "queued"
            state["observations"] = {}
            if outcome == "completed":
                output = {"resolved_by_human": True, "note": note}
                pending_action = state["pending"]["action"]
                if pending_action.get("type") == "select_page":
                    output["page_id"] = pending_action["page_id"]
                self._complete(state, step, output)
            else:
                # Replay only after explicit reconciliation. Re-observe the resource on run.
                state["pending"] = None
                state["revalidate_step"] = step["id"]
            state["observations"] = {}
            state["status"] = "queued"
            state["error"] = None
            self.store.save(state, "action_resolved", {"action_id": action_id, "outcome": outcome, "note": note})
            return state

    def cancel(self, run_id):
        # An active executor observes this between actions; never interrupt a click mid-call.
        self.store.request(run_id, "cancel")
        from .store import BusyError
        try:
            with self.store.guard(run_id):
                state = self.store.get(run_id)
                if state["status"] in TERMINAL:
                    return state
                # Keep uncertain results visible even when cancelling; no replay on cancellation.
                state["status"] = "cancelled"
                self.store.save(state, "cancelled", {"unresolved_action": state["pending"]}, release=True, clear_control="cancel")
                return state
        except BusyError:
            return self.store.get(run_id)
