"""A command-scoped browser for human verification and same-origin requests."""
import base64
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit
from urllib.request import build_opener, ProxyHandler

import requests
from requests.adapters import BaseAdapter

from .client import ChallengeError, QojError


class BrowserError(QojError):
    pass


class BrowserClosed(BrowserError):
    pass


def browser_path(explicit=None):
    if explicit:
        value = shutil.which(explicit) or explicit
        if Path(value).is_file():
            return str(Path(value).resolve())
        raise BrowserError('Browser executable was not found: ' + explicit)
    candidates = ['google-chrome', 'chromium', 'chromium-browser', 'firefox', 'firefox-esr']
    if os.name == 'nt':
        candidates = []
        for root in [os.environ.get('PROGRAMFILES'), os.environ.get('PROGRAMFILES(X86)'), os.environ.get('LOCALAPPDATA')]:
            if root:
                candidates += [str(Path(root) / name) for name in [
                    'Google/Chrome/Application/chrome.exe', 'Microsoft/Edge/Application/msedge.exe',
                    'Mozilla Firefox/firefox.exe']]
    elif sys.platform == 'darwin':
        candidates = ['/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
                      '/Applications/Firefox.app/Contents/MacOS/firefox'] + candidates
    for candidate in candidates:
        value = shutil.which(candidate) or candidate
        if Path(value).is_file():
            return str(Path(value).resolve())
    raise BrowserError('Install Firefox, Chrome or Edge, or select its executable with --browser PATH')


def origin(url):
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or parsed.username or parsed.password or not parsed.netloc:
        raise BrowserError('Invalid browser request origin')
    return parsed.scheme + '://' + parsed.netloc


