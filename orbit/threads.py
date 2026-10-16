"""Incremental causal indexes. Notion, rather than this database, owns story bodies."""

import json
from datetime import timedelta
from uuid import uuid4

from .store import digest, encode
from .timeutil import parse, stamp


SCHEMA_VERSION = 1


def migrate(db):
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise ValueError("运行数据库版本高于当前程序；拒绝降级")
    if version == SCHEMA_VERSION:
        return
    # executescript commits implicitly: use individual statements inside one transaction.
    statements = [
        """CREATE TABLE threads(
            id TEXT PRIMARY KEY, root_node_id TEXT, revision INTEGER NOT NULL DEFAULT 0,
            published_revision INTEGER NOT NULL DEFAULT -1, status TEXT NOT NULL DEFAULT 'awaiting_echo',
            sync_status TEXT NOT NULL DEFAULT 'pending', notion_page_id TEXT, next_check_at TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, closed_by_node_id TEXT,
            no_reply_count INTEGER NOT NULL DEFAULT 0, review_cycle INTEGER NOT NULL DEFAULT 0,
            error_code TEXT, create_write_id TEXT, projection_dirty INTEGER NOT NULL DEFAULT 1,
            missed_checks INTEGER NOT NULL DEFAULT 0)""",
        """CREATE TABLE thread_nodes(
            id TEXT PRIMARY KEY, thread_id TEXT NOT NULL REFERENCES threads(id), parent_node_id TEXT,
            kind TEXT NOT NULL, actor TEXT NOT NULL, provider TEXT NOT NULL, executor TEXT NOT NULL,
            occurred_at TEXT NOT NULL, generated_at TEXT NOT NULL, source_page_id TEXT,
            source_version TEXT, source_kind TEXT NOT NULL, information_nature TEXT,
            notion_page_id TEXT, write_id TEXT, verified_at TEXT, revision INTEGER NOT NULL,
            mode TEXT NOT NULL, metadata TEXT NOT NULL DEFAULT '{}', authority_version TEXT,
            UNIQUE(source_page_id, source_version))""",
        """CREATE TABLE thread_edges(from_node_id TEXT NOT NULL REFERENCES thread_nodes(id),
            to_node_id TEXT NOT NULL REFERENCES thread_nodes(id), relation TEXT NOT NULL,
            PRIMARY KEY(from_node_id,to_node_id,relation))""",
        """CREATE TABLE thread_tasks(id TEXT PRIMARY KEY, thread_id TEXT NOT NULL REFERENCES threads(id),
            kind TEXT NOT NULL, cause_key TEXT NOT NULL, expected_revision INTEGER NOT NULL,
            due_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
            next_at TEXT, error_code TEXT, output_run_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(thread_id,kind,cause_key))""",
        """CREATE TABLE task_dependencies(task_id TEXT NOT NULL REFERENCES thread_tasks(id),
            dependency_type TEXT NOT NULL, dependency_id TEXT NOT NULL,
            PRIMARY KEY(task_id,dependency_type,dependency_id))""",
        """CREATE TABLE thread_plan_links(thread_id TEXT NOT NULL REFERENCES threads(id),
            plan_page_id TEXT NOT NULL, decision_node_id TEXT NOT NULL REFERENCES thread_nodes(id),
            PRIMARY KEY(thread_id,plan_page_id,decision_node_id))""",
        """CREATE TABLE write_dependencies(write_id TEXT NOT NULL REFERENCES outbox(id),
            dependency_id TEXT NOT NULL REFERENCES outbox(id), PRIMARY KEY(write_id,dependency_id))""",
        """CREATE TABLE write_resources(write_id TEXT NOT NULL REFERENCES outbox(id), resource TEXT NOT NULL,
            PRIMARY KEY(write_id,resource))""",
        "ALTER TABLE outbox ADD COLUMN verified_at TEXT",
        "ALTER TABLE outbox ADD COLUMN receipt_hash TEXT",
        "CREATE INDEX thread_due ON thread_tasks(status,due_at,id)",
        "CREATE INDEX node_thread ON thread_nodes(thread_id,revision,occurred_at)",
    ]
    with db:
        for sql in statements:
            db.execute(sql)
        db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


