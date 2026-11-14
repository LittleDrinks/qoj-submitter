"""Interactive, serverless command line interface."""

import argparse
import getpass
import http.cookiejar
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from .client import QojClient, QojError, UnknownSubmission, base_url, contest_id
from .samples import download, write_archives
from .operations import Operations, baseline
from .browser import ChallengeRecovery


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
    recover = sub.add_parser("recover", help="Recover a saved submission receipt using reads only")
    recover.add_argument("operation", nargs="?", type=int, help="operation number (default: sole pending operation)")
    recover.add_argument("--abandon", action="store_true", help="end verification of this operation and allow a new upload")
    add_browser_args(recover)
    return p


def positive_timeout(value):
    seconds = int(value)
    if not 1 <= seconds <= 1800:
        raise argparse.ArgumentTypeError('Use 1 to 1800 seconds')
    return seconds


def add_browser_args(p):
    choice = p.add_mutually_exclusive_group()
    choice.add_argument('--browser', metavar='PATH', help='browser executable for human verification (also enables it outside a terminal)')
    choice.add_argument('--no-browser', action='store_true', help='return the challenge error without opening a browser')
    p.add_argument('--browser-timeout', type=positive_timeout, default=300, metavar='SECONDS',
                   help='human verification timeout (default: 300)')


def browser_recovery(client, args):
    return ChallengeRecovery(client, executable=getattr(args, 'browser', None),
        disabled=getattr(args, 'no_browser', False), timeout=getattr(args, 'browser_timeout', 300))


def add_submit_args(p):
    p.add_argument("-c", "--contest", help="contest ID or URL (overrides binding)")
    p.add_argument("-p", "--problem", help="problem label, e.g. A")
    p.add_argument("-l", "--language", "--lang", help="exact QOJ language name or file extension")
    p.add_argument("-y", "--assume-yes", "--yes", action="store_true", help="skip confirmation")
    p.add_argument("--dry-run", action="store_true", help="validate without submitting")
    p.add_argument("filename", help="source file")
    add_browser_args(p)


def check_contest_host(value, host):
    if "://" in value and f"{urlparse(value).scheme}://{urlparse(value).netloc}" != host:
        raise QojError("Contest URL host differs from the current login; run qoj login --url first")
    return contest_id(value)


def run_submit(args, root, config):
    host = config["host"]
    client = make_client(root, host)
    with browser_recovery(client, args) as recovery:
        submit_with_client(args, root, config, client, recovery)
    save_submission_cookies(root, client)


def submit_with_client(args, root, config, client, recovery):
    host = config["host"]
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
    with Operations(root) as operations:
        pending = operations.pending(host, contest["id"], problem["id"])
        if pending and not args.dry_run:
            operation = pending[0]
            language = choose_language([operation["language"]], args.language, path.name)
            if operation["source"] != contents or operation["language"] != language:
                raise QojError(f"Operation {operation['id']} has a saved source; run qoj recover {operation['id']} "
                               f"or qoj recover {operation['id']} --abandon before a new upload")
            recover_operation(client, operations, operation)
        else:
            form = client.form(contest["id"], problem["id"])
            form_generation = recovery.generation
            language = choose_language(form.languages, args.language, path.name)
            print(f"File: {path}\nContest: {contest['name']} ({contest['id']})\nProblem: {label} - {problem['name']}\nLanguage: {language}\nSite: {host}")
            if args.dry_run:
                print("Dry run: no submission sent.")
            elif args.assume_yes or input("Submit? [y/N] ").strip().lower() in ("y", "yes"):
                before = client.submission_snapshot(contest["id"])
                if form.account and form.account != before[0]:
                    raise QojError("The QOJ account changed before upload; retry with the intended account")
                if recovery.generation != form_generation:
                    form = client.form(contest["id"], problem["id"])
                    if form.account and form.account != before[0]:
                        raise QojError("The QOJ account changed during verification; retry with the intended account")
                    if language not in form.languages:
                        raise QojError("The selected language changed during verification; retry the command")
                recovery.allow_upload(form.action)
                operation = operations.start(host, contest["id"], problem["id"], form, language, contents, before)
                print(f"Operation {operation['id']} saved. Resume verification: qoj recover {operation['id']}", flush=True)
                try:
                    receipt = client.submit(form, path.name, contents, language, contest["id"], recover_receipt=False)
                except UnknownSubmission:
                    recover_operation(client, operations, operation)
                except QojError:
                    operations.finish(operation['id'], 'rejected')
                    raise
                else:
                    confirm_operation(operations, operation, receipt)
            else:
                print("Cancelled.")


