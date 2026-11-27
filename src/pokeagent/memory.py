"""The memory store: one SQLite database per data directory, shared by all runs so that knowledge carries over.

- `runs`: the objective and ROM (never change) and the run's status.
- `checks`: success checks by source. Condition and source never change; only an agent's proposed check moves to
  approved or rejected, and an agent's check is never active (authoritative).
- `timelines`: a run starts on one; restoring a checkpoint branches a new one.
- `events`: append-only history (actions, sessions, checkpoints, claims).
- `knowledge`: what the agent learned, with an FTS5 index. Topic and content never change; status, evidence and
  supersession do. Confirming needs an action event from the run's current timeline.

The triggers enforce these rules for every writer, not only for this module.
"""

import contextlib
import json
import re
import sqlite3
import threading
import time
from pathlib import Path

KINDS = ("mechanic", "technical", "map", "strategy", "lesson", "limitation", "progress")
SCOPES = ("run", "rom", "family", "general")
STATUSES = ("hypothesis", "confirmed", "refuted", "stale")
TOPIC_LIMIT, CONTENT_LIMIT = 200, 2000

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    objective TEXT NOT NULL,
    rom_sha1 TEXT NOT NULL,
    family TEXT NOT NULL,
    created REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'claimed', 'complete')),
    status_reason TEXT NOT NULL DEFAULT ''
);
CREATE TRIGGER IF NOT EXISTS runs_fixed BEFORE UPDATE OF id, objective, rom_sha1, family, created ON runs
BEGIN SELECT RAISE(ABORT, 'a run''s objective and ROM never change'); END;

CREATE TABLE IF NOT EXISTS checks (
    id INTEGER PRIMARY KEY,
    run TEXT NOT NULL REFERENCES runs(id),
    condition TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('user', 'adapter', 'agent')),
    status TEXT NOT NULL CHECK (status IN ('active', 'proposed', 'approved', 'rejected')),
    reason TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL,
    CHECK ((source = 'agent') = (status != 'active'))
);
CREATE TRIGGER IF NOT EXISTS checks_fixed BEFORE UPDATE OF id, run, condition, source, reason, created ON checks
BEGIN SELECT RAISE(ABORT, 'success checks never change'); END;
CREATE TRIGGER IF NOT EXISTS checks_settled BEFORE UPDATE OF status ON checks
WHEN NOT (OLD.status = 'proposed' AND NEW.status IN ('approved', 'rejected'))
BEGIN SELECT RAISE(ABORT, 'only a proposed check can be approved or rejected'); END;
CREATE TRIGGER IF NOT EXISTS checks_kept BEFORE DELETE ON checks
BEGIN SELECT RAISE(ABORT, 'success checks are never deleted'); END;

