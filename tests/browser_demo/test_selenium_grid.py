"""Shared Grid cleanup handles empty slots and independent deletion failures."""
import io
import json
import unittest
from unittest.mock import patch

from saygo.platforms.selenium_grid import cleanup_grid_sessions


class GridCleanupTests(unittest.TestCase):
    def test_failed_deletion_does_not_skip_other_sessions(self):
        status = {'value': {'nodes': [{'slots': [
            {'session': None}, {'session': {'sessionId': 'first'}},
            {'session': {'sessionId': 'second'}}]}]}}
        with patch('urllib.request.urlopen', side_effect=[
            io.BytesIO(json.dumps(status).encode()), OSError('offline'), io.BytesIO(b'')]) as request:
            cleanup_grid_sessions('http://localhost:4444/')
        self.assertEqual(request.call_count, 3)
        self.assertEqual(request.call_args_list[0].args[0], 'http://localhost:4444/status')
        for call, sid in zip(request.call_args_list[1:], ('first', 'second')):
            self.assertEqual(call.args[0].get_method(), 'DELETE')
            self.assertEqual(call.args[0].full_url, f'http://localhost:4444/session/{sid}')

    def test_unavailable_grid_does_not_block_startup(self):
        with patch('urllib.request.urlopen', side_effect=OSError('offline')) as request:
            cleanup_grid_sessions('http://localhost:4444')
        request.assert_called_once()
