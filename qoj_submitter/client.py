"""QOJ web protocol; no browser or local daemon is required."""

import hashlib
import json
import re
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from .curl_adapter import CurlAdapter
from .retry import ReadBudget, ReadFailure


HOSTS = {"qoj.ac", "contest.ucup.ac"}
TIMEOUT = 20


class QojError(Exception):
    pass


class TransientReadError(QojError):
    pass


class ChallengeError(QojError):
    pass


class UnknownSubmission(QojError):
    pass


def base_url(value):
    value = value.strip().rstrip("/")
    parsed = urlparse(value)
    if (parsed.scheme != "https" or parsed.hostname not in HOSTS
            or parsed.netloc != parsed.hostname or parsed.path or parsed.query or parsed.fragment):
        raise QojError("Use https://qoj.ac or https://contest.ucup.ac")
    return value


def contest_id(value):
    value = value.strip()
    if value.isdigit():
        return value
    parsed = urlparse(value)
    match = re.fullmatch(r"/contest/(\d+)/?", parsed.path)
    if (parsed.scheme != "https" or parsed.hostname not in HOSTS
            or parsed.netloc != parsed.hostname or parsed.fragment or not match):
        raise QojError("Expected a contest ID or QOJ contest URL")
    return match.group(1)


def soup(text):
    return BeautifulSoup(text, "html.parser")


def logged_in_user(page):
    links = page.find_all("a", href=True)
    if any(urlparse(link["href"]).path == "/logout" for link in links):
        for link in links:
            path = urlparse(link["href"]).path
            if link.get_text(" ", strip=True).lower() == "my profile" and path.startswith("/user/profile/"):
                return path.removeprefix("/user/profile/")
        return "(account name unavailable)"
    if any(urlparse(link["href"]).path == "/login" for link in links):
        return None
    raise QojError("Cannot determine login status from the site response")


def login_token(html):
    page = soup(html)
    for script in page.find_all("script"):
        content = script.string or script.get_text()
        if "submitLoginPost" not in content:
            continue
        match = re.search(r"\b_token\s*:\s*['\"]([^'\"]+)['\"]", content)
        if match:
            return match.group(1)
    raise QojError("Could not find the login token; QOJ's login page may have changed")


def contest_problems(html, cid):
    page = soup(html)
    problems = {}
    pattern = re.compile(rf"^/contest/{re.escape(cid)}/problem/(\d+)/?$")
    for row in page.select("table tr"):
        cells = row.find_all("td", recursive=False)
        if len(cells) < 2:
            continue
        label = cells[0].get_text(" ", strip=True)
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", label):
            continue
        for link in cells[1].find_all("a", href=True):
            match = pattern.fullmatch(urlparse(link["href"]).path)
            if match:
                key = label.upper()
                item = {"id": match.group(1), "name": link.get_text(" ", strip=True)}
                if key in problems and problems[key] != item:
                    raise QojError(f"Ambiguous mapping for problem {key}")
                problems[key] = item
    if not problems:
        raise QojError("No contest problems found. Check login, contest access and page format")
    return problems


@dataclass
class SubmissionForm:
    action: str
    token: str
    languages: list
    account: str = None


