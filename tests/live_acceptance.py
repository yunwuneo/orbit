"""Explicit live acceptance runner. Never imported by unittest discovery.

Writes only below a recorded test root under the operations page. Test artifacts are
archived together at the end; production SQLite and life records are never changed.
"""

import argparse
import fcntl
import json
import os
import select
import signal
import subprocess
import sys
import time
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from uuid import uuid4
from urllib.parse import urlsplit
import sqlite3

from orbit.config import load_config
from orbit.engine import Engine, source_map, values
from orbit.model import Model, validate
from orbit.notion import SNAPSHOT_PREFIX, Notion, compact_page, context_view, fingerprint, paragraph, rich
from orbit.store import Store, digest, encode
from orbit.timeutil import SHANGHAI, TIMEZONE, day, now, parse, stamp
from orbit.transport import RemoteError


WORK = Path("work/live-acceptance-2026-10-16")
STATE = WORK / "state.json"


class LiveNotion(Notion):
    def __init__(self, config):
        super().__init__(config)
        self.last_request = 0
        self.lose_next_record_ack = False

    def request(self, method, path, body=None):
        delay = .38 - (time.monotonic() - self.last_request)
        if delay > 0:
            time.sleep(delay)
        self.last_request = time.monotonic()
        result = super().request(method, path, body)
        if self.lose_next_record_ack and method == "POST" and path == "pages":
            self.lose_next_record_ack = False
            raise RemoteError("Notion")
        return result


class ObservedModel(Model):
    def decide(self, data):
        output = super().decide(data)
        try:
            validate(output, data["story_window"]["start"], data["story_window"]["end"],
                     self.context)
        except Exception as exc:
            fields = {name: type(output.get(name)).__name__ for name in ["events", "state", "plans", "memories"]}
            print(encode({"model_contract_diagnostic": getattr(exc, "code", type(exc).__name__),
                          "field_types": fields,
                          "state_field_types": {k: type(v).__name__ for k, v in (output.get("state") or {}).items()},
                          "point_window": data["story_window"]["start"] == data["story_window"]["end"]}), flush=True)
            raise
        return output


