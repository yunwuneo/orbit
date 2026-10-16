"""Notion REST adapter; schema-discovered properties and verified incremental writes."""

from itertools import islice
from datetime import datetime, timedelta
import json
from urllib.parse import urlencode

from .store import digest, encode
from .timeutil import parse
from .transport import RemoteError, Transport


class Conflict(RuntimeError):
    retryable = False


class UncertainWrite(RuntimeError):
    retryable = True


def rich(text):
    return [{"type": "text", "text": {"content": text[i:i + 1800]}}
            for i in range(0, len(text), 1800)]


def plain(value):
    if isinstance(value, list):
        return "".join(x.get("plain_text", x.get("text", {}).get("content", "")) for x in value)
    return ""


def prop_value(prop):
    kind = prop["type"]
    value = prop.get(kind)
    if kind in {"title", "rich_text"}:
        return plain(value)
    if kind in {"select", "status"}:
        return value.get("name") if value else None
    if kind == "multi_select":
        return [x["name"] for x in value]
    if kind == "relation":
        return [x["id"] for x in value]
    return value


def property_equal(actual, expected):
    """Notion normalizes dates and adds null metadata; compare their instants."""
    if isinstance(actual, dict) and isinstance(expected, dict) and "start" in actual and "start" in expected:
        def normalized(value):
            if value is None:
                return None
            return parse(value if "T" in value else value + "T00:00:00+08:00")
        try:
            return all(normalized(actual.get(k)) == normalized(expected.get(k)) for k in ["start", "end"])
        except (ValueError, TypeError, AttributeError):
            return False
    return actual == expected


def fingerprint(page):
    return digest({"properties": page.get("properties"), "blocks": page.get("blocks", []),
                   "edited": page.get("last_edited_time"), "archived": page.get("archived", False)})


def date_index(value):
    """Notion date properties are minute indexes; precise times remain in record bodies."""
    if "T" not in value:
        return value
    return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(second=0, microsecond=0).isoformat()


def precise_page_date(page, name):
    date = prop_value(page["properties"][name]) if name in page["properties"] else None
    if not date or not date.get("start"):
        return None
    raw = date["start"]
    indexed = parse(raw if "T" in raw else raw + "T00:00:00+08:00")
    blocks = page.get("blocks", [])
    if name in {"发生时间", "计划时间", "到期时间"} and len(blocks) >= 2 and blocks[0]["text"].startswith("orbit:"):
        try:
            body = json.loads(blocks[1]["text"])
            candidate = body.get("due" if name == "到期时间" else "time")
            # A manual date edit wins when it no longer matches the body's date index.
            if body.get("executor") == "Orbit" and candidate and parse(date_index(candidate)) == indexed:
                return parse(candidate)
        except (ValueError, TypeError, AttributeError):
            pass
    return indexed


def compact_page(page):
    return {"id": page["id"], "url": page.get("url"), "edited_at": page.get("last_edited_time"),
            "properties": {k: prop_value(v) for k, v in page["properties"].items()},
            "content": "\n".join(b["text"] for b in page["blocks"])}


def context_view(context):
    return {"pages": {k: compact_page(v) for k, v in context["pages"].items()},
            **{name: [compact_page(p) for p in context[name]] for name in ["memories", "events", "plans", "diaries"]},
            "mode": context["mode"]}


def paragraph(text):
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": rich(text)}}


SNAPSHOT_PREFIX = "Orbit 当前生活快照\n"


