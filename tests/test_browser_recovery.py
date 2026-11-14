"""Challenge recovery through the public client and command interfaces."""
import os
import sys
import unittest
from unittest.mock import Mock, patch

import requests

from qoj_submitter.browser import BrowserError, BrowserSession, ChallengeRecovery
from qoj_submitter.client import QojClient, QojError, SubmissionForm, UnknownSubmission


def response(status=200, text='ok', **headers):
    value = requests.Response()
    value.status_code = status
    value.url = 'https://qoj.ac/'
    value._content = text.encode()
    value.headers.update(headers)
    return value


class ChallengeHandoffTests(unittest.TestCase):
    def test_read_continues_after_browser_verification(self):
        session = requests.Session()
        session.get = Mock(side_effect=[response(403, **{'cf-mitigated': 'challenge'}), response()])
        verify = Mock()
        client = QojClient('https://qoj.ac', session=session, on_challenge=verify)
        self.assertEqual(client.get('/').text, 'ok')
        verify.assert_called_once_with('https://qoj.ac/')
        self.assertEqual(session.get.call_count, 2)

    def test_human_verification_has_a_separate_timeout_from_reads(self):
        clock = [0]
        session = requests.Session()
        session.get = Mock(side_effect=[response(403, **{'cf-mitigated': 'challenge'}), response()])
        def verify(_):
            clock[0] = 120
        with patch('time.monotonic', side_effect=lambda: clock[0]):
            client = QojClient('https://qoj.ac', session=session, on_challenge=verify)
            self.assertEqual(client.get('/').text, 'ok')
        self.assertGreater(session.get.call_args.kwargs['timeout'], 0)

    def test_upload_challenge_opens_browser_but_never_reposts(self):
        session = requests.Session()
        session.post = Mock(return_value=response(403, **{'cf-mitigated': 'challenge'}))
        verify = Mock()
        client = QojClient('https://qoj.ac', session=session, on_challenge=verify)
        form = SubmissionForm('https://qoj.ac/contest/1/problem/2', 'token', ['C++20'])
        with self.assertRaises(UnknownSubmission):
            client.submit(form, 'a.cpp', b'code', 'C++20', '1', recover_receipt=False)
        verify.assert_called_once_with(form.action)
        self.assertEqual(session.post.call_count, 1)

    def test_disabled_browser_preserves_challenge_error(self):
        session = requests.Session()
        session.get = Mock(return_value=response(403, **{'cf-mitigated': 'challenge'}))
        client = QojClient('https://qoj.ac', session=session)
        with self.assertRaisesRegex(QojError, 'Cloudflare'):
            client.get('/')
        self.assertEqual(session.get.call_count, 1)


class RecoveryPolicyTests(unittest.TestCase):
    def test_auto_open_uses_interactive_terminal_or_explicit_browser(self):
        client = QojClient('https://qoj.ac')
        for terminal, executable, disabled, enabled in [
            (True, None, False, True), (False, None, False, False),
            (False, sys.executable, False, True), (True, None, True, False)]:
            with self.subTest(terminal=terminal, explicit=bool(executable), disabled=disabled):
                with patch('sys.stdin.isatty', return_value=terminal):
                    with ChallengeRecovery(client, executable=executable, disabled=disabled):
                        self.assertEqual(client.on_challenge is not None, enabled)
                self.assertIsNone(client.on_challenge)

    def test_repeated_challenge_opens_one_browser_per_command(self):
        session = requests.Session()
        session.get = Mock(return_value=response(403, **{'cf-mitigated': 'challenge'}))
        client = QojClient('https://qoj.ac', session=session)
        with patch('qoj_submitter.browser.BrowserSession') as browser:
            with ChallengeRecovery(client, executable=sys.executable):
                with self.assertRaisesRegex(QojError, 'still required'):
                    client.get('/')
            browser.assert_called_once()
            browser.return_value.verify.assert_called_once()
        self.assertEqual(session.get.call_count, 2)

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux graphical session check')
    def test_no_graphical_session_returns_before_browser_launch(self):
        with patch.dict(os.environ, {'DISPLAY': '', 'WAYLAND_DISPLAY': ''}):
            with patch('qoj_submitter.browser.subprocess.Popen') as launch:
                with self.assertRaisesRegex(BrowserError, 'graphical session'):
                    BrowserSession('https://qoj.ac', sys.executable).start([])
            launch.assert_not_called()


class CleanupTests(unittest.TestCase):
    def test_export_and_close_failures_preserve_success_or_original_error(self):
        for original in (None, UnknownSubmission('saved receipt check'), KeyboardInterrupt()):
            for failing in ('export_cookies', 'close'):
                with self.subTest(original=type(original).__name__, failing=failing):
                    client = QojClient('https://qoj.ac')
                    recovery = ChallengeRecovery(client, disabled=True)
                    recovery.browser = Mock()
                    getattr(recovery.browser, failing).side_effect = BrowserError('cleanup failed')
                    if original is None:
                        with recovery:
                            pass
                    else:
                        with self.assertRaises(type(original)) as caught:
                            with recovery:
                                raise original
                        self.assertIs(caught.exception, original)
                    recovery.browser.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
