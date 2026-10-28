import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock

from qoj_submitter.client import QojClient, QojError
from qoj_submitter.samples import attachment_files, download, organize, write_archives, zip_bytes


class SamplesTests(unittest.TestCase):
    def test_pairs_and_extra_files(self):
        data = zip_bytes({"ex_1.in": b"input\n", "ex_1.ans": b"answer\n"})
        samples, special, count, pairs = organize(attachment_files(data))
        self.assertEqual(samples, {"1.in": b"input\n", "1.ans": b"answer\n"})
        self.assertEqual((special, count, pairs), (False, 1, 1))

        samples, special, count, pairs = organize({"G0a.in": b"a", "G0b.in": b"b", "run.sh": b"x"})
        self.assertEqual(samples, {"1.in": b"a", "2.in": b"b"})
        self.assertEqual((special, count, pairs), (True, 2, 0))

    def test_unsafe_archive_rejected(self):
        for filename in ("../escape.in", "/absolute.in", "folder\\file.in", "C:/outside.in"):
            with self.subTest(filename=filename), self.assertRaises(QojError):
                attachment_files(zip_bytes({filename: b"x"}))

    def test_download_missing_and_interactive(self):
        client = Mock()
        client.get.side_effect = [
            Mock(status_code=200, url="https://qoj.ac/download.php", content=zip_bytes({"one.in": b"x", "judge.cpp": b"code"})),
            Mock(status_code=404, url="https://qoj.ac/download.php", content=b""),
        ]
        contest = {"id": "1", "problems": {"A": {"id": "2"}, "B": {"id": "3"}}}
        samples, extras, warnings = download(client, contest)
        self.assertEqual(samples, {"samples-A/1.in": b"x"})
        self.assertIn("attachment-extras/problem-A/judge.cpp", extras)
        self.assertEqual(len(warnings), 2)

    def test_html_error_is_not_reported_as_missing(self):
        client = Mock()
        client.get.return_value = Mock(status_code=200, url="https://qoj.ac/download.php", content=b"<!doctype html>challenge")
        with self.assertRaisesRegex(QojError, "HTML"):
            download(client, {"id": "1", "problems": {"A": {"id": "2"}}})

    def test_no_overwrite_without_force(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "samples.zip"
            target.write_bytes(b"original")
            with self.assertRaises(QojError):
                write_archives(directory, {"samples-A/1.in": b"x"}, {})
            self.assertEqual(target.read_bytes(), b"original")
            write_archives(directory, {"samples-A/1.in": b"x"}, {}, force=True)
            with zipfile.ZipFile(target) as archive:
                self.assertEqual(archive.read("samples-A/1.in"), b"x")

    def test_live_login_state_parsing(self):
        session = Mock()
        session.headers = {}
        session.get.return_value = Mock(status_code=200, url="https://qoj.ac/", text='<a href="/user/profile/Alice">My Profile</a><a href="/logout?token=private">Logout</a>')
        client = QojClient("https://qoj.ac", session)
        self.assertEqual(client.status(), "Alice")
        session.get.return_value.text = '<a href="/login">Login</a>'
        self.assertIsNone(client.status())
        session.get.return_value.text = '<p>unexpected response</p>'
        with self.assertRaises(QojError):
            client.status()


if __name__ == "__main__":
    unittest.main()
