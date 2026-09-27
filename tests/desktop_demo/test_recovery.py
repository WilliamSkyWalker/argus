"""The model chooses recovery from evidence; no screenshot means no business input."""
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from PIL import Image
from saygo.desktop.runner import TaskLoop
from saygo.platforms import device_session as ds
from saygo.runtime.interactive import InteractiveRuntime
from saygo.runtime.store import Store


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        p = patch.object(ds, 'STATE_DIR', self.root / 'sessions')
        p.start(); self.addCleanup(p.stop)
        ds.save_state('app', {'kind':'desktop', 'os':'windows', 'app':'Example', 'process_id':42})
        png = io.BytesIO(); Image.new('RGB', (40,20), 'white').save(png, format='PNG')
        self.picture = png.getvalue()
        self.resource = Mock()
        self.resource.observe.return_value = (self.picture, {'screen_size':[40,20]})
        self.resource.diagnose.return_value = {'process_id':42, 'selected_window':'1', 'windows':[
            {'handle':1, 'process_id':42, 'visible':True, 'excluded_reasons':['transparent_overlay']},
            {'handle':2, 'process_id':42, 'visible':True, 'excluded_reasons':[]},
            {'handle':3, 'process_id':99, 'visible':True, 'excluded_reasons':[]}]}
        self.specs = []
        def factory(spec):
            self.specs.append(spec)
            return self.resource
        self.runtime = InteractiveRuntime(Store(self.root/'runtime'), factory=factory)
        self.provider = Mock()
        self.loop = TaskLoop(self.provider, self.runtime)
        self.state = self.loop.create('Inspect the application', {'app':'app'})

    def run_loop(self):
        return self.loop.run(self.state['id'], threading.Event(), threading.Event(), lambda state: None)

    def test_model_gets_failure_and_selects_target_before_new_observation(self):
        self.resource.observe.side_effect = [RuntimeError('uniform image'), (self.picture, {'screen_size':[40,20]})]
        self.provider.complete.side_effect = [json.dumps({'kind':'recover', 'resource':'app',
            'operation':'select_window', 'window_id':'2', 'note':'Window 1 is a transparent overlay; window 2 belongs to the same process.'}),
            '{"kind":"done","note":"The expected application is visible in the fresh screenshot"}']
        result = self.run_loop()
        self.assertEqual(result['status'], 'succeeded')
        first = self.provider.complete.call_args_list[0].args
        context = json.loads(first[1])
        self.assertEqual(first[2], [])
        self.assertIn('uniform image', context['observation_errors']['app'])
        self.assertEqual(context['diagnostics']['app']['selected_window'], '1')
        self.assertTrue(any(s.get('window_id') == '2' for s in self.specs))
        self.resource.execute.assert_not_called()
        events = self.runtime.store.events(result['id'])
        self.assertIn('resource_repair_requested', [e['kind'] for e in events])

    def test_cannot_select_overlay_foreign_process_or_missing_window(self):
        for window in ('1','3','999'):
            with self.assertRaises(ValueError):
                self.runtime.repair_resource(self.state['id'], 'app', 'select_window', 'inspect', window)
        self.resource.execute.assert_not_called()

    def test_no_stale_image_or_false_done_after_observation_failure(self):
        self.runtime.observe(self.state['id'], 'app')
        self.resource.observe.side_effect = RuntimeError('capture unavailable')
        self.provider.complete.return_value = '{"kind":"done","note":"pretend success"}'
        self.run_loop()
        self.assertEqual(self.provider.complete.call_args.args[2], [])
        state = self.runtime.store.get(self.state['id'])
        self.assertEqual(state['status'], 'waiting_for_human')
        self.assertEqual(state['observations'], {})
        self.resource.execute.assert_not_called()

    def test_repeated_capture_failure_is_bounded(self):
        self.resource.observe.side_effect = RuntimeError('capture unavailable')
        self.provider.complete.return_value = '{"kind":"recover","resource":"app","operation":"reobserve","note":"retry discovery"}'
        result = self.run_loop()
        self.assertEqual(result['status'], 'waiting_for_human')
        self.assertEqual(self.provider.complete.call_count, 2)
        self.resource.execute.assert_not_called()

    def test_repair_cannot_replay_uncertain_input(self):
        state = self.runtime.store.get(self.state['id'])
        state.update(status='needs_review', pending={'id':'input'})
        self.runtime.store.save(state, 'test_uncertain')
        with self.assertRaisesRegex(ValueError, 'uncertain'):
            self.runtime.repair_resource(state['id'], 'app', 'reobserve', 'retry')
        self.resource.execute.assert_not_called()

    def test_model_can_correct_invalid_protocol_without_input(self):
        self.provider.complete.side_effect = [
            '{"kind":"recovery","resource":"app","operation":"reobserve"}',
            '{"kind":"done","note":"The application is visible in the current screenshot"}']
        result = self.run_loop()
        self.assertEqual(result['status'], 'succeeded')
        correction = json.loads(self.provider.complete.call_args_list[1].args[1])
        self.assertIn('decision_error', correction)
        self.resource.execute.assert_not_called()

    def test_numeric_window_handles_are_normalized(self):
        from saygo.desktop.runner import decision
        value = decision('{"kind":"recover","resource":"app","operation":"select_window","window_id":2,"note":"eligible window"}', {'app':{}})
        self.assertEqual(value['window_id'], '2')
