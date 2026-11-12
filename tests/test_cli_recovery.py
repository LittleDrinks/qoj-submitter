"""Exercise installed commands across real HTTP failures and process restarts."""
import html
import os
from pathlib import Path
import socket
import signal
import subprocess
import sysconfig
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


SOURCE = b'int main() { return 0; }\r\n'


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, text, status=200, headers=None):
        data = text.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        state = self.server
        if state.challenge and (state.uploads or state.challenge_before):
            self.reply('Verify your browser', 200, {'cf-mitigated': 'challenge'})
            return
        if self.path == '/contest/4600':
            self.reply("<title>Recovery test</title><table><tr><td>A</td>"
                       "<td><a href='/contest/4600/problem/23604'>Example</a></td></tr></table>")
        elif self.path == '/contest/4600/problem/23604':
            self.reply("<form id='form-answer'><input name='_token' value='test-token'>"
                       "<select name='answer_answer_language'><option value='C++20'>C++20</option></select></form>")
        elif self.path == '/contest/4600/submissions':
            state.list_requests += 1
            if state.list_statuses:
                status = state.list_statuses.pop(0)
                if status != 200:
                    self.reply('Temporarily unavailable', status, {'Retry-After': state.retry_after})
                    return
            rows = ''.join(f"<tr><td><a href='/submission/{i}'>ID</a></td>"
                           "<td><a href='/contest/4600/problem/23604'>A</a></td>"
                           "<td><span class='uoj-username'>Tester</span></td><td>C++20</td></tr>"
                           for i in range(1, len(state.uploads) + 1))
            self.reply(f"<a href='/logout'>Logout</a><a href='/user/profile/{state.user}'>My Profile</a>"
                       "<table><tr><th>ID</th><th>Problem</th><th>Submitter</th><th>Language</th></tr>"
                       + rows + '</table>')
        elif self.path.startswith('/submission/'):
            if state.hidden_source:
                state.hidden_source -= 1
                self.reply('Judging')
                return
            self.reply('<pre><code class="sh_cpp">' + html.escape(SOURCE.decode()) + '</code></pre>')
        else:
            self.reply('Not found', 404)

    def do_POST(self):
        self.server.upload_pid = int(self.headers['X-Qoj-Test-Pid'])
        self.server.uploads.append(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.accepted.set()
        self.server.release.wait(15)
        if self.server.post_response:
            status, headers = self.server.post_response
            self.reply('', status, headers)
            return
        # The service accepted the source; the client never receives a response.
        self.connection.shutdown(socket.SHUT_RDWR)
        self.connection.close()


class CliRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'a.cpp'
        self.source.write_bytes(SOURCE)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.uploads = []
        self.server.list_statuses = []
        self.server.hidden_source = 0
        self.server.list_requests = 0
        self.server.retry_after = '0'
        self.server.user = 'Tester'
        self.server.challenge = False
        self.server.challenge_before = False
        self.server.post_response = None
        self.server.accepted = threading.Event()
        self.server.release = threading.Event()
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.close_server)
        self.env = {**os.environ, 'XDG_DATA_HOME': str(self.root / 'state'),
                    'QOJ_TEST_PORT': str(self.server.server_port),
                    'PYTHONPATH': str(Path(__file__).parent / 'loopback')}
        self.assert_ok(self.run_cli('qoj', 'contest', '4600'))

    def close_server(self):
        self.server.release.set()
        self.server.shutdown()
        self.server.server_close()

    def start_cli(self, command, *args):
        suffix = '.exe' if os.name == 'nt' else ''
        entry = Path(sysconfig.get_path('scripts')) / (command + suffix)
        return subprocess.Popen([str(entry), *args], env=self.env, cwd=self.root,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, encoding='utf-8')

    def run_cli(self, command, *args, input=None):
        process = self.start_cli(command, *args)
        try:
            out, err = process.communicate(input=input, timeout=25)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)
            raise
        return process.returncode, out, err

    def assert_ok(self, result):
        self.assertEqual(result[0], 0, result[1] + result[2])

    def interrupted_submit(self, command='submit', *prefix):
        self.server.accepted.clear()
        self.server.release.clear()
        process = self.start_cli(command, *prefix, str(self.source), '-y')
        try:
            self.assertTrue(self.server.accepted.wait(10), 'CLI did not upload')
        finally:
            if os.name == 'nt':
                os.kill(self.server.upload_pid, signal.SIGTERM)
            else:
                process.kill()
            process.communicate(timeout=5)
            self.server.release.set()

    def test_process_restart_recovers_receipt_without_second_upload(self):
        self.interrupted_submit()
        self.source.unlink()
        result = self.run_cli('qoj', 'recover')
        self.assert_ok(result)
        self.assertIn('https://qoj.ac/submission/1', result[1])
        self.assertEqual(len(self.server.uploads), 1)
        self.assertIn(SOURCE, self.server.uploads[0])

    def test_lost_response_recovers_through_temporary_read_failure_and_delayed_source(self):
        self.server.release.set()
        self.server.list_statuses = [200, 503]
        self.server.hidden_source = 1
        result = self.run_cli('qoj-submit', str(self.source), '-y')
        self.assert_ok(result)
        self.assertIn('https://qoj.ac/submission/1', result[1])
        self.assertEqual(len(self.server.uploads), 1)

    def test_all_entrypoints_resume_the_same_intent_and_allow_a_later_new_submission(self):
        for command, prefix in [('submit', ()), ('qoj-submit', ()), ('qoj', ('submit',))]:
            with self.subTest(command=command):
                count = len(self.server.uploads)
                self.interrupted_submit(command, *prefix)
                result = self.run_cli(command, *prefix, str(self.source), '-y')
                self.assert_ok(result)
                self.assertIn(f'/submission/{count + 1}', result[1])
                self.assertEqual(len(self.server.uploads), count + 1)
                self.assert_ok(self.run_cli(command, *prefix, str(self.source), '-y'))
                self.assertEqual(len(self.server.uploads), count + 2)

    def test_changed_source_requires_recovering_or_releasing_saved_intent(self):
        self.interrupted_submit()
        self.source.write_bytes(b'int main() { return 1; }')
        result = self.run_cli('submit', str(self.source), '-y')
        self.assertEqual(result[0], 1, result)
        self.assertIn('qoj recover 1', result[2])
        self.assert_ok(self.run_cli('qoj', 'recover', '1'))
        self.assertEqual(len(self.server.uploads), 1)

    def test_concurrent_command_cannot_upload_while_first_command_is_active(self):
        process = self.start_cli('submit', str(self.source), '-y')
        try:
            self.assertTrue(self.server.accepted.wait(10))
            result = self.run_cli('qoj', 'submit', str(self.source), '-y')
            self.assertEqual(result[0], 1, result)
            self.assertIn('Another submit or recover command', result[2])
        finally:
            self.server.release.set()
            out, err = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0, out + err)
        self.assertEqual(len(self.server.uploads), 1)

    def test_account_change_keeps_intent_pending_until_original_account_returns(self):
        self.interrupted_submit()
        self.server.user = 'OtherAccount'
        result = self.run_cli('qoj', 'recover')
        self.assertEqual(result[0], 2, result)
        self.assertIn('belongs to Tester', result[2])
        self.server.user = 'Tester'
        self.assert_ok(self.run_cli('qoj', 'recover'))
        self.assertEqual(len(self.server.uploads), 1)

    def test_cloudflare_challenge_preserves_intent_for_later_recovery(self):
        self.server.release.set()
        self.server.challenge = True
        result = self.run_cli('submit', str(self.source), '-y')
        self.assertEqual(result[0], 2, result)
        self.assertIn('Cloudflare', result[2])
        self.assertIn('qoj recover 1', result[2])
        self.server.challenge = False
        self.assert_ok(self.run_cli('submit', str(self.source), '-y'))
        self.assertEqual(len(self.server.uploads), 1)

    def test_challenge_before_upload_stops_with_an_actionable_error(self):
        self.server.challenge = self.server.challenge_before = True
        result = self.run_cli('submit', str(self.source), '-y')
        self.assertEqual(result[0], 1, result)
        self.assertIn('Cloudflare', result[2])
        self.assertEqual(len(self.server.uploads), 0)

    def test_server_error_after_acceptance_recovers_receipt(self):
        self.server.release.set()
        self.server.post_response = (502, {})
        result = self.run_cli('qoj', 'submit', str(self.source), '-y')
        self.assert_ok(result)
        self.assertIn('/submission/1', result[1])
        self.assertEqual(len(self.server.uploads), 1)

    def test_persistent_read_failures_stop_and_later_resume_without_uploading(self):
        self.server.release.set()
        self.server.list_statuses = [200, 503, 503, 503]
        result = self.run_cli('submit', str(self.source), '-y')
        self.assertEqual(result[0], 2, result)
        self.assertIn('retry limit', result[2])
        self.assertEqual(self.server.list_requests, 4)
        self.assert_ok(self.run_cli('qoj', 'recover'))
        self.assertEqual(len(self.server.uploads), 1)

    def test_rate_limit_honors_retry_after(self):
        self.server.release.set()
        self.server.list_statuses = [429, 200]
        self.server.retry_after = '1'
        started = time.monotonic()
        self.assert_ok(self.run_cli('submit', str(self.source), '-y'))
        self.assertGreaterEqual(time.monotonic() - started, 1)
        self.assertEqual(len(self.server.uploads), 1)

    def test_retry_after_exceeding_budget_returns_promptly_before_upload(self):
        self.server.list_statuses = [429]
        self.server.retry_after = '120'
        result = self.run_cli('submit', str(self.source), '-y')
        self.assertEqual(result[0], 1, result)
        self.assertIn('budget exhausted', result[2])
        self.assertEqual(len(self.server.uploads), 0)

    def test_abandon_requires_confirmation_and_releases_only_selected_intent(self):
        self.interrupted_submit()
        self.assert_ok(self.run_cli('qoj', 'recover', '1', '--abandon', input='n\n'))
        self.assert_ok(self.run_cli('submit', str(self.source), '-y'))
        self.assertEqual(len(self.server.uploads), 1)
        self.interrupted_submit()
        self.assert_ok(self.run_cli('qoj', 'recover', '2', '--abandon', input='y\n'))
        self.assert_ok(self.run_cli('submit', str(self.source), '-y'))
        self.assertEqual(len(self.server.uploads), 3)

    def test_saved_confirmed_receipt_is_available_without_network(self):
        self.server.release.set()
        self.assert_ok(self.run_cli('submit', str(self.source), '-y'))
        self.server.challenge = self.server.challenge_before = True
        result = self.run_cli('qoj', 'recover', '1')
        self.assert_ok(result)
        self.assertIn('/submission/1', result[1])

    def test_retry_after_http_date_exceeding_budget_preserves_pending_intent(self):
        self.interrupted_submit()
        self.server.list_statuses = [429]
        self.server.retry_after = format_datetime(datetime.now(timezone.utc) + timedelta(minutes=2), usegmt=True)
        result = self.run_cli('qoj', 'recover')
        self.assertEqual(result[0], 2, result)
        self.assertIn('budget exhausted', result[2])
        self.assert_ok(self.run_cli('qoj', 'recover'))
        self.assertEqual(len(self.server.uploads), 1)


if __name__ == '__main__':
    unittest.main()