CREATE TABLE IF NOT EXISTS timelines (
    id INTEGER PRIMARY KEY,
    run TEXT NOT NULL REFERENCES runs(id),
    parent INTEGER REFERENCES timelines(id),
    branch TEXT,
    status TEXT NOT NULL CHECK (status IN ('current', 'abandoned')),
    created REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    run TEXT NOT NULL REFERENCES runs(id),
    timeline INTEGER NOT NULL REFERENCES timelines(id),
    session INTEGER,
    kind TEXT NOT NULL,
    data TEXT NOT NULL,
    created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS events_by_run ON events(run, kind, session);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;

CREATE TABLE IF NOT EXISTS knowledge (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    topic TEXT NOT NULL,
    content TEXT NOT NULL,
    scope TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('hypothesis', 'confirmed', 'refuted', 'stale')),
    evidence INTEGER REFERENCES events(id),
    url TEXT NOT NULL DEFAULT '',
    superseded_by INTEGER REFERENCES knowledge(id),
    run TEXT NOT NULL REFERENCES runs(id),
    timeline INTEGER NOT NULL REFERENCES timelines(id),
    session INTEGER,
    created REAL NOT NULL,
    updated REAL NOT NULL,
    CHECK (status != 'confirmed' OR evidence IS NOT NULL)
);
CREATE TRIGGER IF NOT EXISTS knowledge_fixed
BEFORE UPDATE OF id, kind, topic, content, scope, url, run, timeline, session, created ON knowledge
BEGIN SELECT RAISE(ABORT, 'knowledge is never rewritten; add a new entry that supersedes it'); END;
CREATE TRIGGER IF NOT EXISTS knowledge_kept BEFORE DELETE ON knowledge
BEGIN SELECT RAISE(ABORT, 'knowledge is never deleted; mark it refuted or superseded'); END;
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(topic, content, content='knowledge', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS knowledge_index AFTER INSERT ON knowledge
BEGIN INSERT INTO knowledge_fts(rowid, topic, content) VALUES (new.id, new.topic, new.content); END;
"""


class Memory:
    """The store, bound to one run. Safe to share between the runner and the MCP server's threads."""

    def __init__(self, path: Path, run: str):
        self.run = run
        self.db = sqlite3.connect(path, timeout=10, autocommit=True, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        self.db.executescript(SCHEMA)
        self.scopes: dict[str, str] = {}

    def close(self) -> None:
        self.db.close()

    def rows(self, sql: str, *args) -> list[sqlite3.Row]:
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    def write(self, sql: str, *args) -> int:
        with self.lock:
            return self.db.execute(sql, args).lastrowid

    @contextlib.contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            self.db.execute("COMMIT")

    # ---- runs and checks ----

    def open_run(self, meta: dict) -> None:
        """Record the run from its run.json the first time; afterwards the database decides and run.json must agree."""
        found = self.rows("SELECT objective, rom_sha1 FROM runs WHERE id = ?", self.run)
        if found:
            if tuple(found[0]) != (meta["objective"], meta["rom_sha1"]):
                raise ValueError(f"run {self.run}: run.json no longer matches the recorded objective and ROM")
        else:
            now = time.time()
            with self.transaction() as db:
                db.execute(
                    "INSERT INTO runs (id, objective, rom_sha1, family, created) VALUES (?, ?, ?, ?, ?)",
                    (self.run, meta["objective"], meta["rom_sha1"], meta["family"], now),
                )
                for source, condition in (("adapter", meta.get("check")), ("user", meta.get("success"))):
                    if condition:
                        db.execute(
                            "INSERT INTO checks (run, condition, source, status, created)"
                            " VALUES (?, ?, ?, 'active', ?)",
                            (self.run, json.dumps(condition), source, now),
                        )
                db.execute("INSERT INTO timelines (run, status, created) VALUES (?, 'current', ?)", (self.run, now))
        self.scopes = {
            "run": f"run:{self.run}",
            "rom": f"rom:{meta['rom_sha1']}",
            "family": f"family:{meta['family']}",
            "general": "general",
        }

    def status(self) -> tuple[str, str]:
        row = self.rows("SELECT status, status_reason FROM runs WHERE id = ?", self.run)[0]
        return row["status"], row["status_reason"]

    def set_status(self, status: str, reason: str = "") -> None:
        self.write("UPDATE runs SET status = ?, status_reason = ? WHERE id = ?", status, reason, self.run)

    def checks(self, status: str) -> list[dict]:
        rows = self.rows("SELECT * FROM checks WHERE run = ? AND status = ? ORDER BY id", self.run, status)
        return [dict(row) | {"condition": json.loads(row["condition"])} for row in rows]

    def add_check(self, condition: dict, source: str, reason: str = "") -> int:
        status = "proposed" if source == "agent" else "active"
        return self.write(
            "INSERT INTO checks (run, condition, source, status, reason, created) VALUES (?, ?, ?, ?, ?, ?)",
            self.run,
            json.dumps(condition),
            source,
            status,
            reason,
            time.time(),
        )

    def settle_check(self, check: int, status: str) -> None:
        self.write("UPDATE checks SET status = ? WHERE id = ? AND run = ?", status, check, self.run)

    # ---- timelines and events ----

    def timeline(self) -> int:
        return self.rows("SELECT id FROM timelines WHERE run = ? AND status = 'current'", self.run)[0]["id"]

    def event(self, kind: str, data: dict, session: int | None = None) -> int:
        return self.write(
            "INSERT INTO events (run, timeline, session, kind, data, created) VALUES (?, ?, ?, ?, ?, ?)",
            self.run,
            self.timeline(),
            session,
            kind,
            json.dumps(data),
            time.time(),
        )

    def events(self, kind: str, session: int | None = None, after: int = 0, limit: int = 100) -> list[dict]:
        """The run's latest `kind` events (optionally of one session, after an event id), oldest first."""
        sql = "SELECT * FROM events WHERE run = ? AND kind = ? AND id > ?"
        args = [self.run, kind, after]
        if session is not None:
            sql += " AND session = ?"
            args.append(session)
        rows = self.rows(sql + " ORDER BY id DESC LIMIT ?", *args, limit)
        return [dict(row) | {"data": json.loads(row["data"])} for row in reversed(rows)]

    def _evidence(self, event: int) -> int:
        found = self.rows(
            "SELECT 1 FROM events WHERE id = ? AND run = ? AND timeline = ? AND kind = 'action'",
            event,
            self.run,
            self.timeline(),
        )
        if not found:
            raise ValueError(f"event {event} is not an action of this run's current timeline")
        return event

    # ---- knowledge ----

    def add_knowledge(
        self,
        kind: str,
        topic: str,
        content: str,
        scope: str = "rom",
        url: str = "",
        evidence: int | None = None,
        session: int | None = None,
    ) -> int:
        """Add an entry. It is confirmed only with evidence (an action event) and never when it came from the web."""
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)}")
        if scope not in SCOPES:
            raise ValueError(f"scope must be one of {', '.join(SCOPES)}")
        if kind == "progress" and scope != "run":
            raise ValueError("progress knowledge describes this run's game, so its scope must be run")
        if not topic.strip() or len(topic) > TOPIC_LIMIT or not content.strip() or len(content) > CONTENT_LIMIT:
            raise ValueError(
                f"topic (at most {TOPIC_LIMIT} characters) and content (at most {CONTENT_LIMIT}) are needed"
            )
        status = "hypothesis"
        if evidence and not url:
            status, evidence = "confirmed", self._evidence(evidence)
        now = time.time()
        return self.write(
            "INSERT INTO knowledge (kind, topic, content, scope, status, evidence, url, run, timeline, session,"
            " created, updated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            kind,
            topic,
            content,
            self.scopes[scope],
            status,
            evidence or None,
            url,
            self.run,
            self.timeline(),
            session,
            now,
            now,
        )

    def set_knowledge_status(
        self, entry: int, status: str, evidence: int | None = None, superseded_by: int | None = None
    ) -> None:
        if status not in STATUSES:
            raise ValueError(f"status must be one of {', '.join(STATUSES)}")
        if status == "confirmed":
            if not evidence:
                raise ValueError("confirming needs evidence: the id of an action event that showed it")
            evidence = self._evidence(evidence)
        for known in (entry, superseded_by):
            if known and not self._visible(known):
                raise ValueError(f"unknown knowledge entry {known}")
        if superseded_by and self._breadth(superseded_by) < self._breadth(entry):
            raise ValueError("a replacement must be at least as widely scoped as the entry it supersedes")
        self.write(
            "UPDATE knowledge SET status = ?, evidence = COALESCE(?, evidence),"
            " superseded_by = COALESCE(?, superseded_by), updated = ? WHERE id = ?",
            status,
            evidence or None,
            superseded_by or None,
            time.time(),
            entry,
        )

    def _breadth(self, entry: int) -> int:
        scope = self.rows("SELECT scope FROM knowledge WHERE id = ?", entry)[0]["scope"]
        return list(self.scopes.values()).index(scope)  # run narrowest, general widest

    def discard_progress_after(self, checkpoint_file: str | None) -> None:
        """Refute this run's progress knowledge recorded after the checkpoint `checkpoint_file` (None: since
        power-on): restoring it loses that game progress, so it must not be offered as fact."""
        since = 0.0
        if checkpoint_file is not None:
            found = [e for e in self.events("checkpoint", limit=1000) if e["data"]["file"] == checkpoint_file]
            if not found:
                return
            since = found[-1]["created"]
        # an older entry that a later (now refuted) one superseded is live again; "stale" came from that replacement
        self.write(
            "UPDATE knowledge SET superseded_by = NULL, updated = ?,"
            " status = CASE WHEN status = 'stale' THEN 'hypothesis' ELSE status END"
            " WHERE run = ? AND kind = 'progress' AND created <= ? AND superseded_by IN"
            " (SELECT id FROM knowledge WHERE run = ? AND kind = 'progress' AND created > ?)",
            time.time(),
            self.run,
            since,
            self.run,
            since,
        )
        self.write(
            "UPDATE knowledge SET status = 'refuted', updated = ? WHERE run = ? AND kind = 'progress'"
            " AND created > ? AND status != 'refuted'",
            time.time(),
            self.run,
            since,
        )

    def _visible(self, entry: int) -> bool:
        return bool(
            self.rows("SELECT 1 FROM knowledge WHERE id = ? AND scope IN (?, ?, ?, ?)", entry, *self.scopes.values())
        )

    def search(self, query: str, limit: int = 5) -> list[dict]:
        """Entries in this run's scopes that match any word of `query`, best first; refuted and superseded entries
        are left out."""
        words = re.findall(r"\w+", query.lower())
        if not words:
            return []
        match = " OR ".join(f'"{w}"' for w in words)
        scopes = list(self.scopes.values())
        rows = self.rows(
            "SELECT k.* FROM knowledge_fts JOIN knowledge k ON k.id = knowledge_fts.rowid"
            f" WHERE knowledge_fts MATCH ? AND k.scope IN ({','.join('?' * len(scopes))})"
            " AND k.status != 'refuted' AND k.superseded_by IS NULL ORDER BY bm25(knowledge_fts) LIMIT ?",
            match,
            *scopes,
            limit,
        )
        return [dict(row) for row in rows]


def describe_knowledge(entry: dict) -> str:
    scope = entry["scope"].split(":")[0]
    source = f", from {entry['url']}" if entry["url"] else ""
    return f"#{entry['id']} [{entry['kind']}, {entry['status']}, {scope}{source}] {entry['topic']}: {entry['content']}"
