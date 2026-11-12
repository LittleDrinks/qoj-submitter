"""Route CLI integration tests to their isolated HTTP server."""
import os
from urllib.parse import urlsplit

if "QOJ_TEST_PORT" in os.environ:
    import requests

    original_send = requests.Session.send

    def send(self, request, **kwargs):
        request.headers["X-Qoj-Test-Pid"] = str(os.getpid())
        original_url = request.url
        parts = urlsplit(original_url)
        if parts.hostname not in ("qoj.ac", "contest.ucup.ac"):
            raise AssertionError("Integration test attempted an external request")
        adapter = self.get_adapter(original_url)
        request.url = f"http://127.0.0.1:{os.environ['QOJ_TEST_PORT']}{parts.path}"
        if parts.query:
            request.url += "?" + parts.query
        self.mount("http://127.0.0.1:", adapter)
        kwargs["proxies"] = {}
        try:
            response = original_send(self, request, **kwargs)
            response.url = original_url
            return response
        finally:
            request.url = original_url

    requests.Session.send = send