def submission_form(html, page_url, base, cid, pid):
    page = soup(html)
    form = page.find("form", id="form-answer")
    if not form:
        raise QojError("No submission form found; check login and contest permissions")
    token = form.find("input", attrs={"name": "_token"})
    language_select = form.find("select", attrs={"name": "answer_answer_language"})
    languages = [o.get("value", "") for o in language_select.find_all("option")] if language_select else []
    if not languages and form.find(id="form-group-answer_answer"):
        # The site builds the select in JavaScript from a JSON-encoded HTML fragment.
        pattern = re.compile(r'''source_code_form_group\s*\(\s*['"]answer_answer['"]\s*,\s*(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')\s*,\s*("(?:\\.|[^"\\])*")''')
        for script in page.find_all("script"):
            match = pattern.search(script.string or script.get_text())
            if match:
                try:
                    fragment = json.loads(match.group(1))
                except (ValueError, TypeError):
                    continue
                languages = [o.get("value", "") for o in soup(fragment).find_all("option")]
                break
    if not token or not token.get("value") or not languages:
        raise QojError("QOJ submission form is incomplete or unsupported")
    action = urljoin(page_url, form.get("action") or page_url)
    parsed = urlparse(action)
    if (f"{parsed.scheme}://{parsed.netloc}" != base
            or not re.fullmatch(rf"/contest/{re.escape(cid)}/problem/{re.escape(pid)}(?:/.*)?", parsed.path)):
        raise QojError("Unexpected submission form destination")
    try:
        account = logged_in_user(page)
    except QojError:
        account = None
    return SubmissionForm(action, token["value"], languages, account)


