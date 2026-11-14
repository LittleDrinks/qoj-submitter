"""Durable submission intents, a process lock and a shared diagnostic timeline."""
import json
import os
import sqlite3
import time
from contextlib import AbstractContextManager

from .errors import QojError

DIAGNOSTICS_KEPT = 2000


def record(root, operation_id, event, detail=None):
    """Append a diagnostics row; never fail the command because logging failed."""
    try:
        db = sqlite3.connect(root / 'operations.sqlite3', timeout=5)
        try:
            try:
                os.chmod(root / 'operations.sqlite3', 0o600)
            except OSError:
                pass
            with db:
                _ensure_diagnostics(db)
                db.execute('INSERT INTO diagnostics (at, operation_id, event, detail) VALUES (?,?,?,?)',
                           (time.strftime('%Y-%m-%dT%H:%M:%S%z'), operation_id, event, detail))
                db.execute('DELETE FROM diagnostics WHERE id NOT IN '
                           '(SELECT id FROM diagnostics ORDER BY id DESC LIMIT ?)', (DIAGNOSTICS_KEPT,))
        finally:
            db.close()
    except (OSError, sqlite3.Error):
        pass


def _ensure_diagnostics(db):
    db.execute('''CREATE TABLE IF NOT EXISTS diagnostics (
        id INTEGER PRIMARY KEY, at TEXT NOT NULL, operation_id INTEGER,
        event TEXT NOT NULL, detail TEXT)''')


def timeline(root, operation_id=None, limit=200):
    db = sqlite3.connect(root / 'operations.sqlite3', timeout=5)
    db.row_factory = sqlite3.Row
    try:
        _ensure_diagnostics(db)
        if operation_id is None:
            rows = db.execute('SELECT * FROM diagnostics ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
        else:
            rows = db.execute('SELECT * FROM diagnostics WHERE operation_id IS ? ORDER BY id DESC LIMIT ?',
                              (operation_id, limit)).fetchall()
    finally:
        db.close()
    return list(reversed(rows))


def dump(root, output):
    """Copy diagnostics and operation states without source code or upload baselines."""
    source = sqlite3.connect(root / 'operations.sqlite3', timeout=5)
    try:
        rows = source.execute('SELECT * FROM diagnostics ORDER BY id').fetchall()
        operations = source.execute('SELECT id, host, cid, pid, language, state, receipt '
                                    'FROM operations ORDER BY id').fetchall()
    finally:
        source.close()
    target = sqlite3.connect(output)
    try:
        target.execute('''CREATE TABLE IF NOT EXISTS diagnostics (
            id INTEGER PRIMARY KEY, at TEXT NOT NULL, operation_id INTEGER,
            event TEXT NOT NULL, detail TEXT)''')
        target.execute('''CREATE TABLE IF NOT EXISTS operations (
            id INTEGER PRIMARY KEY, host TEXT, cid TEXT, pid TEXT, language TEXT,
            state TEXT, receipt TEXT)''')
        with target:
            target.executemany('INSERT INTO diagnostics VALUES (?,?,?,?,?)', rows)
            target.executemany('INSERT INTO operations VALUES (?,?,?,?,?,?,?)', operations)
    finally:
        target.close()
    try:
        os.chmod(output, 0o600)
    except OSError:
        pass


class Operations(AbstractContextManager):
    def __init__(self, root):
        self.root = root
        self.lock = None
        self.db = None

    def __enter__(self):
        self.lock = (self.root / 'submit.lock').open('a+b')
        try:
            if self.lock.tell() == 0:
                self.lock.write(b'\0')
                self.lock.flush()
            self.lock.seek(0)
            try:
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(self.lock.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise QojError('Another submit or recover command is active; retry after it finishes') from exc
            path = self.root / 'operations.sqlite3'
            self.db = sqlite3.connect(path)
            os.chmod(path, 0o600)
            self.db.row_factory = sqlite3.Row
            self.db.execute('''CREATE TABLE IF NOT EXISTS operations (
                id INTEGER PRIMARY KEY, host TEXT NOT NULL, cid TEXT NOT NULL,
                pid TEXT NOT NULL, language TEXT NOT NULL, action TEXT NOT NULL,
                source BLOB, baseline TEXT, state TEXT NOT NULL, receipt TEXT)''')
            _ensure_diagnostics(self.db)
            self.db.commit()
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *args):
        if self.db is not None:
            self.db.close()
        if self.lock is not None:
            self.lock.close()

    def pending(self, host=None, cid=None, pid=None):
        rows = self.db.execute("SELECT * FROM operations WHERE state = 'pending' ORDER BY id").fetchall()
        return [row for row in rows if host is None or
                (row['host'], row['cid'], row['pid']) == (host, cid, pid)]

    def start(self, host, cid, pid, form, language, source, baseline):
        # Commit before the network write. A crash from this point requires reads
        # to determine the outcome, including a crash before the POST starts.
        with self.db:
            cursor = self.db.execute('''INSERT INTO operations
                (host,cid,pid,language,action,source,baseline,state)
                VALUES (?,?,?,?,?,?,?,'pending')''',
                (host, cid, pid, language, form.action, source, json.dumps(baseline)))
        return self.get(cursor.lastrowid)

    def get(self, operation_id):
        row = self.db.execute('SELECT * FROM operations WHERE id = ?', (operation_id,)).fetchone()
        if row is None:
            raise QojError(f'Unknown operation {operation_id}')
        return row

    def finish(self, operation_id, state, receipt=None):
        with self.db:
            self.db.execute('''UPDATE operations SET state=?, receipt=?, source=NULL, baseline=NULL
                WHERE id=?''', (state, receipt, operation_id))


def baseline(operation):
    username, rows = json.loads(operation['baseline'])
    return username, {int(sid): tuple(values) for sid, values in rows.items()}
