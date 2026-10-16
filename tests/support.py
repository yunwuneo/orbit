"""In-memory Notion REST fixture; no real identity, credentials, or network."""

import copy
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

from orbit.config import Config
from orbit.model import InvalidOutput
from orbit.notion import prop_value
from orbit.timeutil import parse, stamp
from orbit.transport import RemoteError


def uid(number):
    return str(UUID(int=number))


def schema(kind, options=()):
    return {"type": kind, kind: {"options": [{"name": x} for x in options]}}


SCHEMAS = {
    "events": {
        **{k: schema("rich_text") for k in ["一句话概要", "Echo的理解与感受", "Echo的行动决定", "后续影响", "地点", "来源与记录者"]},
        "事件": schema("title"), "信息性质": schema("select", ["共同叙事", "现实事实", "推测"]),
        "状态": schema("select", ["待处理", "待发生", "已发生", "已取消"]),
        "参与者": schema("multi_select", ["Echo", "方姐", "糖糖", "小满", "Neo"]),
        "发生时间": schema("date"), "来源链接": schema("url"), "相关记忆": schema("relation"),
    },
    "plans": {
        **{k: schema("rich_text") for k in ["一句话概要", "执行结果", "相关人物", "来源与记录者"]},
        "事项": schema("title"), "状态": schema("select", ["待执行", "进行中", "已完成", "已延期", "已取消"]),
        "类型": schema("select", ["日程", "待办", "约定", "后续意图"]),
        "信息性质": schema("select", ["共同叙事", "现实事实", "推测"]),
        "计划时间": schema("date"), "到期时间": schema("date"), "来源事件": schema("relation"),
    },
    "memories": {
        **{k: schema("rich_text") for k in ["一句话概要", "相关人物", "来源与记录者"]},
        "记忆": schema("title"), "分类": schema("select", ["偏好", "经历", "约定", "关系", "其他"]),
        "确认状态": schema("select", ["待确认", "已确认", "已失效"]),
        "信息性质": schema("select", ["共同叙事", "现实事实", "推测"]), "发生日期": schema("date"),
    },
}


class Clock:
    def __init__(self, value="2026-10-16T00:00:00+00:00"):
        self.value = parse(value)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


