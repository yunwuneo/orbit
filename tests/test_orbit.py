import copy
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from orbit.cli import main
from orbit.config import Config, ConfigError, load_config
from orbit.engine import Engine, page_date, values
from orbit.model import InvalidOutput, Model, validate
from orbit.notion import Conflict, Notion, UncertainWrite, context_view, date_index, fingerprint, paragraph, precise_page_date, rich
from orbit.store import Busy, Store, digest
from orbit.timeutil import day, parse, stamp
from orbit.transport import RemoteError, Transport
from tests.support import Clock, EchoModel, Rest, uid


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="orbit-tests-")
        self.addCleanup(self.temp.cleanup)
        self.config = Config(db_path=str(Path(self.temp.name) / "orbit.sqlite3"))
        self.store = Store(self.config.db_path)
        self.addCleanup(lambda: self.store.close())
        self.clock = Clock()
        self.rest = Rest(self.config)
        self.notion = Notion(self.config, self.rest)
        self.model = EchoModel()
        self.engine = Engine(self.config, self.store, self.notion, self.model, self.clock)

    def add_event(self, state="待处理", when=None):
        self.notion.schema("events")
        return self.rest.add("events", self.notion.properties("events", {
            "事件": "同事邀请旅行", "状态": state, "信息性质": "共同叙事",
            "一句话概要": "询问要不要一起旅行，时间和目的地未知", "发生时间": when,
            "来源与记录者": "人工输入"}))

    def add_plan(self, scheduled):
        self.notion.schema("plans")
        return self.rest.add("plans", self.notion.properties("plans", {
            "事项": "审稿", "状态": "待执行", "计划时间": scheduled, "一句话概要": "人工安排"}))

    def test_invitation_gets_echo_decision_with_separate_provenance(self):
        source = self.add_event()
        self.assertEqual(self.engine.tick(), "generated")
        generated = [p for key, p in self.rest.pages.items() if self.rest.source.get(key) == "events" and key != source]
        self.assertEqual(len(generated), 1)
        props = values(generated[0])
        self.assertEqual(props["Echo的行动决定"], "先问时间和目的地，再作打算")
        self.assertIn("决策主体：Echo", props["来源与记录者"])
        self.assertEqual(values(self.rest.pages[source])["状态"], "待处理")
        self.clock.advance(600)
        self.assertEqual(self.engine.tick(), "generated")
        self.assertEqual(self.model.calls[-1]["pending_inputs"], [])
        self.rest.pages[source]["last_edited_time"] = self.rest.edited()
        self.clock.advance(600)
        self.engine.tick()
        self.assertEqual(self.model.calls[-1]["pending_inputs"], [source])

    def test_future_event_not_consumed(self):
        source = self.add_event("待发生", "2026-10-17T08:00:00+08:00")
        self.engine.tick()
        self.assertEqual(self.model.calls[0]["pending_inputs"], [])
        self.assertFalse(self.store.consumed(source, fingerprint(self.notion.page(source))))

    def test_model_failure_creates_no_history_or_fake_response(self):
        self.model.fail = True
        self.assertEqual(self.engine.tick(), "generation_failed")
        self.assertEqual(self.rest.post_count, 0)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM runs WHERE mode IN ('live','backfill')").fetchone()[0], 0)
        self.assertEqual(self.engine.tick(), "generation_deferred")

    def test_retry_bound_and_safe_error_summary(self):
        self.model.fail = True
        for _ in range(self.config.max_attempts):
            self.engine.tick()
            self.clock.advance(400)
        self.assertTrue(self.store.get("generation_blocked"))
        self.assertEqual(len(self.model.calls), self.config.max_attempts)
        self.assertEqual(self.store.get("last_error")["category"], "InvalidOutput")

    def test_manual_edit_during_generation_discards_output(self):
        def change(output, data):
            self.rest.pages[self.config.pages["identity"]]["last_edited_time"] = self.rest.edited()
        self.model.change = change
        self.assertEqual(self.engine.tick(), "generation_failed")
        self.assertEqual(self.rest.post_count, 0)
        self.assertTrue(self.store.get("generation_blocked"))

    def test_lost_create_ack_reconciles_without_second_create_or_model_call(self):
        self.rest.lose_next_create = True
        self.assertEqual(self.engine.tick(), "generated_writes_pending")
        self.assertEqual(self.rest.post_count, 1)
        self.clock.advance(10)
        self.assertTrue(self.engine.flush())
        self.assertEqual(self.rest.post_count, 1)
        self.assertEqual(len(self.model.calls), 1)
        self.assertFalse(self.store.pending())

    def test_lost_append_ack_preserves_manual_text_without_duplicate(self):
        self.rest.lose_next_append = True
        self.engine.tick()
        before = self.rest.append_count
        self.clock.advance(10)
        self.assertTrue(self.engine.flush())
        self.assertEqual(self.rest.append_count, before)
        blocks = self.notion.blocks(self.config.pages["operations"])
        self.assertEqual(blocks[0]["text"], "人工原文 operations")
        self.assertEqual(len(blocks), 2)

    def test_uncertain_absent_write_never_blindly_posts(self):
        with self.store.db:
            self.store.stage({"id": "a", "start": stamp(self.clock()), "end": stamp(self.clock()),
                              "mode": "live", "generated_at": stamp(self.clock()), "evidence": {}},
                             [{"kind": "append", "page_id": self.config.pages["state"], "text": "尚未确认"}])
        self.store.mark_sent("orbit:a:0")
        for _ in range(self.config.max_attempts):
            self.engine.flush()
            self.clock.advance(400)
        self.assertEqual(self.rest.append_count, 0)
        self.assertEqual(self.store.pending()[0]["status"], "blocked")
        self.assertEqual(self.store.pending()[0]["error"], "uncertain_write")

    def test_definite_rate_limit_can_retry_with_no_duplicate(self):
        self.rest.reject_next = 429
        self.engine.tick()
        self.assertEqual(self.store.pending()[0]["sent"], 0)
        self.clock.advance(10)
        self.assertTrue(self.engine.flush())
        self.assertEqual(self.rest.post_count, 1)

    def test_pause_resume_does_not_generate_gap_or_complete_overdue_plan(self):
        self.add_plan("2026-10-16T09:00:00+08:00")
        self.engine.tick()
        self.store.pause(stamp(self.clock()))
        self.clock.advance(7200)
        calls = len(self.model.calls)
        self.assertEqual(self.engine.tick(), "paused")
        self.assertEqual(len(self.model.calls), calls)
        self.store.resume(stamp(self.clock()))
        self.engine.tick()
        window = self.model.calls[-1]["story_window"]
        self.assertEqual(window["start"], stamp(self.clock()))
        self.assertEqual(len(self.store.status()["gaps"]), 1)
        plan = next(p for key, p in self.rest.pages.items() if self.rest.source.get(key) == "plans")
        self.assertEqual(values(plan)["状态"], "待执行")

    def test_restart_recovers_queued_write_and_leaves_gap(self):
        self.rest.lose_next_create = True
        self.engine.tick()
        calls = len(self.model.calls)
        self.store.close()
        self.store = Store(self.config.db_path)
        self.engine.store = self.store
        self.clock.advance(3600)
        self.store.recover(stamp(self.clock()))
        self.assertTrue(self.engine.flush())
        self.assertEqual(len(self.model.calls), calls)
        self.assertEqual(self.rest.post_count, 1)
        self.assertEqual(len(self.store.status()["gaps"]), 1)

    def test_long_wake_delay_records_missing_interval(self):
        self.engine.tick()
        self.clock.advance(3600)
        self.engine.tick()
        gap = self.store.status()["gaps"][0]
        self.assertEqual((parse(gap["end"]) - parse(gap["start"])).total_seconds(), 3000)
        self.assertEqual((parse(self.model.calls[-1]["story_window"]["end"]) - parse(self.model.calls[-1]["story_window"]["start"])).total_seconds(), 600)

    def test_backfill_segments_resume_without_rewinding_state(self):
        self.engine.tick()
        self.clock.advance(1800)
        self.store.recover(stamp(self.clock()))
        gap = self.store.status()["gaps"][0]
        task = self.store.add_backfill(gap["start"], gap["end"])
        current = self.store.get("cursor")
        self.assertEqual(self.engine.backfill(task, 1), "segments_pending")
        self.assertEqual(self.engine.tick(), "backfill_pending")
        self.assertEqual(self.engine.backfill(task, 10), "done")
        count = len(self.model.calls)
        self.assertEqual(self.engine.backfill(task, 10), "done")
        self.assertEqual(len(self.model.calls), count)
        self.assertEqual(self.store.get("cursor"), current)
        times = [data["story_window"]["end"] for data in self.model.calls[1:]]
        self.assertEqual(times, sorted(times))
        self.assertEqual(len(times), 3)
        self.assertEqual(len(self.notion.blocks(self.config.pages["state"])), 1)

    def test_backfill_last_segment_lost_ack_resumes_done(self):
        self.store.recover(stamp(self.clock()))
        self.clock.advance(600)
        self.store.recover(stamp(self.clock()))
        gap = self.store.status()["gaps"][0]
        task = self.store.add_backfill(gap["start"], gap["end"])
        self.rest.lose_next_create = True
        self.assertEqual(self.engine.backfill(task), "writes_pending")
        self.clock.advance(10)
        self.assertEqual(self.engine.backfill(task), "done")
        self.assertEqual(len(self.model.calls), 1)

    def test_backfill_conflict_is_explicit_and_does_not_publish_life(self):
        self.store.recover(stamp(self.clock()))
        self.clock.advance(600)
        self.store.recover(stamp(self.clock()))
        gap = self.store.status()["gaps"][0]
        task = self.store.add_backfill(gap["start"], gap["end"])
        self.model.change = lambda out, data: out["conflicts"].append("与后续记录冲突")
        self.assertEqual(self.engine.backfill(task), "backfill_conflict")
        self.assertEqual(self.rest.post_count, 0)
        self.assertIn("补写冲突", self.notion.blocks(self.config.pages["operations"])[-1]["text"])
        self.assertEqual(self.engine.backfill(task), "backfill_conflict")

    def test_existing_notion_history_in_gap_prevents_overwrite(self):
        self.store.recover(stamp(self.clock()))
        self.clock.advance(600)
        self.store.recover(stamp(self.clock()))
        gap = self.store.status()["gaps"][0]
        self.add_event("已发生", "2026-10-16T00:05:00+00:00")
        task = self.store.add_backfill(gap["start"], gap["end"])
        self.assertEqual(self.engine.backfill(task), "generation_failed")
        self.assertEqual(self.model.calls, [])

    def test_future_plan_cannot_complete_even_with_result(self):
        plan = self.add_plan("2026-10-17T08:00:00+08:00")
        def change(out, data):
            out["events"][0]["result"] = "声称完成"
            out["plan_updates"] = [{"plan_id": plan, "status": "已完成", "source_event": "e1", "reason": "检查"}]
        self.model.change = change
        self.assertEqual(self.engine.tick(), "generation_failed")
        self.assertEqual(values(self.rest.pages[plan])["状态"], "待执行")

    def test_due_plan_completion_requires_result_and_preserves_original(self):
        plan = self.add_plan("2026-10-15T08:00:00+08:00")
        def change(out, data):
            out["events"][0]["result"] = "已核对三页"
            out["plan_updates"] = [{"plan_id": plan, "status": "已完成", "source_event": "e1", "reason": "本轮执行"}]
        self.model.change = change
        self.assertEqual(self.engine.tick(), "generated")
        props = values(self.rest.pages[plan])
        self.assertEqual(props["状态"], "已完成")
        self.assertEqual(props["执行结果"], "已核对三页")
        self.assertEqual(props["一句话概要"], "人工安排")
        self.assertEqual(props["计划时间"]["start"], "2026-10-15T08:00:00+08:00")

    def test_plan_changed_before_write_blocks_narrow_update(self):
        plan = self.add_plan("2026-10-15T08:00:00+08:00")
        def change(out, data):
            out["events"][0]["result"] = "已核对"
            out["plan_updates"] = [{"plan_id": plan, "status": "已完成", "source_event": "e1", "reason": "已执行"}]
        self.model.change = change
        current = stamp(self.clock())
        self.engine.generate(current, current, "live")
        self.rest.pages[plan]["last_edited_time"] = self.rest.edited()
        self.assertFalse(self.engine.flush())
        self.assertEqual(values(self.rest.pages[plan])["状态"], "待执行")
        self.assertEqual(self.store.pending()[0]["status"], "blocked")

    def test_new_plan_has_actual_notion_source_relation(self):
        def change(out, data):
            out["plans"].append({"title": "继续核对", "detail": "明天再看", "time": "2026-10-17T08:00:00+08:00",
                                 "due": None, "source_event": "e1", "people": "Echo"})
        self.model.change = change
        self.assertEqual(self.engine.tick(), "generated")
        plan = next(p for key, p in self.rest.pages.items() if self.rest.source.get(key) == "plans")
        linked = values(plan)["来源事件"][0]
        self.assertEqual(self.rest.source[linked], "events")
        self.assertEqual(values(plan)["状态"], "待执行")

    def test_notion_minute_index_retains_full_story_timestamp(self):
        self.clock.value = parse("2026-10-16T00:00:58.615419+00:00")
        self.assertEqual(self.engine.tick(), "generated")
        page = next(self.notion.page(key) for key, source in self.rest.source.items() if source == "events")
        self.assertEqual(values(page)["发生时间"]["start"], "2026-10-16T00:00:00+00:00")
        self.assertEqual(page_date(page, "发生时间"), self.clock())

    def test_manual_date_edit_overrides_precise_time_in_generated_body(self):
        self.clock.value = parse("2026-10-16T00:00:58.615419+00:00")
        self.engine.tick()
        page_id = next(key for key, source in self.rest.source.items() if source == "events")
        self.rest.pages[page_id]["properties"]["发生时间"]["date"]["start"] = "2026-10-16T00:05:00+00:00"
        self.assertEqual(page_date(self.notion.page(page_id), "发生时间"), parse("2026-10-16T00:05:00+00:00"))

    def test_backfill_finds_precise_history_when_minute_index_precedes_window(self):
        self.clock.value = parse("2026-10-16T00:00:45+00:00")
        self.store.recover(stamp(self.clock()))
        self.clock.value = parse("2026-10-16T00:00:59.999999+00:00")
        self.store.recover(stamp(self.clock()))
        gap = self.store.status()["gaps"][0]
        self.notion.schema("events")
        occurred = "2026-10-16T00:00:58.615419+00:00"
        self.rest.add("events", self.notion.properties("events", {
            "事件": "其他平台已有记录", "状态": "已发生", "发生时间": occurred}),
            [paragraph("orbit:other-writer:0"), paragraph(json.dumps({"executor": "Orbit", "time": occurred}))])
        task = self.store.add_backfill(gap["start"], gap["end"])
        self.assertEqual(self.engine.backfill(task), "generation_failed")
        self.assertEqual(self.model.calls, [])

    def test_generated_plan_within_same_minute_cannot_complete_before_precise_time(self):
        self.notion.schema("plans")
        scheduled = "2026-10-16T00:00:58.615419+00:00"
        plan_id = self.rest.add("plans", self.notion.properties("plans", {
            "事项": "稍后核对", "状态": "待执行", "计划时间": scheduled}),
            [paragraph("orbit:plan-fixture:0"), paragraph(json.dumps({"executor": "Orbit", "time": scheduled}))])
        def change(output, data):
            output["events"][0]["result"] = "声称已完成"
            output["plan_updates"] = [{"plan_id": plan_id, "status": "已完成", "source_event": "e1", "reason": "检查"}]
        self.model.change = change
        self.clock.value = parse("2026-10-16T00:00:20+00:00")
        self.assertEqual(self.engine.tick(), "generation_failed")
        self.assertEqual(values(self.rest.pages[plan_id])["状态"], "待执行")

    def state_output(self, output, data):
        output["state"] = {"source_event": "e1", "location": None, "activity": "核对安排",
                           "present": [], "emotion": "平静", "energy": None}

    def test_state_and_runtime_snapshots_are_bounded_and_keep_manual_text(self):
        self.model.change = self.state_output
        for _ in range(5):
            self.assertEqual(self.engine.tick(), "generated")
            self.clock.advance(600)
        for name in ["state", "operations"]:
            blocks = self.notion.blocks(self.config.pages[name])
            self.assertEqual(len(blocks), 2)
            self.assertEqual(blocks[0]["text"], "人工原文 " + name)
        self.assertEqual(len([s for s in self.rest.source.values() if s == "events"]), 5)
        state = self.notion.blocks(self.config.pages["state"])[1]["text"]
        self.assertIn('"story_time":"2026-10-16T00:40:00.000000+00:00"', state)

    def test_manual_snapshot_edit_after_generation_is_preserved(self):
        self.model.change = self.state_output
        self.engine.tick()
        self.clock.advance(600)
        self.engine.generate(stamp(self.clock()), stamp(self.clock()), "live")
        block = self.rest.blocks[self.config.pages["state"]][1]
        block["paragraph"]["rich_text"][0]["text"]["content"] += "\n人工更正"
        self.assertFalse(self.engine.flush())
        self.assertIn("人工更正", self.notion.blocks(self.config.pages["state"])[1]["text"])
        self.assertEqual(self.store.pending()[0]["status"], "blocked")

    def test_multiple_offline_runtime_commands_sync_in_order_without_growth(self):
        for paused in [True, False, True]:
            self.store.operational(self.config.pages["operations"], stamp(self.clock()), {"paused": paused})
        self.assertTrue(self.engine.flush())
        blocks = self.notion.blocks(self.config.pages["operations"])
        self.assertEqual(len(blocks), 2)
        self.assertIn('"paused":true', blocks[1]["text"])

    def test_completed_before_deadline_is_allowed_when_not_scheduled_in_future(self):
        self.notion.schema("plans")
        plan = self.rest.add("plans", self.notion.properties("plans", {
            "事项": "本周审稿", "状态": "待执行", "到期时间": "2026-10-18T08:00:00+08:00"}))
        def change(out, data):
            out["events"][0]["result"] = "已提前核完本轮稿件"
            out["plan_updates"] = [{"plan_id": plan, "status": "已完成", "source_event": "e1", "reason": "已执行完毕"}]
        self.model.change = change
        self.assertEqual(self.engine.tick(), "generated")
        self.assertEqual(values(self.rest.pages[plan])["状态"], "已完成")

    def test_failed_generation_error_syncs_only_to_operations(self):
        self.model.fail = True
        self.engine.tick()
        self.assertTrue(self.engine.flush())
        self.assertEqual(self.rest.post_count, 0)
        self.assertIn("InvalidOutput", self.notion.blocks(self.config.pages["operations"])[1]["text"])
        self.assertEqual(len(self.notion.blocks(self.config.pages["state"])), 1)

    def test_backfill_cancel_unblocks_realtime_without_discarding_history(self):
        self.store.recover(stamp(self.clock()))
        self.clock.advance(600)
        self.store.recover(stamp(self.clock()))
        gap = self.store.status()["gaps"][0]
        task = self.store.add_backfill(gap["start"], gap["end"])
        self.assertEqual(self.engine.tick(), "backfill_pending")
        with self.store.db:
            self.store.db.execute("UPDATE backfills SET status='cancelled' WHERE id=?", (task,))
        self.assertEqual(self.engine.backfill(task), "cancelled")
        self.assertEqual(self.engine.tick(), "generated")

    def test_memory_dedup_appends_to_existing_without_changing_confirmation(self):
        self.notion.schema("memories")
        memory_id = self.rest.add("memories", self.notion.properties("memories", {
            "记忆": "日常安排", "确认状态": "待确认", "信息性质": "推测"}), [paragraph("人工记忆原文")])
        def change(out, data):
            out["memories"].append({"title": "日常安排", "summary": "形成持续安排", "detail": "持续核对的重要线索",
                                    "category": "经历", "people": "Echo", "source_event": "e1", "existing_id": None, "boundary": "ordinary"})
        self.model.change = change
        self.assertEqual(self.engine.tick(), "generated")
        self.assertEqual(len([s for s in self.rest.source.values() if s == "memories"]), 1)
        self.assertEqual(values(self.rest.pages[memory_id])["确认状态"], "待确认")
        self.assertEqual(self.notion.blocks(memory_id)[0]["text"], "人工记忆原文")

    def test_diary_uses_shanghai_day_and_only_recorded_events(self):
        self.add_event("已发生", "2026-10-15T16:30:00+00:00")  # Shanghai 10/16
        self.add_event("待发生", "2026-10-16T01:00:00+00:00")
        self.clock.advance(24 * 3600)
        self.assertEqual(self.engine.diary("2026-10-16"), "saved")
        self.assertEqual(len(self.model.diaries[0]["events"]), 1)
        self.assertEqual(self.engine.diary("2026-10-16"), "unchanged")
        blocks = self.notion.blocks(self.config.pages["journal"])
        self.assertEqual(blocks[0]["text"], "人工原文 journal")
        self.assertIn("Echo 每日回顾", blocks[-1]["text"])

    def test_diary_has_no_automatic_fabricated_empty_day(self):
        self.assertEqual(self.engine.diary("2026-10-15"), "no_recorded_events")
        self.assertEqual(self.model.diaries, [])

    def test_schema_missing_field_fails_without_creating_record(self):
        del self.rest.schemas["events"]["Echo的行动决定"]
        self.assertEqual(self.engine.tick(), "generated_writes_pending")
        self.assertEqual(self.rest.post_count, 0)
        self.assertEqual(self.store.pending()[0]["status"], "blocked")

    def test_prune_removes_payload_only_after_verified_ack(self):
        self.engine.tick()
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM outbox WHERE payload IS NOT NULL").fetchone()[0], 0)
        self.clock.advance(40 * 86400)
        self.store.prune(stamp(self.clock()), 30)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM outbox").fetchone()[0], 0)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM runs").fetchone()[0], 1)

    def test_transactions_roll_back_every_cursor_and_write_intent(self):
        t = stamp(self.clock())
        with self.assertRaises(RuntimeError):
            with self.store.db:
                self.store.stage({"id": "rollback", "start": t, "end": t, "generated_at": t, "mode": "live", "evidence": {}},
                                 [{"kind": "append", "text": "never", "page_id": "x"}])
                raise RuntimeError()
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM runs").fetchone()[0], 0)
        self.assertEqual(self.store.pending(), [])
        self.assertIsNone(self.store.get("cursor"))

    def test_operation_and_daemon_locks_have_distinct_scope(self):
        other = Store(self.config.db_path)
        self.addCleanup(other.close)
        with self.store.lock("daemon"):
            with self.assertRaises(Busy):
                with other.lock("daemon"):
                    pass
            with other.lock():
                pass
        with self.store.lock():
            with self.assertRaises(Busy):
                with other.lock():
                    pass


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.config = Config()
        self.clock = Clock()
        rest = Rest(self.config)
        self.context = Notion(self.config, rest).context(stamp(self.clock()), stamp(self.clock()))
        self.output = EchoModel().decide({"story_window": {"end": stamp(self.clock())},
                                          "context": context_view(self.context), "pending_inputs": []})

    def test_rejects_outside_window_times_and_unproven_sources(self):
        for field, value in [("time", "2026-10-17T00:00:00+00:00"), ("time", "2026-10-15T00:00:00+00:00"),
                             ("time", "2026-10-16T00:00:00"), ("sources", ["unknown"]), ("boundary", "needs_confirmation")]:
            with self.subTest(field=field, value=value):
                output = copy.deepcopy(self.output)
                output["events"][0][field] = value
                with self.assertRaises(InvalidOutput):
                    validate(output, stamp(self.clock()), stamp(self.clock()), self.context)

    def test_rejects_unknown_fields_instead_of_silently_executing(self):
        self.output["events"][0]["relationship_delta"] = 10
        with self.assertRaises(InvalidOutput):
            validate(self.output, stamp(self.clock()), stamp(self.clock()), self.context)

    def test_model_uses_portable_contract_and_redacts_bad_responses(self):
        class HTTP:
            def request(self, service, method, url, headers, body, attempts):
                self.body = body
                return {"choices": [{"finish_reason": "stop", "message": {"content": '{"text":"ok"}'}}]}
        http = HTTP()
        model = Model(replace(self.config, model_base_url="https://provider.example/v1", model_name="configured", model_key="private-test-credential"), http)
        self.assertEqual(model.diary({}), "ok")
        self.assertEqual(set(http.body), {"model", "messages"})
        self.assertEqual(http.body["model"], "configured")

    def test_secret_echo_never_leaves_model_boundary(self):
        class HTTP:
            def request(self, *args, **kwargs):
                return {"choices": [{"message": {"content": '{"text":"private-test-credential"}'}, "finish_reason": "stop"}]}
        model = Model(replace(self.config, model_key="private-test-credential"), HTTP())
        with self.assertRaises(InvalidOutput) as error:
            model.diary({})
        self.assertNotIn("private-test-credential", str(error.exception))

    def test_transport_timeout_classifies_service_without_exposing_details(self):
        class Broken:
            calls = 0
            def open(self, *args, **kwargs):
                self.calls += 1
                raise URLError(TimeoutError("private-connection-details"))
        http = Transport(timeout=.01, sleep=lambda seconds: None)
        http.opener = Broken()
        with self.assertRaises(RemoteError) as error:
            http.request("Model", "POST", "https://fixture.invalid/v1", {}, {}, attempts=2)
        self.assertEqual(http.opener.calls, 2)
        self.assertEqual(error.exception.service, "Model")
        self.assertEqual(error.exception.reason, "TimeoutError")
        self.assertNotIn("private-connection-details", str(error.exception))

    def test_transport_rejected_credentials_do_not_retry_or_expose_response(self):
        class Rejected:
            calls = 0
            def open(self, *args, **kwargs):
                self.calls += 1
                raise HTTPError("https://fixture.invalid", 401, "private-rejection-detail", {}, io.BytesIO(b"private-response"))
        http = Transport(sleep=lambda seconds: None)
        http.opener = Rejected()
        with self.assertRaises(RemoteError) as error:
            http.request("Notion", "GET", "https://fixture.invalid/v1", {}, attempts=3)
        self.assertEqual(http.opener.calls, 1)
        self.assertEqual(error.exception.status, 401)
        self.assertFalse(error.exception.retryable)
        self.assertNotIn("private-", str(error.exception))

    def test_asia_shanghai_date_ignores_host_timezone(self):
        self.assertEqual(day(parse("2026-10-15T16:30:00+00:00")), "2026-10-16")
        with self.assertRaises(ValueError):
            parse("2026-10-16T08:00:00")

    def test_config_is_env_credentials_and_rejects_unencrypted_urls(self):
        with tempfile.TemporaryDirectory(prefix="orbit-config-") as temp:
            file = Path(temp) / "orbit.toml"
            file.write_text('[model]\nbase_url="https://custom.example/v1"\nname="chosen"\n')
            with patch.dict(os.environ, {"ORBIT_MODEL_API_KEY": "private-test-credential", "ORBIT_NOTION_TOKEN": "private-notion-credential"}, clear=True):
                cfg = load_config(file)
                cfg.require_connections()
                self.assertNotIn("private-test-credential", repr(cfg))
                self.assertEqual(cfg.model_name, "chosen")
                with self.assertRaises(ConfigError):
                    replace(cfg, model_base_url="http://custom.example/v1").require_connections()
                file.write_text('[model]\napi_key="do-not-store-here"\n')
                with self.assertRaises(ConfigError):
                    load_config(file)

    def test_offline_cli_status_pause_resume(self):
        with tempfile.TemporaryDirectory(prefix="orbit-cli-") as temp:
            file = Path(temp) / "orbit.toml"
            file.write_text('[runtime]\ndb_path=' + json.dumps(str(Path(temp) / "run.sqlite3")) + '\n')
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(["--config", str(file), "status"]), 0)
                self.assertEqual(main(["--config", str(file), "pause"]), 0)
                self.assertEqual(main(["--config", str(file), "resume"]), 0)
            self.assertEqual(os.stat(Path(temp) / "run.sqlite3").st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
