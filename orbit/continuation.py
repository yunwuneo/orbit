"""Serial continuation orchestration: verified causes, current decisions, durable effects."""

import json
from datetime import timedelta
from uuid import uuid4

from .engine import source_map, values
from .model import InvalidOutput, validate_continuation
from .notion import Conflict, compact_page, context_view, fingerprint, precise_page_date
from .store import digest, encode
from .thread_schema import KIND_LABELS, STATUS_LABELS, ThreadSchema
from .threads import Threads
from .timeutil import TIMEZONE, parse, stamp
from .transport import RemoteError


class ContinuationEngine:
    def __init__(self, engine):
        self.engine = engine
        self.config, self.store, self.notion = engine.config, engine.store, engine.notion
        self.repo, self.options = Threads(self.store), engine.config.continuation
        self.checked = False

    def run(self, current, evidence=None):
        generated_at = stamp(self.engine.clock())
        return {"id": uuid4().hex, "start": current, "end": generated_at, "generated_at": generated_at,
                "mode": "continuation", "evidence": evidence or {}}

    def check_schema(self):
        if not self.checked:
            ThreadSchema(self.notion, self.store).check()
            self.checked = True

    def receipt(self, write_id, remote_id, operation, current):
        # Capture the read-back fingerprint along with the receipt, not a later optimistic refresh.
        node_id = operation.get("node_id") or operation.get("refresh_node")
        if node_id:
            page = self.notion.page(remote_id)
            with self.store.db:
                self.store.db.execute("UPDATE thread_nodes SET authority_version=? WHERE id=?", (fingerprint(page), node_id))
        if operation.get("thread_plan"):
            page = self.notion.page(remote_id)
            thread = self.repo.get(operation["thread_plan"])
            due = precise_page_date(page, "计划时间") or parse(current)
            with self.store.db:
                self.repo.task(thread["id"], "plan_review", "plan:" + remote_id + ":" + fingerprint(page),
                               thread["revision"], stamp(due), current, [("write", write_id)])
        if operation.get("thread_projection"):
            page = self.notion.page(remote_id)
            managed = [b for b in page["blocks"] if b["text"].startswith("Orbit 事项进度\n")]
            if len(managed) != 1:
                raise Conflict("事项管理块回执缺失")
            with self.store.db:
                self.store.put("thread_snapshot:" + operation["thread_projection"], {
                    "id": managed[0]["id"], "hash": digest(managed[0]["text"]),
                    "values": {k: values(page).get(k) for k in operation["values"]}})

    def adopt(self, page_id, current):
        self.check_schema()
        page = self.notion.page(page_id)
        existing = self.store.db.execute("SELECT thread_id FROM thread_nodes WHERE notion_page_id=? OR source_page_id=?", (page_id, page_id)).fetchall()
        if existing:
            ids = {r[0] for r in existing}
            if len(ids) != 1:
                raise Conflict("事件存在多个事项关联")
            return next(iter(ids))
        if self.store.db.execute("SELECT 1 FROM consumed WHERE id=?", (page_id,)).fetchone():
            raise Conflict("事件已有旧版消费，无法恢复原决定；保留原消费，不重复判断")
        if values(page).get("Echo的行动决定") or values(page).get("Echo的理解与感受"):
            raise Conflict("事件含既有主观判断，须先恢复历史依据，不能再次回应")
        if values(page).get("状态") not in {"待处理", "待发生"}:
            raise Conflict("只能接纳尚未处理的事件")
        when = precise_page_date(page, "发生时间")
        if when and when > parse(current):
            raise Conflict("事件尚在未来，不能提前接纳")
        return self.ingest(page, current)

    def match_thread(self, page):
        props = values(page)
        explicit = props.get("所属事项") or []
        parents = props.get("前序事件") or []
        candidates = set()
        if explicit:
            for page_id in explicit:
                rows = self.store.db.execute("SELECT id FROM threads WHERE notion_page_id=?", (page_id,)).fetchall()
                candidates.update(row[0] for row in rows)
            if len(explicit) != 1 or len(candidates) != 1:
                raise Conflict("人工补充的事项关联未知或有歧义")
            return next(iter(candidates))
        for page_id in parents:
            rows = self.store.db.execute("SELECT DISTINCT thread_id FROM thread_nodes WHERE notion_page_id=?", (page_id,)).fetchall()
            candidates.update(row[0] for row in rows)
        if parents and len(candidates) != 1:
            raise Conflict("人工补充的前序事件不能唯一定位事项")
        if candidates:
            return next(iter(candidates))
        # An exact structured source URL may identify an event; never use titles or model guesses.
        link = props.get("来源链接") or ""
        if link.startswith(("https://www.notion.so/", "https://app.notion.com/p/")):
            page_id = link.split("?")[0].rstrip("/").split("/")[-1].replace("-", "")
            rows = self.store.db.execute("SELECT DISTINCT thread_id FROM thread_nodes WHERE replace(notion_page_id,'-','')=?", (page_id,)).fetchall()
            if len(rows) > 1:
                raise Conflict("结构化来源关联有歧义")
            if rows:
                return rows[0][0]
        return None

    def ingest(self, page, current, thread_id=None):
        props, version = values(page), fingerprint(page)
        page_id = page["id"]
        previous = self.store.db.execute("SELECT * FROM thread_nodes WHERE source_page_id=? ORDER BY revision DESC LIMIT 1", (page_id,)).fetchone()
        if previous and version in {previous["source_version"], previous["authority_version"]}:
            return previous["thread_id"]
        if self.store.db.execute("SELECT 1 FROM thread_nodes WHERE source_page_id=? AND source_version=?", (page_id, version)).fetchone():
            return previous["thread_id"]
        thread_id = thread_id or (previous["thread_id"] if previous else self.match_thread(page))
        occurred = precise_page_date(page, "发生时间")
        if occurred and occurred > parse(current):
            return thread_id
        parents = []
        if thread_id:
            thread = self.repo.get(thread_id)
            by_page = {n["notion_page_id"]: n for n in self.repo.nodes(thread_id, True)}
            parents = [(by_page[p]["id"], "supplements") for p in props.get("前序事件", []) if p in by_page]
            if not parents and thread["root_node_id"]:
                parents = [(thread["root_node_id"], "supplements")]
            if previous:
                parents.append((previous["id"], "supersedes"))
        run = self.run(current, {"sources": {page_id: version}, "input_version": version})
        with self.store.db:
            if not thread_id:
                thread_id = self.repo.create(current)
            thread = self.repo.get(thread_id)
            revision = self.repo.bump(thread_id, thread["revision"], current)
            node_id = self.repo.node(thread_id, revision, current, kind="external_event",
                actor=",".join(props.get("参与者") or []) or "未知", provider=(props.get("来源与记录者") or "Notion 人工输入")[:500],
                occurred_at=stamp(occurred or parse(current)), parents=parents, source_page_id=page_id,
                source_version=version, source_kind="human_input", information_nature=props.get("信息性质"),
                notion_page_id=page_id, verified_at=current, metadata={"occurred_at_unknown": occurred is None})
            self.store.db.execute("UPDATE thread_nodes SET authority_version=? WHERE id=?", (version, node_id))
            self.store.db.execute("UPDATE threads SET status='awaiting_echo',next_check_at=NULL,closed_by_node_id=NULL WHERE id=?", (thread_id,))
            if not thread["root_node_id"]:
                self.store.db.execute("UPDATE threads SET root_node_id=? WHERE id=?", (node_id, thread_id))
            operations = []
            if not thread["notion_page_id"]:
                create_id = thread["create_write_id"]
                if not create_id:
                    create_id = f"orbit:{run['id']}:0"
                    operations.append(self.create_thread(thread_id, page_id, props.get("事件") or "待处理事项", current))
                    self.store.db.execute("UPDATE threads SET create_write_id=? WHERE id=?", (create_id, thread_id))
                references = {"所属事项": create_id}
            else:
                references = {}
            relation_values = {"所属事项": [thread["notion_page_id"]]} if thread["notion_page_id"] else {}
            if parents and not props.get("前序事件"):
                known_pages = {n["id"]: n["notion_page_id"] for n in self.repo.nodes(thread_id, True)}
                relation_values["前序事件"] = list(dict.fromkeys(known_pages[parent] for parent, relation in parents
                    if parent in known_pages and known_pages[parent] != page_id and relation != "supersedes"))
            if not props.get("节点类型"):
                relation_values["节点类型"] = "外部事件"
            operations.append({"kind": "properties", "page_id": page_id, "source": "events", "expected": version,
                "values": relation_values, "references": references, "thread_id": thread_id, "refresh_node": node_id,
                "resources": ["thread:" + thread_id]})
            self.store.stage(run, operations)
            self.store.db.execute("INSERT INTO consumed VALUES (?,?) ON CONFLICT(id) DO UPDATE SET version=excluded.version", (page_id, version))
            self.repo.task(thread_id, "echo_decision", "input:" + page_id + ":" + version, revision, current, current,
                           [("node", node_id), ("write", f"orbit:{run['id']}:{len(operations)-1}"), ("source", page_id + ":" + version)])
        return thread_id

    @staticmethod
    def create_thread(thread_id, root_page_id, title, current):
        return {"kind": "record", "source": "threads", "thread_id": thread_id, "thread_create": thread_id,
                "resources": ["thread:" + thread_id], "values": {
                    "事项": title[:200], "Orbit事项ID": thread_id, "原始事件": [root_page_id],
                    "最新事件": [root_page_id], "处理进度": "待Echo判断", "同步状态": "待核对",
                    "来源与记录者": "事件来源：Notion；决策主体：Echo；执行者：Orbit"},
                "text": encode({"thread_id": thread_id, "created_at": current, "executor": "Orbit"})}

    def poll(self, current):
        rows = self.notion.rows("events", {"or": [
            {"property": "状态", "select": {"equals": "待处理"}},
            {"property": "状态", "select": {"equals": "待发生"}}]})
        if self.store.get("continuation_enabled_at") is None:
            # Historical unconsumed inputs are NOT implicitly adopted on upgrade.
            with self.store.db:
                self.store.put("continuation_enabled_at", current)
                self.store.put("continuation_baseline", {p["id"]: fingerprint(self.notion.page(p["id"])) for p in rows})
        baseline = self.store.get("continuation_baseline", {})
        pages = {}
        for row in rows:
            try:
                pages[row["id"]] = self.notion.page(row["id"])
            except (Conflict, RemoteError) as exc:
                for owner in self.store.db.execute("SELECT DISTINCT thread_id FROM thread_nodes WHERE notion_page_id=?", (row["id"],)):
                    self.block(owner[0], type(exc).__name__, current)
        # Re-read indexed authority even when a human cancels, deletes, or edits an old node.
        for thread in self.repo.list():
            if not thread["root_node_id"]:
                continue
            if thread["notion_page_id"]:
                for row in self.notion.rows("events", {"property": "所属事项", "relation": {"contains": thread["notion_page_id"]}}):
                    try:
                        pages[row["id"]] = self.notion.page(row["id"])
                    except (Conflict, RemoteError) as exc:
                        self.block(thread["id"], type(exc).__name__, current)
            for node in self.repo.nodes(thread["id"], True):
                try:
                    page = self.notion.page(node["notion_page_id"])
                    version = fingerprint(page)
                    if node["authority_version"] and version != node["authority_version"]:
                        if node["source_kind"] == "human_input" and values(page).get("状态") in {"待处理", "待发生", "已发生"}:
                            pages[page["id"]] = page
                        else:
                            raise Conflict("事项来源被修改或取消，停止依赖任务")
                except (Conflict, RemoteError) as exc:
                    self.block(thread["id"], type(exc).__name__, current)
        for page in pages.values():
            # A remote create whose receipt was lost remains unpublished technical output.
            if page["blocks"] and any(page["blocks"][0]["text"].startswith(row["id"] + ":") for row in self.store.pending()):
                continue
            version = fingerprint(page)
            if version == baseline.get(page["id"]):
                continue
            if any(n["authority_version"] == version or n["source_version"] == version for n in self.store.db.execute("SELECT authority_version,source_version FROM thread_nodes WHERE notion_page_id=?", (page["id"],))):
                continue
            if self.store.consumed(page["id"], version):
                continue
            known_generated = self.store.db.execute("SELECT 1 FROM thread_nodes WHERE notion_page_id=? AND source_kind='orbit_narrative'", (page["id"],)).fetchone()
            if known_generated:
                continue
            if values(page).get("状态") == "已取消":
                continue
            try:
                self.ingest(page, current)
            except (Conflict, RemoteError) as exc:
                with self.store.db:
                    self.store.put("continuation_input_error", {"page_id": page["id"], "error_code": type(exc).__name__})
        with self.store.db:
            self.store.put("next_input_poll", stamp(parse(current) + timedelta(seconds=self.options.input_poll_seconds)))

    def block(self, thread_id, code, current):
        with self.store.db:
            self.store.db.execute("UPDATE threads SET sync_status='blocked',error_code=? WHERE id=?", (code, thread_id))
            self.store.db.execute("UPDATE thread_tasks SET status='blocked',error_code=?,updated_at=? WHERE thread_id=? AND status='pending'", (code, current, thread_id))

    def assert_projection(self, thread_id, page):
        acknowledged = self.store.get("thread_snapshot:" + thread_id)
        if not acknowledged:
            return
        managed = [b for b in page["blocks"] if b["text"].startswith("Orbit 事项进度\n")]
        if len(managed) != 1 or managed[0]["id"] != acknowledged["id"] or digest(managed[0]["text"]) != acknowledged["hash"]:
            raise Conflict("已核对事项管理块被改写或删除，保留人工修改")
        if any(values(page).get(k) != v for k, v in acknowledged["values"].items()):
            raise Conflict("已核对事项属性被人工修改，未覆盖")

    def projection(self, thread, current):
        if not thread["notion_page_id"] or (thread["published_revision"] >= thread["revision"] and not thread["projection_dirty"]):
            return
        if self.store.db.execute("SELECT 1 FROM thread_nodes WHERE thread_id=? AND verified_at IS NULL", (thread["id"],)).fetchone():
            return
        # Do not snapshot a projection while earlier writes in this thread are unresolved.
        if self.store.db.execute("SELECT 1 FROM write_resources r JOIN outbox o ON r.write_id=o.id WHERE r.resource=? AND o.status!='done'", ("thread:" + thread["id"],)).fetchone():
            return
        page = self.notion.page(thread["notion_page_id"])
        nodes = self.repo.nodes(thread["id"], True)
        if not nodes:
            return
        latest = nodes[-1]
        root = next(n for n in nodes if n["id"] == thread["root_node_id"])
        plans = [r[0] for r in self.store.db.execute("SELECT DISTINCT plan_page_id FROM thread_plan_links WHERE thread_id=?", (thread["id"],))]
        desired = {"处理进度": STATUS_LABELS[thread["status"]], "同步状态": "已核对", "原始事件": [root["notion_page_id"]],
                   "最新事件": [latest["notion_page_id"]], "相关计划": plans, "下次检查": thread["next_check_at"]}
        managed = [b for b in page["blocks"] if b["text"].startswith("Orbit 事项进度\n")]
        if len(managed) > 1:
            raise Conflict("重复事项管理块")
        previous = {"id": managed[0]["id"], "hash": digest(managed[0]["text"])} if managed else None
        self.assert_projection(thread["id"], page)
        metadata = next((json.loads(n["metadata"]) for n in reversed(nodes) if n["kind"] == "echo_decision"), {})
        body = {"thread_id": thread["id"], "verified_revision": thread["revision"], "status": thread["status"],
                "latest_decision": metadata, "nodes": [{k: n[k] for k in ["id", "kind", "notion_page_id", "occurred_at", "generated_at", "verified_at", "source_version", "information_nature"]} for n in nodes],
                "next_check_at": thread["next_check_at"], "no_reply_count": thread["no_reply_count"],
                "review_cycle": thread["review_cycle"], "missed_checks": thread["missed_checks"],
                "check_notice": "检查时间不承诺回复或完成", "verified_at": current}
        operation = {"kind": "projection", "page_id": page["id"], "thread_id": thread["id"], "thread_projection": thread["id"],
                     "revision": thread["revision"], "values": desired, "previous_values": {k: values(page).get(k) for k in desired},
                     "projection_state": {k: thread[k] for k in ["revision", "status", "next_check_at", "no_reply_count", "review_cycle", "missed_checks"]},
                     "previous": previous, "text": encode(body), "resources": ["thread:" + thread["id"]]}
        with self.store.db:
            self.store.stage(self.run(current), [operation])

    def reconcile(self, current):
        # A missing receipt fingerprint cannot be reconstructed by trusting a possibly edited page.
        for node in self.store.db.execute("SELECT * FROM thread_nodes WHERE verified_at IS NOT NULL AND authority_version IS NULL"):
            self.block(node["thread_id"], "receipt_fingerprint_missing", current)
        for thread in self.repo.list():
            if thread["sync_status"] == "blocked":
                continue
            try:
                self.projection(thread, current)
            except (RemoteError, Conflict) as exc:
                self.block(thread["id"], type(exc).__name__, current)

    def context(self, task, current):
        thread = self.repo.get(task["thread_id"])
        nodes = self.repo.nodes(thread["id"], True)
        context = self.notion.thread_context(thread, nodes, current, current,
                                             self.engine.verified_context(current, current, check_budget=False))
        if thread["notion_page_id"]:
            thread_page = self.notion.page(thread["notion_page_id"])
            self.assert_projection(thread["id"], thread_page)
            context["pages"]["thread"] = thread_page
        # Only verified history enters a prompt; pending local life outputs are constraints, not facts.
        pending_ids = {r[0] for r in self.store.db.execute("SELECT notion_page_id FROM thread_nodes WHERE verified_at IS NULL")}
        context["events"] = [p for p in context["events"] if p["id"] not in pending_ids]
        plan_pages = {p["id"]: p for p in context["plans"]}
        for row in self.store.db.execute("SELECT DISTINCT plan_page_id FROM thread_plan_links WHERE thread_id=?", (thread["id"],)):
            plan_pages[row[0]] = self.notion.page(row[0])
        context["plans"] = list(plan_pages.values())
        before = source_map(context)
        causal = []
        last_decision = next((n["id"] for n in reversed(nodes) if n["kind"] == "echo_decision"), None)
        # Keep root, latest decision, latest input and all unresolved questions verbatim.
        latest_input = next((n["id"] for n in reversed(nodes) if n["kind"] == "external_event"), None)
        essential = {thread["root_node_id"], last_decision, latest_input}
        for node in nodes:
            meta = json.loads(node["metadata"])
            if meta.get("needed_information") and thread["status"] == "awaiting_world":
                essential.add(node["id"])
        by_page = {p["id"]: p for p in context["events"]}
        budget = self.config.max_context_chars
        for node in nodes:
            page = by_page[node["notion_page_id"]]
            causal.append({k: node[k] for k in ["id", "kind", "actor", "provider", "executor", "occurred_at", "generated_at", "information_nature", "source_version", "revision"]}
                          | {"page": compact_page(page), "metadata": json.loads(node["metadata"])})
        data = {"protocol_version": 2, "task_id": task["id"], "call_kind": task["kind"], "thread_id": thread["id"],
                "expected_revision": thread["revision"], "story_window": {"start": current, "end": current, "timezone": TIMEZONE},
                "generated_at": current, "causal_nodes": causal, "context": context_view(context),
                "source_versions": before,
                "no_reply_count": thread["no_reply_count"], "review_cycle": thread["review_cycle"],
                "task_cause": task["cause_key"], "capabilities": ["Notion 共同叙事记录"],
                "unknowns": [json.loads(n["metadata"]).get("needed_information", []) for n in nodes if n["id"] in essential],
                "constraints": "只生成当前时刻。已有计划不是已完成。旧状态不是停机后的现状。世界生成不得替Echo决定；不虚构Neo。"}
        if task["error_code"]:
            data["protocol_feedback"] = {"previous_error": task["error_code"], "thread_id": thread["id"],
                                         "expected_revision": thread["revision"],
                                         "instruction": "仅纠正协议错误，精确复制本次标识与修订；不改变Echo的自主选择来迎合校验。"}
        # Remove duplicate event bodies in the general context; causal pages retain the authority.
        causal_pages = {n["notion_page_id"] for n in nodes}
        data["context"]["events"] = [p for p in data["context"]["events"] if p["id"] not in causal_pages]
        if "thread" in data["context"]["pages"]:
            # The managed index duplicates causal_nodes and is technical bookkeeping, not new story evidence.
            data["context"]["pages"]["thread"]["content"] = "\n".join(
                b["text"] for b in context["pages"]["thread"]["blocks"] if not b["text"].startswith("Orbit 事项进度\n"))
        if len(encode(data)) > budget:
            for entry in causal:
                if entry["id"] in essential:
                    continue
                page = by_page[next(n["notion_page_id"] for n in nodes if n["id"] == entry["id"])]
                props = values(page)
                entry["page"] = {"id": page["id"], "version": fingerprint(page), "summary": props.get("一句话概要"),
                                 "decision": props.get("Echo的行动决定"), "information_nature": props.get("信息性质")}
                entry["compression"] = "authoritative_properties; re-read each call; not a new fact"
        if len(encode(data)) > budget:
            raise Conflict("必要事项依据超过上下文预算；未截断后判断")
        return thread, nodes, context, before, data

    def checked_time(self, current, follow):
        minimum = parse(current) + timedelta(seconds=self.options.min_check_seconds)
        proposed = follow and follow.get("suggested_check_at")
        if proposed and parse(proposed) >= minimum:
            return stamp(parse(proposed))
        return stamp(parse(current) + timedelta(seconds=self.options.default_check_seconds))

    def shared_pending(self):
        for row in self.store.pending():
            op = json.loads(row["payload"])
            if op["kind"] in {"snapshot", "plan_update"}:
                return True
            if op["kind"] == "record" and op.get("source") in {"memories", "plans"}:
                return True
            if op["kind"] == "append" and op["page_id"] not in {self.config.pages["operations"], self.config.pages["journal"]}:
                return True
        return False

    def no_event(self, task, thread, current, run):
        # No node, no experience, no revision change, and no diary content.
        with self.store.db:
            self.store.stage(run, [])
            self.finish(task, run["id"], current)
            if not thread["root_node_id"]:
                self.store.db.execute("UPDATE threads SET status='closed',sync_status='verified' WHERE id=?", (thread["id"],))
                return
            count = thread["no_reply_count"] + 1
            cycle = thread["review_cycle"]
            if count >= self.options.no_reply_review_after:
                kind, due = "echo_decision", current
                cause = f"no_reply_review:{thread['revision']}:{cycle}"
            else:
                kind, due = "world_check", self.checked_time(current, None)
                cause = f"no_reply:{thread['revision']}:{cycle}:{count}"
            self.store.db.execute("UPDATE threads SET no_reply_count=?,next_check_at=?,projection_dirty=1 WHERE id=?", (count, due, thread["id"]))
            self.repo.task(thread["id"], kind, cause, thread["revision"], due, current)

    def finish(self, task, run_id, current):
        self.store.db.execute("UPDATE thread_tasks SET status='done',output_run_id=?,updated_at=?,error_code=NULL WHERE id=?", (run_id, current, task["id"]))

    def execute(self, task, current):
        # Source reads and previous tasks may have taken long enough for the tick's
        # original timestamp to become stale. Each decision gets a fresh story time.
        current = stamp(self.engine.clock())
        if self.shared_pending() or not self.repo.ready(task, current):
            return
        with self.store.db:
            claimed = self.store.db.execute("UPDATE thread_tasks SET status='running',updated_at=? WHERE id=? AND status='pending' AND output_run_id IS NULL", (current, task["id"]))
            if claimed.rowcount != 1:
                return
        thread, nodes, context, before, data = self.context(task, current)
        if task["kind"] == "plan_review":
            plan_id = task["cause_key"].split(":")[1]
            page = next((p for p in context["plans"] if p["id"] == plan_id), None)
            if not page or values(page).get("状态") in {"已完成", "已取消"}:
                with self.store.db:
                    self.store.db.execute("UPDATE thread_tasks SET status='superseded' WHERE id=?", (task["id"],))
                return
            scheduled = precise_page_date(page, "计划时间")
            if scheduled and scheduled > parse(current):
                with self.store.db:
                    self.store.db.execute("UPDATE thread_tasks SET due_at=?,status='pending' WHERE id=?", (stamp(scheduled), task["id"]))
                return
        output = validate_continuation(self.engine.model.continue_thread(data), data, context)
        generated_at = stamp(self.engine.clock())
        self.notion.assert_sources(before)
        if self.repo.get(thread["id"])["revision"] != thread["revision"]:
            raise Conflict("模型期间事项修订变化")
        run = self.run(current, {"sources": before, "output": digest(output), "task_id": task["id"], "call_kind": task["kind"]})
        run["generated_at"] = generated_at
        run["end"] = generated_at
        current = generated_at
        if task["kind"] == "world_check" and output["outcome"] == "no_event":
            self.no_event(task, thread, current, run)
            return
        self.commit(task, thread, nodes, context, before, data, output, run, current)

    def commit(self, task, thread, nodes, context, before, data, output, run, current):
        world = task["kind"] == "world_check"
        generated_at = run["generated_at"]
        events = [output["event"]] if world else [output["decision"]] + [r["node"] for r in output["execution_results"]]
        operations, event_ids, write_ids = [], {}, {}
        thread_id = thread["id"]
        with self.store.db:
            revision = self.repo.bump(thread_id, thread["revision"], current)
            for event in events:
                write_id = f"orbit:{run['id']}:{len(operations)}"
                parents = [(event_ids.get(p, p), "results_from" if event["kind"] == "execution_result" else "responds_to" if event["kind"] == "echo_decision" else "supplements") for p in event["parent_node_ids"]]
                metadata = {} if world or event["kind"] == "execution_result" else {k: output[k] for k in ["decision_kind", "needed_information", "question_asked", "follow_up", "thread_disposition"]}
                node_id = self.repo.node(thread_id, revision, generated_at, kind=event["kind"], actor=event["actor"],
                    provider=event["provider"], occurred_at=event["time"], parents=parents, write_id=write_id, metadata=metadata)
                event_ids[event["id"]] = node_id
                write_ids[event["id"]] = write_id
                parent_pages = [next(n["notion_page_id"] for n in nodes if n["id"] == p) for p in event["parent_node_ids"] if p not in event_ids]
                refs = {"前序事件": [write_ids[p] for p in event["parent_node_ids"] if p in write_ids]}
                if parent_pages:
                    # Mixed existing/new parents are resolved by explicit IDs plus write references.
                    refs = {"前序事件": refs["前序事件"]} if refs["前序事件"] else {}
                else:
                    refs = refs if refs["前序事件"] else {}
                vals = {"事件": event["title"], "一句话概要": event["external"][:1500], "发生时间": event["time"],
                    "信息性质": "共同叙事", "状态": "已发生", "参与者": event["participants"], "地点": event["location"],
                    "Echo的理解与感受": "\n".join(x for x in [event["interpretation"], event["feeling"]] if x) or None,
                    "Echo的行动决定": event["decision"], "后续影响": event["intention"], "节点类型": KIND_LABELS[event["kind"]],
                    "前序事件": parent_pages, "所属事项": [thread["notion_page_id"]] if thread["notion_page_id"] else [],
                    "来源与记录者": f"事件提供者：{event['provider']}；决策主体：{'Echo' if not world else '无'}；执行者：Orbit"}
                if not thread["notion_page_id"]:
                    vals.pop("所属事项")
                operations.append({"kind": "record", "source": "events", "values": vals, "references": refs,
                    "thread_id": thread_id, "node_id": node_id, "guards": before, "resources": ["thread:" + thread_id],
                    "text": encode({**event, "node_id": node_id, "thread_id": thread_id, "generated_at": generated_at,
                                    "executor": "Orbit", "mode": "live", "information_nature": "共同叙事", **metadata})})
            root_node = thread["root_node_id"]
            if not root_node:
                root_node = event_ids[events[0]["id"]]
                self.store.db.execute("UPDATE threads SET root_node_id=? WHERE id=?", (root_node, thread_id))
                root_write = write_ids[events[0]["id"]]
                create_id = f"orbit:{run['id']}:{len(operations)}"
                operation = self.create_thread(thread_id, None, events[0]["title"], current)
                operation["values"].pop("原始事件")
                operation["values"].pop("最新事件")
                operation["references"] = {"原始事件": root_write, "最新事件": root_write}
                operations.append(operation)
                self.store.db.execute("UPDATE threads SET create_write_id=? WHERE id=?", (create_id, thread_id))
                # Relationship backfill is staged after root and minimal thread receipts by reconcile_root.
            if world:
                status, next_at = "awaiting_echo", None
                self.repo.task(thread_id, "echo_decision", "node:" + event_ids[events[0]["id"]], revision, current, current,
                               [("write", write_ids[events[0]["id"]])])
            else:
                # Reuse the legacy operation builder for protected state, plans and memory, then add causal barriers.
                legacy = {"events": [{k: v for k, v in event.items() if k not in {"kind", "actor", "parent_node_ids"}} for event in events],
                          "state": output["state"], "plans": output["plans"], "plan_updates": output["plan_updates"],
                          "memories": output["memories"], "proposals": output["proposals"], "conflicts": []}
                for event in legacy["events"]:
                    for name in ["interpretation", "feeling", "decision"]:
                        event[name] = event[name] or ""
                effects = self.engine.operations(legacy, {**run, "mode": "live"}, context)[len(events):]
                for op in effects:
                    if op["kind"] == "runtime":
                        continue
                    op["thread_id"] = thread_id
                    op["resources"] = list(set(op.get("resources", [])) | {"thread:" + thread_id})
                    op["depends_on"] = list(write_ids.values())
                    if op["kind"] == "record" and op["source"] == "plans":
                        op["values"]["所属事项"] = [thread["notion_page_id"]]
                        op["thread_plan"] = thread_id
                        op["decision_node_id"] = event_ids[output["decision"]["id"]]
                    operations.append(op)
                waiting = output["thread_disposition"] == "等待补充"
                status = "awaiting_world" if waiting else "decided"
                linked_ids = {r[0] for r in self.store.db.execute("SELECT DISTINCT plan_page_id FROM thread_plan_links WHERE thread_id=?", (thread_id,))}
                existing_plans = [p for p in context["plans"] if p["id"] in linked_ids]
                updates = {u["plan_id"]: u["status"] for u in output["plan_updates"]}
                open_plans = any(updates.get(p["id"], values(p).get("状态")) not in {"已完成", "已取消"} for p in existing_plans)
                if output["thread_disposition"] == "请求结束" and not output["plans"] and not open_plans:
                    status = "closed"
                elif output["thread_disposition"] == "请求结束":
                    metadata = {**json.loads(self.store.db.execute("SELECT metadata FROM thread_nodes WHERE id=?", (event_ids[output["decision"]["id"]],)).fetchone()[0]),
                                "runtime_constraint": "仍有关联的未完成计划；事项暂保留已有决定，计划到期交回Echo判断"}
                    self.store.db.execute("UPDATE thread_nodes SET metadata=? WHERE id=?", (encode(metadata), event_ids[output["decision"]["id"]]))
                follow = output["follow_up"]
                next_at = self.checked_time(generated_at, follow) if waiting or follow is not None else None
                self.store.db.execute("UPDATE threads SET no_reply_count=0,review_cycle=review_cycle+1,closed_by_node_id=? WHERE id=?",
                    (event_ids[output["decision"]["id"]] if status == "closed" else None, thread_id))
                if next_at and status != "closed":
                    kind = "world_check" if waiting and (follow is None or follow["kind"] == "wait_information") else "echo_decision"
                    self.repo.task(thread_id, kind, "decision:" + event_ids[output["decision"]["id"]], revision, next_at, current,
                                   [("write", w) for w in write_ids.values()])
                # An unresolved due plan remains reviewable after a no-action/defer decision.
                if task["kind"] == "plan_review" and open_plans and status != "closed":
                    plan_id = task["cause_key"].split(":")[1]
                    page = next(p for p in context["plans"] if p["id"] == plan_id)
                    if updates.get(plan_id, values(page).get("状态")) not in {"已完成", "已取消"}:
                        due = next_at or self.checked_time(current, None)
                        self.repo.task(thread_id, "plan_review", "plan:" + plan_id + ":decision:" + event_ids[output["decision"]["id"]],
                                       revision, due, current, [("write", w) for w in write_ids.values()])
            self.store.db.execute("UPDATE threads SET status=?,next_check_at=?,sync_status='pending' WHERE id=?", (status, next_at, thread_id))
            self.store.stage(run, operations)
            self.finish(task, run["id"], current)
            # Rebind still-open plan review tasks to the newly committed revision, never mark completed by time.
            self.store.db.execute("UPDATE thread_tasks SET expected_revision=?,status='pending' WHERE thread_id=? AND kind='plan_review' AND status='superseded' AND error_code IS NULL AND output_run_id IS NULL", (revision, thread_id))
            if not world:
                for page in existing_plans:
                    plan_id = page["id"]
                    if updates.get(plan_id, values(page).get("状态")) in {"已完成", "已取消"}:
                        continue
                    if not self.store.db.execute("SELECT 1 FROM thread_tasks WHERE thread_id=? AND kind='plan_review' AND cause_key LIKE ? AND status IN ('pending','running','blocked')", (thread_id, "plan:" + plan_id + ":%" )).fetchone():
                        scheduled = precise_page_date(page, "计划时间")
                        due = stamp(scheduled) if scheduled and scheduled > parse(current) else self.checked_time(current, output["follow_up"])
                        self.repo.task(thread_id, "plan_review", "plan:" + plan_id + ":decision:" + event_ids[output["decision"]["id"]], revision, due, current,
                                       [("write", w) for w in write_ids.values()])

    def reconcile_root(self, current):
        for thread in self.repo.list():
            if not thread["notion_page_id"] or not thread["root_node_id"]:
                continue
            node = next(n for n in self.repo.nodes(thread["id"]) if n["id"] == thread["root_node_id"])
            if not node["verified_at"] or node["source_kind"] != "orbit_narrative":
                continue
            if self.store.get("root_link:" + thread["id"]):
                continue
            page = self.notion.page(node["notion_page_id"])
            op = {"kind": "properties", "source": "events", "page_id": page["id"], "expected": fingerprint(page),
                  "values": {"所属事项": [thread["notion_page_id"]]}, "refresh_node": node["id"],
                  "thread_id": thread["id"], "resources": ["thread:" + thread["id"]]}
            with self.store.db:
                self.store.stage(self.run(current), [op])
                self.store.put("root_link:" + thread["id"], True)

    def tick(self, current):
        self.check_schema()
        self.engine.flush()
        self.reconcile_root(current)
        self.reconcile(current)
        self.engine.flush()
        if self.store.get("paused", False):
            return "paused"
        if self.store.db.execute("SELECT 1 FROM backfills WHERE status IN ('pending','running','blocked')").fetchone():
            return "backfill_pending"
        cursor = self.store.get("cursor")
        if cursor is None:
            self.store.recover(current)
        elif parse(cursor) > parse(current):
            raise ValueError("时钟早于持久游标")
        if not self.store.get("next_input_poll") or self.store.get("next_input_poll") <= current:
            self.poll(current)
        self.engine.flush()
        self.reconcile(current)
        self.engine.flush()
        # Fairness: task budget applies to ready work only; normal life has its own clock.
        processed = set()
        for _ in range(self.options.max_tasks_per_tick):
            if self.shared_pending():
                break
            ready = [t for t in self.repo.due(current, self.options.max_tasks_per_tick) if t["id"] not in processed]
            if not ready:
                break
            task = ready[0]
            processed.add(task["id"])
            try:
                self.execute(task, current)
            except (RemoteError, Conflict, InvalidOutput) as exc:
                self.repo.fail(task, current, exc, self.config.max_attempts)
            self.engine.flush()
            self.reconcile_root(current)
            self.reconcile(current)
            self.engine.flush()
        last_life = self.store.get("continuation_life_at")
        life_due = not last_life or parse(current) >= parse(last_life) + timedelta(seconds=self.config.interval_seconds)
        if life_due:
            with self.store.db:
                self.store.put("continuation_life_at", current)
                # New world checks may introduce an objective root; no_event falls back to ordinary life.
                thread_id = self.repo.create(current)
                self.repo.task(thread_id, "world_check", "root", 0, current, current)
            task = self.repo.due(current, self.options.max_tasks_per_tick)
            root_task = next((t for t in task if t["thread_id"] == thread_id), None)
            if root_task and len(processed) < self.options.max_tasks_per_tick:
                try:
                    self.execute(root_task, current)
                except (RemoteError, Conflict, InvalidOutput) as exc:
                    self.repo.fail(root_task, current, exc, self.config.max_attempts)
            self.engine.flush()
            self.reconcile_root(current)
            self.reconcile(current)
            self.engine.flush()
            # Normal life only waits on shared global writes, not a failing independent thread.
            shared = self.shared_pending()
            if not shared and self.engine.generation_ready(current):
                start = max(parse(self.store.get("cursor")), parse(current) - timedelta(seconds=self.config.window_seconds))
                with self.store.db:
                    self.store.gap(self.store.get("cursor"), stamp(start), "realtime_window_exceeded")
                try:
                    self.engine.generate(stamp(start), current, "live")
                except (RemoteError, Conflict, InvalidOutput) as exc:
                    self.engine.failure(current, exc)
            self.engine.flush()
        self.store.prune(current, self.config.retention_days)
        with self.store.db:
            self.store.put("last_continuation_check", current)
        overview = {"continuation": self.store.continuation_status(),
                    "errors": [{"thread_id": t["id"], "error_code": t["error_code"], "sync_status": t["sync_status"]}
                               for t in self.repo.list() if t["error_code"]],
                    "input_error": self.store.get("continuation_input_error")}
        signature = digest(overview)
        if signature != self.store.get("continuation_overview"):
            self.store.operational(self.config.pages["operations"], current, {"at": current, **overview})
            with self.store.db:
                self.store.put("continuation_overview", signature)
            self.engine.flush()
        return "continuation_processed" if processed or life_due else "continuation_waiting"
