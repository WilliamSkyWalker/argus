import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from saygo.desktop import model
from saygo.desktop.runner import TaskLoop, decision


class DesktopTests(unittest.TestCase):
    def test_scroll_decisions_receive_units_and_before_after_images(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'image.png'
            path.write_bytes(b'current-image')
            caps = {'actions': ['scroll_at'], 'scroll_at_pixels_per_unit': 100}
            state = {'id': 'test', 'desktop_goal': 'Find an item', 'status': 'idle',
                     'workflow': {'resources': {'web': {}}},
                     'observations': {'web': {'id': 'obs', 'path': str(path),
                                              'screen_size': [1000, 800], 'capabilities': caps}}}
            rt = Mock()
            rt.store.get.return_value = state
            frames = iter([b'frame-before', b'frame-after', b'frame-final'])
            def observe(*args):
                path.write_bytes(next(frames))
                return state
            rt.observe.side_effect = observe
            rt.submit.return_value = state
            rt.timeline.return_value = {'timeline': []}
            provider = Mock()
            calls = []

            def complete(system, context, images):
                context = json.loads(context)
                calls.append((context, images))
                self.assertEqual(context['observations']['web']['screen_size'], [1000, 800])
                self.assertEqual(context['observations']['web']['capabilities'], caps)
                if len(calls) <= 2:
                    amount = -3 if len(calls) == 1 else -.6
                    return json.dumps({'kind': 'act', 'resource': 'web', 'note': 'Inspect the list',
                                       'action': {'type': 'scroll_at', 'x': 35, 'y': 60,
                                                  'coordinate_space': 'percent', 'amount': amount}})
                return json.dumps({'kind': 'done', 'note': 'Target visible'})

            provider.complete.side_effect = complete
            TaskLoop(provider, rt).run('test', threading.Event(), threading.Event(), lambda state: None)
            self.assertIsNone(calls[0][0]['previous_scroll'])
            self.assertEqual(len(calls[0][1]), 1)
            self.assertEqual(calls[1][0]['previous_scroll']['action']['amount'], -3)
            self.assertEqual([value for _,value in calls[1][1]], [b'frame-before',b'frame-after'])
            self.assertEqual([value for _,value in calls[2][1]], [b'frame-after',b'frame-final'])
            submitted = [call.args[2] for call in rt.submit.call_args_list]
            self.assertEqual([action['amount'] for action in submitted], [-3,-.6])
            self.assertEqual([name for name, _ in calls[1][1]], ['web before previous scroll', 'web'])
            self.assertEqual([call.args[2]['amount'] for call in rt.submit.call_args_list], [-3, -.6])

    def test_settings_never_persist_secret(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,{'SAYGO_HOME_DIR':tmp}):
            model.save_settings({'base_url':'https://example.com/v1','model':'vision'},'private-token',False)
            self.assertNotIn('private-token',model.settings_path().read_text())
            self.assertEqual(model.load_key(model.load_settings()),'')

    def test_no_plaintext_fallback(self):
        with patch('saygo.desktop.model.secure_backend',side_effect=RuntimeError('no secure store')):
            with self.assertRaisesRegex(RuntimeError,'no secure store'):
                model.save_settings({'base_url':'https://example.com/v1','model':'vision'},'key',True)

    def test_unbound_or_invalid_action_is_rejected(self):
        for value in [{'kind':'act','note':'x','resource':'other','action':{'type':'tap'}},
                      {'kind':'act','note':'x','resource':'web','action':{'type':'shell'}},
                      {'kind':'act','note':'x','resource':'web','action':{'type':'tap','x':10,'y':20}}]:
            with self.assertRaises(ValueError): decision(json.dumps(value),{'web':{}})

    def test_pause_during_model_request_never_dispatches(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'image.png';path.write_bytes(b'png')
            state={'id':'test','desktop_goal':'test','status':'idle','workflow':{'resources':{'web':{}}},
                   'observations':{'web':{'id':'obs','path':str(path)}}}
            rt=Mock();rt.store.get.return_value=state;rt.observe.return_value=state
            rt.timeline.return_value={'timeline':[]}
            pause=threading.Event(); provider=Mock()
            def complete(*args):
                pause.set();return '{"kind":"act","note":"click","resource":"web","action":{"type":"tap","x":10,"y":20,"coordinate_space":"percent"}}'
            provider.complete.side_effect=complete
            TaskLoop(provider,rt).run('test',pause,threading.Event(),lambda state:None)
            rt.submit.assert_not_called();rt.handoff_task.assert_called_once()

    def test_uncertain_action_is_not_replayed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'image.png';path.write_bytes(b'png')
            state={'id':'test','desktop_goal':'test','status':'idle','workflow':{'resources':{'web':{}}},
                   'observations':{'web':{'id':'obs','path':str(path)}}}
            rt=Mock();rt.store.get.return_value=state;rt.observe.return_value=state;rt.timeline.return_value={'timeline':[]}
            rt.submit.return_value={**state,'status':'needs_review'}
            provider=Mock();provider.complete.return_value='{"kind":"act","note":"click","resource":"web","action":{"type":"tap","x":10,"y":20,"coordinate_space":"percent"}}'
            result=TaskLoop(provider,rt).run('test',threading.Event(),threading.Event(),lambda state:None)
            self.assertEqual(result['status'],'needs_review');rt.submit.assert_called_once();provider.complete.assert_called_once()

    def test_credentials_only_sent_to_https_or_loopback(self):
        for url in ['http://example.com/v1','https://user:pass@example.com/v1','https://example.com/v1?key=secret']:
            with self.assertRaises(ValueError):model.validate({'base_url':url,'model':'vision'})

    def test_loop_uses_real_durable_runtime_and_exports_evidence(self):
        import io
        from PIL import Image
        from saygo.platforms import device_session as ds
        from saygo.runtime.interactive import InteractiveRuntime
        from saygo.runtime.store import Store
        with tempfile.TemporaryDirectory() as tmp, patch.object(ds,'STATE_DIR',Path(tmp)/'sessions'):
            ds.save_state('demo',{'kind':'browser','browser_backend':'extension','bridge_directory':tmp})
            raw=io.BytesIO();Image.new('RGB',(40,20),'white').save(raw,format='PNG')
            resource=Mock(); resource.observe.return_value=(raw.getvalue(),{'screen_size':[40,20]})
            resource.wait.return_value={'condition_met':True}
            resource.prepare.side_effect=lambda action,obs:action
            resource.execute.return_value={'dispatched':True,'business_success':None}
            runtime=InteractiveRuntime(Store(Path(tmp)/'runtime'),factory=lambda spec:resource)
            provider=Mock();provider.complete.side_effect=[
                '{"kind":"act","resource":"web","note":"click","action":{"type":"tap","x":10,"y":20,"coordinate_space":"percent"}}',
                '{"kind":"done","note":"The result is visible in the new screenshot"}']
            loop=TaskLoop(provider,runtime);state=loop.create('test',{'web':'demo'})
            result=loop.run(state['id'],threading.Event(),threading.Event(),lambda state:None)
            self.assertEqual(result['status'],'succeeded');resource.execute.assert_called_once()
            loaded=Store(Path(tmp)/'runtime').get(state['id'])
            self.assertEqual(loaded['desktop_goal'],'test')
            self.assertEqual(len(loaded['requests']),1)
            runtime.export(state['id'],Path(tmp)/'report.zip')
            self.assertTrue((Path(tmp)/'report.zip').is_file())
