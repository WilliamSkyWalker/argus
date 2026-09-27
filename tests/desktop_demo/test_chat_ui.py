"""Offline checks for conversation rendering and safe task interaction."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import unittest
from unittest.mock import patch

try:
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
except ImportError:
    QApplication = None


@unittest.skipIf(QApplication is None, 'PySide6 is optional')
class ChatTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from saygo.desktop.app import make_window
        self.patches = [patch('saygo.devices.control.sessions', return_value=[]),
                        patch('saygo.runtime.interactive.call', return_value={'tasks': []}),
                        patch('saygo.desktop.model.load_settings', side_effect=ValueError('not configured'))]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.window = make_window()
        self.addCleanup(self.window.close)

    def test_messages_escape_markup_and_include_human_reply(self):
        state = {'desktop_goal': '<b>literal task</b>', 'status': 'waiting_for_human',
                 'human': {'instructions': 'Please sign in'}}
        self.window.render_conversation(state, [
            {'kind': 'agent_annotation', 'note': 'Opening settings'},
            {'kind': 'human_returned', 'note': 'Signed in, continue'}])
        text = self.window.timeline.toPlainText()
        for expected in ('<b>literal task</b>', 'Opening settings', 'Signed in, continue', 'Please sign in'):
            self.assertIn(expected, text)
        self.assertNotIn('agent_annotation', text)

    def test_reply_resumes_and_uncertain_action_does_not_dispatch(self):
        self.window.goal.setPlainText('I have signed in')
        self.window.task_state = {'status': 'waiting_for_human'}
        with patch.object(self.window, 'resume_task') as resume:
            self.window.send_message()
            resume.assert_called_once_with('I have signed in')
        self.window.task_state = {'status': 'needs_review'}
        with patch.object(self.window, 'start_task') as start, patch('PySide6.QtWidgets.QMessageBox.information'):
            self.window.send_message()
            start.assert_not_called()

    def test_enter_sends_and_shift_enter_inserts_newline(self):
        self.window.goal.setPlainText('Task')
        calls = []
        self.window.goal.submitted.connect(lambda: calls.append(True))
        QTest.keyClick(self.window.goal, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
        self.assertEqual(calls, [])
        self.assertIn('\n', self.window.goal.toPlainText())
        QTest.keyClick(self.window.goal, Qt.Key.Key_Return)
        self.assertEqual(calls, [True])
