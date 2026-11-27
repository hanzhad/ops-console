"""The SQLite read model. One connection, one RLock; every write bumps one global rev and publishes a frame.

The machine is the source of truth: this file is deletable and rebuilt by the watchers.
"""

import json
import secrets
import sqlite3
import threading
import time

from sw import core

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS blocks(name TEXT PRIMARY KEY, json TEXT NOT NULL, rev INTEGER NOT NULL,
  pass_seq INTEGER NOT NULL, updated_at REAL NOT NULL, period REAL NOT NULL, error TEXT, error_at REAL);
CREATE TABLE IF NOT EXISTS commands(id TEXT PRIMARY KEY, route TEXT NOT NULL, body TEXT NOT NULL,
  zones TEXT NOT NULL, waiting TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN('running','ok','failed')),
  message TEXT, created_at REAL NOT NULL, deadline_at REAL NOT NULL, finished_seq INTEGER, done_at REAL, rev INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS pruned_ids(n INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE);
CREATE INDEX IF NOT EXISTS commands_rev ON commands(rev);
CREATE TABLE IF NOT EXISTS events(rev INTEGER PRIMARY KEY, at REAL NOT NULL, text TEXT NOT NULL);
"""
CMD_COLS = ("id", "route", "body", "zones", "waiting", "state", "message", "created_at", "deadline_at",
            "finished_seq", "done_at", "rev")
BLOCK_COLS = "name,json,rev,pass_seq,updated_at,period,error,error_at"
KEEP_COMMANDS = 200
KEEP_EVENTS = 500
KEEP_PRUNED = 5000  # tombstones of pruned command ids: a resubmitted id stays a 409


def block_payload(row):
    name, js, rev, _seq, updated_at, period, error, _error_at = row
    return {"name": name, "rev": rev, "updated_at": updated_at, "period": period, "error": error, "data": json.loads(js)}


def command_payload(c):
    return {"id": c["id"], "route": c["route"], "body": json.loads(c["body"]), "state": c["state"],
            "message": c["message"], "deadline_at": c["deadline_at"], "rev": c["rev"]}


class Store:
    def __init__(self, path):
        self.publish = None  # fn(event, payload, rev), called under the lock after each write
        self.lock = threading.RLock()
        self.con = sqlite3.connect(str(path), check_same_thread=False, timeout=5)
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.execute("PRAGMA synchronous=NORMAL")
        self.con.execute("PRAGMA busy_timeout=5000")
        with self.lock, self.con:
            self.con.executescript(SCHEMA)
        self.rev = self._max_rev()

    def close(self):
        with self.lock:
            self.con.close()

    def _max_rev(self):
        q = "SELECT COALESCE(MAX(rev),0) FROM "
        return max(self.con.execute(q + t).fetchone()[0] for t in ("blocks", "commands", "events"))

    def boot(self):
        """Counters restart with the process: pass_seq goes to 0, running commands fail, a new boot id."""
        with self.lock, self.con:
            self.con.execute("UPDATE blocks SET pass_seq=0")
            for c in self.commands("running"):
                self._write_command(core.cmd_on_boot(c), publish=False)
            self.con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('boot',?)", (secrets.token_hex(8),))
            self.rev = self._max_rev()

    def boot_id(self):
        with self.lock:
            return self.con.execute("SELECT value FROM meta WHERE key='boot'").fetchone()[0]

    def _next_rev(self):
        self.rev += 1
        return self.rev

    def _emit(self, event, payload, rev):
        if self.publish:
            self.publish(event, payload, rev)

    # --- blocks ---------------------------------------------------------------------------

    def block_row(self, name):
        return self.con.execute("SELECT %s FROM blocks WHERE name=?" % BLOCK_COLS, (name,)).fetchone()

    def put_block(self, name, data, pass_seq, period=None):
        """Returns the new rev, or None when the pass is older or the data unchanged."""
        js = core.canonical(data)
        with self.lock:
            with self.con:
                done = self._put_block(name, js, pass_seq, period)
            if done is None:
                return None
            rev, payload = done
            self._emit("block", payload, rev)  # after COMMIT: a frame never announces a write that rolled back
            return rev

    def _put_block(self, name, js, pass_seq, period):
        row = self.block_row(name)
        prev = {"json": row[1], "pass_seq": row[3]} if row else None
        verdict = core.decide_put(prev, js, pass_seq)
        if verdict == "older":
            return None
        now = time.time()
        if verdict == "same" and row[6] is None:
            self.con.execute("UPDATE blocks SET pass_seq=?, updated_at=? WHERE name=?", (pass_seq, now, name))
            return None
        rev = self._next_rev()
        per = period if period is not None else (row[5] if row else 0)
        self.con.execute("INSERT OR REPLACE INTO blocks(%s) VALUES(?,?,?,?,?,?,NULL,NULL)" % BLOCK_COLS,
                         (name, js, rev, pass_seq, now, per))
        return rev, block_payload(self.block_row(name))

    def put_error(self, name, err):
        now = time.time()
        with self.lock, self.con:
            row = self.block_row(name)
            if row is not None and row[6] == str(err)[:300]:
                return row[2]  # unchanged: no rev bump, no frame
            rev = self._next_rev()
            if self.block_row(name) is None:
                self.con.execute("INSERT INTO blocks(%s) VALUES(?,?,?,?,?,?,?,?)" % BLOCK_COLS,
                                 (name, "null", rev, 0, now, 0, str(err)[:300], now))
            else:
                self.con.execute("UPDATE blocks SET rev=?, error=?, error_at=? WHERE name=?", (rev, str(err)[:300], now, name))
            self._emit("block", block_payload(self.block_row(name)), rev)
            return rev

    def blocks(self):
        with self.lock:
            rows = self.con.execute("SELECT %s FROM blocks ORDER BY name" % BLOCK_COLS).fetchall()
            return [block_payload(r) for r in rows]

    def block_data(self, name):
        with self.lock:
            row = self.block_row(name)
            return json.loads(row[1]) if row else None

    def block_errors(self):
        with self.lock:
            return dict(self.con.execute("SELECT name, error FROM blocks WHERE error IS NOT NULL").fetchall())

    # --- commands -------------------------------------------------------------------------

    @staticmethod
    def _cmd(row):
        c = dict(zip(CMD_COLS, row))
        c["zones"], c["waiting"] = json.loads(c["zones"]), json.loads(c["waiting"])
        return c

    def get_command(self, cid):
        with self.lock:
            row = self.con.execute("SELECT %s FROM commands WHERE id=?" % ",".join(CMD_COLS), (cid,)).fetchone()
            return self._cmd(row) if row else None

    def was_pruned(self, cid):
        with self.lock:
            return self.con.execute("SELECT 1 FROM pruned_ids WHERE id=?", (cid,)).fetchone() is not None

    def commands(self, state=None):
        with self.lock:
            q = "SELECT %s FROM commands" % ",".join(CMD_COLS)
            rows = self.con.execute(q + (" WHERE state=?" if state else "") + " ORDER BY rev", (state,) if state else ())
            return [self._cmd(r) for r in rows.fetchall()]

    def _write_command(self, c, publish=True):
        c = {**c, "rev": self._next_rev()}
        vals = [json.dumps(c[k]) if k in ("zones", "waiting") else c[k] for k in CMD_COLS]
        marks = ",".join("?" * len(CMD_COLS))
        self.con.execute("INSERT OR REPLACE INTO commands(%s) VALUES(%s)" % (",".join(CMD_COLS), marks), vals)
        if publish:
            self._emit("command", command_payload(c), c["rev"])
        return c

    def save_command(self, c):
        """Insert or replace one command, bump its rev, publish it, and prune the terminal ones."""
        with self.lock, self.con:
            c = self._write_command(c)
            if c["state"] != "running":
                rows = self.con.execute("SELECT id,state,rev FROM commands WHERE state!='running'").fetchall()
                plan = core.prune_plan([{"id": r[0], "state": r[1], "rev": r[2]} for r in rows], KEEP_COMMANDS)
                for cid in plan:
                    self.con.execute("DELETE FROM commands WHERE id=?", (cid,))
                    self.con.execute("INSERT OR IGNORE INTO pruned_ids(id) VALUES(?)", (cid,))
                if plan:
                    self.con.execute("DELETE FROM pruned_ids WHERE n <= (SELECT MAX(n) FROM pruned_ids) - ?", (KEEP_PRUNED,))
            return c

    # --- log events -----------------------------------------------------------------------

    def add_event(self, text):
        with self.lock, self.con:
            rev, now = self._next_rev(), time.time()
            self.con.execute("INSERT INTO events(rev,at,text) VALUES(?,?,?)", (rev, now, text))
            self.con.execute("DELETE FROM events WHERE rev NOT IN (SELECT rev FROM events ORDER BY rev DESC LIMIT ?)", (KEEP_EVENTS,))
            self._emit("log", {"rev": rev, "at": now, "text": text}, rev)
            return rev

    def events(self, n):
        with self.lock:
            rows = self.con.execute("SELECT rev,at,text FROM events ORDER BY rev DESC LIMIT ?", (n,)).fetchall()
            return [{"rev": r, "at": a, "text": t} for r, a, t in reversed(rows)]

    # --- a consistent view for a connecting stream ----------------------------------------

    def full_view(self):
        """(rev, blocks, commands, log lines), read under the lock the publishers hold."""
        with self.lock:
            cmds = self.commands()
            keep = [c for c in cmds if c["state"] == "running"] + [c for c in cmds if c["state"] != "running"][-20:]
            return self.rev, self.blocks(), sorted(keep, key=lambda c: c["rev"]), self.events(20)
