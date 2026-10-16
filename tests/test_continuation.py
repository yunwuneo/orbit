"""Fictional end-to-end continuation acceptance. No external service or credentials."""

import copy
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from orbit.cli import main, parser
from orbit.config import Config, ConfigError, Continuation, load_config
from orbit.engine import Engine, values
from orbit.model import InvalidOutput, validate_continuation
from orbit.notion import Notion, Conflict, fingerprint, paragraph, rich
from orbit.scheduler import wake_seconds
from orbit.store import Store, digest, encode
from orbit.thread_schema import ThreadSchema, requirements
from orbit.threads import Threads, migrate
from orbit.timeutil import parse, stamp
from orbit.transport import RemoteError
from tests.support import Clock, EchoModel, Rest, uid


class ChainModel(EchoModel):
    def __init__(self):
        super().__init__()
        self.chain_calls = []
        self.outputs = []
        self.hook = None
        self.kind = "ask"
        self.make_plan = False

    def continue_thread(self, data):
        self.chain_calls.append(copy.deepcopy(data))
        parents = [n["id"] for n in data["causal_nodes"][-1:]]
        source = [data["causal_nodes"][-1]["page"]["id"]] if parents else [data["context"]["pages"]["identity"]["id"]]
        def node(kind, key="d"):
            return {"id": key, "kind": kind, "actor": "Echo" if kind != "external_event" else "糖糖",
                "title": "虚构事项节点", "time": data["story_window"]["end"], "external": "当前共同叙事观察",
                "provider": "Echo" if kind != "external_event" else "Orbit 共同叙事",
                "participants": ["Echo", "糖糖"], "location": None,
                "interpretation": "需要先核对" if kind == "echo_decision" else None,
                "feeling": "无明显变化" if kind == "echo_decision" else None,
                "decision": "先询问日期" if kind == "echo_decision" else None,
                "intention": None, "result": None, "sources": source,
                "boundary": "ordinary", "parent_node_ids": parents}
        base = {"protocol_version": 2, "thread_id": data["thread_id"], "expected_revision": data["expected_revision"]}
        if data["call_kind"] == "world_check":
            result = {**base, "outcome": "no_event", "event": None}
        else:
            result = {**base, "parent_node_ids": parents, "decision_kind": self.kind, "needed_information": ["出发日期"] if self.kind == "ask" else [],
                "question_asked": self.kind == "ask", "follow_up": {"kind": "wait_information", "suggested_check_at": None, "reason": "以后检查"} if self.kind == "ask" else None,
                "thread_disposition": "等待补充" if self.kind == "ask" else "请求结束" if self.kind == "decline" else "已有决定",
                "decision": node("echo_decision"), "execution_results": [], "plans": [], "plan_updates": [], "state": None, "memories": [], "proposals": []}
            if self.make_plan:
                result["plans"] = [{"title": "虚构计划", "detail": "未来共同叙事意图", "time": stamp(parse(data["story_window"]["end"]) + timedelta(seconds=600.123456)),
                                    "due": None, "source_event": "d", "people": "Echo"}]
        if self.outputs:
            custom = self.outputs.pop(0)
            result = custom(result, data, node) if callable(custom) else custom
        if self.hook:
            self.hook(result, data)
        return result


class ContinuationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="orbit-chain-")
        self.addCleanup(self.temp.cleanup)
        self.config = replace(Config(db_path=str(Path(self.temp.name) / "test.sqlite3")),
                              sources={**Config().sources, "threads": uid(900)}, continuation=Continuation(enabled=True))
        self.store = Store(self.config.db_path)
        self.addCleanup(lambda: self.store.close())
        self.clock = Clock()
        self.rest = Rest(self.config)
        self.notion = Notion(self.config, self.rest)
        self.model = ChainModel()
        self.engine = Engine(self.config, self.store, self.notion, self.model, self.clock)
        self.chain = self.engine.continuation
        self.repo = self.chain.repo
        self.chain.check_schema()
        self.chain.poll(stamp(self.clock()))
        with self.store.db:
            self.store.put("continuation_life_at", stamp(self.clock()))

    def event(self, title="同事的邀请", when=None, thread=None, parents=()):
        props = {"事件": title, "一句话概要": "拟一起旅行，日期目的地未知", "状态": "待处理", "信息性质": "共同叙事",
                 "发生时间": when, "参与者": ["糖糖"], "来源与记录者": "人工虚构输入"}
        if thread:
            props["所属事项"] = [self.repo.get(thread)["notion_page_id"]]
        if parents:
            props["前序事件"] = list(parents)
        return self.rest.add("events", self.notion.properties("events", props), [paragraph("人工原文，不得改写")])

    def tick(self, seconds=60):
        self.clock.advance(seconds)
        return self.engine.tick()

    def threads(self):
        return [t for t in self.repo.list() if t["root_node_id"]]

    def input_and_ask(self):
        source = self.event()
        self.tick()
        thread_id = self.store.db.execute("SELECT thread_id FROM thread_nodes WHERE source_page_id=?", (source,)).fetchone()[0]
        thread = self.repo.get(thread_id)
        self.assertEqual(thread["status"], "awaiting_world")
        self.assertEqual(thread["sync_status"], "verified")
        return source, thread["id"]

    def test_A01_invitation_question_new_world_reply_and_decision(self):
        source, tid = self.input_and_ask()
        def reply(out, data, node):
            if data["call_kind"] == "world_check" and data["causal_nodes"]:
                return {**out, "outcome": "event", "event": {**node("external_event", "reply"), "external": "糖糖新补充拟下周出发"}}
            return out
        self.model.hook = None
        self.model.outputs = [reply]
        self.model.kind = "accept"
        self.tick(600)
        nodes = self.repo.nodes(tid)
        self.assertEqual([n["kind"] for n in nodes], ["external_event", "echo_decision", "external_event", "echo_decision"])
        self.assertTrue(all(n["verified_at"] for n in nodes))
        self.assertEqual(self.repo.get(tid)["status"], "decided")
        self.assertEqual(values(self.notion.page(source))["一句话概要"], "拟一起旅行，日期目的地未知")
        self.assertEqual(len(self.repo.summary(tid)["edges"]), 3)
        self.assertIsNone(values(self.notion.page(nodes[2]["notion_page_id"]))["Echo的行动决定"] or None)
        self.assertTrue(all(n["executor"] == "Orbit" for n in nodes))

    def test_generated_at_uses_actual_model_completion_time(self):
        self.event()
        def delayed(_result, _data):
            self.clock.advance(11)
        self.model.hook = delayed
        self.tick()
        decision = next(n for n in self.repo.nodes(self.threads()[0]["id"]) if n["kind"] == "echo_decision")
        self.assertEqual(decision["generated_at"], stamp(self.clock()))
        run = self.store.db.execute("SELECT start,end,generated_at FROM runs WHERE id=(SELECT output_run_id FROM thread_tasks WHERE status='done' AND output_run_id IS NOT NULL ORDER BY updated_at DESC LIMIT 1)").fetchone()
        self.assertGreater(parse(run["generated_at"]), parse(run["start"]))
        self.assertEqual(run["end"], run["generated_at"])

    def test_A02_decisions_do_not_force_questions_or_emotions(self):
        for kind in ["accept", "decline", "no_action", "defer"]:
            self.model.kind = kind
            source = self.event(title="邀请" + kind)
            self.tick()
            node = self.store.db.execute("SELECT thread_id FROM thread_nodes WHERE source_page_id=?", (source,)).fetchone()
            thread = self.repo.get(node[0])
            self.assertEqual(thread["status"], "closed" if kind == "decline" else "decided")
            self.assertEqual(len(self.repo.nodes(thread["id"])), 2)
            self.assertEqual(self.store.db.execute("SELECT count(*) FROM thread_plan_links WHERE thread_id=?", (thread["id"],)).fetchone()[0], 0)

    def test_A03_human_supplement_beats_same_tick_world_task(self):
        source, tid = self.input_and_ask()
        self.event("日期补充", thread=tid, parents=[source])
        self.model.kind = "accept"
        old_world_count = sum(c["call_kind"] == "world_check" and c["thread_id"] == tid for c in self.model.chain_calls)
        self.tick(600)
        self.assertEqual(sum(c["call_kind"] == "world_check" and c["thread_id"] == tid for c in self.model.chain_calls), old_world_count)
        self.assertEqual(len(self.repo.nodes(tid)), 4)
        self.assertEqual(self.repo.get(tid)["revision"], 4)

    def test_A04_no_reply_reviews_without_fake_events(self):
        _, tid = self.input_and_ask()
        initial = len(self.repo.nodes(tid))
        for _ in range(3):
            self.tick(600)
        self.assertEqual(len(self.repo.nodes(tid)), initial + 1)  # review decision only
        self.assertEqual(self.repo.get(tid)["review_cycle"], 2)
        self.assertEqual(self.repo.get(tid)["no_reply_count"], 0)
        self.assertGreaterEqual(len(self.model.calls), 3)  # ordinary life keeps advancing
        self.assertFalse(any(n["kind"] == "external_event" and n["source_kind"] == "orbit_narrative" for n in self.repo.nodes(tid)))

    def test_A05_future_input_and_precise_due_boundary(self):
        future = self.event(when="2026-10-16T00:01:00.000001+00:00")
        # Simulate an authoritative page with a complete timestamp in its body, minute index in properties.
        self.rest.blocks[future] = [{"id": self.rest.new_id(), "type": "paragraph", "paragraph": {"rich_text": rich(t)}} for t in
                                   ["orbit:fixture", encode({"time": "2026-10-16T00:01:00.000001+00:00", "executor": "Orbit"})]]
        self.tick(60)
        self.assertFalse(self.store.db.execute("SELECT 1 FROM thread_nodes WHERE source_page_id=?", (future,)).fetchone())
        self.tick(0.000001)
        # Poll has a separate clock; it does not eagerly consume between polls.
        self.assertFalse(self.store.db.execute("SELECT 1 FROM thread_nodes WHERE source_page_id=?", (future,)).fetchone())
        self.tick(60)
        self.assertTrue(self.store.db.execute("SELECT 1 FROM thread_nodes WHERE source_page_id=?", (future,)).fetchone())

    def test_A06_failing_thread_does_not_block_other_input(self):
        first, second = self.event("第一事项"), self.event("第二事项")
        original = self.notion.apply
        def fail_one(key, op, sent, mark):
            if op.get("source") == "threads" and op["values"]["原始事件"] == [first]:
                raise RemoteError("Notion", 429)
            return original(key, op, sent, mark)
        with patch.object(self.notion, "apply", side_effect=fail_one):
            self.tick()
        first_thread = self.store.db.execute("SELECT thread_id FROM thread_nodes WHERE source_page_id=?", (first,)).fetchone()[0]
        second_thread = self.store.db.execute("SELECT thread_id FROM thread_nodes WHERE source_page_id=?", (second,)).fetchone()[0]
        self.assertEqual(len(self.repo.nodes(first_thread)), 1)
        self.assertEqual(len(self.repo.nodes(second_thread)), 2)
        self.assertEqual(self.repo.get(second_thread)["sync_status"], "verified")

    def test_A07_lost_ack_restart_reuses_output(self):
        self.event()
        self.rest.lose_next_create = True
        self.tick()
        created = self.rest.post_count
        self.assertEqual(len(self.model.chain_calls), 0)
        self.store.close()
        self.store = Store(self.config.db_path)
        self.engine = Engine(self.config, self.store, self.notion, self.model, self.clock)
        self.chain, self.repo = self.engine.continuation, self.engine.continuation.repo
        self.tick(60)
        self.assertEqual(len(self.model.chain_calls), 1)
        self.assertEqual(len(self.threads()), 1)
        self.assertEqual(self.rest.post_count, created + 1)  # the decision, no duplicate thread

    def test_A08_invalid_world_subjectivity_and_future_output(self):
        _, tid = self.input_and_ask()
        before = len(self.repo.nodes(tid))
        def bad(out, data, node):
            result = node("external_event", "bad")
            result["feeling"] = "强制高兴"
            return {**out, "outcome": "event", "event": result}
        self.model.outputs = [bad]
        self.tick(600)
        self.assertEqual(len(self.repo.nodes(tid)), before)
        task = self.store.db.execute("SELECT * FROM thread_tasks WHERE thread_id=? AND status='pending'", (tid,)).fetchone()
        self.assertEqual(task["error_code"], "world_subjective_fields")
        self.assertNotIn("强制高兴", encode(self.store.status()))

    def test_A09_edit_during_model_invalidates_output(self):
        self.event()
        def edit(out, data):
            self.rest.pages[self.config.pages["identity"]]["last_edited_time"] = self.rest.edited()
        self.model.hook = edit
        self.tick()
        tid = self.threads()[0]["id"]
        self.assertEqual(len(self.repo.nodes(tid)), 1)
        self.assertEqual(self.repo.get(tid)["sync_status"], "blocked")

    def test_A10_root_outside_recent_limit_is_refetched(self):
        source, tid = self.input_and_ask()
        for i in range(15):
            self.event("无关旧事件" + str(i))
        # Explicitly reference root; ordinary recent history cannot be relied on.
        self.event("补充", thread=tid)
        self.model.kind = "accept"
        self.tick()
        call = [c for c in self.model.chain_calls if c["thread_id"] == tid][-1]
        self.assertIn(source, [n["page"]["id"] for n in call["causal_nodes"]])

    def test_A11_same_titles_do_not_merge_and_ambiguous_relation_blocks(self):
        self.event("相同邀请")
        self.event("相同邀请")
        self.tick()
        self.assertEqual(len(self.threads()), 2)
        a, b = self.threads()
        page = self.event("歧义")
        self.rest.pages[page]["properties"]["所属事项"] = {"type": "relation", "relation": [{"id": a["notion_page_id"]}, {"id": b["notion_page_id"]}]}
        self.tick()
        self.assertFalse(self.store.db.execute("SELECT 1 FROM thread_nodes WHERE source_page_id=?", (page,)).fetchone())
        self.assertEqual(self.store.get("continuation_input_error")["error_code"], "Conflict")

    def test_A12_plan_completion_depends_on_actual_result(self):
        self.model.kind, self.model.make_plan = "accept", True
        source = self.event()
        self.tick()
        tid = self.threads()[0]["id"]
        plan = self.store.db.execute("SELECT plan_page_id FROM thread_plan_links WHERE thread_id=?", (tid,)).fetchone()[0]
        self.assertEqual(values(self.notion.page(plan))["状态"], "待执行")
        self.model.make_plan = False
        def complete(out, data, node):
            if data["call_kind"] != "plan_review":
                return out
            out["decision_kind"] = "act"
            result = node("execution_result", "r")
            result.update(result="共同叙事中完成本次活动", parent_node_ids=["d"])
            out["execution_results"] = [{"node": result, "plan_id": plan, "decision_id": "d"}]
            out["plan_updates"] = [{"plan_id": plan, "status": "已完成", "source_event": "r", "reason": "已有当前结果"}]
            return out
        self.tick(600.123455)
        self.assertEqual(values(self.notion.page(plan))["状态"], "待执行")
        self.model.outputs = [complete]
        self.tick(0.000001)
        self.assertEqual(values(self.notion.page(plan))["状态"], "已完成")
        results = [n for n in self.repo.nodes(tid) if n["kind"] == "execution_result"]
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["verified_at"])
        self.assertIn("人工", self.rest.blocks[source][0]["paragraph"]["rich_text"][0]["text"]["content"])

    def test_A13_pause_restart_no_missed_reply_replay(self):
        _, tid = self.input_and_ask()
        with self.store.db:
            self.store.pause(stamp(self.clock()))
        count = len(self.model.chain_calls)
        self.tick(7200)
        self.assertEqual(len(self.model.chain_calls), count)
        self.store.resume(stamp(self.clock()))
        self.tick(0)
        worlds = [c for c in self.model.chain_calls[count:] if c["thread_id"] == tid and c["call_kind"] == "world_check"]
        self.assertLessEqual(len(worlds), 1)
        self.assertTrue(all(c["story_window"]["start"] == stamp(self.clock()) for c in worlds))
        self.assertTrue(self.store.status()["gaps"])
        self.clock.advance(-1)
        with self.assertRaises(ValueError):
            self.engine.tick()

    def test_A14_upgrade_baseline_and_explicit_adoption(self):
        old = self.event()
        with self.store.db:
            self.store.db.execute("DELETE FROM meta WHERE key='continuation_enabled_at'")
        self.chain.poll(stamp(self.clock()))
        self.tick()
        self.assertFalse(self.store.db.execute("SELECT 1 FROM thread_nodes WHERE source_page_id=?", (old,)).fetchone())
        tid = self.chain.adopt(old, stamp(self.clock()))
        self.tick()
        self.assertEqual(len(self.repo.nodes(tid)), 2)
        self.assertEqual(self.chain.adopt(old, stamp(self.clock())), tid)
        consumed = self.event()
        with self.store.db:
            self.store.db.execute("INSERT INTO consumed VALUES (?,?)", (consumed, fingerprint(self.notion.page(consumed))))
        with self.assertRaises(Conflict):
            self.chain.adopt(consumed, stamp(self.clock()))

    def test_A15_schema_plan_is_read_only_and_type_conflict_preserved(self):
        self.rest.schemas["events"].pop("前序事件")
        before = len(self.rest.calls)
        result = ThreadSchema(self.notion, self.store).plan()
        self.assertIn("前序事件", result["sources"]["events"]["add"])
        self.assertTrue(all(method in {"GET", "POST"} and (method != "POST" or path.endswith("/query")) for method, path in self.rest.calls[before:]))
        self.rest.lose_next_schema = True
        with self.assertRaises(RemoteError):
            ThreadSchema(self.notion, self.store).apply()
        self.assertTrue(ThreadSchema(self.notion, self.store).apply()["verified"])
        self.rest.schemas["events"]["前序事件"] = {"type": "rich_text", "rich_text": {}}
        with self.assertRaises(Conflict):
            ThreadSchema(self.notion, self.store).apply()
        self.assertEqual(self.rest.schemas["events"]["前序事件"]["type"], "rich_text")

    def test_A16_backfill_does_not_change_current_thread(self):
        _, tid = self.input_and_ask()
        before = self.repo.summary(tid)
        end = stamp(self.clock())
        start = stamp(parse(end) - timedelta(minutes=10))
        with self.store.db:
            self.store.gap(start, end, "test_gap")
        task = self.store.add_backfill(start, end)
        self.engine.backfill(task)
        self.assertEqual(self.repo.summary(tid), before)

    def test_A17_prune_preserves_causal_receipts_and_diary_excludes_no_reply(self):
        _, tid = self.input_and_ask()
        self.tick(600)
        self.store.prune(stamp(parse(stamp(self.clock())) + timedelta(days=40)), 30)
        self.assertTrue(all(n["notion_page_id"] for n in self.repo.nodes(tid)))
        for node in self.repo.nodes(tid):
            if node["write_id"]:
                self.assertTrue(self.store.db.execute("SELECT 1 FROM outbox WHERE id=?", (node["write_id"],)).fetchone())
        self.clock.advance(86400)
        self.engine.diary("2026-10-16")
        self.assertTrue(all("no_event" not in p["content"] for p in self.model.diaries[-1]["events"]))

    def test_A18_poll_rate_and_admin_recheck_idempotent(self):
        _, tid = self.input_and_ask()
        key = self.repo.recheck(tid, stamp(self.clock()))
        self.assertEqual(key, self.repo.recheck(tid, stamp(self.clock())))
        self.store.pause(stamp(self.clock()))
        count = len(self.model.chain_calls)
        self.tick()
        self.assertEqual(count, len(self.model.chain_calls))
        self.store.resume(stamp(self.clock()))
        for _ in range(8):
            self.tick(60)
        self.assertLessEqual(len(self.model.calls), 1)
        self.assertGreaterEqual(wake_seconds(self.engine), 1)


    def test_lost_receipts_at_each_publication_boundary(self):
        for target in ["decision", "projection", "root_relation", "plan"]:
            with self.subTest(target=target):
                source = self.event(title=target)
                self.model.kind, self.model.make_plan = "accept", target == "plan"
                original = self.notion.apply
                fired = []
                def lose(key, op, sent, mark):
                    result = original(key, op, sent, mark)
                    hit = ((target == "decision" and op.get("source") == "events" and op.get("kind") == "record")
                           or (target == "projection" and op.get("kind") == "projection")
                           or (target == "root_relation" and op.get("refresh_node"))
                           or (target == "plan" and op.get("thread_plan")))
                    if hit and not fired:
                        fired.append(key)
                        raise RemoteError("Notion")
                    return result
                with patch.object(self.notion, "apply", side_effect=lose):
                    self.tick()
                self.assertTrue(fired)
                model_count, creates = len(self.model.chain_calls), self.rest.post_count
                self.clock.advance(10)
                self.engine.flush()
                self.chain.reconcile(stamp(self.clock()))
                self.engine.flush()
                self.assertEqual(self.rest.post_count, creates)
                # A lost relationship/initial projection may have deferred the first judgment.
                self.tick(60)
                thread_id = self.store.db.execute("SELECT thread_id FROM thread_nodes WHERE source_page_id=?", (source,)).fetchone()[0]
                self.assertEqual(len(self.repo.nodes(thread_id)), 2)
                self.assertEqual(len([n for n in self.repo.nodes(thread_id) if n["kind"] == "echo_decision"]), 1)
                self.assertLessEqual(len(self.model.chain_calls) - model_count, 1)
                self.assertFalse(self.store.pending())

    def test_generated_root_requires_receipts_and_relationship_before_echo(self):
        def root(out, data, node):
            return {**out, "outcome": "event", "event": node("external_event", "invitation")}
        self.model.outputs = [root]
        self.tick(600)
        tid = self.threads()[0]["id"]
        self.assertEqual(len(self.repo.nodes(tid)), 1)
        root_node = self.repo.nodes(tid)[0]
        self.assertEqual(values(self.notion.page(root_node["notion_page_id"]))["所属事项"], [self.repo.get(tid)["notion_page_id"]])
        self.tick(60)
        self.assertEqual(len(self.repo.nodes(tid)), 2)

    def test_projection_notes_survive_and_managed_edits_are_not_overwritten(self):
        _, tid = self.input_and_ask()
        thread_page = self.repo.get(tid)["notion_page_id"]
        self.rest.blocks[thread_page].append({"id": self.rest.new_id(), **paragraph("人工事项备注")})
        self.tick(600)
        self.assertIn("人工事项备注", [b["text"] for b in self.notion.page(thread_page)["blocks"]])
        managed = next(b for b in self.rest.blocks[thread_page] if ''.join(t['text']['content'] for t in b['paragraph']['rich_text']).startswith("Orbit 事项进度"))
        managed['paragraph']['rich_text'] = rich("Orbit 事项进度\n人工更正")
        self.tick(600)
        self.assertEqual(self.repo.get(tid)["sync_status"], "blocked")
        self.assertIn("人工更正", encode(self.notion.page(thread_page)))

    def test_human_edits_supersede_versions_and_history_remains(self):
        source, tid = self.input_and_ask()
        old = self.repo.nodes(tid)[0]
        self.rest.pages[source]["properties"]["一句话概要"] = {"type": "rich_text", "rich_text": rich("人工补充新日期")}
        self.rest.pages[source]["last_edited_time"] = self.rest.edited()
        self.model.kind = "accept"
        self.tick()
        nodes = self.repo.nodes(tid)
        self.assertEqual(len(nodes), 4)
        self.assertEqual(nodes[0]["source_version"], old["source_version"])
        self.assertNotEqual(nodes[0]["source_version"], nodes[2]["source_version"])
        self.assertTrue(any(e["relation"] == "supersedes" for e in self.repo.summary(tid)["edges"]))
        self.assertEqual(self.repo.get(tid)["status"], "decided")

    def test_cancel_delete_and_permission_loss_only_block_the_affected_thread(self):
        for action in ["cancel", "delete", "permission"]:
            source, tid = self.input_and_ask()
            if action == "cancel":
                self.rest.pages[source]["properties"]["状态"] = {"type": "select", "select": {"name": "已取消"}}
            elif action == "delete":
                self.rest.pages[source]["archived"] = True
            original = self.notion.page
            def page(page_id):
                if action == "permission" and page_id == source:
                    raise RemoteError("Notion", 403, retryable=False)
                return original(page_id)
            self.model.kind = "accept"
            with patch.object(self.notion, "page", side_effect=page):
                # Direct per-thread poll failure must be isolated before general context is read.
                try:
                    self.tick()
                except (Conflict, RemoteError):
                    pass
            self.assertEqual(self.repo.get(tid)["sync_status"], "blocked")
            self.assertEqual(len(self.repo.nodes(tid)), 2)
            # Restore fixture for the next independent subcase.
            self.rest.pages[source].pop("archived", None)
            self.model.kind = "ask"

    def test_atomic_output_commit_rolls_back_revision_nodes_and_effects(self):
        self.event()
        original = self.store.stage
        def fail(run, operations, consumed=()):
            original(run, operations, consumed)
            if run["evidence"].get("call_kind"):
                raise sqlite3.OperationalError("injected commit failure")
        with patch.object(self.store, "stage", side_effect=fail):
            with self.assertRaises(sqlite3.OperationalError):
                self.tick()
        tid = self.threads()[0]["id"]
        self.assertEqual(self.repo.get(tid)["revision"], 1)
        self.assertEqual(len(self.repo.nodes(tid)), 1)
        self.store.recover(stamp(self.clock()))
        self.tick()
        self.assertEqual(len(self.repo.nodes(tid)), 2)

    def test_new_supplement_marked_occurred_is_found_by_thread_relation(self):
        _, tid = self.input_and_ask()
        page = self.event("已发生的新补充", thread=tid)
        self.rest.pages[page]["properties"]["状态"] = {"type": "select", "select": {"name": "已发生"}}
        self.model.kind = "accept"
        self.tick()
        self.assertEqual(len(self.repo.nodes(tid)), 4)
        root = self.repo.nodes(tid)[0]['notion_page_id']
        self.assertEqual(values(self.notion.page(page))['前序事件'], [root])

    def test_future_linked_supplement_is_not_used_or_treated_as_conflict(self):
        _, tid = self.input_and_ask()
        self.event("未来补充", when="2026-10-17T00:00:00+00:00", thread=tid)
        self.tick(600)
        self.assertNotEqual(self.repo.get(tid)["sync_status"], "blocked")
        call = [c for c in self.model.chain_calls if c["thread_id"] == tid][-1]
        self.assertFalse(any(p['properties'].get('事件') == "未来补充" for p in call['context']['events']))

    def test_plan_manual_postponement_and_cancellation_preserve_body(self):
        self.model.kind, self.model.make_plan = "accept", True
        self.event()
        self.tick()
        tid = self.threads()[0]["id"]
        plan = self.store.db.execute("SELECT plan_page_id FROM thread_plan_links WHERE thread_id=?", (tid,)).fetchone()[0]
        self.model.make_plan = False
        self.rest.blocks[plan].append({"id": self.rest.new_id(), **paragraph("人工计划原文")})
        self.rest.pages[plan]["properties"]["计划时间"] = {"type": "date", "date": {"start": "2026-10-18T00:00:00+00:00"}}
        self.tick(601)
        self.assertFalse(any(c['call_kind'] == 'plan_review' for c in self.model.chain_calls))
        self.rest.pages[plan]["properties"]["状态"] = {"type": "select", "select": {"name": "已取消"}}
        self.tick(2 * 86400)
        self.assertEqual(values(self.notion.page(plan))["状态"], "已取消")
        self.assertIn("人工计划原文", encode(self.notion.page(plan)))

    def test_unacknowledged_result_blocks_plan_completion(self):
        self.model.kind, self.model.make_plan = "accept", True
        self.event()
        self.tick()
        tid = self.threads()[0]["id"]
        plan = self.store.db.execute("SELECT plan_page_id FROM thread_plan_links WHERE thread_id=?", (tid,)).fetchone()[0]
        self.model.make_plan = False
        def finish(out, data):
            if data["call_kind"] != "plan_review":
                return
            out["decision_kind"] = "act"
            result = copy.deepcopy(out["decision"])
            result.update(id="r", kind="execution_result", result="当前完成", interpretation=None, feeling=None, decision=None, intention=None, parent_node_ids=["d"])
            out["execution_results"] = [{"node": result, "plan_id": plan, "decision_id": "d"}]
            out["plan_updates"] = [{"plan_id": plan, "status": "已完成", "source_event": "r", "reason": "当前结果"}]
        self.model.hook = finish
        original = self.notion.apply
        def fail_result(key, op, sent, mark):
            if op.get("source") == "events" and op.get("values", {}).get("节点类型") == "执行结果":
                raise RemoteError("Notion", 429)
            return original(key, op, sent, mark)
        with patch.object(self.notion, "apply", side_effect=fail_result):
            self.tick(601)
        self.assertEqual(values(self.notion.page(plan))["状态"], "待执行")
        count = len(self.model.chain_calls)
        self.clock.advance(10)
        self.engine.flush()
        self.assertEqual(values(self.notion.page(plan))["状态"], "已完成")
        self.assertEqual(len(self.model.chain_calls), count)

    def test_completion_protocol_rejects_intention_and_unknown_sources(self):
        self.event()
        self.model.kind = "accept"
        def bad(out, data):
            out['decision']['sources'] = [uid(999999)]
        self.model.hook = bad
        self.tick()
        tid = self.threads()[0]["id"]
        self.assertEqual(len(self.repo.nodes(tid)), 1)
        self.assertEqual(self.store.db.execute("SELECT error_code FROM thread_tasks WHERE thread_id=?", (tid,)).fetchone()[0], "event_sources")

    def test_recovery_coalesces_overdue_world_and_plan_into_current_review(self):
        _, tid = self.input_and_ask()
        revision = self.repo.get(tid)['revision']
        with self.store.db:
            self.repo.task(tid, "plan_review", "plan:fake:overdue", revision, stamp(self.clock()), stamp(self.clock()))
        before = len(self.model.chain_calls)
        self.clock.advance(86400)
        self.store.recover(stamp(self.clock()))
        self.tick(0)
        calls = [c for c in self.model.chain_calls[before:] if c['thread_id'] == tid]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['call_kind'], 'echo_decision')
        self.assertTrue(calls[0]['task_cause'].startswith('recovery:'))
        self.assertGreaterEqual(self.repo.get(tid)['missed_checks'], 2)

    def test_dependency_budget_and_no_reply_projection_dirty_without_revision_bump(self):
        _, tid = self.input_and_ask()
        revision = self.repo.get(tid)['revision']
        self.tick(600)
        thread = self.repo.get(tid)
        self.assertEqual(thread['revision'], revision)
        self.assertEqual(thread['published_revision'], revision)
        self.assertFalse(thread['projection_dirty'])
        page = self.notion.page(thread['notion_page_id'])
        self.assertIn('"no_reply_count":1', encode(page).replace('\\"', '"'))

    def test_long_chain_compresses_older_pages_with_versions_and_preserves_root(self):
        self.model.kind = 'accept'
        source = self.event()
        self.tick()
        tid = self.threads()[0]['id']
        for index in range(5):
            page = self.event('补充' + str(index), thread=tid)
            self.rest.blocks[page].append({'id': self.rest.new_id(), **paragraph('旧的普通生活细节' * 1200)})
            self.tick()
        cfg = replace(self.config, max_context_chars=40000)
        self.engine.config = self.notion.config = self.chain.config = cfg
        self.repo.recheck(tid, stamp(self.clock()))
        self.tick(1)
        call = [c for c in self.model.chain_calls if c['thread_id'] == tid][-1]
        self.assertLessEqual(len(encode(call)), cfg.max_context_chars)
        self.assertEqual(call['causal_nodes'][0]['page']['id'], source)
        self.assertTrue(any('compression' in n and n['page'].get('version') for n in call['causal_nodes']))
        self.assertIn('content', call['causal_nodes'][0]['page'])
        self.assertIn('content', call['causal_nodes'][-1]['page'])

    def test_manual_check_time_edit_blocks_before_model_without_overwrite(self):
        _, tid = self.input_and_ask()
        thread_page = self.repo.get(tid)['notion_page_id']
        self.rest.pages[thread_page]['properties']['下次检查'] = {'type': 'date', 'date': {'start': '2026-10-18T00:00:00+00:00'}}
        count = len([c for c in self.model.chain_calls if c['thread_id'] == tid])
        self.tick(600)
        self.assertEqual(self.repo.get(tid)['sync_status'], 'blocked')
        self.assertEqual(count, len([c for c in self.model.chain_calls if c['thread_id'] == tid]))
        self.assertEqual(values(self.notion.page(thread_page))['下次检查']['start'], '2026-10-18T00:00:00+00:00')

    def test_disabled_generation_still_restores_persisted_plan_receipts(self):
        self.model.kind, self.model.make_plan = 'accept', True
        self.event()
        original = self.notion.apply
        def fail_plan(key, op, sent, mark):
            if op.get('thread_plan'):
                raise RemoteError('Notion', 429)
            return original(key, op, sent, mark)
        with patch.object(self.notion, 'apply', side_effect=fail_plan):
            self.tick()
        tid = self.threads()[0]['id']
        disabled = replace(self.config, continuation=Continuation(enabled=False))
        engine = Engine(disabled, self.store, self.notion, self.model, self.clock)
        self.clock.advance(10)
        count = len(self.model.chain_calls)
        engine.flush()
        self.assertEqual(len(self.model.chain_calls), count)
        self.assertTrue(self.store.db.execute('SELECT 1 FROM thread_plan_links WHERE thread_id=?', (tid,)).fetchone())
        self.assertTrue(self.store.db.execute("SELECT 1 FROM thread_tasks WHERE thread_id=? AND kind='plan_review'", (tid,)).fetchone())

    def test_v2_rejects_future_actor_and_unrecognized_fields(self):
        for mutation, code in [(lambda o: o['decision'].update(time='2026-10-18T00:00:00+00:00'), 'event_outside_story_window'),
                               (lambda o: o['decision'].update(actor='Orbit'), 'decision_subject'),
                               (lambda o: o.update(force_accept=True), 'object_shape')]:
            self.event()
            self.model.hook = lambda out, data: mutation(out)
            self.tick()
            thread = self.threads()[-1]
            self.assertEqual(len(self.repo.nodes(thread['id'])), 1)
            self.assertTrue(self.store.db.execute('SELECT 1 FROM thread_tasks WHERE thread_id=? AND error_code=?', (thread['id'], code)).fetchone())
            # Keep retries from consuming later synthetic mutations.
            self.store.db.execute("UPDATE thread_tasks SET status='blocked' WHERE thread_id=?", (thread['id'],))
            self.store.db.commit()

    def test_unready_shared_resource_stays_ordered_across_runs(self):
        current = stamp(self.clock())
        page = self.config.pages['state']
        first, second = self.chain.run(current), self.chain.run(current)
        with self.store.db:
            self.store.stage(first, [{'kind': 'append', 'page_id': page, 'text': 'first'}])
            self.store.stage(second, [{'kind': 'append', 'page_id': page, 'text': 'second'}])
        first_id, second_id = 'orbit:' + first['id'] + ':0', 'orbit:' + second['id'] + ':0'
        self.assertFalse(self.store.write_ready(second_id))
        self.assertTrue(self.store.write_ready(first_id))
        self.engine.flush()
        self.assertTrue(self.store.write_ready(second_id))
        blocks = [b['text'] for b in self.notion.page(page)['blocks']]
        self.assertTrue(blocks[-2].endswith('first'))
        self.assertTrue(blocks[-1].endswith('second'))

    def test_projection_accepts_real_notion_date_serialization_and_null_metadata(self):
        self.rest.normalize_dates = True
        _, tid = self.input_and_ask()
        self.tick(600)
        self.assertEqual(self.repo.get(tid)['sync_status'], 'verified')
        self.assertFalse(self.store.pending())
        self.assertEqual(self.repo.get(tid)['no_reply_count'], 1)

    def test_old_projection_receipt_does_not_clear_new_recovery_schedule(self):
        _, tid = self.input_and_ask()
        with self.store.db:
            self.store.db.execute('UPDATE threads SET projection_dirty=1 WHERE id=?', (tid,))
        self.chain.projection(self.repo.get(tid), stamp(self.clock()))
        self.clock.advance(7200)
        self.store.recover(stamp(self.clock()))
        self.engine.flush()
        self.assertTrue(self.repo.get(tid)['projection_dirty'])
        self.chain.reconcile(stamp(self.clock()))
        self.engine.flush()
        self.assertFalse(self.repo.get(tid)['projection_dirty'])
        self.assertEqual(self.repo.get(tid)['next_check_at'], stamp(self.clock()))


