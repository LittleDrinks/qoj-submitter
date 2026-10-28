"""Interactive, serverless command line interface."""

import argparse
import getpass
import http.cookiejar
import json
import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from .client import QojClient, QojError, UnknownSubmission, base_url, contest_id
from .samples import download, write_archives


def state_dir():
    root = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "qoj-submitter"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    return root


def atomic_write(path, writer):
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    os.close(fd)
    try:
        os.chmod(name, 0o600)
        writer(name)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def load_config(root):
    path = root / "config.json"
    if not path.exists():
        return {"host": "https://qoj.ac", "contests": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        base_url(data["host"])
        if not isinstance(data.get("contests"), dict):
            raise ValueError()
        return data
    except (ValueError, KeyError, TypeError, QojError) as exc:
        raise QojError("Invalid configuration; inspect " + str(path)) from exc


def save_config(root, data):
    atomic_write(root / "config.json", lambda path: Path(path).write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"))


def make_client(root, base):
    jar = http.cookiejar.MozillaCookieJar(str(root / (urlparse(base).hostname + ".cookies")))
    if Path(jar.filename).exists():
        try:
            jar.load(ignore_discard=True, ignore_expires=False)
        except (OSError, http.cookiejar.LoadError) as exc:
            raise QojError("Cannot read saved session; run qoj login") from exc
    client = QojClient(base)
    client.session.cookies = jar
    return client


def save_cookies(root, client):
    jar = client.session.cookies
    atomic_write(Path(jar.filename), lambda path: jar.save(path, ignore_discard=True, ignore_expires=False))


def choose_language(languages, requested, filename):
    requested = requested or Path(filename).suffix.lower().lstrip(".")
    aliases = {"cpp": "c++20", "cc": "c++20", "cxx": "c++20", "c++": "c++20",
               "c": "c11", "java": "java21", "py": "python3", "py3": "python3",
               "kt": "kotlin", "kts": "kotlin", "rs": "rust"}
    desired = aliases.get(requested.lower(), requested.lower())
    normalize = lambda value: "".join(c for c in value.lower() if c.isalnum() or c == "+")
    matches = [value for value in languages if normalize(value) == normalize(desired)]
    if len(matches) != 1:
        raise QojError("Language not found or ambiguous: " + requested + "; available: " + ", ".join(languages))
    return matches[0]


def parser():
    p = argparse.ArgumentParser(prog="qoj", description="Direct QOJ contest submissions")
    sub = p.add_subparsers(dest="command", required=True)
    login = sub.add_parser("login", help="Log in and save the session locally")
    login.add_argument("--url", default="https://qoj.ac", choices=["https://qoj.ac", "https://contest.ucup.ac"])
    contest = sub.add_parser("contest", help="Bind a contest by ID or URL")
    contest.add_argument("contest")
    sub.add_parser("status", help="Show current site, verified login and bound contest")
    samples = sub.add_parser("samples", help="Download samples for the bound contest")
    samples.add_argument("-c", "--contest", help="contest ID or URL (overrides binding)")
    samples.add_argument("-o", "--output-dir", default=".", help="output directory (default: current directory)")
    samples.add_argument("--force", action="store_true", help="replace existing archives")
    submit = sub.add_parser("submit", help="Submit a source file")
    add_submit_args(submit)
    return p


def add_submit_args(p):
    p.add_argument("-c", "--contest", help="contest ID or URL (overrides binding)")
    p.add_argument("-p", "--problem", help="problem label, e.g. A")
    p.add_argument("-l", "--language", "--lang", help="exact QOJ language name or file extension")
    p.add_argument("-y", "--assume-yes", "--yes", action="store_true", help="skip confirmation")
    p.add_argument("--dry-run", action="store_true", help="validate without submitting")
    p.add_argument("filename", help="source file")


def check_contest_host(value, host):
    if "://" in value and f"{urlparse(value).scheme}://{urlparse(value).netloc}" != host:
        raise QojError("Contest URL host differs from the current login; run qoj login --url first")
    return contest_id(value)


def run_submit(args, root, config):
    host = config["host"]
    client = make_client(root, host)
    bound = config["contests"].get(host)
    if args.contest:
        contest = client.contest(check_contest_host(args.contest, host))
    elif bound:
        contest = bound
    else:
        raise QojError("No contest bound; run qoj contest CONTEST_ID")
    label = (args.problem or Path(args.filename).stem).upper()
    problem = contest["problems"].get(label)
    if not problem:
        raise QojError("Unknown problem " + label + "; available: " + ", ".join(contest["problems"]))
    path = Path(args.filename)
    try:
        contents = path.read_bytes()
    except OSError as exc:
        raise QojError("Cannot read source file: " + str(exc)) from exc
    if not contents:
        raise QojError("Source file is empty")
    form = client.form(contest["id"], problem["id"])
    language = choose_language(form.languages, args.language, path.name)
    print(f"File: {path}\nContest: {contest['name']} ({contest['id']})\nProblem: {label} - {problem['name']}\nLanguage: {language}\nSite: {host}")
    if args.dry_run:
        print("Dry run: no submission sent.")
    elif args.assume_yes or input("Submit? [y/N] ").strip().lower() in ("y", "yes"):
        print("Submission received: " + client.submit(form, path.name, contents, language, contest["id"]))
    else:
        print("Cancelled.")
    save_cookies(root, client)


def execute(args):
    root = state_dir()
    config = load_config(root)
    if args.command == "login":
        host = base_url(args.url)
        client = QojClient(host)
        username = input("QOJ username: ").strip()
        password = getpass.getpass("QOJ password: ")
        if not username or not password:
            raise QojError("Username and password are required")
        client.login(username, password)
        del password
        jar = http.cookiejar.MozillaCookieJar(str(root / (urlparse(host).hostname + ".cookies")))
        for cookie in client.session.cookies:
            jar.set_cookie(cookie)
        client.session.cookies = jar
        save_cookies(root, client)
        config["host"] = host
        save_config(root, config)
        print("Logged in to " + host + " as " + username)
    elif args.command == "contest":
        host = config["host"]
        client = make_client(root, host)
        contest = client.contest(check_contest_host(args.contest, host))
        config["contests"][host] = contest
        save_cookies(root, client)
        save_config(root, config)
        print(f"Bound contest {contest['id']}: {contest['name']}")
        for label, problem in contest["problems"].items():
            print(f"  {label}: {problem['name']} ({problem['id']})")
    elif args.command == "status":
        host = config["host"]
        contest = config["contests"].get(host)
        print("Site: " + host)
        print(f"Contest: {contest['name']} ({contest['id']})" if contest else "Contest: not bound")
        client = make_client(root, host)
        try:
            username = client.status()
        except QojError as exc:
            print("Login: unknown (" + str(exc) + ")")
            raise QojError("Could not verify login status") from exc
        print("Login: " + ("logged in as " + username if username else "not logged in"))
        save_cookies(root, client)
    elif args.command == "samples":
        host = config["host"]
        client = make_client(root, host)
        if args.contest:
            contest = client.contest(check_contest_host(args.contest, host))
        else:
            contest = config["contests"].get(host)
        if not contest:
            raise QojError("No contest bound; run qoj contest CONTEST_ID")
        output = Path(args.output_dir)
        if not output.is_dir():
            raise QojError(f"Output directory does not exist: {output}")
        if (output / "samples.zip").exists() and not args.force:
            raise QojError(f"{output / 'samples.zip'} already exists; use --force to replace it")
        print(f"Downloading {contest['name']} ({contest['id']}) from {host}")
        samples, extras, warnings = download(client, contest)
        for warning in warnings:
            print("WARNING: " + warning, file=sys.stderr)
        for path in write_archives(output, samples, extras, args.force):
            print("Created: " + str(path))
        save_cookies(root, client)
    else:
        run_submit(args, root, config)


def main():
    try:
        execute(parser().parse_args())
    except (QojError, OSError) as exc:
        print("Error: " + str(exc), file=sys.stderr)
        return 2 if isinstance(exc, UnknownSubmission) else 1
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130
    return 0


def submit_main():
    p = argparse.ArgumentParser(prog="qoj-submit", description="Submit to a bound QOJ contest")
    add_submit_args(p)
    args = p.parse_args()
    args.command = "submit"
    try:
        execute(args)
    except (QojError, OSError) as exc:
        print("Error: " + str(exc), file=sys.stderr)
        return 2 if isinstance(exc, UnknownSubmission) else 1
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130
    return 0
