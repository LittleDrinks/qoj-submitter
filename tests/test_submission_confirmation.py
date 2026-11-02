import html
import unittest
from unittest.mock import Mock, patch

import requests

from qoj_submitter.client import QojClient, QojError, SubmissionForm, UnknownSubmission


SOURCE = b'#include <iostream>\r\nint main() { std::cout << "ok"; }\r\n'


def listing(*rows, user="LittleDrinks"):
    header = f'''<a href='/logout'>Logout</a>
    <a href='//qoj.ac/user/profile/{user}'>My Profile</a>
    <table><thead><tr><th>ID</th><th>Problem</th><th>Submitter</th>
    <th>Result</th><th>Language</th></tr></thead><tbody>'''
    return header + "".join(
        f'''<tr><td><a href='/submission/{sid}'>#{sid}</a></td>
        <td><a href='/contest/4600/problem/{pid}'>M. Minimum Records</a></td>
        <td><span class='uoj-username' data-nickname='{owner}'>{owner}</span></td>
        <td>Judging</td><td><a href='/submission/{sid}'>{language}</a></td></tr>'''
        for sid, pid, owner, language in rows
    ) + "</tbody></table>"


OLD = (3345779, "23604", "LittleDrinks", "C++20")
NEW = (3345815, "23604", "LittleDrinks", "C++20")


def response(text, path="/contest/4600/submissions"):
    return Mock(status_code=200, url="https://qoj.ac" + path, text=text)


def source_response(source=SOURCE, sid=NEW[0]):
    code = html.escape(source.decode("utf-8").replace("\r\n", "\n"))
    return response(f'<pre><code class="sh_cpp">{code}</code></pre>',
                    f"/submission/{sid}")


class SubmissionConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock(headers={})
        # Captured Windows response: HTTP 302, empty body, this Location.
        self.session.post.return_value = Mock(status_code=302, text="", headers={
            "Location": "/contest/4600/submissions"})
        self.client = QojClient("https://qoj.ac", self.session)
        self.form = SubmissionForm(
            "https://qoj.ac/contest/4600/problem/23604/statement/zh_cn",
            "test-token", ["C++20"])
        sleep_patch = patch("time.sleep")
        self.sleep = sleep_patch.start()
        self.addCleanup(sleep_patch.stop)

    def submit(self):
        return self.client.submit(self.form, "m.cpp", SOURCE, "C++20", "4600")

    def test_captured_list_redirect_confirms_new_submission(self):
        self.session.get.side_effect = [response(listing(OLD)), response(listing(NEW, OLD)),
                                        source_response()]
        self.assertEqual(self.submit(), "https://qoj.ac/submission/3345815")
        self.session.post.assert_called_once()
        self.assertEqual(self.session.method_calls[0][0], "get")

    def test_first_submission_with_empty_baseline(self):
        self.session.get.side_effect = [response(listing()), response(listing(NEW)), source_response()]
        self.assertEqual(self.submit(), "https://qoj.ac/submission/3345815")

    def test_old_submission_is_never_reported_as_new(self):
        self.session.get.return_value = response(listing(OLD))
        with self.assertRaises(UnknownSubmission):
            self.submit()
        self.session.post.assert_called_once()
        self.assertLessEqual(self.session.get.call_count, 4)

    def test_delayed_list_visibility_only_retries_reads(self):
        self.session.get.side_effect = [response(listing(OLD)), response(listing(OLD)),
                                        response(listing(NEW, OLD)), source_response()]
        self.assertEqual(self.submit(), "https://qoj.ac/submission/3345815")
        self.session.post.assert_called_once()

    def test_unrelated_rows_are_ignored(self):
        unrelated = [(3345818, "23604", "OtherUser", "C++20"),
                     (3345817, "23600", "LittleDrinks", "C++20"),
                     (3345816, "23604", "LittleDrinks", "C++23")]
        self.session.get.side_effect = [response(listing(OLD)),
                                        response(listing(*unrelated, NEW, OLD)), source_response()]
        self.assertEqual(self.submit(), "https://qoj.ac/submission/3345815")

    def test_language_column_can_follow_time_and_memory(self):
        def with_metrics(text):
            return text.replace("<th>Language</th>", "<th>Time</th><th>Memory</th><th>Language</th>").replace(
                "<td>Judging</td>", "<td>Judging</td><td>10 ms</td><td>1024 KB</td>")
        self.session.get.side_effect = [response(with_metrics(listing(OLD))),
                                        response(with_metrics(listing(NEW, OLD))), source_response()]
        self.assertEqual(self.submit(), "https://qoj.ac/submission/3345815")

    def test_raw_username_with_contest_provenance_link(self):
        text = listing(NEW, OLD).replace("</span></td>", "</span><sup><a href='/contest/4600'>#</a></sup></td>")
        self.session.get.side_effect = [response(listing(OLD)), response(text), source_response()]
        self.assertEqual(self.submit(), "https://qoj.ac/submission/3345815")

    def test_source_comparison_preserves_unicode_and_normalizes_crlf(self):
        source = '// 中文\r\nint main() {}\r\n'.encode()
        raw_code = '<pre><code class="sh_cpp">' + html.escape(source.decode()) + '</code></pre>'
        self.session.get.side_effect = [response(listing(OLD)), response(listing(NEW, OLD)),
                                        response(raw_code, f"/submission/{NEW[0]}")]
        self.assertEqual(self.client.submit(self.form, "m.cpp", source, "C++20", "4600"),
                         "https://qoj.ac/submission/3345815")

    def test_different_source_is_not_confirmation(self):
        self.session.get.side_effect = [response(listing(OLD)), response(listing(NEW, OLD)),
                                        source_response(b"int main() {}"),
                                        response(listing(NEW, OLD)), response(listing(NEW, OLD))]
        with self.assertRaises(UnknownSubmission):
            self.submit()
        self.session.post.assert_called_once()

    def test_multiple_identical_new_submissions_are_ambiguous(self):
        other = (3345816, *NEW[1:])
        self.session.get.side_effect = [response(listing(OLD)), response(listing(other, NEW, OLD)),
                                        source_response(sid=other[0]), source_response()]
        with self.assertRaises(UnknownSubmission):
            self.submit()
        self.session.post.assert_called_once()

    def test_read_failure_after_post_preserves_unknown_outcome(self):
        self.session.get.side_effect = [response(listing(OLD)), requests.Timeout()]
        with self.assertRaises(UnknownSubmission):
            self.submit()
        self.session.post.assert_called_once()

    def test_invalid_baseline_prevents_post(self):
        self.session.get.return_value = response("<a href='/login'>Login</a>")
        with self.assertRaises(QojError):
            self.submit()
        self.session.post.assert_not_called()

    def test_baseline_network_error_prevents_post(self):
        self.session.get.side_effect = requests.Timeout()
        with self.assertRaises(QojError):
            self.submit()
        self.session.post.assert_not_called()

    def test_account_change_after_post_is_not_confirmation(self):
        self.session.get.side_effect = [response(listing(OLD)), response(listing(NEW, user="OtherUser"))]
        with self.assertRaises(UnknownSubmission):
            self.submit()
        self.session.post.assert_called_once()

    def test_unexpected_redirect_is_not_followed(self):
        for location in ("https://evil.test/submission/3345815", "/contest/999/submissions", "/login"):
            with self.subTest(location=location):
                self.session.get.return_value = response(listing(OLD))
                self.session.post.return_value.headers = {"Location": location}
                with self.assertRaises(UnknownSubmission):
                    self.submit()
        self.assertEqual(self.session.get.call_count, 3)


if __name__ == "__main__":
    unittest.main()
