import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

from qoj_submitter.curl_adapter import CurlAdapter


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Set-Cookie", "session=test-session; Path=/; HttpOnly")
        self.end_headers()
        self.wfile.write(b"ready")

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(200 if self.headers.get("Cookie") == "session=test-session"
                           and b"sample-code" in body else 400)
        self.end_headers()
        self.wfile.write(b"ok")


class CurlTransportTests(unittest.TestCase):
    def test_cookie_and_multipart_round_trip(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        session = requests.Session()
        session.trust_env = False
        session.mount("http://", CurlAdapter())
        url = f"http://127.0.0.1:{server.server_port}/"
        try:
            self.assertEqual(session.get(url, timeout=5).text, "ready")
            response = session.post(url, files={"code": ("a.cpp", b"sample-code")}, timeout=5)
            self.assertEqual(response.status_code, 200)
        finally:
            session.close()
            server.shutdown()
            server.server_close()
            worker.join()


if __name__ == "__main__":
    unittest.main()
