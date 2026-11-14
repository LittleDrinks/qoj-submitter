"""Optional desktop-browser acceptance against a controlled verification page.

Set QOJ_BROWSER_TEST_EXECUTABLE to run with an installed Firefox/Chrome/Edge.
The test clicks its own fixture button; it never solves a real Cloudflare challenge.
"""
import os
import sys
import time
import unittest
from pathlib import Path
from http.server import ThreadingHTTPServer
import threading
import tempfile

import test_cli_recovery as recovery
from test_cli_recovery import Handler, SOURCE


class BrowserHandler(Handler):
    def verified(self):
        return 'fixture_verified=yes' in self.headers.get('Cookie', '')

    def do_GET(self):
        state = self.server
        should_verify = state.browser_challenge and (state.uploads or '/submissions' in self.path)
        if should_verify and not self.verified():
            return self.reply('''<!doctype html><meta charset="utf-8"><title>QOJ verification test</title>
              <h1>Browser handoff test</h1><p>This local page tests human verification recovery.</p>
              <button id="test-verify" onclick="window.fixtureVerification=fetch('/verify',{method:'POST'}).then(()=>this.textContent='Test verification complete')">Complete test verification</button>''',
              403, {'cf-mitigated': 'challenge'})
        if self.path == '/contest/4600/problem/23604':
            token = 'browser-token' if self.verified() else 'direct-token'
            return self.reply(f"<a href='/logout'>Logout</a><a href='/user/profile/{state.user}'>My Profile</a>"
                f"<form id='form-answer'><input name='_token' value='{token}'>"
                "<select name='answer_answer_language'><option value='C++20'>C++20</option></select></form>")
        return super().do_GET()

    def do_POST(self):
        if self.path == '/verify':
            self.rfile.read(int(self.headers.get('Content-Length', '0')))
            self.server.verifications += 1
            if self.server.change_user:
                self.server.user = 'OtherAccount'
            return self.reply('verified', headers={'Set-Cookie': 'fixture_verified=yes; Path=/; HttpOnly; SameSite=Lax'})
        self.server.upload_pid = int(self.headers.get('X-Qoj-Test-Pid', '0'))
        payload = self.rfile.read(int(self.headers['Content-Length']))
        token = b'browser-token' if self.verified() else b'direct-token'
        if token not in payload:
            return self.reply('expired form', 403)
        self.server.uploads.append(payload)
        self.server.accepted.set()
        if self.server.challenge_upload_response:
            self.server.browser_challenge = True
            return self.reply('Verify the browser', 403, {'cf-mitigated': 'challenge'})
        # Deliberately return only the list after accepting the one upload.
        return self.reply('', self.server.upload_redirect, {'Location': '/contest/4600/submissions'})


