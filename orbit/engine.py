"""Serialized simulation, durable publication, explicit historical backfill."""

import json
import re
from datetime import timedelta
from uuid import uuid4

from .model import InvalidOutput, validate
from .notion import SNAPSHOT_PREFIX, Conflict, UncertainWrite, compact_page, context_view, fingerprint, precise_page_date, prop_value
from .store import digest, encode
from .timeutil import SHANGHAI, day, parse, stamp
from .transport import RemoteError


def values(page):
    return {name: prop_value(p) for name, p in page["properties"].items()}


def page_date(page, name):
    return precise_page_date(page, name)


def source_map(context):
    pages = list(context["pages"].values())
    pages += [p for name in ["memories", "events", "plans", "diaries"] for p in context[name]]
    return {p["id"]: fingerprint(p) for p in pages}


def normalized(text):
    return re.sub(r"\W+", "", text.casefold())


class Engine:
    def __init__(self, config, store, notion, model, clock):
        self.config, self.store, self.notion, self.model, self.clock = config, store, notion, model, clock

    def flush(self):
        for row in self.store.pending():
            current = stamp(self.clock())
            if row["status"] == "blocked" or (row["next_at"] and row["next_at"] > current):
                return False
            try:
                operation = json.loads(row["payload"])
                if operation["kind"] == "runtime":
                    operation = {**operation, "kind": "snapshot", "prefix": "Orbit 运行状态快照\n",
                                 "previous": self.store.get("snapshot:" + operation["page_id"])}
                for field, reference in operation.get("references", {}).items():
                    receipt = self.store.db.execute("SELECT remote_id FROM outbox WHERE id=? AND status='done'", (reference,)).fetchone()
                    if not receipt or not receipt[0]:
                        raise Conflict("前置写入尚未核对，不能建立来源关联")
                    operation["values"][field] = [receipt[0]]
                remote_id = self.notion.apply(row["id"], operation, bool(row["sent"]),
                                              lambda: self.store.mark_sent(row["id"]))
                self.store.done(row["id"], remote_id, operation)
            except (RemoteError, Conflict, UncertainWrite) as exc:
                # HTTP 4xx definite rejections (except timeout/conflict) did not perform a write.
                if isinstance(exc, RemoteError) and exc.status and 400 <= exc.status < 500 and exc.status not in {408, 409}:
                    with self.store.db:
                        self.store.db.execute("UPDATE outbox SET sent=0 WHERE id=?", (row["id"],))
                self.store.fail(row, current, exc, self.config.max_attempts,
                                uncertain=isinstance(exc, UncertainWrite))
                return False
        return True

    def generation_ready(self, current):
        retry_at = self.store.get("next_generation_at")
        return not self.store.get("generation_blocked", False) and (not retry_at or retry_at <= current)

    def failure(self, current, exc):
        attempts = self.store.get("generation_failures", 0) + 1
        with self.store.db:
            self.store.put("generation_failures", attempts)
            self.store.put("last_error", {"category": type(exc).__name__, "at": current,
                                           "code": exc.code if isinstance(exc, InvalidOutput) else None,
                                           "service": exc.service if isinstance(exc, RemoteError) else None,
                                           "reason": exc.reason if isinstance(exc, RemoteError) else None,
                                           "http_status": exc.status if isinstance(exc, RemoteError) else None})
            self.store.put("generation_blocked", attempts >= self.config.max_attempts or not getattr(exc, "retryable", True))
            self.store.put("next_generation_at", stamp(parse(current) + timedelta(seconds=min(300, 2 ** min(attempts, 8)))))
        self.store.operational(self.config.pages["operations"], current, {
            "at": current, "error_category": type(exc).__name__, "consecutive_failures": attempts,
            "service": exc.service if isinstance(exc, RemoteError) else None,
            "reason": exc.reason if isinstance(exc, RemoteError) else None,
            "generation_blocked": self.store.get("generation_blocked"),
            "life_committed": False, "next_retry": self.store.get("next_generation_at"),
        })

    def tick(self):
        current = stamp(self.clock())
        # Persisted writes are technical recovery and can finish even while life is paused.
        if not self.flush():
            return "writes_pending"
        if self.store.get("paused", False):
            return "paused"
        if not self.generation_ready(current):
            return "generation_deferred"
        pending_backfill = self.store.db.execute("SELECT id FROM backfills WHERE status IN ('pending','running','blocked') LIMIT 1").fetchone()
        if pending_backfill:
            return "backfill_pending"  # The administrator explicitly runs or resolves it.
        cursor = self.store.get("cursor")
        if cursor is None:
            self.store.recover(current)
            cursor = current
        if parse(cursor) > parse(current):
            raise ValueError("现实时间早于游标，拒绝倒退")
        start = max(parse(cursor), parse(current) - timedelta(seconds=self.config.window_seconds))
        with self.store.db:
            self.store.gap(cursor, stamp(start), "realtime_window_exceeded")
        try:
            self.generate(stamp(start), current, "live")
            self.flush()
            self.store.prune(current, self.config.retention_days)
            return "generated" if not self.store.pending() else "generated_writes_pending"
        except (RemoteError, Conflict, InvalidOutput) as exc:
            self.failure(current, exc)
            return "generation_failed"

    def pending_inputs(self, context, end):
        result = []
        for page in context["events"]:
            props = values(page)
            occurs = page_date(page, "发生时间")
            if props.get("状态") in {"待处理", "待发生"} and (occurs is None or occurs <= parse(end)):
                if not self.store.consumed(page["id"], fingerprint(page)):
                    result.append(page)
        return result

    def generate(self, start, end, mode, backfill_id=None):
        context = self.notion.context(start, end, mode)
        if mode == "backfill":
            # Existing human/other-platform history is authoritative, even if absent from SQLite.
            if any(values(p).get("状态") == "已发生" and page_date(p, "发生时间") is not None
                   and parse(start) < page_date(p, "发生时间") <= parse(end) for p in context["events"]):
                raise Conflict("补写窗口已有 Notion 历史记录，拒绝覆盖或重复生成")
        before = source_map(context)
        pending = self.pending_inputs(context, end) if mode == "live" else []
        data = {"story_window": {"start": start, "end": end, "timezone": "Asia/Shanghai"},
                "mode": mode, "generated_at": stamp(self.clock()), "context": context_view(context),
                "pending_inputs": [p["id"] for p in pending],
                "gaps": self.store.status()["gaps"],
                "instruction": "pending_inputs 是本轮唯一待回应输入；其他待处理条目可能已回应或尚未到时。旧状态仅代表其原有故事时间。"}
        previous_error = self.store.get("last_error")
        if previous_error and previous_error.get("category") == "InvalidOutput":
            data["protocol_feedback"] = {"previous_error": previous_error.get("code"),
                                         "instruction": "只纠正协议格式、时间或引用，不替换 Echo 的主观选择。"}
        if mode == "backfill":
            data["later_history"] = [dict(x) for x in self.store.db.execute(
                "SELECT start,end,mode FROM runs WHERE start>=? ORDER BY start LIMIT 100", (end,))]
        output = validate(self.model.decide(data), start, end, context)
        referenced = {s for e in output["events"] for s in e["sources"]}
        if any(p["id"] not in referenced for p in pending) and not output["conflicts"]:
            raise InvalidOutput("待回应事件未获得 Echo 判断")
        # Re-read authority after model latency; no silent fallback or stale write proposals.
        if source_map(self.notion.context(start, end, mode)) != before:
            raise Conflict("Notion 资料在生成期间变化，丢弃尚未发布的生成结果")
        run = {"id": uuid4().hex, "start": start, "end": end, "mode": mode,
               "generated_at": stamp(self.clock()), "evidence": {"sources": before, "output": digest(output)}}
        if mode == "backfill" and output["conflicts"]:
            run["mode"] = "conflict"
            operations = [self.append("operations", "补写冲突，未提交生活内容：\n" + encode({"start": start, "end": end, "conflicts": output["conflicts"]}))]
        else:
            operations = self.operations(output, run, context)
        # One transaction commits outputs, write intents, input receipts and the story cursor.
        with self.store.db:
            self.store.stage(run, operations, [(p["id"], fingerprint(p)) for p in pending if p["id"] in referenced])
            if backfill_id:
                self.store.db.execute("UPDATE backfills SET cursor=?,status=? WHERE id=?",
                                      (start if run["mode"] == "conflict" else end,
                                       "blocked" if run["mode"] == "conflict" else "running", backfill_id))
            self.store.put("generation_failures", 0)
            self.store.put("generation_blocked", False)
            self.store.put("next_generation_at", None)
            self.store.put("last_error", None)
        return run["mode"]

    def append(self, name, text):
        return {"kind": "append", "page_id": self.config.pages[name], "text": text}

    def runtime(self, text):
        return {"kind": "runtime", "page_id": self.config.pages["operations"], "text": text}

    def operations(self, output, run, context):
        operations = []
        events = {e["id"]: e for e in output["events"]}
        event_links = {key: f"orbit-run:{run['id']}#{key}" for key in events}
        provenance = {"run": run["id"], "mode": run["mode"], "generated_at": run["generated_at"],
                      "decision_subject": "Echo", "executor": "Orbit"}
        for event in output["events"]:
            body = {**provenance, "event_ref": event_links[event["id"]], **event,
                    "future_intentions": [p for p in output["plans"] if p["source_event"] == event["id"]],
                    "plan_decisions": [p for p in output["plan_updates"] if p["source_event"] == event["id"]],
                    "state_decision": output["state"] if output["state"] and output["state"]["source_event"] == event["id"] else None}
            source = event["sources"][0]
            operations.append({"kind": "record", "source": "events", "values": {
                "事件": event["title"], "一句话概要": event["external"][:1500],
                "发生时间": event["time"], "信息性质": "共同叙事", "状态": "已发生",
                "参与者": event["participants"], "地点": event["location"],
                "Echo的理解与感受": event["interpretation"] + "\n" + event["feeling"],
                "Echo的行动决定": event["decision"], "后续影响": event["intention"],
                "来源链接": "https://www.notion.so/" + source.replace("-", ""),
                "来源与记录者": f"事件提供者：{event['provider']}；决策主体：Echo；执行者：Orbit；{run['id']}"},
                "text": encode(body)})
        if output["state"] is not None and run["mode"] == "live":
            state = output["state"]
            managed = [b for b in context["pages"]["state"]["blocks"] if b["text"].startswith(SNAPSHOT_PREFIX)]
            if len(managed) > 1:
                raise Conflict("多个受管理生活快照，请先核对")
            previous = {"id": managed[0]["id"], "hash": digest(managed[0]["text"])} if managed else None
            operations.append({"kind": "snapshot", "page_id": self.config.pages["state"], "previous": previous,
                               "text": "此前快照只代表原时间；未知项留空。\n" + encode({
                **provenance, **state, "story_time": events[state["source_event"]]["time"],
                "source": event_links[state["source_event"]], "timezone": "Asia/Shanghai"})})
        event_keys = {event["id"]: f"orbit:{run['id']}:{i}" for i, event in enumerate(output["events"])}
        for plan in output["plans"]:
            # Historical intentions stay in the historical event body, not today's outstanding tasks.
            if run["mode"] == "backfill":
                continue
            operations.append({"kind": "record", "source": "plans", "values": {
                "事项": plan["title"], "一句话概要": plan["detail"], "类型": "后续意图",
                "状态": "待执行", "信息性质": "共同叙事", "计划时间": plan["time"],
                "到期时间": plan["due"], "相关人物": plan["people"],
                "来源与记录者": f"决策主体：Echo；执行者：Orbit；{event_links[plan['source_event']]}"},
                "references": {"来源事件": event_keys[plan["source_event"]]},
                "text": encode({**provenance, **plan, "source": event_links[plan["source_event"]]})})
        current_plans = {p["id"]: p for p in context["plans"]}
        for update in output["plan_updates"]:
            if run["mode"] == "backfill":
                continue  # historical decisions are retained with the source event below
            page = current_plans[update["plan_id"]]
            desired = {"状态": update["status"]}
            if not values(page).get("执行结果") and update["status"] == "已完成":
                desired["执行结果"] = events[update["source_event"]]["result"]
            operations.append({"kind": "plan_update", "page_id": page["id"],
                               "expected": fingerprint(page), "values": desired})
            operations.append({"kind": "append", "page_id": page["id"],
                               "text": encode({**provenance, **update, "source": event_links[update["source_event"]],
                                               "original_schedule": values(page).get("计划时间")})})
        for memory in output["memories"]:
            event = events[memory["source_event"]]
            body = encode({**provenance, **memory, "source": event_links[memory["source_event"]],
                           "story_time": event["time"], "information_nature": "共同叙事"})
            existing = memory["existing_id"]
            matches = [p for p in context["memories"] if normalized(values(p).get("记忆", "")) == normalized(memory["title"])]
            if matches and existing is None:
                existing = matches[0]["id"]
            if existing:
                page = next(p for p in context["memories"] if p["id"] == existing)
                if normalized(memory["detail"]) in normalized("\n".join(b["text"] for b in page["blocks"])):
                    continue
                operations.append({"kind": "append", "page_id": existing, "text": "共同叙事增量，保留原确认状态与信息性质：\n" + body})
            else:
                operations.append({"kind": "record", "source": "memories", "values": {
                    "记忆": memory["title"], "一句话概要": memory["summary"], "分类": memory["category"],
                    "确认状态": "已确认", "信息性质": "共同叙事", "发生日期": day(parse(event["time"])),
                    "相关人物": memory["people"], "来源与记录者": f"Echo 自主提炼；Orbit 执行；{event_links[memory['source_event']]}"},
                    "text": body})
        # Preserve all future intentions/results independently, including during backfill.
        operations.append(self.runtime("Orbit 推进记录：\n" + encode({
            **provenance, "story_window": [run["start"], run["end"]], "event_count": len(events),
            "proposals": output["proposals"], "conflicts": output["conflicts"],
            "historical_intentions": output["plans"] if run["mode"] == "backfill" else [],
            "historical_plan_decisions": output["plan_updates"] if run["mode"] == "backfill" else [],
            "paused": self.store.get("paused", False),
            "next_wake_not_before": stamp(parse(run["end"]) + timedelta(seconds=self.config.interval_seconds)) if run["mode"] == "live" else None,
            "publication": "前序写回已逐项读回核对；本条核对后本轮写回完成"})))
        return operations

    def backfill(self, task_id, max_segments=12):
        if not self.flush():
            return "writes_pending"
        task = self.store.db.execute("SELECT * FROM backfills WHERE id=?", (task_id,)).fetchone()
        if not task:
            raise ValueError("补写任务不存在")
        if task["status"] == "blocked":
            return "backfill_conflict"
        if task["status"] == "cancelled":
            return "cancelled"
        if task["status"] == "done":
            return "done"
        if task["cursor"] == task["end"]:
            with self.store.db:
                self.store.db.execute("UPDATE backfills SET status='done' WHERE id=?", (task_id,))
            return "done"
        if not self.generation_ready(stamp(self.clock())):
            return "generation_deferred"
        for _ in range(max_segments):
            start = task["cursor"]
            end = stamp(min(parse(task["end"]), parse(start) + timedelta(seconds=self.config.backfill_seconds)))
            if parse(end) > self.clock():
                raise ValueError("不能补写现实未来")
            try:
                mode = self.generate(start, end, "backfill", task_id)
            except (RemoteError, Conflict, InvalidOutput) as exc:
                self.failure(stamp(self.clock()), exc)
                return "generation_failed"
            if not self.flush():
                return "writes_pending"
            if mode == "conflict":
                return "backfill_conflict"
            task = self.store.db.execute("SELECT * FROM backfills WHERE id=?", (task_id,)).fetchone()
            if task["cursor"] == task["end"]:
                with self.store.db:
                    self.store.db.execute("UPDATE backfills SET status='done' WHERE id=?", (task_id,))
                return "done"
        return "segments_pending"

    def diary(self, date):
        from datetime import date as Date, datetime, time
        target = Date.fromisoformat(date)
        if target >= self.clock().astimezone(SHANGHAI).date():
            raise ValueError("只整理已经结束的上海日期")
        if not self.flush():
            return "writes_pending"
        start = datetime.combine(target, time.min, SHANGHAI)
        end = start + timedelta(days=1)
        self.notion.schema("events")
        pages = self.notion.rows("events", {"and": [
            {"property": "状态", "select": {"equals": "已发生"}},
            {"property": "发生时间", "date": {"on_or_after": start.isoformat()}},
            {"property": "发生时间", "date": {"before": end.isoformat()}},
        ]})
        pages = [self.notion.page(p["id"]) for p in pages]
        if not pages:
            return "no_recorded_events"
        signature = digest(source_map({"pages": {}, "events": pages, "memories": [], "plans": [], "diaries": []}))
        existing = self.store.db.execute("SELECT signature FROM diaries WHERE day=?", (date,)).fetchone()
        if existing and existing[0] == signature:
            return "unchanged"
        gaps = [g for g in self.store.status()["gaps"] if parse(g["start"]) < end and parse(g["end"]) > start]
        journal = self.notion.page(self.config.pages["journal"])
        identity = self.notion.page(self.config.pages["identity"])
        diary_data = {"date": date, "events": [compact_page(p) for p in pages], "unmodeled_intervals": gaps,
                      "identity": compact_page(identity), "existing_journal": compact_page(journal),
                      "notice": "只依据已记录事件，补写已覆盖范围可参考事件本身；原缺口保留供审计。"}
        if len(encode(diary_data)) > self.config.max_context_chars:
            raise Conflict("当日日记资料超过上下文上限，未截断或伪造摘要")
        text = self.model.diary(diary_data)
        if any(fingerprint(self.notion.page(p["id"])) != fingerprint(p) for p in pages + [journal, identity]):
            raise Conflict("资料在日记生成期间变化")
        run = {"id": uuid4().hex, "start": stamp(start), "end": stamp(end), "mode": "diary",
               "generated_at": stamp(self.clock()), "evidence": {"signature": signature}}
        with self.store.db:
            self.store.stage(run, [self.append("journal", f"{date}｜Echo 每日回顾（增量版本，保留原文）\n"
                                               + encode({"generated_at": run["generated_at"], "source_ids": [p["id"] for p in pages],
                                                         "unmodeled_intervals": gaps, "text": text}))])
            self.store.db.execute("INSERT INTO diaries VALUES (?,?) ON CONFLICT(day) DO UPDATE SET signature=excluded.signature", (date, signature))
        return "saved" if self.flush() else "writes_pending"