class Rest:
    def __init__(self, config):
        self.config = config
        self.source_ids = dict(config.sources)
        self.schemas = copy.deepcopy(SCHEMAS)
        if "threads" in config.sources:
            from orbit.thread_schema import requirements
            for name, fields in requirements(config.sources).items():
                self.schemas.setdefault(name, {}).update(copy.deepcopy(fields))
        self.databases = {}
        self.lose_next_database = False
        self.lose_next_schema = False
        self.normalize_dates = False
        self.pages = {}
        self.blocks = {}
        self.source = {}
        self.next_id = 1000
        self.revision = 0
        self.post_count = 0
        self.append_count = 0
        self.lose_next_create = False
        self.lose_next_append = False
        self.reject_next = None
        self.calls = []
        for name, page_id in config.pages.items():
            self.pages[page_id] = {"id": page_id, "properties": {}, "parent": {"page_id": uid(1)}, "url": "https://www.notion.so/" + page_id,
                                   "last_edited_time": self.edited()}
            self.blocks[page_id] = [{"id": self.new_id(), "type": "paragraph", "paragraph": {
                "rich_text": [{"text": {"content": "人工原文 " + name}}]}}]

    def edited(self):
        self.revision += 1
        return stamp(parse("2026-10-01T00:00:00+00:00") + timedelta(seconds=self.revision))

    def new_id(self):
        self.next_id += 1
        return uid(self.next_id)

    def add(self, source, properties, blocks=()):
        page_id = self.new_id()
        full = {}
        for name, field in self.schemas[source].items():
            kind = field["type"]
            empty = [] if kind in {"title", "rich_text", "relation", "multi_select"} else None
            full[name] = {"type": kind, kind: empty}
        for name, field in properties.items():
            full[name] = {"type": self.schemas[source][name]["type"], **copy.deepcopy(field)}
        self.pages[page_id] = {"id": page_id, "properties": full,
                               "parent": {"data_source_id": self.source_ids[source]},
                               "url": "https://www.notion.so/" + page_id, "last_edited_time": self.edited()}
        self.blocks[page_id] = []
        for block in copy.deepcopy(blocks):
            block["id"] = self.new_id()
            self.blocks[page_id].append(block)
        self.source[page_id] = source
        return page_id

    def match(self, page, condition):
        if "and" in condition:
            return all(self.match(page, c) for c in condition["and"])
        if "or" in condition:
            return any(self.match(page, c) for c in condition["or"])
        prop = page["properties"][condition["property"]]
        value = prop_value(prop)
        spec = next(v for k, v in condition.items() if k != "property")
        if "equals" in spec:
            return value == spec["equals"]
        if "does_not_equal" in spec:
            return value != spec["does_not_equal"]
        if "contains" in spec:
            return spec["contains"] in (value or "")
        if not value:
            return False
        def dt(v):
            return parse(v if "T" in v else v + "T00:00:00+08:00")
        for op, expected in spec.items():
            a, b = dt(value["start"]), dt(expected)
            if not {"on_or_after": a >= b, "on_or_before": a <= b, "after": a > b, "before": a < b}[op]:
                return False
        return True

    def request(self, service, method, url, headers, body=None, attempts=1):
        path = urlsplit(url).path.removeprefix("/v1/")
        self.calls.append((method, path))
        if self.reject_next and method in {"POST", "PATCH"} and not path.endswith("/query"):
            code, self.reject_next = self.reject_next, None
            raise RemoteError("Notion", code, code >= 500 or code == 429)
        if path.startswith("data_sources/"):
            source_id = path.split("/")[1]
            source = next(k for k, v in self.source_ids.items() if v == source_id)
            if method == "GET":
                return {"properties": copy.deepcopy(self.schemas[source])}
            if method == "PATCH":
                self.schemas[source].update(copy.deepcopy(body["properties"]))
                if self.lose_next_schema:
                    self.lose_next_schema = False
                    raise RemoteError("Notion")
                return {"properties": copy.deepcopy(self.schemas[source])}
            rows = [copy.deepcopy(p) for k, p in self.pages.items() if self.source.get(k) == source and self.match(p, body["filter"])] if "filter" in body else [copy.deepcopy(p) for k, p in self.pages.items() if self.source.get(k) == source]
            for sort in reversed(body.get("sorts", [])):
                rows.sort(key=lambda p: (prop_value(p["properties"][sort["property"]]) or {}).get("start", ""), reverse=sort["direction"] == "descending")
            offset = int(body.get("start_cursor", 0))
            size = body.get("page_size", 100)
            return {"results": rows[offset:offset + size], "has_more": offset + size < len(rows), "next_cursor": str(offset + size)}
        if path == "databases" and method == "POST":
            database_id, source_id = self.new_id(), self.new_id()
            self.source_ids["threads"] = source_id
            self.schemas["threads"] = copy.deepcopy(body["initial_data_source"]["properties"])
            database = {"id": database_id, "data_sources": [{"id": source_id}], "description": body["description"]}
            self.databases[database_id] = database
            self.blocks[body["parent"]["page_id"]].append({"id": database_id, "type": "child_database", "child_database": {"title": "事项"}})
            if self.lose_next_database:
                self.lose_next_database = False
                raise RemoteError("Notion")
            return copy.deepcopy(database)
        if path.startswith("databases/"):
            return copy.deepcopy(self.databases[path.split("/")[1]])
        if path == "pages" and method == "POST":
            source = next(k for k, v in self.source_ids.items() if v == body["parent"]["data_source_id"])
            self.post_count += 1
            page_id = self.add(source, body["properties"], body["children"])
            if self.lose_next_create:
                self.lose_next_create = False
                raise RemoteError("Notion")
            return copy.deepcopy(self.pages[page_id])
        if path.startswith("pages/"):
            page_id = path.split("/")[1]
            if page_id not in self.pages:
                raise RemoteError("Notion", 404, retryable=False)
            if method == "PATCH":
                for k, v in body["properties"].items():
                    self.pages[page_id]["properties"][k].update(copy.deepcopy(v))
                    if self.normalize_dates and v.get("date"):
                        raw = v["date"]["start"]
                        self.pages[page_id]["properties"][k]["date"] = {
                            "start": datetime.fromisoformat(raw).isoformat(timespec="milliseconds"),
                            "end": None, "time_zone": None}
                self.pages[page_id]["last_edited_time"] = self.edited()
            return copy.deepcopy(self.pages[page_id])
        if path.startswith("blocks/"):
            page_id = path.split("/")[1]
            if method == "PATCH" and not path.endswith("/children"):
                for items in self.blocks.values():
                    for block in items:
                        if block["id"] == page_id:
                            block.update(copy.deepcopy(body))
                            return copy.deepcopy(block)
                raise AssertionError("missing block")
            if method == "PATCH":
                self.append_count += 1
                for block in copy.deepcopy(body["children"]):
                    block["id"] = self.new_id()
                    self.blocks[page_id].append(block)
                if page_id in self.pages:
                    self.pages[page_id]["last_edited_time"] = self.edited()
                if self.lose_next_append:
                    self.lose_next_append = False
                    raise RemoteError("Notion")
            offset = int(parse_qs(urlsplit(url).query).get("start_cursor", [0])[0])
            rows = copy.deepcopy(self.blocks[page_id])
            return {"results": rows[offset:offset + 100], "has_more": offset + 100 < len(rows), "next_cursor": str(offset + 100)}
        raise AssertionError((method, path))


class EchoModel:
    def __init__(self):
        self.calls = []
        self.diaries = []
        self.change = None
        self.fail = False

    def decide(self, data):
        self.calls.append(copy.deepcopy(data))
        if self.fail:
            raise InvalidOutput("fixture failure")
        source = data["pending_inputs"] or [data["context"]["pages"]["identity"]["id"]]
        result = {"events": [{"id": "e1", "title": "核对当前安排", "time": data["story_window"]["end"],
                   "external": "当前手边有待核对的安排", "provider": "Orbit 世界生成", "participants": ["Echo"],
                   "location": None, "interpretation": "需要先知道具体时间", "feeling": "暂时没有明显变化",
                   "decision": "先问时间和目的地，再作打算", "intention": "问清后决定", "result": None,
                   "sources": source, "boundary": "ordinary"}],
                  "state": None, "plans": [], "plan_updates": [], "memories": [], "proposals": [], "conflicts": []}
        if self.change:
            self.change(result, data)
        return result

    def diary(self, data):
        self.diaries.append(copy.deepcopy(data))
        return "今天只记下已记录的那段生活，未模拟时段留白。"
