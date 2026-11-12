"""Durable submission intents and a process lock shared by the CLI entrypoints."""
import json
import os
import sqlite3
from contextlib import AbstractContextManager

from .client import QojError


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