class QojClient:
    def __init__(self, base, session=None, *, on_challenge=None):
        self.base = base_url(base)
        self.on_challenge = on_challenge
        self._read_budget = None
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "qoj-submitter-direct/0.1"})
        if session is None and self.base == "https://qoj.ac":
            self.session.mount(self.base + "/", CurlAdapter())

    def get(self, path, allow_404=False):
        budget = self._read_budget or ReadBudget(requests=6)
        url = urljoin(self.base + "/", path)
        attempt = 0
        while True:
            parsed = urlparse(url)
            if f"{parsed.scheme}://{parsed.netloc}" != self.base:
                raise QojError("Unexpected redirect to another host")
            try:
                timeout = budget.take()
                try:
                    response = self.session.get(url, timeout=timeout, allow_redirects=False)
                except (requests.Timeout, requests.ConnectionError) as exc:
                    if isinstance(exc, requests.exceptions.SSLError):
                        raise QojError("TLS certificate verification failed") from exc
                    reason = f"Network read failed: {exc.__class__.__name__}"
                    response = None
                except requests.RequestException as exc:
                    raise QojError(f"Network read failed: {exc.__class__.__name__}") from exc
                if response is not None:
                    if response.headers.get('cf-mitigated') == 'challenge':
                        if self.on_challenge is None:
                            raise ChallengeError("Cloudflare verification required; complete verification in your browser, then retry")
                        started = time.monotonic()
                        try:
                            self.on_challenge(url)
                        finally:
                            # Human verification has its own timeout, outside the read budget.
                            budget.deadline += time.monotonic() - started
                        continue
                    parsed = urlparse(response.url)
                    if f"{parsed.scheme}://{parsed.netloc}" != self.base:
                        raise QojError("Unexpected redirect to another host")
                    if response.status_code in (301, 302, 303, 307, 308):
                        location = response.headers.get('Location')
                        if not location:
                            raise QojError("Redirect response is missing its destination")
                        url = urljoin(url, location)
                        continue
                    if response.status_code in (408, 429, 500, 502, 503, 504):
                        reason = f"QOJ read returned HTTP {response.status_code}"
                    else:
                        if response.status_code in (401, 403):
                            raise QojError(f"HTTP {response.status_code}: check your QOJ login and contest access")
                        if response.status_code >= 400 and not (allow_404 and response.status_code == 404):
                            raise QojError(f"QOJ returned HTTP {response.status_code}")
                        return response
                attempt += 1
                if attempt >= 3:
                    raise TransientReadError(reason + "; read retry limit reached")
                budget.pause(attempt - 1, response.headers.get('Retry-After') if response is not None else None)
            except ReadFailure as exc:
                raise TransientReadError(str(exc)) from exc

    def status(self):
        response = self.get("/")
        return logged_in_user(soup(response.text))

    def login(self, username, password):
        response = self.get("/login")
        token = login_token(response.text)
        hashed = hashlib.md5(password.encode("utf-8"), usedforsecurity=False).hexdigest()
        try:
            response = self.session.post(self.base + "/login", data={
                "_token": token, "login": "", "username": username,
                "password": hashed, "trust": "",
            }, timeout=TIMEOUT, allow_redirects=False)
        except requests.RequestException as exc:
            raise QojError(f"Login request failed: {exc.__class__.__name__}") from exc
        if response.status_code != 200:
            raise QojError(f"Login failed: HTTP {response.status_code}")
        result = response.text.strip()
        messages = {
            "failed": "Incorrect username or password",
            "2fa": "Two-factor authentication is required; CLI 2FA is not supported yet",
            "pending_2fa": "Additional verification is required; CLI login is not supported yet",
            "secure": "QOJ rejected this login for security reasons",
            "expired": "Login page expired; try again",
            "banned": "This account is not allowed to log in",
        }
        if result != "ok":
            raise QojError(messages.get(result, "QOJ did not confirm login"))

    def contest(self, cid):
        response = self.get(f"/contest/{cid}")
        if urlparse(response.url).path.startswith("/login"):
            raise QojError("Not logged in or session expired; run qoj login")
        page = soup(response.text)
        title = page.title.get_text(" ", strip=True).split(" - Dashboard - Contest - ")[0] if page.title else f"Contest {cid}"
        return {"id": cid, "name": title, "problems": contest_problems(response.text, cid)}

    def form(self, cid, pid):
        response = self.get(f"/contest/{cid}/problem/{pid}")
        return submission_form(response.text, response.url, self.base, cid, pid)

    def _submission_list(self, cid):
        path = f"/contest/{cid}/submissions"
        response = self.get(path)
        page = soup(response.text)
        username = logged_in_user(page)
        table = next((t for t in page.find_all("table")
                      if t.find("th") and t.find("th").get_text(strip=True) == "ID"), None)
        if (urlparse(response.url).path.rstrip("/") != path or table is None
                or username in (None, "(account name unavailable)")):
            raise QojError("Cannot read the current account's submission list; check login and contest access")
        headings = [cell.get_text(" ", strip=True) for cell in table.find_all("th")]
        if "Language" not in headings:
            raise QojError("Unsupported QOJ submission list format: missing Language heading")
        language_column = headings.index("Language")
        rows = {}
        for row in table.find_all("tr"):
            cells = row.find_all("td", recursive=False)
            if not cells:
                continue
            if len(cells) <= max(2, language_column):
                raise QojError("Unsupported QOJ submission list format: incomplete row")
            links = [cell.find("a", href=True) for cell in cells[:2]]
            owner_link = cells[2].find("a", href=True)
            owner_span = cells[2].find("span", class_="uoj-username")
            if not all(links) or not (owner_link or owner_span):
                raise QojError("Unsupported QOJ submission list format: missing submission, problem or account link")
            # QOJ's JavaScript turns this username span into a profile link.
            owner = ("/user/profile/" + owner_span.get_text(strip=True) if owner_span else
                     urlparse(owner_link["href"]).path)
            sid = re.fullmatch(r"/submission/(\d+)/?", urlparse(links[0]["href"]).path)
            if not sid:
                raise QojError("Unsupported QOJ submission link")
            rows[int(sid.group(1))] = (urlparse(links[1]["href"]).path,
                                      owner, cells[language_column].get_text(" ", strip=True))
        return username, rows

    def submission_snapshot(self, cid):
        """Capture the account and existing receipts before an upload."""
        return self._submission_list(cid)

    def recover_submission(self, action, contents, language, cid, before):
        """Find a receipt using reads only and the saved pre-upload snapshot."""
        previous_budget = self._read_budget
        self._read_budget = ReadBudget()
        try:
            receipt = self._confirm_submission(SubmissionForm(action, "", []),
                                               contents, language, cid, before)
        except (QojError, UnicodeError, ReadFailure) as exc:
            raise UnknownSubmission(f"Receipt verification paused: {exc}") from exc
        finally:
            self._read_budget = previous_budget
        if not receipt:
            raise UnknownSubmission("A unique matching receipt is not visible yet")
        return receipt

    def _confirm_submission(self, form, contents, language, cid, before):
        username, old_rows = before
        last_id = max(old_rows, default=0)
        problem_path = re.match(r"/contest/\d+/problem/\d+", urlparse(form.action).path).group()
        expected = (problem_path, f"/user/profile/{username}", language)
        # HTML parsing normalizes line endings, including files uploaded on Windows.
        source = contents.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
        checked = {}
        for attempt in range(3):
            current_user, rows = self._submission_list(cid)
            if current_user != username:
                raise QojError(f"Saved operation belongs to {username}; log in to that account to recover it")
            candidates = [sid for sid, row in rows.items() if sid > last_id and row == expected]
            for sid in candidates:
                if sid not in checked:
                    response = self.get(f"/submission/{sid}", allow_404=True)
                    code = soup(response.text).select_one("pre > code[class^=sh_]")
                    if (urlparse(response.url).path.rstrip("/") == f"/submission/{sid}"
                            and code is not None):
                        checked[sid] = code.get_text().replace("\r\n", "\n").replace("\r", "\n") == source
            confirmed = [sid for sid in candidates if checked.get(sid)]
            if len(confirmed) == 1 and all(sid in checked for sid in candidates):
                return f"{self.base}/submission/{confirmed[0]}"
            if len(confirmed) > 1:
                break
            if attempt < 2:
                (self._read_budget or ReadBudget()).pause(attempt)
        return None

    def submit(self, form, filename, contents, language, cid, *, recover_receipt=True):
        # QOJ may redirect to the list rather than to the new submission. Capture
        # a baseline before the only POST so an older accepted solution cannot match.
        # Stateful consumers can own recovery in their durable operation ledger.
        before = self._submission_list(cid) if recover_receipt else None
        data = {"_token": form.token, "answer_answer_upload_type": "file",
                "answer_answer_language": language, "submit-answer": "answer"}
        try:
            response = self.session.post(form.action, data=data,
                files={"answer_answer_file": (filename, contents, "text/plain")},
                timeout=TIMEOUT, allow_redirects=False)
        except requests.RequestException as exc:
            if recover_receipt:
                return self.recover_submission(form.action, contents, language, cid, before)
            raise UnknownSubmission("Upload response lost; recover the saved receipt") from exc
        if response.headers.get('cf-mitigated') == 'challenge':
            if self.on_challenge is not None:
                try:
                    self.on_challenge(form.action)
                except QojError as exc:
                    raise UnknownSubmission(f"Receipt verification paused: {exc}") from exc
            raise UnknownSubmission("Cloudflare verification required; recover the saved receipt after verification")
        if response.status_code >= 500 or response.status_code == 408:
            if recover_receipt:
                return self.recover_submission(form.action, contents, language, cid, before)
            raise UnknownSubmission(f"Upload returned HTTP {response.status_code}; recover the saved receipt")
        if response.status_code >= 400:
            raise QojError(f"QOJ rejected the request with HTTP {response.status_code}")
        location = response.headers.get("Location", "")
        if 300 <= response.status_code < 400 and location:
            target = urljoin(form.action, location)
            parsed = urlparse(target)
            if f"{parsed.scheme}://{parsed.netloc}" != self.base:
                raise UnknownSubmission("Unexpected redirect; check QOJ before retrying")
            if re.fullmatch(r"/submission/\d+/?", parsed.path):
                return target
            if recover_receipt and parsed.path.rstrip("/") == f"/contest/{cid}/submissions":
                return self.recover_submission(form.action, contents, language, cid, before)
        raise UnknownSubmission(f"QOJ did not provide a confirmed submission ID. Check {self.base}/contest/{cid}/submissions before retrying")