class BrowserSession:
    """Own one temporary browser profile; never attach to a user's browser."""
    def __init__(self, base, executable=None):
        self.base = origin(base)
        self.executable = browser_path(executable)
        self.firefox = 'firefox' in Path(self.executable).name.lower()
        self.profile = None
        self.process = None
        self.ws = None
        self.sequence = 0
        self.context = None

    def start(self, cookies):
        if sys.platform.startswith('linux') and not (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):
            raise BrowserError('Browser verification needs a graphical session; retry from a desktop terminal')
        import websocket
        self.profile = tempfile.TemporaryDirectory(prefix='qoj-verification-')
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        if self.firefox:
            args = [self.executable, '--no-remote', '--profile', self.profile.name,
                    '--remote-debugging-port', str(port), 'about:blank']
        else:
            args = [self.executable, '--remote-debugging-address=127.0.0.1',
                    '--remote-debugging-port=' + str(port), '--user-data-dir=' + self.profile.name,
                    '--no-first-run', '--no-default-browser-check', 'about:blank']
        self.process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 15
        opener = build_opener(ProxyHandler({}))
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise BrowserError('Browser closed before verification started')
            try:
                if self.firefox:
                    endpoint = f'ws://127.0.0.1:{port}/session'
                else:
                    with opener.open(f'http://127.0.0.1:{port}/json/list', timeout=1) as reply:
                        pages = json.load(reply)
                    endpoint = next(page['webSocketDebuggerUrl'] for page in pages if page['type'] == 'page')
                self.ws = websocket.create_connection(endpoint, timeout=2, suppress_origin=True,
                                                       http_no_proxy=['127.0.0.1', 'localhost'])
                break
            except (OSError, ValueError, StopIteration, websocket.WebSocketException):
                time.sleep(.1)
        if self.ws is None:
            raise BrowserError('Could not connect to the browser; retry with --browser PATH')
        if self.firefox:
            self.command('session.new', {'capabilities': {}})
            self.context = self.command('browsingContext.getTree')['contexts'][0]['context']
        self.import_cookies(cookies)

    def command(self, method, params=None, timeout=10):
        import websocket
        if self.process.poll() is not None:
            raise BrowserClosed('Browser window closed; retry the command to resume verification')
        self.sequence += 1
        deadline = time.monotonic() + timeout
        try:
            self.ws.send(json.dumps({'id': self.sequence, 'method': method, 'params': params or {}}))
            while time.monotonic() < deadline:
                self.ws.settimeout(max(.1, deadline - time.monotonic()))
                response = json.loads(self.ws.recv())
                if response.get('id') != self.sequence:
                    continue
                if response.get('error') in ('no such frame', 'no such window'):
                    raise BrowserClosed('Browser window closed; retry the command to resume verification')
                if 'error' in response:
                    raise BrowserError('Browser command failed: ' + str(response['error']))
                return response.get('result', {})
        except websocket.WebSocketTimeoutException as exc:
            raise BrowserError('Browser request timed out; retry the command to resume verification') from exc
        except (OSError, ValueError, websocket.WebSocketException) as exc:
            raise BrowserClosed('Browser connection closed; retry the command to resume verification') from exc
        raise BrowserError('Browser request timed out; retry the command to resume verification')

    def evaluate(self, expression, timeout=10):
        if self.firefox:
            result = self.command('script.evaluate', {'expression': expression, 'target': {'context': self.context},
                                                       'awaitPromise': True}, timeout=timeout)
            if result.get('type') != 'success':
                raise BrowserError('The browser page changed during the request')
            return result['result'].get('value')
        result = self.command('Runtime.evaluate', {'expression': expression, 'returnByValue': True,
                                                  'awaitPromise': True}, timeout=timeout)
        if result.get('exceptionDetails'):
            raise BrowserError('The browser could not complete the same-origin request')
        return result.get('result', {}).get('value')

    def import_cookies(self, cookies):
        host = urlsplit(self.base).hostname
        for cookie in cookies:
            if cookie.is_expired() or cookie.domain.lstrip('.') != host or cookie.name.startswith(('cf_', '__cf')):
                continue
            value = {'name': cookie.name, 'value': cookie.value, 'domain': cookie.domain,
                     'path': cookie.path or '/', 'secure': cookie.secure,
                     'httpOnly': cookie.has_nonstandard_attr('HttpOnly')}
            if cookie.expires is not None:
                value['expiry' if self.firefox else 'expires'] = cookie.expires
            if self.firefox:
                value['value'] = {'type': 'string', 'value': cookie.value}
                self.command('storage.setCookie', {'cookie': value,
                    'partition': {'type': 'context', 'context': self.context}})
            else:
                self.command('Network.setCookies', {'cookies': [value]})

    def export_cookies(self, jar):
        if self.firefox:
            cookies = self.command('storage.getCookies', {'partition': {'type': 'context', 'context': self.context}})['cookies']
        else:
            cookies = self.command('Network.getCookies', {'urls': [self.base + '/']})['cookies']
        host = urlsplit(self.base).hostname
        for value in cookies:
            if value['domain'].lstrip('.') != host or value['name'].startswith(('cf_', '__cf')):
                continue
            expiry = value.get('expiry', value.get('expires'))
            jar.set_cookie(requests.cookies.create_cookie(value['name'],
                value['value']['value'] if self.firefox else value['value'], domain=value['domain'],
                path=value.get('path', '/'), secure=value.get('secure', False),
                expires=int(expiry) if expiry and expiry > 0 else None,
                rest={'HttpOnly': None} if value.get('httpOnly') else {}))

    def navigate(self, url):
        if origin(url) != self.base:
            raise BrowserError('Verification must stay on the current QOJ site')
        if self.firefox:
            self.command('browsingContext.navigate', {'context': self.context, 'url': url, 'wait': 'none'})
        else:
            self.command('Page.navigate', {'url': url})

    def request(self, method, url, headers=None, body=None, timeout=8):
        if origin(url) != self.base or method not in ('GET', 'POST'):
            raise BrowserError('Browser request is outside this submission')
        if not isinstance(body, (bytes, bytearray, type(None))):
            raise BrowserError('Unsupported browser upload body')
        payload = {'method': method, 'url': url, 'origin': self.base,
                   'headers': {k: v for k, v in (headers or {}).items() if k.lower() in ('content-type', 'accept')},
                   'body': base64.b64encode(body).decode() if body is not None else None,
                   'timeout': max(1, int(timeout * 1000))}
        expression = '''(async p=>{
          if(location.origin!==p.origin) throw Error('origin changed');
          const response=await fetch(p.url,{method:p.method,mode:'same-origin',credentials:'same-origin',
            redirect:p.method==='POST'?'error':'follow',cache:'no-store',headers:p.headers,
            body:p.body===null?undefined:Uint8Array.from(atob(p.body),c=>c.charCodeAt(0)),
            signal:AbortSignal.timeout(p.timeout)});
          const bytes=new Uint8Array(await response.arrayBuffer());
          let binary='';for(let i=0;i<bytes.length;i+=32768) binary+=String.fromCharCode(...bytes.subarray(i,i+32768));
          return JSON.stringify({url:response.url,status:response.status,redirected:response.redirected,
            headers:Object.fromEntries(response.headers),body:btoa(binary)});
        })(''' + json.dumps(payload) + ')'
        result = json.loads(self.evaluate(expression, timeout=timeout + 2))
        if origin(result['url']) != self.base:
            raise BrowserError('Browser request left the current QOJ site')
        reply = requests.Response()
        reply.url = url
        reply.status_code = result['status']
        reply.headers.update(result['headers'])
        reply._content = base64.b64decode(result['body'])
        reply.encoding = 'utf-8'
        if result['redirected']:
            reply.status_code = 302
            reply.headers['Location'] = result['url']
        return reply

    def verify(self, url, timeout):
        self.navigate(url)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise BrowserClosed('Browser window closed; retry the command to resume verification')
            try:
                response = self.request('GET', url, timeout=min(5, max(.1, deadline - time.monotonic())))
                if response.headers.get('cf-mitigated') != 'challenge' and response.status_code not in (401, 403, 429, 503):
                    return
            except BrowserClosed:
                raise
            except BrowserError:
                # Navigation replaces the script context while a human is verifying.
                if self.process.poll() is not None:
                    raise
            time.sleep(min(1, max(0, deadline - time.monotonic())))
        raise BrowserError('Browser verification timed out; retry the command to resume verification')

    def close(self):
        if self.ws:
            try:
                if not self.firefox:
                    self.command('Browser.close', timeout=2)
            except BrowserError:
                pass
            self.ws.close()
            self.ws = None
        if self.process:
            if self.process.poll() is None:
                self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self.profile:
            # A browser may release profile files shortly after its main process exits.
            for attempt in range(10):
                try:
                    self.profile.cleanup()
                    break
                except OSError:
                    if attempt == 9:
                        raise BrowserError('Browser closed; remove temporary profile ' + self.profile.name)
                    time.sleep(.1)


