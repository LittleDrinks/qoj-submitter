import tempfile
import unittest
import argparse
import contextlib
import io
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from qoj_submitter.cli import choose_language, check_contest_host, make_client, run_submit, save_cookies
from qoj_submitter.client import (QojClient, QojError, UnknownSubmission, base_url,
                                  contest_id, contest_problems, login_token, submission_form)


CONTEST = """<title>Round - Dashboard - Contest - QOJ.ac</title><table>
<tr><td>A</td><td><a href='/contest/3387/problem/15419'>New Gomoku</a></td></tr>
<tr><td>B</td><td><a href='/contest/3387/problem/15420'>Abstract Portal</a></td></tr></table>"""
FORM = """<form id='form-answer' action='/contest/3387/problem/15419/statement/zh_cn'>
<input name='_token' value='test-token'><select name='answer_answer_language'>
<option value='C++20'>C++ 20</option><option value='Python3'>Python 3</option></select></form>"""
DYNAMIC_FORM = r'''<form id="form-answer" action="/contest/3387/problem/15419/statement/zh_cn">
<input name="_token" value="test-token"><div id="form-group-answer_answer"></div></form>
<script>$('#form-group-answer_answer').source_code_form_group('answer_answer', 'Source code:answer', "<option value=\"C++20\">C++ 20<\/option><option value=\"Python3\">Python 3<\/option>", true);</script>'''


class ProtocolTests(unittest.TestCase):
    def test_contest_url(self):
        self.assertEqual(contest_id("https://contest.ucup.ac/contest/3387?x=1"), "3387")
        for value in ("https://evil.test/contest/3387", "https://qoj.ac:443/contest/3387", "https://evil@qoj.ac/contest/3387"):
            with self.assertRaises(QojError):
                contest_id(value)
        with self.assertRaises(QojError):
            check_contest_host("https://contest.ucup.ac/contest/3387", "https://qoj.ac")
        with self.assertRaises(QojError):
            base_url("http://qoj.ac")

    def test_parsers(self):
        self.assertEqual(contest_problems(CONTEST, "3387")["B"]["id"], "15420")
        self.assertEqual(login_token("<script>function submitLoginPost() {x={_token: 'abc'};}</script>"), "abc")
        form = submission_form(FORM, "https://qoj.ac/contest/3387/problem/15419", "https://qoj.ac", "3387", "15419")
        self.assertEqual(form.languages, ["C++20", "Python3"])
        self.assertEqual(choose_language(form.languages, None, "a.cpp"), "C++20")
        with self.assertRaises(QojError):
            choose_language(form.languages, "C++26", "a.cpp")
        with self.assertRaises(QojError):
            submission_form(FORM.replace("/contest/3387", "https://evil.test/contest/3387"),
                            "https://qoj.ac/x", "https://qoj.ac", "3387", "15419")

    def test_dynamic_language_options(self):
        form = submission_form(DYNAMIC_FORM, "https://qoj.ac/contest/3387/problem/15419", "https://qoj.ac", "3387", "15419")
        self.assertEqual(form.languages, ["C++20", "Python3"])

    def test_login_and_submit(self):
        session = Mock()
        session.headers = {}
        session.get.return_value = Mock(status_code=200, url="https://qoj.ac/login", text="<script>function submitLoginPost() {x={_token: 'abc'};}</script>")
        session.post.return_value = Mock(status_code=200, text="ok")
        client = QojClient("https://qoj.ac", session)
        client.login("user", "password")
        self.assertNotEqual(session.post.call_args.kwargs["data"]["password"], "password")
        form = submission_form(FORM, "https://qoj.ac/contest/3387/problem/15419", "https://qoj.ac", "3387", "15419")
        session.post.return_value = Mock(status_code=302, headers={"Location": "/submission/123"})
        self.assertEqual(client.submit(form, "a.cpp", b"code", "C++20", "3387"), "https://qoj.ac/submission/123")
        session.post.return_value = Mock(status_code=200, headers={})
        with self.assertRaises(UnknownSubmission):
            client.submit(form, "a.cpp", b"code", "C++20", "3387")
        session.post.side_effect = requests.Timeout()
        with self.assertRaises(UnknownSubmission):
            client.submit(form, "a.cpp", b"code", "C++20", "3387")

    def test_login_rejects_2fa(self):
        session = Mock()
        session.headers = {}
        session.get.return_value = Mock(status_code=200, url="https://qoj.ac/login", text="<script>function submitLoginPost() {x={_token: 'abc'};}</script>")
        session.post.return_value = Mock(status_code=200, text="2fa")
        with self.assertRaisesRegex(QojError, "Two-factor"):
            QojClient("https://qoj.ac", session).login("user", "secret")

    def test_cookie_storage(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            client = make_client(root, "https://qoj.ac")
            save_cookies(root, client)
            self.assertEqual((root / "qoj.ac.cookies").stat().st_mode & 0o777, 0o600)
            self.assertNotEqual(client.session.cookies.filename, make_client(root, "https://contest.ucup.ac").session.cookies.filename)

    def test_dry_run_does_not_submit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "a.cpp"
            source.write_text("int main() {}", encoding="utf-8")
            args = argparse.Namespace(contest=None, problem=None, language=None,
                                      filename=str(source), dry_run=True, assume_yes=False)
            config = {"host": "https://qoj.ac", "contests": {"https://qoj.ac": {
                "id": "3387", "name": "Round", "problems": contest_problems(CONTEST, "3387")}}}
            client = Mock()
            client.form.return_value = submission_form(FORM, "https://qoj.ac/contest/3387/problem/15419", "https://qoj.ac", "3387", "15419")
            with patch("qoj_submitter.cli.make_client", return_value=client), patch("qoj_submitter.cli.save_cookies"), contextlib.redirect_stdout(io.StringIO()) as output:
                run_submit(args, root, config)
            client.submit.assert_not_called()
            self.assertIn("Dry run: no submission sent", output.getvalue())
            client.form.assert_called_once_with("3387", "15419")


if __name__ == "__main__":
    unittest.main()