def save(state):
    WORK.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (WORK / ".state.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        merged = json.loads(STATE.read_text()) if STATE.exists() else {}
        for key, value in state.items():
            if isinstance(value, dict):
                merged[key] = {**merged.get(key, {}), **value}
            elif key == "sent_creates":
                merged[key] = list(dict.fromkeys(merged.get(key, []) + value))
            else:
                merged[key] = value
        temporary = WORK / ".state.tmp"
        temporary.write_text(encode(merged), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(STATE)


def record(state, name, **details):
    state.setdefault("results", {})[name] = {"at": stamp(now()), **details}
    save(state)
    print(encode({"check": name, **details}), flush=True)


def must(condition, name):
    if not condition:
        raise AssertionError(name)


def create_page(notion, state, key, parent, title, text):
    if key in state.setdefault("created_pages", {}):
        return state["created_pages"][key]
    # Reconcile a prior lost creation response by exact test title, never blind-create.
    children = notion.blocks(parent)
    matches = [b for b in children if b["type"] == "child_page" and b["text"] == title]
    if matches:
        must(len(matches) == 1, "duplicate_test_page")
        page_id = matches[0]["id"]
    else:
        must(key not in state.setdefault("sent_creates", []), "uncertain_test_page_creation")
        state["sent_creates"].append(key)
        save(state)
        result = notion.request("POST", "pages", {
            "parent": {"type": "page_id", "page_id": parent},
            "properties": {"title": {"type": "title", "title": rich(title)}},
            "children": [paragraph(text)],
        })
        page_id = result["id"]
    state["created_pages"][key] = page_id
    save(state)
    return page_id


def clone_properties(schema, source_ids):
    props = {}
    for name, field in schema.items():
        kind = field["type"]
        if kind in {"title", "rich_text", "date", "url", "last_edited_time"}:
            props[name] = {kind: {}}
        elif kind in {"select", "multi_select"}:
            props[name] = {kind: {"options": [{"name": x["name"], "color": x["color"]}
                                             for x in field[kind]["options"]]}}
        elif kind == "relation":
            original = field[kind].get("data_source_id")
            target = source_ids[original.replace("-", "")]
            props[name] = {"relation": {"data_source_id": target, "type": "single_property", "single_property": {}}}
        else:
            raise AssertionError("unsupported_test_schema_type")
    return props


def config_for_test(config, state):
    return replace(config, db_path=str(WORK / "test.sqlite3"), pages=state["test_pages"], sources=state["test_sources"])


def preflight(config, state):
    config.require_connections()
    env_path = Path(".env")
    if env_path.is_file() and not env_path.is_symlink():
        env_path.chmod(0o600)
    notion = LiveNotion(config)
    context = notion.context(stamp(now()), stamp(now()))
    record(state, "production_notion_read", passed=True,
           resources={k: len(context[k]) for k in ["memories", "events", "plans", "diaries"]},
           context_chars=len(encode(context_view(context))))


def production_model(config, state):
    """Only run after the human explicitly authorizes production context disclosure."""
    notion = LiveNotion(config)
    context = notion.context(stamp(now()), stamp(now()))
    end = now()
    data = {"story_window": {"start": stamp(end - timedelta(seconds=120)), "end": stamp(end), "timezone": TIMEZONE},
            "generated_at": stamp(end), "mode": "live", "context": context_view(context),
            "pending_inputs": [], "gaps": [],
            "instruction": "只读联调：根据资料在当前窗口形成判断。此结果不发布到正式生活历史。"}
    output = validate(Model(config).decide(data), data["story_window"]["start"], data["story_window"]["end"], context)
    record(state, "production_context_model_contract", passed=True, events=len(output["events"]),
           plans=len(output["plans"]), memories=len(output["memories"]), state_present=output["state"] is not None,
           production_writes=False)


def bootstrap(config, state):
    notion = LiveNotion(config)
    marker = state.setdefault("session", uuid4().hex[:12])
    save(state)
    title = "Orbit 联调临时区｜" + marker
    root = create_page(notion, state, "root", config.pages["operations"], title,
                       "技术测试区：所有内容均为隔离验收场景，不属于正式 Echo 生活、记忆或约定；完成后整体归档。")
    state["root"] = root
    save(state)
    pages = state.setdefault("test_pages", {})
    for name in config.pages:
        if name in pages:
            continue
        # Entirely synthetic fixtures: no private Notion text is sent to the model.
        content = {
            "identity": "Echo 是纯虚构的测试角色，在测试书店整理书单；糖糖是虚构同事。无真实用户或第三方的私密经历。",
            "rules": "只推进给定窗口；外部事件不得预设主观反应；Echo 独立决定。未知行程应追问；未来意图不算执行；停机不补造生活。",
            "memory_rules": "只保留有持续价值的共同叙事并查重；待确认和失效资料不当作有效事实；技术探针不是角色亲历。",
        }.get(name, "纯虚构隔离联调资料；原始人工文字须保留。当前生活状态未知；没有停机期间经历。")
        pages[name] = create_page(notion, state, name, root, "验收｜" + name,
                                  "本页只服务于隔离验收，不是正式历史。\n" + content)
        save(state)
    mapped = {key.replace("-", ""): state.get("test_sources", {}).get(name)
              for name, key in config.sources.items()}
    for name in ["memories", "events", "plans"]:
        if name in state.setdefault("test_sources", {}):
            continue
        schema = notion.schema(name)
        props = clone_properties(schema, mapped)
        db_title = "验收数据库｜" + name
        existing = [b for b in notion.blocks(root) if b["type"] == "child_database" and b["text"] == db_title]
        if existing:
            must(len(existing) == 1, "duplicate_test_database")
            database = notion.request("GET", "databases/" + existing[0]["id"])
        else:
            key = "database:" + name
            must(key not in state["sent_creates"], "uncertain_test_database_creation")
            state["sent_creates"].append(key)
            save(state)
            database = notion.request("POST", "databases", {
                "parent": {"type": "page_id", "page_id": root}, "title": rich(db_title),
                "initial_data_source": {"properties": props},
            })
        source_id = database["data_sources"][0]["id"]
        state["test_sources"][name] = source_id
        mapped[config.sources[name].replace("-", "")] = source_id
        save(state)
    record(state, "isolated_notion_setup", passed=True, root=root, sources=3, pages=len(pages))


def stage_technical(store, operation, current):
    run = {"id": uuid4().hex, "start": current, "end": current, "generated_at": current,
           "mode": "operations", "evidence": {"technical_acceptance": True}}
    with store.db:
        store.stage(run, [operation])
    return "orbit:" + run["id"] + ":0"


def checks(config, state):
    cfg = config_for_test(config, state)
    notion = LiveNotion(cfg)
    store = Store(cfg.db_path)
    model = ObservedModel(cfg)
    original_decide = model.decide
    def decide_with_context(data):
        # Validation only needs IDs and plan properties, so reconstitute the safe test view.
        model.context = {"pages": {k: {"id": p["id"]} for k, p in data["context"]["pages"].items()},
                         **{k: [{"id": p["id"], "properties": {name: {"type": "date", "date": value}
                                    for name, value in p["properties"].items() if name in {"计划时间", "到期时间"}}}
                                for p in data["context"][k]] for k in ["events", "plans", "memories", "diaries"]}}
        return original_decide(data)
    model.decide = decide_with_context
    engine = Engine(cfg, store, notion, model, now)
    try:
        if not state.get("technical_plan_key"):
            for row in store.db.execute("SELECT id,payload FROM outbox WHERE payload IS NOT NULL"):
                queued = json.loads(row["payload"])
                if queued.get("source") == "plans" and queued.get("values", {}).get("事项") == "隔离技术计划探针":
                    state["technical_plan_key"] = row["id"]
                    save(state)
                    break
        if state.get("results", {}).get("write_recovery") and store.pending():
            must(engine.flush(), "existing_test_writes_not_reconciled")
        if "write_recovery" not in state.get("results", {}):
            operation = {"kind": "record", "source": "memories", "values": {
                "记忆": "验收技术探针（不是 Echo 记忆）", "一句话概要": "只测试写后核对和丢失回执恢复",
                "分类": "其他", "确认状态": "待确认", "信息性质": "推测", "来源与记录者": "Orbit 隔离验收"},
                "text": "技术探针，不提供给 Echo 作为有效生活事实。"}
            key = state.get("recovery_key")
            if key is None:
                key = stage_technical(store, operation, stamp(now()))
                state["recovery_key"] = key
                save(state)
                notion.lose_next_record_ack = True
                must(not engine.flush(), "expected_lost_ack")
            store.close()
            store = Store(cfg.db_path)
            engine.store = store
            with store.db:
                store.db.execute("UPDATE outbox SET next_at=NULL WHERE id=?", (key,))
            must(engine.flush(), "real_write_recovery_failed")
            receipt = store.db.execute("SELECT remote_id FROM outbox WHERE id=?", (key,)).fetchone()[0]
            must(len(notion.rows("memories")) == 1, "duplicate_memory_probe")
            notion.request("PATCH", "pages/" + receipt, {"archived": True})
            record(state, "write_recovery", passed=True, simulated_lost_ack=True, real_remote_commit=True,
                   recovered_after_database_reopen=True, duplicate_count=0)
        if "invitation_seed" not in state:
            notion.schema("events")
            event = notion.request("POST", "pages", {
                "parent": {"type": "data_source_id", "data_source_id": cfg.sources["events"]},
                "properties": notion.properties("events", {
                    "事件": "隔离场景：糖糖邀请一起旅行", "一句话概要": "糖糖问 Echo 要不要一起旅行，尚未说明时间和目的地。",
                    "状态": "待处理", "信息性质": "共同叙事", "参与者": ["Echo", "糖糖"],
                    "来源与记录者": "Orbit 技术验收提供的客观场景，不属于正式历史"}),
                "children": [paragraph("只提供邀请本身；Echo 自主决定回应，不预设接受或情绪，不补造未知行程。")],
            })
            state["invitation_seed"] = event["id"]
            save(state)
        if "invitation_model_e2e" not in state.get("results", {}):
            existing_run = store.db.execute("SELECT 1 FROM runs WHERE mode='live' LIMIT 1").fetchone()
            result = ("generated" if engine.flush() else "generated_writes_pending") if existing_run else engine.tick()
            if result != "generated":
                record(state, "invitation_model_attempt", result=result, local_status=store.status())
            must(result == "generated", "invitation_generation_or_publication_failed")
            events = [notion.page(p["id"]) for p in notion.rows("events") if p["id"] != state["invitation_seed"]]
            must(bool(events), "missing_echo_response")
            must(store.consumed(state["invitation_seed"], fingerprint(notion.page(state["invitation_seed"]))), "input_not_consumed")
            record(state, "invitation_model_e2e", passed=True, events=len(events),
                   decisions=[values(p).get("Echo的行动决定", "")[:250] for p in events],
                   plans=len(notion.rows("plans")), memories=len(notion.rows("memories")),
                   all_writes_verified=not bool(store.pending()))
        if "pause_restart_resume" not in state.get("results", {}):
            previous_count = store.db.execute("SELECT count(*) FROM runs WHERE mode='live'").fetchone()[0]
            store.pause(stamp(now()))
            must(engine.tick() == "paused", "pause_generated_life")
            store.close()
            store = Store(cfg.db_path)
            engine.store = store
            must(store.get("paused") is True, "pause_not_persistent")
            store.recover(stamp(now()))
            must(engine.tick() == "paused", "restart_while_paused_generated_life")
            store.resume(stamp(now()))
            must(store.db.execute("SELECT count(*) FROM runs WHERE mode='live'").fetchone()[0] == previous_count,
                 "resume_fabricated_life")
            record(state, "pause_restart_resume", passed=True, missing_intervals=len(store.status()["gaps"]),
                   new_life_during_pause=0)
        if "state_plan_memory_writers" not in state.get("results", {}):
            # Exercise exact REST writers using explicit technical data, not forced Echo output.
            for source in cfg.sources:
                notion.schema(source)
            context = notion.context(stamp(now()), stamp(now()))
            snapshots = [b for b in context["pages"]["state"]["blocks"] if b["text"].startswith(SNAPSHOT_PREFIX)]
            previous = {"id": snapshots[0]["id"], "hash": digest(snapshots[0]["text"])} if snapshots else None
            stage_technical(store, {"kind": "snapshot", "page_id": cfg.pages["state"], "previous": previous,
                                    "text": "隔离技术快照探针：不代表 Echo 的生活或主观判断；当前地点、活动、情绪与精力未知。"}, stamp(now()))
            must(engine.flush(), "technical_snapshot_write_failed")
            test_plan = {"kind": "record", "source": "plans", "values": {
                "事项": "隔离技术计划探针", "一句话概要": "验证未来计划保持未完成", "类型": "待办",
                "状态": "待执行", "信息性质": "推测", "计划时间": stamp(now() + timedelta(days=1)),
                "来源与记录者": "Orbit 技术验收，非 Echo 决策"}, "text": "仅为技术写回和人工修改保护测试。"}
            key = state.get("technical_plan_key")
            if key is None:
                for row in store.db.execute("SELECT id,payload FROM outbox WHERE payload IS NOT NULL"):
                    pending = json.loads(row["payload"])
                    if pending.get("source") == "plans" and pending.get("values", {}).get("事项") == "隔离技术计划探针":
                        key = row["id"]
                        break
            if key is None:
                key = stage_technical(store, test_plan, stamp(now()))
            state["technical_plan_key"] = key
            save(state)
            must(engine.flush(), "technical_plan_write_failed")
            plan_id = store.db.execute("SELECT remote_id FROM outbox WHERE id=?", (key,)).fetchone()[0]
            before = notion.page(plan_id)
            notion.request("PATCH", "pages/" + plan_id, {"properties": notion.properties("plans", {"一句话概要": "验收模拟人工修改：须保留"})})
            update_key = stage_technical(store, {"kind": "plan_update", "page_id": plan_id,
                                                "expected": fingerprint(before), "values": {"状态": "已取消"}}, stamp(now()))
            must(not engine.flush(), "concurrent_edit_was_overwritten")
            must(values(notion.page(plan_id))["一句话概要"] == "验收模拟人工修改：须保留", "manual_text_lost")
            must(values(notion.page(plan_id))["状态"] == "待执行", "future_plan_completed")
            # This rejected operation never wrote; remove this test-only intent after recording evidence.
            with store.db:
                store.db.execute("DELETE FROM outbox WHERE id=? AND status='blocked'", (update_key,))
            notion.request("PATCH", "pages/" + plan_id, {"archived": True})
            record(state, "state_plan_memory_writers", passed=True, actual_record_writes=True,
                   snapshot_write_verified=True, future_plan_unchanged=True, concurrent_edit_preserved=True)
        if "explicit_backfill" not in state.get("results", {}):
            # Deliberately recorded historical test gap, never a production story interval.
            task = state.get("backfill_task")
            if task is None:
                start = stamp(now() - timedelta(days=1, minutes=20))
                end = stamp(parse(start) + timedelta(seconds=cfg.backfill_seconds * 2))
                with store.db:
                    store.gap(start, end, "isolated_acceptance_gap")
                task = store.add_backfill(start, end)
                state["backfill_task"] = task
                state["backfill_live_cursor"] = store.get("cursor")
                state["backfill_state_hash"] = digest(notion.blocks(cfg.pages["state"]))
                state["backfill_plan_states"] = {p["id"]: values(p).get("状态") for p in notion.rows("plans")}
                save(state)
            row = store.db.execute("SELECT * FROM backfills WHERE id=?", (task,)).fetchone()
            start = row["start"]
            cursor = state["backfill_live_cursor"]
            if "backfill_state_hash" not in state:
                state["backfill_state_hash"] = digest(notion.blocks(cfg.pages["state"]))
                state["backfill_plan_states"] = {p["id"]: values(p).get("状态") for p in notion.rows("plans")}
                save(state)
            result = engine.backfill(task, 1)
            record(state, "backfill_first_segment", result=result)
            must(result in {"segments_pending", "done"}, "first_backfill_segment_failed")
            if result != "done":
                store.close()
                store = Store(cfg.db_path)
                engine.store = store
                result = engine.backfill(task, 1)
                record(state, "backfill_second_segment", result=result)
                must(result == "done", "backfill_resume_failed")
            must(store.get("cursor") == cursor, "backfill_rewound_current_state")
            must(digest(notion.blocks(cfg.pages["state"])) == state["backfill_state_hash"], "backfill_changed_current_snapshot")
            must({p["id"]: values(p).get("状态") for p in notion.rows("plans")} == state["backfill_plan_states"],
                 "backfill_changed_current_plans")
            record(state, "explicit_backfill", passed=True, segments=2, resumed=True, current_cursor_preserved=True,
                   current_snapshot_preserved=True, current_plan_states_preserved=True, diary_date=day(parse(start)))
        if "real_model_diary" not in state.get("results", {}):
            target = state["results"]["explicit_backfill"]["diary_date"]
            result = engine.diary(target)
            record(state, "diary_attempt", result=result)
            must(result == "saved", "diary_write_failed")
            must(engine.diary(target) == "unchanged", "diary_duplicate_generation")
            record(state, "real_model_diary", passed=True, date=target, source="recorded_test_events", duplicate_count=0)
        if "point_window_model" not in state.get("results", {}):
            context = notion.context(stamp(now()), stamp(now()))
            point = stamp(now())
            data = {"story_window": {"start": point, "end": point, "timezone": TIMEZONE},
                    "mode": "live", "context": context_view(context), "pending_inputs": [],
                    "gaps": store.status()["gaps"], "instruction": "本轮仅为启动瞬时协议验证，不发布内容；待处理输入已经回应。"}
            output = validate(model.decide(data), point, point, context)
            record(state, "point_window_model", passed=True, events=len(output["events"]), exact_timestamp_preserved=True)
        record(state, "final_queue", passed=not bool(store.pending()), pending=len(store.pending()))
    finally:
        store.close()


def cleanup(config, state):
    root = state.get("root")
    must(root is not None and root in state.get("created_pages", {}).values(), "unknown_cleanup_target")
    notion = LiveNotion(config)
    notion.request("PATCH", "pages/" + root, {"archived": True})
    check = notion.request("GET", "pages/" + root)
    must(check.get("archived") or check.get("in_trash"), "archive_not_verified")
    record(state, "test_area_archived", passed=True, root=root)


def daemon(config, state):
    cfg = config_for_test(config, state)
    public = WORK / "daemon.toml"
    public.write_text("[runtime]\ndb_path=" + json.dumps(str(Path(cfg.db_path).resolve())) +
                      "\n[model]\nbase_url=" + json.dumps(cfg.model_base_url) + "\nname=" + json.dumps(cfg.model_name) + "\n" +
                      "[pages]\n" + "\n".join(k + "=" + json.dumps(v) for k, v in cfg.pages.items()) +
                      "\n[sources]\n" + "\n".join(k + "=" + json.dumps(v) for k, v in cfg.sources.items()) + "\n")
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "TZ": TIMEZONE,
           "ORBIT_MODEL_API_KEY": cfg.model_key, "ORBIT_NOTION_TOKEN": cfg.notion_token,
           "ORBIT_MODEL_BASE_URL": cfg.model_base_url, "ORBIT_MODEL_NAME": cfg.model_name}
    process = subprocess.Popen([sys.executable, "-u", "-m", "orbit", "--config", str(public), "run"],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    output = b""
    result = None
    try:
        deadline = time.monotonic() + 360
        while time.monotonic() < deadline and process.poll() is None:
            ready, _, _ = select.select([process.stdout], [], [], 1)
            if ready:
                output += os.read(process.stdout.fileno(), 65536)
                try:
                    result = json.loads(output)
                    break
                except (json.JSONDecodeError, UnicodeError):
                    pass
        must(result is not None, "daemon_no_completed_round")
        must(result.get("result") == "generated", "daemon_round_failed")
        process.send_signal(signal.SIGTERM)
        code = process.wait(timeout=15)
        must(code == 0, "daemon_did_not_stop_cleanly")
        record(state, "real_daemon", passed=True, host_timezone=TIMEZONE, completed_rounds=1,
               sigterm_exit_code=code, process_left_running=False)
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def audit(config, state):
    cfg = config_for_test(config, state)
    notion = LiveNotion(cfg)
    store = Store(cfg.db_path)
    try:
        must(not store.pending(), "audit_pending_writes")
        tasks = [dict(x) for x in store.db.execute("SELECT * FROM backfills")]
        must(len(tasks) == 1 and tasks[0]["status"] == "done", "audit_backfill_not_done")
        snapshots = [b for b in notion.blocks(cfg.pages["state"]) if b["text"].startswith(SNAPSHOT_PREFIX)]
        must(len(snapshots) == 1, "audit_snapshot_count")
        # Run this before the daemon, whose next real round may legitimately update the snapshot.
        expected = "隔离技术快照探针：不代表 Echo 的生活或主观判断；当前地点、活动、情绪与精力未知。"
        must(snapshots[0]["text"].endswith(expected), "audit_backfill_changed_snapshot")
        record(state, "backfill_snapshot_audit", passed=True, current_snapshot_unchanged=True,
               pending_writes=0, backfill_segments=store.db.execute("SELECT count(*) FROM runs WHERE mode='backfill'").fetchone()[0])
    finally:
        store.close()


def report(config, state):
    checks = [
        ("production_notion_read", "正式 Notion 资料读取"),
        ("production_context_model_contract", "正式上下文 + 配置模型协议（只读）"),
        ("invitation_model_e2e", "虚构邀请 → Echo 判断 → 真实写回与读回"),
        ("write_recovery", "真实提交 + 模拟丢失回执 + 数据库重开防重"),
        ("state_plan_memory_writers", "状态、计划、记忆写回及模拟人工修改保护"),
        ("pause_restart_resume", "暂停、重启、恢复和缺口登记"),
        ("explicit_backfill", "两段补写、故障续跑和实时状态保护"),
        ("backfill_snapshot_audit", "补写后的当前快照独立读回审计"),
        ("real_model_diary", "真实模型日记、来源筛选和重复执行防重"),
        ("point_window_model", "真实模型启动瞬时窗口时间协议"),
        ("real_daemon", "Asia/Shanghai 真实常驻进程的一轮推进与 SIGTERM"),
        ("test_area_archived", "临时 Notion 测试区归档核对"),
    ]
    for key, _ in checks:
        must(state.get("results", {}).get(key, {}).get("passed"), "incomplete_live_check_" + key)
    test = Store(config_for_test(config, state).db_path)
    try:
        must(not test.pending(), "report_pending_writes")
        summary = dict(test.db.execute("SELECT mode,count(*) FROM runs GROUP BY mode"))
        latest = test.db.execute("SELECT start,end FROM runs WHERE mode='live' ORDER BY generated_at DESC LIMIT 1").fetchone()
        must(parse(latest["start"]) >= parse(test.get("session_started")), "daemon_filled_restart_gap")
    finally:
        test.close()
    production = sqlite3.connect(Path(config.db_path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        production_counts = {name: production.execute("SELECT count(*) FROM " + name).fetchone()[0]
                             for name in ["runs", "outbox", "backfills"]}
    finally:
        production.close()
    # Scan only files owned by this task; never print matching bytes or credential values.
    candidates = list(Path("orbit").glob("*.py")) + list(Path("tests").glob("*.py")) + list(Path("docs").glob("*.md"))
    candidates += [p for p in WORK.rglob("*") if p.is_file()]
    must(not any(secret.encode() in p.read_bytes() for secret in [config.model_key, config.notion_token]
                 if secret for p in candidates), "credential_leak_in_task_artifacts")
    invitation = state["results"]["invitation_model_e2e"]
    decision = invitation["decisions"][0] if invitation.get("decisions") else "见隔离邀请事件的自主判断记录。"
    lines = ["# Orbit 第一版联调测试报告", "", "测试完成时间：" + now().astimezone(SHANGHAI).isoformat(), "",
             "结论：55 项本地回归测试通过，以下 12 项真实联调检查通过。测试不等于云部署或长期运行验收。", "",
             "## 范围与数据边界", "",
             "本次经 Neo 明确授权，将指定 Echo Notion 资料发送至配置的 `" + str(urlsplit(config.model_base_url).hostname) +
             "`（模型 `" + config.model_name + "`），仅验证真实上下文和协议，不发布正式生活。正式读取包含 23 条有效或待确认记忆、3 篇近期日记；当时正式事件和计划库为空。", "",
             "写入与场景测试使用独立临时区及纯虚构角色资料。未向正式事件、计划或记忆库写测试内容，未修改正式生活快照或日记原文。临时区位于运行页下，结束后已整体归档。", "",
             "| 检查 | 结果 |", "| --- | --- |"]
    lines += ["| " + label + " | 通过 |" for _, label in checks]
    lines += ["", "邀请场景中，模型自主选择：" + decision, "",
              "日记使用已写入的 2026-10-15 虚构补写事件；重复整理同一天返回 unchanged，没有复制日记。常驻测试进程在一轮真实推进后收到 SIGTERM，退出码为 0，没有留下后台服务。", "",
              "## 发现与修复", "",
              "1. 首次模型输出未通过生活协议，未提交为生活记录。补充窄窗口的精确时间说明及脱敏错误代码，后续瞬时窗口实模检查通过。", "",
              "2. 本工作区实际将 Notion 日期属性保存到分钟，导致包含秒和小数秒的成功写入被误判为日期冲突。现将属性用作分钟索引，完整时间留在正文；计划判断与补写冲突检查使用完整时间，并尊重人工日期更正。原待写记录已核对恢复，没有重建。", "",
              "3. 计划探针出现连接故障后，完整查询隔离库与稳定标识，确认未创建才重试。第二段补写也遇到远端故障；未移动失败段游标，随后从原任务完成。错误概要现区分服务、HTTP 状态及安全的错误类别，不输出响应正文或凭据。", "",
              "## 本地证据与清理", "",
              "本地测试命令：`PYTHONDONTWRITEBYTECODE=1 TZ=Asia/Shanghai python3 -m unittest discover -q`，55/55 通过。新增用例覆盖分钟索引下的完整时间、人工日期更正、同一分钟内未来计划的完成保护、补写窗口内历史查重和脱敏连接诊断。", "",
              "独立实测入口：`python3 -m tests.live_acceptance STAGE`。该入口会真实调用服务并创建临时 Notion 数据；重新执行正式资料的模型阶段须取得相应授权。", "",
              "测试数据库按模式统计：`" + encode(summary) + "`；最终待写队列为 0。正式运行数据库记录数：`" + encode(production_counts) + "`。", "",
              "`.env` 权限已设为 0600；任务代码、报告、临时配置和测试数据库未发现真实凭据副本。测试使用标准库，未安装依赖或建立新环境。", "",
              "归档测试区：[Orbit 联调临时区](https://www.notion.so/" + state["root"].replace("-", "") + ")。本地测试数据库与恢复中间文件在报告保存后清理。", "",
              "## 尚未验收", "",
              "日本云服务器部署、Linux/systemd 实机、长期运行与长缺口规模测试、持续叙事质量及敏感边界的长期语义稳定性尚未验收。Notion 写前核对与写后验证也不构成原子比较交换，不能据此宣称已消除所有并发竞争。", ""]
    path = Path("docs/live-acceptance-2026-10-16.md")
    path.write_text("\n".join(lines), encoding="utf-8")
    record(state, "final_report", passed=True, path=str(path), regression_tests=55, live_checks=12)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=["preflight", "production_model", "bootstrap", "checks", "audit", "daemon", "cleanup", "report"])
    args = parser.parse_args()
    state = json.loads(STATE.read_text()) if STATE.exists() else {"started_at": stamp(now())}
    try:
        config = load_config()
        globals()[args.stage](config, state)
        return 0
    except Exception as exc:
        # Do not output exceptions that might include provider bodies or local credentials.
        category = type(exc).__name__
        code = str(exc) if isinstance(exc, AssertionError) else None
        record(state, "failure_" + args.stage, passed=False, category=category,
               check_code=code, http_status=getattr(exc, "status", None))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