@unittest.skipUnless(os.environ.get('QOJ_BROWSER_TEST_EXECUTABLE'), 'Set QOJ_BROWSER_TEST_EXECUTABLE for a desktop browser')
class BrowserCliTests(unittest.TestCase):
    start_cli = recovery.CliRecoveryTests.start_cli
    run_cli = recovery.CliRecoveryTests.run_cli
    assert_ok = recovery.CliRecoveryTests.assert_ok
    close_server = recovery.CliRecoveryTests.close_server

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'a.cpp'; self.source.write_bytes(SOURCE)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), BrowserHandler)
        for name, value in dict(uploads=[], list_statuses=[], hidden_source=0, list_requests=0,
            retry_after='0', user='Tester', challenge=False, challenge_before=False,
            post_response=None, browser_challenge=False, verifications=0, change_user=False, upload_redirect=302, challenge_upload_response=False).items():
            setattr(self.server, name, value)
        self.server.accepted = threading.Event(); self.server.release = threading.Event()
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.close_server)
        self.env = {**os.environ, 'XDG_DATA_HOME': str(self.root/'state'),
                    'QOJ_TEST_PORT': str(self.server.server_port), 'QOJ_BROWSER_TEST_MODE': 'pass',
                    'PYTHONPATH': str(Path(__file__).parent/'loopback')}
        self.browser = os.environ['QOJ_BROWSER_TEST_EXECUTABLE']
        self.assert_ok(self.run_cli('qoj', 'contest', '4600'))

    def submit(self, command='submit', *prefix):
        return self.run_cli(command, *prefix, str(self.source), '-y', '--browser', self.browser, '--browser-timeout', '10')

    def test_before_upload_verification_refreshes_form_and_uploads_once(self):
        self.server.browser_challenge = True
        result = self.submit()
        self.assert_ok(result)
        self.assertIn('/submission/1', result[1])
        self.assertEqual(self.server.verifications, 1)
        self.assertEqual(len(self.server.uploads), 1)
        self.assertIn(b'browser-token', self.server.uploads[0])

    def test_browser_upload_redirects_never_repeat_the_post(self):
        self.server.browser_challenge = True
        for status in (307, 308):
            with self.subTest(status=status):
                self.server.upload_redirect = status
                result = self.submit()
                self.assert_ok(result)
                self.assertEqual(len(self.server.uploads), status - 306)

    def test_accepted_upload_then_challenge_recovers_without_second_upload(self):
        # Only the list read after upload becomes a challenge.
        original = self.server.finish_request
        def finish(request, address):
            if self.server.uploads:
                self.server.browser_challenge = True
            return original(request, address)
        self.server.finish_request = finish
        result = self.submit('qoj', 'submit')
        self.assert_ok(result)
        self.assertIn('/submission/1', result[1])
        self.assertEqual(self.server.verifications, 1)
        self.assertEqual(len(self.server.uploads), 1)

    def test_upload_challenge_recovers_receipt_without_second_upload(self):
        self.server.challenge_upload_response = True
        result = self.submit()
        self.assert_ok(result)
        self.assertEqual(self.server.verifications, 1)
        self.assertEqual(len(self.server.uploads), 1)

    def test_repeated_submit_resumes_cancelled_verification(self):
        self.server.challenge_upload_response = True
        self.env['QOJ_BROWSER_TEST_MODE'] = 'close'
        result = self.submit()
        self.assertEqual(result[0], 2, result)
        self.env['QOJ_BROWSER_TEST_MODE'] = 'pass'
        result = self.submit()
        self.assert_ok(result)
        self.assertEqual(len(self.server.uploads), 1)

    def test_account_change_during_verification_stops_before_upload(self):
        self.server.browser_challenge = self.server.change_user = True
        result = self.submit('qoj-submit')
        self.assertEqual(result[0], 1, result)
        self.assertIn('account changed', result[2])
        self.assertEqual(len(self.server.uploads), 0)

    def test_timeout_preserves_accepted_operation_for_next_process(self):
        original = self.server.finish_request
        def finish(request, address):
            if self.server.uploads:self.server.browser_challenge = True
            return original(request, address)
        self.server.finish_request = finish
        self.env['QOJ_BROWSER_TEST_MODE'] = 'timeout'
        result = self.submit()
        self.assertEqual(result[0], 2, result)
        self.assertIn('timed out', result[2])
        self.source.unlink()
        self.env['QOJ_BROWSER_TEST_MODE'] = 'pass'
        self.assert_ok(self.run_cli('qoj', 'recover', '--browser', self.browser, '--browser-timeout', '10'))
        self.assertEqual(len(self.server.uploads), 1)

    def test_closed_window_preserves_accepted_operation(self):
        original = self.server.finish_request
        def finish(request, address):
            if self.server.uploads:self.server.browser_challenge = True
            return original(request, address)
        self.server.finish_request = finish
        self.env['QOJ_BROWSER_TEST_MODE'] = 'close'
        result = self.submit()
        self.assertEqual(result[0], 2, result)
        self.env['QOJ_BROWSER_TEST_MODE'] = 'pass'
        self.assert_ok(self.run_cli('qoj', 'recover', '--browser', self.browser, '--browser-timeout', '10'))
        self.assertEqual(len(self.server.uploads), 1)


if __name__ == '__main__':
    unittest.main()