def confirm_operation(operations, operation, receipt):
    print("Submission received: " + receipt, flush=True)
    try:
        operations.finish(operation['id'], 'confirmed', receipt)
    except (OSError, sqlite3.Error) as exc:
        print(f"Receipt is confirmed; save it above. Local state update failed: {type(exc).__name__}", file=sys.stderr)


def recover_operation(client, operations, operation):
    print(f"Recovering operation {operation['id']} ({operation['host']}, contest {operation['cid']})", flush=True)
    try:
        receipt = client.recover_submission(operation['action'], operation['source'], operation['language'],
                                            operation['cid'], baseline(operation))
    except UnknownSubmission as exc:
        raise UnknownSubmission(f"{exc}. Continue with qoj recover {operation['id']}") from exc
    confirm_operation(operations, operation, receipt)


def save_submission_cookies(root, client):
    try:
        save_cookies(root, client)
    except OSError as exc:
        print(f"Session save failed: {type(exc).__name__}; run qoj login to refresh the saved session", file=sys.stderr)


def run_recover(args, root):
    with Operations(root) as operations:
        if args.operation is None:
            if args.abandon:
                raise QojError("Choose the operation to end: qoj recover NUMBER --abandon")
            pending = operations.pending()
            if not pending:
                print("All saved operations are resolved.")
                return
            if len(pending) != 1:
                raise QojError("Choose qoj recover NUMBER from pending operations: " +
                               ", ".join(str(row['id']) for row in pending))
            operation = pending[0]
        else:
            operation = operations.get(args.operation)
        if operation['state'] == 'confirmed':
            print("Submission received: " + operation['receipt'])
            return
        if operation['state'] != 'pending':
            print(f"Operation {operation['id']}: {operation['state']}")
            return
        if args.abandon:
            print(f"Check {operation['host']}/contest/{operation['cid']}/submissions first. "
                  "Ending verification permits another upload of the same code.")
            if input("End verification? [y/N] ").strip().lower() in ("y", "yes"):
                operations.finish(operation['id'], 'abandoned')
                print(f"Operation {operation['id']} released; submit can start a new operation.")
            return
        client = make_client(root, operation['host'])
        with browser_recovery(client, args):
            recover_operation(client, operations, operation)
        save_submission_cookies(root, client)


def execute(args):
    root = state_dir()
    config = load_config(root)
    if args.command == "recover":
        run_recover(args, root)
    elif args.command == "login":
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
    except (QojError, OSError, sqlite3.Error) as exc:
        print("Error: " + str(exc), file=sys.stderr)
        return 2 if isinstance(exc, UnknownSubmission) else 1
    except KeyboardInterrupt:
        print("\nInterrupted. Use qoj recover to check saved submissions.", file=sys.stderr)
        return 130
    return 0


def submit_main():
    p = argparse.ArgumentParser(prog="qoj-submit", description="Submit to a bound QOJ contest")
    add_submit_args(p)
    args = p.parse_args()
    args.command = "submit"
    try:
        execute(args)
    except (QojError, OSError, sqlite3.Error) as exc:
        print("Error: " + str(exc), file=sys.stderr)
        return 2 if isinstance(exc, UnknownSubmission) else 1
    except KeyboardInterrupt:
        print("\nInterrupted. Use qoj recover to check saved submissions.", file=sys.stderr)
        return 130
    return 0
