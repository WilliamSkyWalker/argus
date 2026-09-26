import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw
from argus.devices import service, control, observations, actions
from argus.platforms import device_session as ds
from argus.platforms.base import Platform
from argus.runtime.interactive import InteractiveRuntime
from argus.runtime.store import Store, BusyError
from argus.runtime.locking import resource_guard, session_keys


def png(color='white', dot=None):
    image = Image.new('RGB', (200, 100), color)
    if dot:
        ImageDraw.Draw(image).rectangle(dot, fill='black')
    out = io.BytesIO()
    image.save(out, format='PNG')
    return out.getvalue()


class Device(Platform):
    screen_size = (400, 200)
    scale = .5
    platform_name = 'browser'
    def __init__(self):
        self.calls = []
        self.frame = png()
        self.fail = False
    def setup(self, config): pass
    def teardown(self): pass
    def disconnect(self): pass
    def screenshot_raw(self): return self.frame
    def screenshot_png(self): return self.frame
    def get_system_prompt_segment(self): return ''
    def tap(self, x, y):
        self.calls.append(('tap', x, y))
        if self.fail: raise RuntimeError('response lost after input')
    def input_text(self, text): self.calls.append(('input', text))
    def press_key(self, key): self.calls.append(('key', key))
    def swipe(self, *args): self.calls.append(('swipe', *args))
    def scroll_up(self): self.calls.append(('scroll_up',))
    def scroll_down(self): self.calls.append(('scroll_down',))
    def open_target(self, target): self.calls.append(('open', target))


class AgentServiceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        patcher = patch.object(ds, 'STATE_DIR', self.root / 'sessions')
        patcher.start(); self.addCleanup(patcher.stop)
        self.device = Device()
        for session in ('phone', 'mail', 'admin'):
            ds.save_state(session, {'kind': 'browser', 'browser_backend':'playwright'})
        patcher = patch.object(ds, 'attach_browser', return_value=self.device)
        self.attach = patcher.start(); self.addCleanup(patcher.stop)

    def test_cli_mcp_share_binding_and_release(self):
        from argus.cli import main
        from argus.mcp import server
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main(['device', 'tap', '20', '30', '--session', 'mail'])
        self.assertTrue(json.loads(out.getvalue())['ok'])
        result = server.device_tap(40, 50, 'mail')
        self.assertTrue(result['ok'])
        self.assertEqual(self.device.calls, [('tap',20,30), ('tap',40,50)])
        self.assertEqual([c.args[0] for c in self.attach.call_args_list], ['mail','mail'])

    def test_percent_image_crop_mapping_and_stale_target(self):
        shot = service.execute('screenshot', 'mail', crop=[20,10,80,60])
        self.assertEqual(shot['image_to_screen'], [2,2])
        for action, expected in [
            ({'type':'tap','x':50,'y':50,'coordinate_space':'percent'}, ('tap',200,100)),
            ({'type':'tap','x':50,'y':30,'coordinate_space':'image'}, ('tap',100,60)),
            ({'type':'tap','x':20,'y':10,'coordinate_space':'crop'}, ('tap',60,30)),
        ]:
            result = service.execute('act','mail',action=action,observation_id=shot['id'])
            self.assertTrue(result['ok'], result)
            self.assertEqual(self.device.calls[-1], expected)
        self.device.frame = png(dot=[29,14,30,15])
        result = service.execute('act','mail',action={'type':'tap','x':60,'y':30},observation_id=shot['id'])
        self.assertFalse(result['ok'])
        self.assertIn('Target region changed', result['error'])

    def test_unsupported_and_missing_sessions_never_launch(self):
        result = service.execute('act','mail',action={'type':'right_click','x':1,'y':1})
        self.assertFalse(result['ok'])
        self.assertEqual(self.device.calls, [])
        with patch.object(ds, 'start') as start:
            self.assertFalse(service.execute('tap', 'missing', x=1,y=2)['ok'])
            start.assert_not_called()

    def test_mutual_exclusion_and_global_desktop_key(self):
        with resource_guard(session_keys('mail')):
            result = service.execute('tap','mail',x=1,y=2)
        self.assertFalse(result['ok'])
        self.assertEqual(result['error_type'],'BusyError')
        self.assertEqual(self.device.calls, [])
        self.assertIn('desktop:local',session_keys('window-a',{'kind':'desktop'}))
        self.assertIn('desktop:local',session_keys('window-b',{'kind':'desktop'}))

    def test_handoff_blocks_actions_and_page_management(self):
        from argparse import Namespace
        control.handoff(Namespace(session='mail',reason='login',instructions='Sign in'))
        for command, options in [('tap',dict(x=1,y=2)),('pages',{}),('start',dict(os='browser'))]:
            result = service.execute(command,'mail',**options)
            self.assertFalse(result['ok'],result)
        result = control.resume(Namespace(session='mail',note='Signed in'))
        self.assertIn('observation',result)
        self.assertNotIn('handoff',ds.load_state('mail'))

    def test_standalone_desktop_handoff_blocks_other_windows(self):
        from argparse import Namespace
        for session in ('one-window', 'other-window'):
            ds.save_state(session, {'kind':'desktop', 'os':'windows', 'app':session})
        control.handoff(Namespace(session='one-window',reason='login',instructions='Sign in'))
        with patch.object(ds,'attach_desktop',return_value=self.device):
            result=service.execute('tap','other-window',x=1,y=2)
            self.assertFalse(result['ok'])
            self.assertEqual(result['error_type'],'BusyError')
            control.resume(Namespace(session='one-window',note='User returned control'))
            self.assertTrue(service.execute('tap','other-window',x=1,y=2)['ok'])

    def test_mcp_image_content_and_journal_uncertainty(self):
        from argus.mcp import server
        content = server.device_observe('mail')
        self.assertEqual([c.type for c in content], ['text','image'])
        self.device.fail=True
        result=service.execute('act','mail',action={'type':'tap','x':1,'y':2})
        self.assertEqual(result['outcome'],'uncertain')
        events=[json.loads(line) for line in Path(result['record']).read_text().splitlines()]
        self.assertIn('dispatching',[e['kind'] for e in events])
        self.assertIn('after',[e['kind'] for e in events])

    def test_interactive_cross_resource_restart_and_no_replay(self):
        store=Store(self.root/'runtime')
        runtime=InteractiveRuntime(store)
        state=runtime.create_task({'phone':'phone','mail':'mail','admin':'admin'})
        tid=state['id']
        state=runtime.observe(tid,'phone')
        oid=state['observations']['phone']['id']
        kwargs=dict(resource='phone', action={'type':'tap','x_pct':50,'y_pct':50}, observation_id=oid,request_id='first')
        state=runtime.submit(tid,**kwargs)
        self.assertEqual(state['status'],'idle',state)
        self.assertEqual(state['cursor'],1)
        InteractiveRuntime(Store(store.root)).submit(tid,**kwargs)
        self.assertEqual(len(self.device.calls),1)
        state=runtime.observe(tid,'admin')
        self.device.fail=True
        state=runtime.submit(tid,'admin',{'type':'tap','x':5,'y':5},state['observations']['admin']['id'],'second')
        self.assertEqual(state['status'],'needs_review')
        recovered=InteractiveRuntime(Store(store.root)).recover_task(tid)
        self.assertEqual(recovered['pending'],state['pending'])
        self.assertEqual(len(self.device.calls),2)
        with self.assertRaises(ValueError):
            runtime.submit(tid,'mail',{'type':'input','text':'hello'},'no-observation','third')
        state=runtime.resolve_task(tid,'completed','Verified resulting screen')
        self.assertEqual(state['status'],'idle')
        self.assertEqual(len(self.device.calls),2)
        runtime.handoff_task(tid,'Please log in')
        self.assertFalse(service.execute('input','mail',text='blocked')['ok'])
        state=runtime.resume_task(tid,'User finished login')
        self.assertEqual(set(state['observations']), {'phone','mail','admin'})
        evidence=runtime.export(tid,str(self.root/'evidence.zip'))
        self.assertTrue(Path(evidence['path']).is_file())

    def test_task_ownership_blocks_cli_and_other_runtime_directories(self):
        runtime = InteractiveRuntime(Store(self.root/'one'))
        task = runtime.create_task({'web':'mail'})
        self.assertFalse(service.execute('tap','mail',x=1,y=2)['ok'])
        other = InteractiveRuntime(Store(self.root/'two'))
        with self.assertRaises(BusyError):
            other.create_task({'web':'mail'})
        runtime.finish(task['id'], 'No action needed')
        self.assertTrue(service.execute('tap','mail',x=1,y=2)['ok'])
        other.create_task({'web':'mail'})
        self.assertTrue(Path(runtime.export(task['id'],str(self.root/'old-task.zip'))['path']).exists())

    def test_explicit_output_does_not_overwrite_observation_evidence(self):
        out = str(self.root/'latest.png')
        old = service.execute('screenshot','mail',out=out)
        self.device.frame = png('black')
        service.execute('screenshot','mail',out=out)
        result = service.execute('act','mail',action={'type':'tap','x':10,'y':10},observation_id=old['id'])
        self.assertFalse(result['ok'])
        self.assertEqual(self.device.calls,[])
        self.assertEqual(Path(old['source_path']).read_bytes(),png())

    def test_crash_pending_recovery_does_not_execute(self):
        store=Store(self.root/'runtime')
        runtime=InteractiveRuntime(store)
        state=runtime.create_task({'web':'mail'})
        state.update(status='running',pending={'id':'attempt','step':'step_a','resource':'web','action':{'type':'tap','x':2,'y':3}})
        store.save(state,'action_dispatching',state['pending'])
        result=runtime.recover_task(state['id'])
        self.assertEqual(result['status'],'needs_review')
        self.assertEqual(self.device.calls,[])

    def test_task_mcp_observe_and_submit_return_images(self):
        from argus.mcp import server
        store = Store(self.root/'runtime')
        with patch('argus.runtime.interactive.default_store', return_value=store):
            task = server.agent_task('create', options={'bindings':{'web':'mail'}})
            seen = server.agent_task('observe', task['id'], {'resource':'web'})
            self.assertEqual([c.type for c in seen], ['text','image'])
            state=json.loads(seen[0].text)
            result=server.agent_task('submit',task['id'],{'resource':'web','action':{'type':'tap','x':1,'y':2},
                'observation_id':state['observations']['web']['id'],'request_id':'mcp-input'})
            self.assertEqual([c.type for c in result], ['text','image'])
            self.assertEqual(json.loads(result[0].text)['status'],'idle')
            server.agent_task('cancel',task['id'],{'note':'End test'})

    def test_mcp_sdk_preserves_image_content(self):
        import asyncio
        from argus.mcp import server
        result = asyncio.run(server.mcp.call_tool('device_observe', {'session':'mail'}))
        self.assertEqual([block.type for block in result], ['text','image'])

    def test_mobile_input_failure_and_unknown_keys_propagate(self):
        from unittest.mock import Mock
        from argus.platforms.appium import AppiumPlatform
        platform = AppiumPlatform()
        platform._os = 'android'
        platform._driver = Mock()
        with self.assertRaises(ValueError):
            platform.press_key('unsupported-key')
        platform._driver.press_keycode.assert_not_called()
        platform._driver.execute_script.side_effect = RuntimeError('offline')
        platform._driver.switch_to.active_element.send_keys.side_effect = RuntimeError('offline')
        with self.assertRaises(RuntimeError):
            platform.input_text('example')

    def test_wait_timeout_and_frame_change(self):
        result=observations.wait(self.device,'change',timeout=.01,interval=.001)
        self.assertTrue(result['timed_out'])
        self.assertEqual(observations.change_fraction(png(),png(dot=[0,0,1,1])),.0002)


if __name__ == '__main__': unittest.main()
