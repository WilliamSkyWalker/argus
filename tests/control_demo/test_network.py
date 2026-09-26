"""Network CLI must select an existing extension session without starting devices."""
import contextlib
import io
import json
import sys
import unittest
from unittest.mock import Mock, patch

from argus.cli import main
from argus.platforms import device_session as ds


class NetworkCLITests(unittest.TestCase):
    def test_routes_query_and_releases_only_controller(self):
        controller = Mock()
        controller.network.return_value = {'events': [], 'active': True}
        with patch.object(sys, 'argv', ['argus', 'device', 'network', '--session', 'web',
                '--after', '3', '--url', '/api', '--kind', 'ws', '--capture-id', 'capture']), \
                patch.object(ds, 'load_state', return_value={'kind':'browser','browser_backend':'extension'}), \
                patch.object(ds, 'attach_browser', return_value=controller) as attach, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            main()
        attach.assert_called_once_with('web', backend='extension')
        controller.network.assert_called_once_with('read', after=3, limit=100, url='/api', kind='ws', capture_id='capture')
        self.assertTrue(json.loads(output.getvalue())['active'])
        controller.disconnect.assert_called_once()
        controller.teardown.assert_not_called()

    def test_unsupported_backend_never_starts_a_browser(self):
        with patch.object(sys, 'argv', ['argus','device','network','--session','web']), \
                patch.object(ds, 'load_state', return_value={'kind':'browser','browser_backend':'playwright'}), \
                patch.object(ds, 'start') as start, patch.object(ds, 'attach_browser') as attach, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(SystemExit): main()
        start.assert_not_called(); attach.assert_not_called()
        self.assertIn('extension', json.loads(output.getvalue())['error'])
