"""SQLite holds scheduling and write intents, not an alternative Notion archive."""

import fcntl
import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from .timeutil import parse, stamp


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


class Busy(RuntimeError):
    pass


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.is_symlink():
            raise ValueError("运行数据库不能是符号链接")
        self.db = sqlite3.connect(self.path, timeout=10)
        os.chmod(self.path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=DELETE;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS gaps(
                id INTEGER PRIMARY KEY, start TEXT NOT NULL, end TEXT NOT NULL, reason TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS runs(
                id TEXT PRIMARY KEY, start TEXT NOT NULL, end TEXT NOT NULL,
                generated_at TEXT NOT NULL, mode TEXT NOT NULL, evidence TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS consumed(id TEXT PRIMARY KEY, version TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS outbox(
                id TEXT PRIMARY KEY, run_id TEXT NOT NULL, seq INTEGER NOT NULL,
                payload TEXT, status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0, next_at TEXT,
                error TEXT, remote_id TEXT, sent INTEGER NOT NULL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS outbox_work ON outbox(status, seq);
            CREATE TABLE IF NOT EXISTS backfills(
                id TEXT PRIMARY KEY, start TEXT NOT NULL, end TEXT NOT NULL,
                cursor TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending');
            CREATE TABLE IF NOT EXISTS diaries(day TEXT PRIMARY KEY, signature TEXT NOT NULL);
        """)

    def close(self):
        self.db.close()

    @contextmanager
    def lock(self, name="operation"):
        lock_path = self.path.with_suffix(f".{name}.lock")
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Busy("另一个 Orbit 操作正在执行；稍后重试") from None
            yield
        finally:
            os.close(fd)

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key, value):
        self.db.execute("INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, encode(value)))

    def gap(self, start, end, reason):
        if start >= end:
            return
        # Coalesce overlapping outages; keep completed backfill ranges in runs as the coverage ledger.
        rows = self.db.execute("SELECT * FROM gaps WHERE start<=? AND end>=?", (end, start)).fetchall()
        for row in rows:
            start, end = min(start, row["start"]), max(end, row["end"])
            self.db.execute("DELETE FROM gaps WHERE id=?", (row["id"],))
        self.db.execute("INSERT INTO gaps(start,end,reason) VALUES (?,?,?)", (start, end, reason))

    def recover(self, current, reason="process_start"):
        with self.db:
            previous = self.get("cursor")
            if previous and parse(current) < parse(previous):
                raise ValueError("时钟早于持久游标；等待现实时间追上")
            if previous:
                self.gap(previous, current, reason)
            self.put("cursor", current)
            self.put("session_started", current)

    def pause(self, current):
        with self.db:
            self.put("paused", True)
            self.put("paused_at", current)

    def resume(self, current):
        self.recover(current, "pause_or_restart")
        with self.db:
            self.put("paused", False)

    def consumed(self, page_id, version):
        row = self.db.execute("SELECT version FROM consumed WHERE id=?", (page_id,)).fetchone()
        return bool(row and row[0] == version)

    def pending(self):
        return self.db.execute("SELECT * FROM outbox WHERE status!='done' ORDER BY rowid").fetchall()

    def stage(self, run, operations, consumed=()):
        # Caller owns the transaction, including backfill/diary cursors.
        self.db.execute("INSERT INTO runs VALUES (?,?,?,?,?,?)",
                        (run["id"], run["start"], run["end"], run["generated_at"],
                         run["mode"], encode(run["evidence"])))
        for seq, operation in enumerate(operations):
            key = f"orbit:{run['id']}:{seq}"
            self.db.execute("INSERT INTO outbox(id,run_id,seq,payload) VALUES (?,?,?,?)",
                            (key, run["id"], seq, encode(operation)))
        if run["mode"] == "live":
            for page_id, version in consumed:
                self.db.execute("INSERT INTO consumed VALUES (?,?) ON CONFLICT(id) DO UPDATE SET version=excluded.version",
                                (page_id, version))
            self.put("cursor", run["end"])

    def mark_sent(self, key):
        with self.db:
            self.db.execute("UPDATE outbox SET sent=1 WHERE id=?", (key,))

    def done(self, key, remote_id, operation=None):
        with self.db:
            if operation and operation["kind"] == "snapshot":
                marker = key + ":" + digest(operation)[:16]
                body = operation.get("prefix", "Orbit 当前生活快照\n") + marker + "\n" + operation["text"]
                self.put("snapshot:" + operation["page_id"], {"id": remote_id, "hash": digest(body)})
            # Drop full generated payload after acknowledgement; keep tiny dedup evidence.
            self.db.execute("UPDATE outbox SET status='done',payload=NULL,error=NULL,remote_id=? WHERE id=?",
                            (remote_id, key))

    def operational(self, page_id, current, details):
        run = {"id": uuid4().hex, "start": current, "end": current, "generated_at": current,
               "mode": "operations", "evidence": {}}
        with self.db:
            self.stage(run, [{"kind": "runtime", "page_id": page_id,
                              "text": "Orbit 运行状态（技术记录）：\n" + encode(details)}])

    def fail(self, row, current, error, max_attempts, uncertain=False):
        attempts = row["attempts"] + 1
        terminal = attempts >= max_attempts or not getattr(error, "retryable", True)
        status = "blocked" if terminal else "pending"
        # error is a controlled code/category only, never arbitrary model/server text.
        message = "uncertain_write" if uncertain else type(error).__name__
        if not uncertain and getattr(error, "status", None) is not None:
            message += f":HTTP{error.status}"
        if not uncertain and getattr(error, "service", None):
            message = error.service + ":" + message
        with self.db:
            self.db.execute("UPDATE outbox SET attempts=?,next_at=?,error=?,status=? WHERE id=?",
                            (attempts, stamp(parse(current) + timedelta(seconds=min(300, 2 ** min(attempts, 8)))),
                             message, status, row["id"]))

    def add_backfill(self, start, end):
        if parse(start) >= parse(end):
            raise ValueError("补写区间不能为空")
        gap = self.db.execute("SELECT 1 FROM gaps WHERE start<=? AND end>=?", (start, end)).fetchone()
        if not gap:
            raise ValueError("只能补写已记录的未模拟区间")
        key = digest([start, end])[:24]
        existing = self.db.execute("SELECT * FROM backfills WHERE id=?", (key,)).fetchone()
        if existing:
            return key
        if self.db.execute("SELECT 1 FROM runs WHERE mode IN ('live','backfill') AND start<? AND end>?", (end, start)).fetchone():
            raise ValueError("补写区间与已有生活记录重叠")
        if self.db.execute("SELECT 1 FROM backfills WHERE start<? AND end>?", (end, start)).fetchone():
            raise ValueError("补写区间与已有补写任务重叠，请续跑原任务")
        with self.db:
            self.db.execute("INSERT INTO backfills(id,start,end,cursor) VALUES (?,?,?,?)", (key, start, end, start))
        return key

    def status(self):
        writes = []
        for row in self.pending():
            payload = json.loads(row["payload"])
            writes.append({key: row[key] for key in ["id", "status", "attempts", "error", "next_at", "sent"]}
                          | {"operation": payload["kind"], "page_id": payload.get("page_id"),
                             "source": payload.get("source"), "marker_prefix": row["id"]})
        return {
            "paused": self.get("paused", False), "cursor": self.get("cursor"),
            "last_error": self.get("last_error"), "next_generation_at": self.get("next_generation_at"),
            "generation_blocked": self.get("generation_blocked", False),
            "gaps": [dict(x) for x in self.db.execute("SELECT * FROM gaps ORDER BY start")],
            "backfills": [dict(x) for x in self.db.execute("SELECT * FROM backfills ORDER BY start")],
            "writes": writes,
        }

    def prune(self, current, retention_days):
        cutoff = stamp(parse(current) - timedelta(days=retention_days))
        with self.db:
            self.db.execute("DELETE FROM outbox WHERE status='done' AND run_id IN (SELECT id FROM runs WHERE generated_at<?)",
                            (cutoff,))
            # Keep coverage and source fingerprints; remove large per-source hash maps after retention.
            self.db.execute("UPDATE runs SET evidence='{}' WHERE generated_at<?", (cutoff,))
            self.db.execute("PRAGMA incremental_vacuum")