class Notion:
    def __init__(self, config, transport=None):
        self.config = config
        self.http = transport or Transport(config.timeout_seconds)
        self.schemas = {}

    def request(self, method, path, body=None):
        # Mutations get exactly one HTTP attempt; the durable outbox reconciles before retry.
        return self.http.request("Notion", method, "https://api.notion.com/v1/" + path,
                                 {"Authorization": "Bearer " + self.config.notion_token,
                                  "Notion-Version": "2025-09-03"}, body,
                                 attempts=3 if method == "GET" or path.endswith("/query") else 1)

    def listing(self, path, body=None):
        cursor = None
        count = 0
        while True:
            if body is None:
                query = urlencode({"page_size": 100, **({"start_cursor": cursor} if cursor else {})})
                data = self.request("GET", path + "?" + query)
            else:
                data = self.request("POST", path, {**body, "page_size": 100,
                                                   **({"start_cursor": cursor} if cursor else {})})
            if data.get("request_status", {}).get("type") == "incomplete" or data.get("request_status", {}).get("incomplete_reason"):
                raise Conflict("Notion 查询结果不完整，未据此生成或核对")
            for item in data["results"]:
                count += 1
                if count > self.config.max_source_items:
                    raise Conflict("Notion 读取达到配置上限；未使用截断资料生成生活")
                yield item
            if not data.get("has_more"):
                return
            cursor = data.get("next_cursor")
            if not cursor:
                raise Conflict("Notion 分页不完整")

    def blocks(self, page_id, depth=0):
        if depth > 12:
            raise Conflict("Notion 嵌套过深，资料未完整读取")
        blocks = []
        for item in self.listing(f"blocks/{page_id}/children"):
            kind = item["type"]
            if kind in {"unsupported", "synced_block"}:
                raise Conflict("存在未支持的 Notion 块；请提供可读取的普通块")
            text = plain(item.get(kind, {}).get("rich_text", []))
            if kind == "table_row":
                text = " | ".join(plain(x) for x in item[kind]["cells"])
            if kind in {"child_page", "child_database"}:
                text = item[kind].get("title", "")
            blocks.append({"id": item["id"], "type": kind, "text": text})
            if item.get("has_children") and kind not in {"child_page", "child_database"}:
                blocks.extend(self.blocks(item["id"], depth + 1))
        if len(encode(blocks)) > self.config.max_context_chars:
            raise Conflict("Notion 单页超过上下文上限")
        return blocks

    def page(self, page_id):
        page = self.request("GET", f"pages/{page_id}")
        if page.get("archived") or page.get("in_trash"):
            raise Conflict("Notion 来源已归档或删除")
        return {"id": page["id"], "url": page.get("url"),
                "parent": page.get("parent"),
                "properties": page["properties"], "last_edited_time": page["last_edited_time"],
                "blocks": self.blocks(page_id)}

    def schema(self, source):
        data = self.request("GET", f"data_sources/{self.config.sources[source]}")
        self.schemas[source] = data["properties"]
        return data["properties"]

    def rows(self, source, filter=None, limit=None, sorts=None):
        body = {**({"filter": filter} if filter else {}), **({"sorts": sorts} if sorts else {})}
        iterator = self.listing(f"data_sources/{self.config.sources[source]}/query", body)
        return list(islice(iterator, limit)) if limit else list(iterator)

    def context(self, start, end, mode="live", check_budget=True, exclude_ids=()):
        for name in ["memories", "events", "plans"] + (["threads"] if self.config.continuation.enabled else []):
            self.schema(name)
        pages = {name: self.page(page_id) for name, page_id in self.config.pages.items()
                 if name != "operations"}
        memories = self.rows("memories", {"property": "确认状态", "select": {"does_not_equal": "已失效"}})
        # Full memory bodies preserve source, confirmation and narrative nature, including legacy blanks.
        memories = [self.page(x["id"]) for x in memories]
        since = (parse(start) - timedelta(days=self.config.context_days)).isoformat()
        inputs = self.rows("events", {"or": [
            {"property": "状态", "select": {"equals": "待处理"}},
            {"property": "状态", "select": {"equals": "待发生"}},
        ]})
        history = self.rows("events", {"and": [
            {"property": "状态", "select": {"equals": "已发生"}},
            {"property": "发生时间", "date": {"on_or_after": since}},
        ]}, limit=self.config.recent_events, sorts=[{"property": "发生时间", "direction": "descending"}])
        events = list({p["id"]: p for p in inputs + history}.values())
        if mode == "backfill":
            window_history = self.rows("events", {"and": [
                {"property": "状态", "select": {"equals": "已发生"}},
                {"property": "发生时间", "date": {"on_or_after": date_index(start)}},
                {"property": "发生时间", "date": {"on_or_before": date_index(end)}},
            ]})
            events = list({p["id"]: p for p in events + window_history}.values())
        events = [self.page(x["id"]) for x in events if x["id"] not in exclude_ids]
        plans = self.rows("plans", {"and": [
            {"property": "状态", "select": {"does_not_equal": "已完成"}},
            {"property": "状态", "select": {"does_not_equal": "已取消"}},
        ]})
        plans = [self.page(x["id"]) for x in plans]
        # Read original diary pages in bounded recent chronological order. No local history mirror.
        children = [x for x in pages["journal"]["blocks"] if x["type"] == "child_page"]
        # Legacy titles carry their dates; read current recent diaries without copying all archives.
        diaries = [self.page(x["id"]) for x in children[-min(3, self.config.context_days):]]
        result = {"pages": pages, "memories": memories, "events": events,
                  "plans": plans, "diaries": diaries, "mode": mode}
        if check_budget and len(encode(context_view(result))) > self.config.max_context_chars:
            raise Conflict("资料超过上下文上限；请调整配置或整理 Notion，未使用旧副本")
        return result

    def thread_context(self, thread, nodes, start, end, context=None):
        context = context if context is not None else self.context(start, end)
        pages = {p["id"]: p for p in context["events"]}
        # Query the relation as well as the local index: imports/manual additions must be visible.
        related = self.rows("events", {"property": "所属事项", "relation": {"contains": thread["notion_page_id"]}}) if thread["notion_page_id"] else []
        for node in nodes:
            if not node["verified_at"] or not node["notion_page_id"]:
                raise Conflict("事项存在未核对节点")
            page = self.page(node["notion_page_id"])
            latest_version = not any(n["notion_page_id"] == node["notion_page_id"] and n["revision"] > node["revision"] for n in nodes)
            if latest_version and node.get("authority_version") and fingerprint(page) != node["authority_version"]:
                raise Conflict("事项来源已变化；等待输入轮询核对")
            pages[page["id"]] = page
        for row in related:
            if row["id"] not in {n["notion_page_id"] for n in nodes}:
                page = self.page(row["id"])
                when = precise_page_date(page, "发生时间")
                if (when and when > parse(end)) or prop_value(page["properties"]["状态"]) == "已取消":
                    continue
                # New unindexed supplementation must pass the input poll before influencing a decision.
                raise Conflict("事项出现尚未接纳的新关联来源，等待轮询")
        context["events"] = list(pages.values())
        # Explicitly retrieve linked completed/cancelled plans too, beyond the normal open-plan query.
        return context

    def assert_sources(self, versions):
        for page_id, version in versions.items():
            if fingerprint(self.page(page_id)) != version:
                raise Conflict("使用的来源已变化；未发布旧输出")

    def properties(self, source, values):
        schema = self.schemas.get(source) or self.schema(source)
        result = {}
        for name, value in values.items():
            if value is None:
                continue
            if name not in schema:
                raise Conflict("Notion schema 缺少所需属性")
            kind = schema[name]["type"]
            if kind in {"title", "rich_text"}:
                result[name] = {kind: rich(str(value))}
            elif kind in {"select", "status", "multi_select"}:
                options = {x["name"] for x in schema[name][kind].get("options", [])}
                choices = value if kind == "multi_select" else [value]
                if not set(choices) <= options:
                    raise Conflict("Notion 属性选项不兼容，未自动新增选项")
                result[name] = {kind: [{"name": x} for x in choices] if kind == "multi_select" else {"name": value}}
            elif kind == "date":
                result[name] = {"date": {"start": date_index(value)}}
            elif kind == "url":
                result[name] = {"url": value}
            elif kind == "relation":
                result[name] = {"relation": [{"id": x} for x in value]}
            else:
                raise Conflict("不支持写入该 Notion 属性类型")
        return result

    def find_record(self, source, marker):
        rows = self.rows(source, {"property": "来源与记录者", "rich_text": {"contains": marker}})
        if len(rows) > 1:
            raise Conflict("稳定标识对应多个页面，请人工核对重复")
        return rows[0] if rows else None

    def apply(self, key, operation, sent, mark_sent):
        marker = key + ":" + digest(operation)[:16]
        kind = operation["kind"]
        if kind == "projection":
            page = self.page(operation["page_id"])
            desired = self.properties("threads", operation["values"])
            if operation["values"].get("下次检查") is None:
                desired["下次检查"] = {"date": None}
            # Protect managed properties as well as human notes outside the managed block.
            for name, old in operation["previous_values"].items():
                actual = prop_value(page["properties"][name])
                wanted = prop_value({"type": next(iter(desired[name])), **desired[name]})
                if not property_equal(actual, old) and not property_equal(actual, wanted):
                    raise Conflict("事项进度被人工修改；保留人工版本")
            if any(not property_equal(prop_value(page["properties"][name]), prop_value({"type": next(iter(value)), **value})) for name, value in desired.items()):
                self.request("PATCH", f"pages/{page['id']}", {"properties": desired})
            snapshot = {"kind": "snapshot", "page_id": page["id"], "prefix": "Orbit 事项进度\n",
                        "previous": operation["previous"], "text": operation["text"]}
            # A new block's uncertain creation is reconciled using its stable marker.
            self.apply(key + ":body", snapshot, sent, mark_sent)
            check = self.page(page["id"])
            if any(not property_equal(prop_value(check["properties"][name]), prop_value({"type": next(iter(value)), **value})) for name, value in desired.items()):
                raise Conflict("事项投影写后核对失败")
            return page["id"]
        if kind == "properties":
            page = self.page(operation["page_id"])
            desired = operation["values"]
            if all(prop_value(page["properties"][name]) == value for name, value in desired.items()):
                return page["id"]
            if fingerprint(page) != operation["expected"]:
                raise Conflict("关系目标页面已变化；未覆盖人工内容")
            mark_sent()
            self.request("PATCH", "pages/" + page["id"], {"properties": self.properties(operation["source"], desired)})
            check = self.page(page["id"])
            if not all(prop_value(check["properties"][name]) == value for name, value in desired.items()):
                raise Conflict("关系写后核对失败")
            return page["id"]
        if kind == "snapshot":
            prefix = operation.get("prefix", SNAPSHOT_PREFIX)
            blocks = self.blocks(operation["page_id"])
            managed = [b for b in blocks if b["text"].startswith(prefix)]
            expected = prefix + marker + "\n" + operation["text"]
            if len(managed) > 1:
                raise Conflict("多个受管理生活快照，请核对后再继续")
            if managed and managed[0]["text"] == expected:
                return managed[0]["id"]
            previous = operation["previous"]
            if managed:
                block = managed[0]
                if not previous or block["id"] != previous["id"] or digest(block["text"]) != previous["hash"]:
                    raise Conflict("生活快照在生成后被修改；未覆盖人工内容")
                mark_sent()
                self.request("PATCH", f"blocks/{block['id']}", {"paragraph": {"rich_text": rich(expected)}})
            else:
                if previous:
                    raise Conflict("原有生活快照已被移走或改写；未自动恢复删除内容")
                if sent:
                    raise UncertainWrite("快照创建回执不明确，等待核对")
                mark_sent()
                self.request("PATCH", f"blocks/{operation['page_id']}/children", {"children": [paragraph(expected)]})
            check = [b for b in self.blocks(operation["page_id"]) if b["text"] == expected]
            if len(check) != 1:
                raise UncertainWrite("生活快照写后核对未完成")
            return check[0]["id"]
        if kind == "record":
            source = operation["source"]
            self.schema(source)
            existing = self.find_record(source, marker)
            if existing:
                page = self.page(existing["id"])
                self.verify_record(page, operation, marker)
                return existing["id"]
            if sent:
                raise UncertainWrite("创建回执不明确，暂未查到稳定标识；禁止盲目重建")
            values = {**operation["values"], "来源与记录者": operation["values"].get("来源与记录者", "") + "\n" + marker}
            properties = self.properties(source, values)
            children = self.record_blocks(operation, marker)
            mark_sent()
            try:
                response = self.request("POST", "pages", {
                    "parent": {"type": "data_source_id", "data_source_id": self.config.sources[source]},
                    "properties": properties, "children": children})
            except RemoteError as exc:
                # A definite rejected request can safely be retried after configuration repair.
                raise exc
            page = self.page(response["id"])
            self.verify_record(page, operation, marker)
            return response["id"]
        if kind == "append":
            blocks = self.blocks(operation["page_id"])
            expected = marker + "\n" + operation["text"]
            matches = [x for x in blocks if x["text"].startswith(marker)]
            if matches:
                if len(matches) != 1 or matches[0]["text"] != expected:
                    raise Conflict("受管理记录已被人工修改或重复，保留现有内容")
                return matches[0]["id"]
            if sent:
                raise UncertainWrite("追加回执不明确；等待重新核对")
            block = paragraph(expected)
            if len(block["paragraph"]["rich_text"]) > 100:
                raise Conflict("单次追加过大")
            mark_sent()
            self.request("PATCH", f"blocks/{operation['page_id']}/children", {"children": [block]})
            matches = [x for x in self.blocks(operation["page_id"]) if x["text"] == expected]
            if len(matches) != 1:
                raise UncertainWrite("追加后未确认完整内容")
            return matches[0]["id"]
        if kind == "plan_update":
            self.schema("plans")
            page = self.page(operation["page_id"])
            values = operation["values"]
            if all(prop_value(page["properties"][k]) == v for k, v in values.items()):
                return page["id"]
            if fingerprint(page) != operation["expected"]:
                raise Conflict("计划在生成后被修改；未覆盖人工变更")
            mark_sent()
            self.request("PATCH", f"pages/{page['id']}", {"properties": self.properties("plans", values)})
            check = self.page(page["id"])
            if not all(prop_value(check["properties"][k]) == v for k, v in values.items()):
                raise Conflict("计划更新后核对失败")
            return page["id"]
        raise Conflict("未知写回操作")

    @staticmethod
    def record_blocks(operation, marker):
        return [paragraph(marker), paragraph(operation["text"])]

    def verify_record(self, page, operation, marker):
        parent = page.get("parent", {})
        expected_source = self.config.sources[operation["source"]].replace("-", "")
        if parent.get("data_source_id", "").replace("-", "") != expected_source:
            raise Conflict("记录父数据源核对失败")
        if page["blocks"][:2] == [] or [x["text"] for x in page["blocks"][:2]] != [marker, operation["text"]]:
            raise Conflict("新记录正文核对失败或已被人工修改")
        for name, value in operation["values"].items():
            if value is None:
                continue
            actual = prop_value(page["properties"][name])
            if name == "来源与记录者":
                if actual != value + "\n" + marker:
                    raise Conflict("记录来源核对失败")
            elif isinstance(actual, dict) and "start" in actual:
                expected_date = date_index(value)
                if parse(actual["start"] if "T" in actual["start"] else actual["start"] + "T00:00:00+08:00") != parse(expected_date if "T" in expected_date else expected_date + "T00:00:00+08:00"):
                    raise Conflict("记录日期核对失败")
            elif actual != value:
                raise Conflict("记录属性核对失败")
