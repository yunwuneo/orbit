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
        from .threads import migrate
        migrate(self.db)

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
            self.put("next_input_poll", current)
            self.db.execute("UPDATE thread_tasks SET status='pending' WHERE status='running' AND output_run_id IS NULL")
            # Collapse overdue checks to one current wake per valid cause; never replay elapsed windows.
            self.db.execute("UPDATE threads SET missed_checks=missed_checks+(SELECT count(*) FROM thread_tasks t WHERE t.thread_id=threads.id AND t.status='pending' AND t.due_at<?) WHERE root_node_id IS NOT NULL", (current,))
            from .threads import Threads
            repo = Threads(self)
            for thread in repo.list():
                overdue = self.db.execute("SELECT error_code FROM thread_tasks WHERE thread_id=? AND status='pending' AND due_at<? ORDER BY error_code IS NULL,id LIMIT 1", (thread["id"], current)).fetchone()
                if overdue and thread["status"] != "closed":
                    self.db.execute("UPDATE thread_tasks SET status='superseded',error_code=coalesce(error_code,'recovery_coalesced') WHERE thread_id=? AND status='pending' AND due_at<?", (thread["id"], current))
                    cycle = thread["review_cycle"] + 1
                    self.db.execute("UPDATE threads SET review_cycle=?,next_check_at=?,projection_dirty=1 WHERE id=?", (cycle, current, thread["id"]))
                    task_id = repo.task(thread["id"], "echo_decision", f"recovery:{thread['revision']}:{cycle}", thread["revision"], current, current,
                                        [("node", n["id"]) for n in repo.nodes(thread["id"])])
                    if overdue["error_code"]:
                        self.db.execute("UPDATE thread_tasks SET error_code=? WHERE id=?", (overdue["error_code"], task_id))

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
            dependencies = set(operation.get("depends_on", []))
            for reference in operation.get("references", {}).values():
                dependencies.update(reference if isinstance(reference, list) else [reference])
            if seq and run["mode"] != "continuation":
                dependencies.add(f"orbit:{run['id']}:{seq - 1}")
            resources = set(operation.get("resources", []))
            if operation["kind"] in {"snapshot", "runtime", "append", "plan_update", "properties", "projection"}:
                resources.add("page:" + operation["page_id"])
            for resource in resources:
                # Resource order spans runs, while unrelated threads may publish independently.
                prior = self.db.execute("SELECT r.write_id FROM write_resources r JOIN outbox o ON o.id=r.write_id WHERE resource=? AND o.status!='done' AND r.write_id!=? ORDER BY o.rowid DESC LIMIT 1", (resource, key)).fetchone()
                if prior:
                    dependencies.add(prior[0])
                self.db.execute("INSERT INTO write_resources VALUES (?,?)", (key, resource))
            for dependency in dependencies:
                self.db.execute("INSERT INTO write_dependencies VALUES (?,?)", (key, dependency))
        if run["mode"] == "live":
            for page_id, version in consumed:
                self.db.execute("INSERT INTO consumed VALUES (?,?) ON CONFLICT(id) DO UPDATE SET version=excluded.version",
                                (page_id, version))
            self.put("cursor", run["end"])

    def mark_sent(self, key):
        with self.db:
            self.db.execute("UPDATE outbox SET sent=1 WHERE id=?", (key,))

    def write_ready(self, key):
        return not self.db.execute("SELECT 1 FROM write_dependencies d LEFT JOIN outbox o ON o.id=d.dependency_id WHERE d.write_id=? AND (o.status IS NULL OR o.status!='done')", (key,)).fetchone()

    def done(self, key, remote_id, operation=None, current=None):
        with self.db:
            if operation and operation["kind"] == "snapshot":
                marker = key + ":" + digest(operation)[:16]
                body = operation.get("prefix", "Orbit 当前生活快照\n") + marker + "\n" + operation["text"]
                self.put("snapshot:" + operation["page_id"], {"id": remote_id, "hash": digest(body)})
            # Drop full generated payload after acknowledgement; keep tiny dedup evidence.
            self.db.execute("UPDATE outbox SET status='done',payload=NULL,error=NULL,remote_id=?,verified_at=?,receipt_hash=? WHERE id=?",
                            (remote_id, current, digest(operation) if operation else None, key))
            self.db.execute("UPDATE thread_nodes SET notion_page_id=?,verified_at=? WHERE write_id=?", (remote_id, current, key))
            if operation and operation.get("thread_create"):
                self.db.execute("UPDATE threads SET notion_page_id=? WHERE id=?", (remote_id, operation["thread_create"]))
            if operation and operation.get("thread_projection"):
                thread = self.db.execute("SELECT * FROM threads WHERE id=?", (operation["thread_projection"],)).fetchone()
                matches = all(thread[k] == v for k, v in operation.get("projection_state", {
                    "revision": operation["revision"], "next_check_at": operation["values"].get("下次检查")}).items())
                self.db.execute("UPDATE threads SET published_revision=max(published_revision,?),sync_status=?,error_code=NULL,projection_dirty=? WHERE id=?",
                                (operation["revision"], "verified" if matches else "pending", 0 if matches else 1, operation["thread_projection"]))
            if operation and operation.get("thread_plan"):
                self.db.execute("INSERT OR IGNORE INTO thread_plan_links VALUES (?,?,?)",
                                (operation["thread_plan"], remote_id, operation["decision_node_id"]))

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
            "schema_version": self.db.execute("PRAGMA user_version").fetchone()[0],
            "continuation": self.continuation_status(),
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
            # Empty world probes are technical checks, not archived business matters.
            empty = "SELECT id FROM threads WHERE root_node_id IS NULL AND status='closed' AND created_at<?"
            self.db.execute("DELETE FROM task_dependencies WHERE task_id IN (SELECT id FROM thread_tasks WHERE thread_id IN (" + empty + "))", (cutoff,))
            self.db.execute("DELETE FROM thread_tasks WHERE thread_id IN (" + empty + ")", (cutoff,))
            self.db.execute("DELETE FROM threads WHERE id IN (" + empty + ")", (cutoff,))
            self.db.execute("DELETE FROM write_dependencies WHERE write_id IN (SELECT id FROM outbox WHERE status='done') AND dependency_id IN (SELECT id FROM outbox WHERE status='done')")
            self.db.execute("DELETE FROM write_resources WHERE write_id IN (SELECT id FROM outbox WHERE status='done')")
            self.db.execute("DELETE FROM outbox WHERE status='done' AND id NOT IN (SELECT dependency_id FROM write_dependencies UNION SELECT write_id FROM write_dependencies UNION SELECT write_id FROM thread_nodes WHERE write_id IS NOT NULL UNION SELECT dependency_id FROM task_dependencies WHERE dependency_type='write') AND run_id IN (SELECT id FROM runs WHERE generated_at<?)",
                            (cutoff,))
            # Keep coverage and source fingerprints; remove large per-source hash maps after retention.
            self.db.execute("UPDATE runs SET evidence='{}' WHERE generated_at<? AND id NOT IN (SELECT output_run_id FROM thread_tasks WHERE output_run_id IS NOT NULL)", (cutoff,))
            self.db.execute("PRAGMA incremental_vacuum")

    def continuation_status(self):
        from .threads import Threads
        threads = Threads(self)
        tasks = [dict(x) for x in self.db.execute("SELECT * FROM thread_tasks")]
        current = self.get("last_continuation_check") or self.get("cursor")
        ready = sum(threads.ready(task, current) for task in tasks) if current else 0
        return {"threads": {r[0]: r[1] for r in self.db.execute("SELECT status,count(*) FROM threads WHERE root_node_id IS NOT NULL GROUP BY status")},
                "runnable_tasks": ready, "waiting_tasks": sum(t["status"] == "pending" for t in tasks) - ready,
                "earliest_check": self.db.execute("SELECT min(next_check_at) FROM threads WHERE status!='closed'").fetchone()[0],
                "tasks": {status: sum(t["status"] == status for t in tasks) for status in ["pending", "blocked", "superseded", "done"]}}
