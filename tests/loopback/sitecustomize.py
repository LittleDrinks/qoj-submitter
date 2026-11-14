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
        if os.environ.get('QOJ_BROWSER_TEST_MODE'):
            from qoj_submitter.browser import BrowserAdapter
            if isinstance(adapter, BrowserAdapter):
                return original_send(self, request, **kwargs)
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

    if os.environ.get('QOJ_BROWSER_TEST_MODE'):
        import time
        from qoj_submitter import browser

        class LoopbackBrowser(browser.BrowserSession):
            def __init__(self, base, executable=None):
                self.logical = base
                super().__init__(f"http://127.0.0.1:{os.environ['QOJ_TEST_PORT']}", executable)

            def local_url(self, url):
                return self.base + url[len(self.logical):] if url.startswith(self.logical + '/') else url

            def navigate(self, url):
                return super().navigate(self.local_url(url))

            def request(self, method, url, *args, **kwargs):
                result = super().request(method, self.local_url(url), *args, **kwargs)
                result.url = url
                if result.headers.get('Location', '').startswith(self.base + '/'):
                    result.headers['Location'] = self.logical + result.headers['Location'][len(self.base):]
                return result

            def verify(self, url, timeout):
                mode = os.environ['QOJ_BROWSER_TEST_MODE']
                self.navigate(url)
                if mode in ('pass', 'close'):
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        try:
                            if self.evaluate('Boolean(document.getElementById("test-verify"))'):
                                if mode == 'close':
                                    self.command('browsingContext.close', {'context': self.context}) if self.firefox else self.command('Page.close')
                                else:
                                    self.evaluate('(()=>{document.getElementById("test-verify").click(); return true})()')
                                    deadline = time.monotonic() + 10
                                    while time.monotonic() < deadline:
                                        try:
                                            if self.evaluate('(()=>{const b=document.getElementById("test-verify");'
                                                             'return !b||b.textContent==="Test verification complete"})()'):
                                                break
                                        except browser.BrowserError:
                                            pass
                                        time.sleep(.1)
                                break
                        except browser.BrowserError:
                            pass
                        time.sleep(.1)
                return super().verify(url, timeout)

        browser.BrowserSession = LoopbackBrowser
