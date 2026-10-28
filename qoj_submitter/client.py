"""QOJ web protocol; no browser or local daemon is required."""

import hashlib
import json
import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from .curl_adapter import CurlAdapter


HOSTS = {"qoj.ac", "contest.ucup.ac"}
TIMEOUT = 20


class QojError(Exception):
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
    return SubmissionForm(action, token["value"], languages)


class QojClient:
    def __init__(self, base, session=None):
        self.base = base_url(base)
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "qoj-submitter-direct/0.1"})
        if session is None and self.base == "https://qoj.ac":
            self.session.mount(self.base + "/", CurlAdapter())

    def get(self, path):
        try:
            response = self.session.get(urljoin(self.base + "/", path), timeout=TIMEOUT)
        except requests.RequestException as exc:
            raise QojError(f"Network request failed: {exc.__class__.__name__}") from exc
        if response.status_code == 403:
            raise QojError("HTTP 403: QOJ or a security check blocked this client. Browser access does not guarantee CLI access")
        if response.status_code >= 400:
            raise QojError(f"QOJ returned HTTP {response.status_code}")
        parsed = urlparse(response.url)
        if f"{parsed.scheme}://{parsed.netloc}" != self.base:
            raise QojError("Unexpected redirect to another host")
        return response

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

    def submit(self, form, filename, contents, language, cid):
        data = {"_token": form.token, "answer_answer_upload_type": "file",
                "answer_answer_language": language, "submit-answer": "answer"}
        try:
            response = self.session.post(form.action, data=data,
                files={"answer_answer_file": (filename, contents, "text/plain")},
                timeout=TIMEOUT, allow_redirects=False)
        except requests.RequestException as exc:
            raise UnknownSubmission("Submission outcome is unknown after a network error. Check QOJ before retrying") from exc
        if response.status_code >= 500:
            raise UnknownSubmission(f"QOJ returned HTTP {response.status_code}; check submissions before retrying")
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
        raise UnknownSubmission(f"QOJ did not provide a confirmed submission ID. Check {self.base}/contest/{cid}/submissions before retrying")
