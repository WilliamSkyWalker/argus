"""Offline contract tests: no devices, model calls, or external services."""

import copy
import hashlib
from pathlib import Path
import sqlite3
import json
import os
import subprocess
import sys
from unittest.mock import patch
import tempfile
import unittest

from argus.runtime import Runtime, Store
from argus.runtime.resources import PreconditionError, SQLiteResource, VisualResource, resource_key
from argus.runtime.schema import resolve, validate
from argus.runtime.store import BusyError


def ref(path):
    return {"$ref": path}


class FakeVisual:
    def __init__(self, name, trace):
        self.name, self.trace = name, trace
        self.frame = b"stable image"
        self.failure = None

    def observe(self):
        self.trace.append((self.name, "observe"))
        return self.frame, {"screen_size": [100, 200], "image_sha256": hashlib.sha256(self.frame).hexdigest()}

    def prepare(self, action, observation):
        if observation["image_sha256"] != hashlib.sha256(self.frame).hexdigest():
            raise PreconditionError("changed frame")
        return action

    def execute(self, action):
        self.trace.append((self.name, "execute"))
        if self.failure:
            raise self.failure
        return {"dispatched": True}

    def close(self):
        pass


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "state")
        self.trace = []
        self.phone = FakeVisual("phone", self.trace)
        self.browser = FakeVisual("browser", self.trace)
        self.database = self.root / "orders.sqlite"
        with sqlite3.connect(self.database) as db:
            db.execute("CREATE TABLE orders(id TEXT PRIMARY KEY, status TEXT)")
            db.execute("INSERT INTO orders VALUES ('order-1','pending')")
        self.specs = {
            "phone": {"kind": "android", "session": "phone"},
            "browser": {"kind": "browser", "session": "browser"},
            "db": {"kind": "sqlite", "path": str(self.database),
                   "queries": {"get": "SELECT id,status FROM orders WHERE id=:id"}},
        }
        self.runtime = Runtime(self.store, self.factory)

    def factory(self, spec):
        if spec["kind"] == "sqlite":
            return SQLiteResource(spec)
        return self.phone if spec["session"] == "phone" else self.browser

    def workflow(self, steps):
        return {"version": 1, "resources": copy.deepcopy(self.specs),
                "inputs": {"order_id": "order-1"}, "steps": steps}

    def action_steps(self):
        return [
            {"id": "screen", "kind": "observe", "resource": "phone"},
            {"id": "tap", "kind": "action", "resource": "phone",
             "observation": ref("steps.screen"), "action": {"type": "tap", "x_pct": 50, "y_pct": 50}},
        ]

    def payment_steps(self):
        return self.action_steps() + [
            {"id": "lookup", "kind": "tool", "resource": "db", "operation": "get",
             "arguments": {"id": ref("inputs.order_id")}},
            {"id": "admin", "kind": "observe", "resource": "browser"},
            {"id": "pay", "kind": "human", "resource": "phone",
             "instructions": "Complete payment, then return control.", "verify_step": "paid"},
            {"id": "after", "kind": "tool", "resource": "db", "operation": "get",
             "arguments": {"id": ref("steps.lookup.rows.0.id")}},
            {"id": "paid", "kind": "check", "actual": ref("steps.after.rows.0.status"), "equals": "paid"},
        ]

    def test_cross_resource_handoff_survives_new_runtime(self):
        run = self.runtime.create(self.workflow(self.payment_steps()))
        state = self.runtime.run(run["id"])
        self.assertEqual(state["status"], "waiting_for_human")
        self.assertEqual(state["cursor"], 4)
        self.assertEqual(self.trace.count(("phone", "execute")), 1)
        with sqlite3.connect(self.database) as db:
            db.execute("UPDATE orders SET status='paid'")
        runtime = Runtime(Store(self.root / "state"), self.factory)
        state = runtime.resume(run["id"], "Payment completed on phone")
        self.assertEqual(state["status"], "succeeded")
        self.assertTrue(state["outputs"]["paid"]["verified"])
        self.assertEqual(self.trace.count(("phone", "execute")), 1)
        self.assertEqual(self.store.events(run["id"])[-1]["kind"], "succeeded")

    def test_human_can_return_typed_data_for_later_query(self):
        steps = [
            {"id": "manual", "kind": "human", "resource": "phone",
             "instructions": "Provide order id", "verify_step": "verified"},
            {"id": "lookup", "kind": "tool", "resource": "db", "operation": "get",
             "arguments": {"id": ref("steps.manual.data.order_id")}},
            {"id": "verified", "kind": "check", "actual": ref("steps.lookup.rows.0.id"),
             "equals": ref("steps.manual.data.order_id")},
        ]
        run = self.runtime.create(self.workflow(steps))
        self.runtime.run(run["id"])
        state = self.runtime.resume(run["id"], "Created order", {"order_id": "order-1"})
        self.assertEqual(state["status"], "succeeded")
        self.assertEqual(state["outputs"]["manual"]["data"], {"order_id": "order-1"})

    def test_human_acknowledgement_is_not_payment_success(self):
        run = self.runtime.create(self.workflow(self.payment_steps()))
        self.runtime.run(run["id"])
        state = self.runtime.resume(run["id"], "Clicked continue")
        self.assertEqual(state["status"], "failed")
        self.assertNotIn("paid", state["outputs"])

    def test_pause_retains_resource_ownership(self):
        run = self.runtime.create(self.workflow(self.payment_steps()))
        self.runtime.run(run["id"])
        other = self.runtime.create(self.workflow(self.action_steps()))
        with self.assertRaises(BusyError):
            self.runtime.run(other["id"])
        self.runtime.cancel(run["id"])
        self.assertEqual(self.runtime.run(other["id"])["status"], "succeeded")

    def test_failed_dispatch_is_not_retried(self):
        self.phone.failure = RuntimeError("driver lost response after click")
        run = self.runtime.create(self.workflow(self.action_steps()))
        state = self.runtime.run(run["id"])
        self.assertEqual(state["status"], "needs_review")
        with self.assertRaises(ValueError):
            self.runtime.run(run["id"])
        self.runtime.resolve_action(run["id"], "completed", "Observed that the click succeeded")
        self.assertEqual(self.runtime.run(run["id"])["status"], "succeeded")
        self.assertEqual(self.trace.count(("phone", "execute")), 1)

    def test_crash_after_dispatch_requires_reconciliation(self):
        self.phone.failure = KeyboardInterrupt()
        run = self.runtime.create(self.workflow(self.action_steps()))
        with self.assertRaises(KeyboardInterrupt):
            self.runtime.run(run["id"])
        self.assertEqual(self.store.get(run["id"])["status"], "running")
        state = self.runtime.recover(run["id"])
        self.assertEqual(state["status"], "needs_review")
        self.assertEqual(self.trace.count(("phone", "execute")), 1)

    def test_explicit_retry_refreshes_observation(self):
        self.phone.failure = RuntimeError("uncertain")
        run = self.runtime.create(self.workflow(self.action_steps()))
        self.runtime.run(run["id"])
        self.runtime.resolve_action(run["id"], "not_executed", "Confirmed no click was delivered")
        self.phone.failure = None
        self.assertEqual(self.runtime.run(run["id"])["status"], "succeeded")
        self.assertEqual(self.trace.count(("phone", "execute")), 2)

    def test_changed_screen_rejects_retry(self):
        self.phone.failure = RuntimeError("uncertain")
        run = self.runtime.create(self.workflow(self.action_steps()))
        self.runtime.run(run["id"])
        self.runtime.resolve_action(run["id"], "not_executed", "No click delivered")
        self.phone.frame = b"different page"
        self.phone.failure = None
        self.assertEqual(self.runtime.run(run["id"])["status"], "failed")
        self.assertEqual(self.trace.count(("phone", "execute")), 1)

    def test_cross_resource_observation_is_rejected(self):
        steps = self.action_steps()
        steps[1]["resource"] = "browser"
        run = self.runtime.create(self.workflow(steps))
        self.assertEqual(self.runtime.run(run["id"])["status"], "failed")
        self.assertNotIn(("browser", "execute"), self.trace)

    def test_consumed_observation_cannot_be_reused(self):
        steps = self.action_steps()
        steps.append(dict(steps[-1], id="again"))
        run = self.runtime.create(self.workflow(steps))
        self.assertEqual(self.runtime.run(run["id"])["status"], "failed")
        self.assertEqual(self.trace.count(("phone", "execute")), 1)

    def test_requested_pause_and_resume(self):
        run = self.runtime.create(self.workflow(self.action_steps()))
        self.store.request(run["id"], "pause")
        self.assertEqual(self.runtime.run(run["id"])["status"], "waiting_for_human")
        self.assertEqual(self.trace, [])
        self.assertEqual(self.runtime.resume(run["id"], "Ready") ["status"], "succeeded")

    def test_active_runner_cannot_be_recovered(self):
        run = self.runtime.create(self.workflow(self.action_steps()))
        with self.store.guard(run["id"]):
            with self.assertRaises(BusyError):
                self.runtime.recover(run["id"])

    def test_sqlite_query_only_and_parameters(self):
        spec = copy.deepcopy(self.specs["db"])
        adapter = SQLiteResource(spec)
        self.assertEqual(adapter.call("get", {"id": "' OR 1=1 --"})["rows"], [])
        for query in ["DELETE FROM orders", "PRAGMA user_version=1", "ATTACH DATABASE ':memory:' AS extra"]:
            spec["queries"]["bad"] = query
            with self.assertRaises(sqlite3.DatabaseError):
                adapter.call("bad", {})
        self.assertEqual(len(adapter.call("get", {"id": "order-1"})["rows"]), 1)

    def test_workflow_validation(self):
        workflow = self.workflow(self.payment_steps())
        workflow["steps"][4]["verify_step"] = "missing"
        with self.assertRaises(ValueError):
            validate(workflow)
        workflow = self.workflow(self.action_steps())
        workflow["resources"]["phone"]["session"] = "../escape"
        with self.assertRaises(ValueError):
            validate(workflow)

    def test_desktop_windows_share_input_lock(self):
        self.assertEqual(resource_key({"kind": "windows", "app": "One"}),
                         resource_key({"kind": "windows", "app": "Two"}))

    def test_references_preserve_types_and_fail_closed(self):
        self.assertEqual(resolve(ref("steps.x.rows.0.n"), {"steps": {"x": {"rows": [{"n": 3}]}}}), 3)
        with self.assertRaises(ValueError):
            resolve(ref("steps.missing"), {"steps": {}})

    def test_checkpoint_failure_after_input_keeps_uncertain_intent(self):
        original = self.store.save
        def fail_once(state, kind, data=None, release=False):
            if kind == "step_completed" and data["step"] == "tap":
                raise OSError("checkpoint temporarily unavailable")
            return original(state, kind, data, release)
        run = self.runtime.create(self.workflow(self.action_steps()))
        with patch.object(self.store, "save", side_effect=fail_once):
            state = self.runtime.run(run["id"])
        self.assertEqual(state["status"], "needs_review")
        self.assertEqual(state["pending"]["step"], "tap")
        self.assertEqual(state["cursor"], 1)
        self.assertEqual(self.trace.count(("phone", "execute")), 1)

    def test_crash_during_handoff_capture_can_resume(self):
        run = self.runtime.create(self.workflow(self.payment_steps()))
        original = self.store.save
        def crash_after_observe(state, kind, data=None, release=False):
            result = original(state, kind, data, release)
            if state["status"] == "waiting_for_human" and kind == "observed":
                raise KeyboardInterrupt()
            return result
        with patch.object(self.store, "save", side_effect=crash_after_observe):
            with self.assertRaises(KeyboardInterrupt):
                self.runtime.run(run["id"])
        self.assertEqual(self.store.get(run["id"])["human"]["id"], "pay")
        with sqlite3.connect(self.database) as db:
            db.execute("UPDATE orders SET status='paid'")
        self.assertEqual(self.runtime.resume(run["id"], "Paid")["status"], "succeeded")

    def test_pause_arriving_during_prepare_stops_dispatch(self):
        run = self.runtime.create(self.workflow(self.action_steps()))
        original = self.phone.prepare
        def pause(action, observation):
            self.store.request(run["id"], "pause")
            return original(action, observation)
        with patch.object(self.phone, "prepare", side_effect=pause):
            self.assertEqual(self.runtime.run(run["id"])["status"], "waiting_for_human")
        self.assertNotIn(("phone", "execute"), self.trace)
        self.assertEqual(self.runtime.resume(run["id"], "Continue")["status"], "succeeded")

    def test_cancel_during_action_takes_effect_after_return(self):
        run = self.runtime.create(self.workflow(self.action_steps()))
        original = self.phone.execute
        def cancel(action):
            self.runtime.cancel(run["id"])
            return original(action)
        with patch.object(self.phone, "execute", side_effect=cancel):
            state = self.runtime.run(run["id"])
        self.assertEqual(state["status"], "cancelled")
        self.assertIsNone(state["pending"])
        self.assertIn("tap", state["outputs"])

    def test_windows_adapter_uses_existing_driver_config_contract(self):
        class Desktop:
            def setup(self, config):
                self.config = config
        desktop = Desktop()
        with patch("argus.platforms.create_platform", return_value=desktop):
            adapter = VisualResource({"kind": "windows", "app": "Example"})
            self.assertIs(adapter._attach(), desktop)
        self.assertEqual(desktop.config["win"], {"app": "Example", "launch": ""})

    def test_cli_persists_real_query_result(self):
        workflow = self.workflow([
            {"id": "query", "kind": "tool", "resource": "db", "operation": "get",
             "arguments": {"id": ref("inputs.order_id")}},
            {"id": "check", "kind": "check", "actual": ref("steps.query.rows.0.id"), "equals": "order-1"},
        ])
        path = self.root / "workflow.json"
        path.write_text(json.dumps(workflow))
        command = [sys.executable, "-m", "argus.cli", "workflow", "--state-dir", str(self.store.root)]
        created = subprocess.run(command + ["create", str(path)], capture_output=True, text=True, check=True,
                                 env={**os.environ, "LLM_API_KEY": ""})
        run_id = json.loads(created.stdout)["id"]
        finished = subprocess.run(command + ["run", run_id], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(finished.stdout)["status"], "succeeded")
        self.assertEqual(Store(self.store.root).get(run_id)["outputs"]["query"]["rows"][0]["id"], "order-1")

    def test_visual_coordinate_conversion_and_changed_screen(self):
        class Platform:
            screen_size = (200, 400)
            def screenshot_raw(self):
                return b"screenshot"
        adapter = VisualResource({"kind": "android", "session": "phone"})
        adapter.platform = Platform()
        _, obs = adapter.observe()
        action = adapter.prepare({"type": "tap", "x_pct": 50, "y_pct": 100}, obs)
        self.assertEqual(action, {"type": "tap", "x": 100, "y": 399})
        with self.assertRaises(PreconditionError):
            adapter.prepare({"type": "tap", "x_pct": float("nan"), "y_pct": 50}, obs)
        obs["image_sha256"] = "stale"
        with self.assertRaises(PreconditionError):
            adapter.prepare({"type": "input", "text": "hello"}, obs)


if __name__ == "__main__":
    unittest.main()