class Threads:
    def __init__(self, store):
        self.store, self.db = store, store.db

    def get(self, thread_id):
        row = self.db.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone()
        if not row:
            raise ValueError("事项不存在")
        return dict(row)

    def list(self):
        return [dict(x) for x in self.db.execute("SELECT * FROM threads WHERE root_node_id IS NOT NULL ORDER BY created_at,id")]

    def nodes(self, thread_id, verified=False):
        return [dict(x) for x in self.db.execute(
            "SELECT * FROM thread_nodes WHERE thread_id=?" + (" AND verified_at IS NOT NULL" if verified else "")
            + " ORDER BY revision,occurred_at,rowid", (thread_id,))]

    def summary(self, thread_id):
        thread = self.get(thread_id)
        nodes = self.nodes(thread_id)
        for node in nodes:
            node.pop("metadata")
        return {"thread": thread, "nodes": nodes,
                "edges": [dict(x) for x in self.db.execute(
                    "SELECT e.* FROM thread_edges e JOIN thread_nodes n ON n.id=e.to_node_id WHERE n.thread_id=?", (thread_id,))],
                "tasks": [dict(x) for x in self.db.execute("SELECT * FROM thread_tasks WHERE thread_id=? ORDER BY due_at,id", (thread_id,))]}

    def create(self, current):
        key = str(uuid4())
        self.db.execute("INSERT INTO threads(id,created_at,updated_at) VALUES (?,?,?)", (key, current, current))
        return key

    def bump(self, thread_id, revision, current):
        changed = self.db.execute("UPDATE threads SET revision=revision+1,updated_at=?,sync_status='pending',error_code=NULL,projection_dirty=1 WHERE id=? AND revision=?",
                                  (current, thread_id, revision))
        if changed.rowcount != 1:
            raise ValueError("事项修订已变化")
        self.db.execute("UPDATE thread_tasks SET status='superseded',updated_at=? WHERE thread_id=? AND expected_revision!=? AND status IN ('pending','running','blocked') AND output_run_id IS NULL",
                        (current, thread_id, revision + 1))
        return revision + 1

    def node(self, thread_id, revision, current, *, kind, actor, provider, occurred_at,
             parents=(), source_page_id=None, source_version=None, source_kind="orbit_narrative",
             information_nature="共同叙事", notion_page_id=None, write_id=None, verified_at=None, metadata=None):
        key = str(uuid4())
        self.db.execute("""INSERT INTO thread_nodes(id,thread_id,parent_node_id,kind,actor,provider,executor,
            occurred_at,generated_at,source_page_id,source_version,source_kind,information_nature,
            notion_page_id,write_id,verified_at,revision,mode,metadata) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (key, thread_id, parents[0][0] if parents else None, kind, actor, provider, "Orbit", occurred_at,
             current, source_page_id, source_version, source_kind, information_nature, notion_page_id,
             write_id, verified_at, revision, "live", encode(metadata or {})))
        for parent, relation in parents:
            self.db.execute("INSERT INTO thread_edges VALUES (?,?,?)", (parent, key, relation))
        # parent_node_id contains a single ID; multiple causal parents live in edges.
        if parents:
            self.db.execute("UPDATE thread_nodes SET parent_node_id=? WHERE id=?", (parents[0][0], key))
        return key

    def task(self, thread_id, kind, cause, revision, due, current, dependencies=()):
        key = "task:" + digest([thread_id, kind, cause])[:32]
        self.db.execute("""INSERT OR IGNORE INTO thread_tasks(id,thread_id,kind,cause_key,expected_revision,due_at,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?)""", (key, thread_id, kind, cause, revision, due, current, current))
        for dependency_type, dependency_id in dependencies:
            self.db.execute("INSERT OR IGNORE INTO task_dependencies VALUES (?,?,?)", (key, dependency_type, dependency_id))
        return key

    def recheck(self, thread_id, current):
        thread = self.get(thread_id)
        if not thread["root_node_id"] or thread["status"] == "closed":
            raise ValueError("没有可复评的活动事项；结束事项需要明确新输入")
        with self.db:
            return self.task(thread_id, "echo_decision", "admin:" + str(thread["revision"]), thread["revision"], current, current,
                             [("node", n["id"]) for n in self.nodes(thread_id)])

    def ready(self, task, current):
        if task["status"] != "pending" or task["due_at"] > current or (task["next_at"] and task["next_at"] > current):
            return False
        thread = self.get(task["thread_id"])
        if task["expected_revision"] != thread["revision"]:
            return False
        # Publication of all committed nodes and projections is a per-thread barrier.
        if self.db.execute("SELECT 1 FROM thread_nodes WHERE thread_id=? AND verified_at IS NULL", (thread["id"],)).fetchone():
            return False
        if thread["root_node_id"] and (not thread["notion_page_id"] or thread["published_revision"] < thread["revision"] or thread["projection_dirty"]):
            return False
        if self.db.execute("SELECT 1 FROM write_resources r JOIN outbox o ON o.id=r.write_id WHERE r.resource=? AND o.status!='done'", ("thread:" + thread["id"],)).fetchone():
            return False
        for dep in self.db.execute("SELECT * FROM task_dependencies WHERE task_id=?", (task["id"],)):
            kind, key = dep["dependency_type"], dep["dependency_id"]
            if kind == "write":
                row = self.db.execute("SELECT status FROM outbox WHERE id=?", (key,)).fetchone()
                if not row or row[0] != "done":
                    return False
            elif kind == "node":
                row = self.db.execute("SELECT verified_at FROM thread_nodes WHERE id=?", (key,)).fetchone()
                if not row or not row[0]:
                    return False
            elif kind == "task":
                row = self.db.execute("SELECT status FROM thread_tasks WHERE id=?", (key,)).fetchone()
                if not row or row[0] != "done":
                    return False
            elif kind == "source":
                # The actual source version is re-read by the caller, never trusted locally.
                continue
            else:
                return False
        return True

    def due(self, current, limit):
        # Filter dependency-blocked tasks before applying budget; they cannot starve other threads.
        rows = self.db.execute("SELECT * FROM thread_tasks WHERE status='pending' AND due_at<=? ORDER BY due_at,id", (current,))
        result = []
        for row in rows:
            task = dict(row)
            if self.ready(task, current):
                result.append(task)
                if len(result) >= limit:
                    break
        return result

    def fail(self, task, current, error, max_attempts):
        attempts = task["attempts"] + 1
        code = getattr(error, "code", type(error).__name__)
        with self.db:
            self.db.execute("UPDATE thread_tasks SET status=?,attempts=?,next_at=?,error_code=?,updated_at=? WHERE id=?",
                ("blocked" if attempts >= max_attempts or not getattr(error, "retryable", True) else "pending",
                 attempts, stamp(parse(current) + timedelta(seconds=min(300, 2 ** min(attempts, 8)))), code, current, task["id"]))
            self.db.execute("UPDATE threads SET sync_status=?,error_code=? WHERE id=?", ("blocked" if attempts >= max_attempts or not getattr(error, "retryable", True) else "pending", code, task["thread_id"]))
