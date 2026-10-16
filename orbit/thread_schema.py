"""Explicit, additive Notion schema installation with durable creation reconciliation."""

from dataclasses import replace

from .notion import Conflict, UncertainWrite, plain, rich
from .store import digest


STATUS_LABELS = {"awaiting_echo": "待Echo判断", "awaiting_world": "等待补充", "decided": "已有决定", "closed": "已结束"}
KIND_LABELS = {"external_event": "外部事件", "echo_decision": "Echo判断", "execution_result": "执行结果"}


def field(kind, options=()):
    return {"type": kind, kind: {"options": [{"name": x} for x in options]} if options else {}}


def relation(target):
    return {"type": "relation", "relation": {"data_source_id": target, "type": "single_property", "single_property": {}}}


def requirements(sources):
    thread_id = sources.get("threads")
    return {
        "threads": {
            "事项": field("title"), "Orbit事项ID": field("rich_text"),
            "处理进度": field("select", STATUS_LABELS.values()),
            "同步状态": field("select", ["待核对", "已核对", "技术阻塞"]),
            "原始事件": relation(sources["events"]), "最新事件": relation(sources["events"]),
            "相关计划": relation(sources["plans"]), "下次检查": field("date"),
            "来源与记录者": field("rich_text"), "最近更新": field("last_edited_time"),
        },
        "events": {"所属事项": relation(thread_id), "前序事件": relation(sources["events"]),
                   "节点类型": field("select", KIND_LABELS.values())},
        "plans": {"所属事项": relation(thread_id)},
    }


def changes(actual, expected):
    additions, errors = {}, []
    for name, spec in expected.items():
        previous = actual.get(name)
        if previous is None:
            additions[name] = spec
            continue
        kind = spec["type"]
        if previous["type"] != kind:
            errors.append({"property": name, "error": "type_mismatch", "expected": kind})
        elif kind == "relation" and (previous["relation"].get("data_source_id", "").replace("-", "") != (spec["relation"]["data_source_id"] or "").replace("-", "")):
            errors.append({"property": name, "error": "relation_target_mismatch"})
        elif kind == "select":
            old = previous[kind].get("options", [])
            missing = [v for v in spec[kind]["options"] if v["name"] not in {x["name"] for x in old}]
            if missing:
                additions[name] = {"type": kind, kind: {"options": old + missing}}
    return additions, errors


class ThreadSchema:
    def __init__(self, notion, store):
        self.notion, self.store = notion, store
        self.install_key = "thread_schema:" + digest([notion.config.pages["center"], notion.config.sources["events"]])[:20]

    def discovered_source(self):
        configured = self.notion.config.sources.get("threads")
        saved = self.store.get(self.install_key, {})
        return configured or saved.get("source_id")

    def use_source(self, source_id):
        self.notion.config = replace(self.notion.config, sources={**self.notion.config.sources, "threads": source_id})

    def plan(self):
        source_id = self.discovered_source()
        sources = {**self.notion.config.sources, **({"threads": source_id} if source_id else {})}
        expected = requirements(sources)
        result = {"creation_parent": self.notion.config.pages["center"], "threads_source_id": source_id,
                  "create_database": not bool(source_id), "sources": {}, "writes": False}
        if source_id:
            self.use_source(source_id)
        for name, props in expected.items():
            if name == "threads" and not source_id:
                result["sources"][name] = {"add": props, "errors": []}
                continue
            additions, errors = changes(self.notion.schema(name), props)
            result["sources"][name] = {"add": additions, "errors": errors}
        return result

    def check(self):
        if not self.notion.config.sources.get("threads"):
            raise Conflict("启用后续链须配置 sources.threads 并显式初始化 schema")
        result = self.plan()
        if any(p["add"] or p["errors"] for p in result["sources"].values()):
            raise Conflict("后续链 schema 缺失或不兼容；请先执行 threads-schema --plan")
        return result

    def apply(self):
        plan = self.plan()
        if any(p["errors"] for p in plan["sources"].values()):
            raise Conflict("同名属性类型或关系不兼容；保留既有结构")
        source_id = plan["threads_source_id"]
        if not source_id:
            marker = "Orbit事项结构:" + self.install_key
            # Scan only child databases of the configured parent, never a title-only global search.
            candidates = []
            for block in self.notion.blocks(plan["creation_parent"]):
                if block["type"] == "child_database":
                    db = self.notion.request("GET", "databases/" + block["id"])
                    if plain(db.get("description", [])) == marker:
                        candidates.append(db)
            if len(candidates) > 1:
                raise Conflict("初始化标识对应多个数据库")
            saved = self.store.get(self.install_key, {})
            if candidates:
                database = candidates[0]
            else:
                if saved.get("sent"):
                    raise UncertainWrite("建库回执不明且未查到标识；保留步骤，禁止重复创建")
                with self.store.db:
                    self.store.put(self.install_key, {"sent": True})
                try:
                    database = self.notion.request("POST", "databases", {
                        "parent": {"type": "page_id", "page_id": plan["creation_parent"]},
                        "title": rich("事项"), "description": rich(marker), "is_inline": True,
                        "initial_data_source": {"properties": requirements(self.notion.config.sources)["threads"]}})
                except Exception as exc:
                    if getattr(exc, "status", None) and 400 <= exc.status < 500 and exc.status not in {408, 409}:
                        with self.store.db:
                            self.store.put(self.install_key, {"sent": False})
                    raise
            if not database.get("data_sources"):
                database = self.notion.request("GET", "databases/" + database["id"])
            if len(database.get("data_sources", [])) != 1:
                raise Conflict("新事项库的数据源无法唯一核对")
            source_id = database["data_sources"][0]["id"]
            self.use_source(source_id)
            with self.store.db:
                self.store.put(self.install_key, {"database_id": database["id"], "source_id": source_id, "sent": True})
        # Each PATCH is additive and read back. An interrupted/lost PATCH is found by the next plan.
        plan = self.plan()
        for name, props in plan["sources"].items():
            if props["errors"]:
                raise Conflict("结构发生并发变化，未修改不兼容属性")
            if props["add"]:
                self.notion.request("PATCH", "data_sources/" + self.notion.config.sources[name], {"properties": props["add"]})
            add, errors = changes(self.notion.schema(name), requirements(self.notion.config.sources)[name])
            if add or errors:
                raise Conflict("新增 schema 尚未核对完整")
        return {"threads_source_id": source_id, "verified": True,
                "config": {'sources.threads': source_id},
                "view_setup": "在 Notion 事项库按处理进度分组，展示下次检查、同步状态、原始事件、相关计划；视图未由 API 创建。"}