class MigrationTests(unittest.TestCase):
    def test_incremental_upgrade_preserves_legacy_rows_and_rolls_back_failure(self):
        with tempfile.TemporaryDirectory(prefix="orbit-migrate-") as temp:
            path = str(Path(temp) / "old.sqlite3")
            db = sqlite3.connect(path)
            db.executescript("CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL); INSERT INTO meta VALUES ('original','42');")
            db.close()
            store = Store(path)
            self.assertEqual(store.get("original"), 42)
            self.assertEqual(store.db.execute("PRAGMA user_version").fetchone()[0], 1)
            migrate(store.db)
            store.close()
            store = Store(path)
            self.assertEqual(store.get("original"), 42)
            store.close()
            db = sqlite3.connect(":memory:")
            db.execute("CREATE TABLE threads(original TEXT)")
            with self.assertRaises(sqlite3.OperationalError):
                migrate(db)
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertFalse(db.execute("SELECT 1 FROM sqlite_master WHERE name='thread_tasks'").fetchone())
            db.close()

    def test_schema_creation_lost_ack_reconciles_without_mutating_config(self):
        with tempfile.TemporaryDirectory(prefix="orbit-schema-") as temp:
            config = Config(db_path=str(Path(temp) / "schema.sqlite3"))
            store = Store(config.db_path)
            self.addCleanup(store.close)
            rest, notion = Rest(config), None
            notion = Notion(config, rest)
            installer = ThreadSchema(notion, store)
            self.assertTrue(installer.plan()['create_database'])
            self.assertFalse(rest.databases)
            rest.lose_next_database = True
            with self.assertRaises(RemoteError):
                installer.apply()
            self.assertEqual(len(rest.databases), 1)
            result = installer.apply()
            self.assertTrue(result['verified'])
            self.assertEqual(len(rest.databases), 1)
            self.assertNotIn('threads', config.sources)
            self.assertEqual(installer.apply()['threads_source_id'], result['threads_source_id'])

    def test_config_and_cli_entries(self):
        with tempfile.TemporaryDirectory(prefix="orbit-config-") as temp:
            path = Path(temp) / "config.toml"
            path.write_text('[continuation]\nenabled=true\ninput_poll_seconds=60\n')
            self.assertTrue(load_config(path).continuation.enabled)
            for content in ['[continuation]\nenabled="yes"', '[continuation]\nmax_tasks_per_tick=0', '[sources]\nthreads="invalid"']:
                path.write_text(content)
                with self.assertRaises(ConfigError):
                    load_config(path)
            path.write_text('[runtime]\ndb_path=' + json.dumps(str(Path(temp) / 'cli.sqlite3')))
            for args in [["threads"], ["status"]]:
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(main(["--config", str(path), *args]), 0)
        for command in [["threads-schema", "--plan"], ["threads-schema", "--apply"], ["thread-adopt", uid(1)], ["thread-recheck", uid(2)]]:
            self.assertEqual(parser().parse_args(command).command, command[0])


if __name__ == "__main__":
    unittest.main()