class BrowserAdapter(BaseAdapter):
    def __init__(self, browser):
        self.browser = browser
        self.upload_url = None
        self.uploaded = False

    def send(self, request, **kwargs):
        if request.method == 'POST':
            if request.url != self.upload_url or self.uploaded:
                raise requests.RequestException('This operation permits only one upload')
            self.uploaded = True
        elif request.method != 'GET':
            raise requests.RequestException('Unsupported browser request method')
        try:
            response = self.browser.request(request.method, request.url, request.headers,
                request.body.encode() if isinstance(request.body, str) else request.body,
                timeout=kwargs.get('timeout') or 8)
            response.request = request
            return response
        except BrowserError as exc:
            raise requests.ConnectionError(str(exc)) from exc

    def close(self):
        pass


class ChallengeRecovery:
    def __init__(self, client, *, executable=None, disabled=False, timeout=300):
        self.client = client
        self.executable = executable
        self.enabled = not disabled and (executable is not None or sys.stdin.isatty())
        self.timeout = timeout
        self.browser = None
        self.adapter = None
        self.generation = 0
        self.previous = None
        self.attempted = False

    def __enter__(self):
        self.previous = self.client.on_challenge
        if self.enabled:
            self.client.on_challenge = self.verify
        return self

    def verify(self, url):
        if self.attempted:
            raise ChallengeError('Cloudflare verification is still required; retry the command to resume')
        self.attempted = True
        print('Cloudflare verification: opening a separate browser. Complete verification there; this command will continue. Close it or press Ctrl+C to cancel.', flush=True)
        self.browser = BrowserSession(self.client.base, self.executable)
        self.browser.start(self.client.session.cookies)
        self.browser.verify(url, self.timeout)
        self.adapter = BrowserAdapter(self.browser)
        self.client.session.mount(self.client.base + '/', self.adapter)
        self.generation += 1
        print('Browser access confirmed. Continuing the saved operation.', flush=True)

    def allow_upload(self, url):
        if self.adapter:
            self.adapter.upload_url = url

    def __exit__(self, exc_type, *_):
        self.client.on_challenge = self.previous
        if self.browser:
            try:
                if exc_type is None:
                    self.browser.export_cookies(self.client.session.cookies)
            except Exception:
                print('Verification browser session ended.', file=sys.stderr)
            try:
                self.browser.close()
            except Exception:
                print('Close the verification browser to finish cleanup.', file=sys.stderr)
