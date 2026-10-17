"""Administrative CLI. Commands never print credentials or raw remote errors."""

import argparse
import json
import os
import signal
import sqlite3
import sys
import time
from datetime import timedelta

from .config import ConfigError, load_config
from .engine import Engine
from .model import InvalidOutput, Model
from .notion import Conflict, Notion, UncertainWrite
from .store import Busy, Store
from .timeutil import day, now, parse, stamp
from .transport import RemoteError


def parser():
    root = argparse.ArgumentParser(prog="orbit", description="Echo Notion 生活模拟管理员")
    root.add_argument("--config", default="orbit.toml")
    root.add_argument("--env-file", help="环境变量文件；默认读取配置文件同目录的 .env")
    commands = root.add_subparsers(dest="command", required=True)
    for name in ["status", "pause", "resume", "run", "once", "sync", "check", "retry", "threads"]:
        commands.add_parser(name)
    backfill = commands.add_parser("backfill")
    backfill.add_argument("--start")
    backfill.add_argument("--end")
    backfill.add_argument("--resume", dest="task_id")
    backfill.add_argument("--max-segments", type=int, default=12)
    diary = commands.add_parser("diary")
    diary.add_argument("date", help="上海日期 YYYY-MM-DD，必须已经结束")
    reconcile = commands.add_parser("confirm-absent", help="管理员核对远端确实未写入后才可使用")
    reconcile.add_argument("write_id")
    reconcile.add_argument("--verified-in-notion", action="store_true", required=True)
    cancel = commands.add_parser("cancel-backfill", help="停止后续补写；保留已生成记录与待写回任务")
    cancel.add_argument("task_id")
    for name, argument in [("thread", "thread_id"), ("thread-recheck", "thread_id"), ("thread-adopt", "event_page_id")]:
        commands.add_parser(name).add_argument(argument)
    schema = commands.add_parser("threads-schema", help="事项结构预检或显式加法初始化")
    action = schema.add_mutually_exclusive_group(required=True)
    action.add_argument("--plan", action="store_true")
    action.add_argument("--apply", action="store_true")
    login = commands.add_parser("chatgpt-login", help="Continue with ChatGPT；在本机浏览器手动打开授权链接")
    login.add_argument("--port", type=int, default=1455, help="本机 127.0.0.1 回调端口；0 自动分配")
    login.add_argument("--timeout", type=int, default=180, help="等待授权秒数，1–600")
    login.add_argument("--new-account", action="store_true", help="添加账号注册，不覆盖已有账号")
    for name in ["chatgpt-status", "chatgpt-models", "chatgpt-logout", "chatgpt-test"]:
        commands.add_parser(name)
    commands.add_parser("chatgpt-use", help="选择已有账号注册").add_argument("client_id")
    commands.add_parser("chatgpt-import", help="导入受保护的 Orbit 凭据文件；保留本机 Host ID").add_argument("file")
    return root


def print_json(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main(argv=None):
    args = parser().parse_args(argv)
    os.umask(0o077)
    store = None
    try:
        config = load_config(args.config, env_file=args.env_file)
        if args.command.startswith("chatgpt-"):
            return chatgpt_command(args, config)
        store = Store(config.db_path)
        if args.command in {"threads", "thread", "thread-recheck"}:
            from .threads import Threads
            repo = Threads(store)
            with store.lock():
                if args.command == "threads":
                    print_json(repo.list())
                elif args.command == "thread":
                    print_json(repo.summary(args.thread_id))
                else:
                    print_json({"task_id": repo.recheck(args.thread_id, stamp(now())), "queued": True})
            return 0
        if args.command == "threads-schema":
            from .thread_schema import ThreadSchema
            config.require_connections(model=False)
            with store.lock():
                schema = ThreadSchema(Notion(config), store)
                print_json(schema.apply() if args.apply else schema.plan())
            return 0
        if args.command == "status":
            print_json(store.status())
            return 0
        if args.command in {"pause", "resume", "retry", "confirm-absent", "cancel-backfill"}:
            with store.lock():
                if args.command == "pause":
                    store.pause(stamp(now()))
                elif args.command == "resume":
                    store.resume(stamp(now()))
                elif args.command == "retry":
                    with store.db:
                        store.db.execute("UPDATE outbox SET status='pending',attempts=0,next_at=NULL WHERE status='blocked'")
                        store.db.execute("UPDATE thread_tasks SET status='pending',attempts=0,next_at=NULL,error_code=NULL WHERE status='blocked' AND output_run_id IS NULL")
                        store.db.execute("UPDATE threads SET sync_status='pending',error_code=NULL WHERE sync_status='blocked'")
                        store.put("generation_failures", 0)
                        store.put("generation_blocked", False)
                        store.put("next_generation_at", None)
                elif args.command == "cancel-backfill":
                    with store.db:
                        cursor = store.db.execute("UPDATE backfills SET status='cancelled' WHERE id=? AND status!='done'", (args.task_id,))
                        if cursor.rowcount != 1:
                            raise ValueError("没有可取消的补写任务")
                else:
                    with store.db:
                        cursor = store.db.execute("UPDATE outbox SET sent=0,status='pending',attempts=0,next_at=NULL WHERE id=? AND status!='done' AND sent=1", (args.write_id,))
                        if cursor.rowcount != 1:
                            raise ValueError("没有该待核对写入")
                store.operational(config.pages["operations"], stamp(now()), {
                    "command": args.command, "at": stamp(now()), "paused": store.get("paused", False),
                    "cursor": store.get("cursor"), "publication": "待下次 sync 或服务唤醒写回并核对"})
                print_json(store.status())
            return 0
        config.require_connections(model=args.command not in {"sync", "check", "thread-adopt"})
        engine = Engine(config, store, Notion(config), Model(config), now)
        if args.command == "run":
            with store.lock("daemon"):
                return run_daemon(engine)
        with store.lock():
            if args.command == "check":
                if engine.continuation:
                    engine.continuation.check_schema()
                context = engine.notion.context(stamp(now()), stamp(now()))
                print_json({"notion_read": True, "resources": {k: len(context[k]) for k in ["memories", "events", "plans", "diaries"]},
                            "model_live_test": False, "writes": False})
                return 0
            if args.command == "thread-adopt":
                if not engine.continuation:
                    raise ValueError("thread-adopt 需要启用后续链")
                print_json({"thread_id": engine.continuation.adopt(args.event_page_id, stamp(now())), "queued": True})
                return 0
            if args.command == "once":
                store.recover(stamp(now()))
                result = engine.tick()
            elif args.command == "sync":
                result = "synced" if engine.flush() else "writes_pending"
            elif args.command == "diary":
                result = engine.diary(args.date)
            else:
                if args.max_segments <= 0:
                    raise ValueError("max-segments 必须大于零")
                if args.task_id and (args.start or args.end):
                    raise ValueError("续跑与新建补写参数不能混用")
                if args.task_id:
                    task_id = args.task_id
                elif args.start and args.end:
                    start, end = stamp(parse(args.start)), stamp(parse(args.end))
                    if parse(end) > now():
                        raise ValueError("不能补写现实未来")
                    task_id = store.add_backfill(start, end)
                else:
                    raise ValueError("提供 start/end 或 resume 任务 ID")
                result = engine.backfill(task_id, args.max_segments)
                print_json({"backfill_id": task_id, "result": result})
                return 0 if result in {"done", "segments_pending"} else 1
            print_json({"result": result})
            return 0 if result in {"generated", "paused", "synced", "saved", "unchanged", "no_recorded_events"} else 1
    except (ConfigError, ValueError, Busy, Conflict, RemoteError, UncertainWrite, InvalidOutput) as exc:
        # Known exceptions contain safe fixed messages; never print raw provider responses.
        if isinstance(exc, (ConfigError, Busy, Conflict, RemoteError, UncertainWrite)):
            print(str(exc), file=sys.stderr)
        else:
            print(f"操作失败：{type(exc).__name__}；请检查参数或运行状态", file=sys.stderr)
        return 1
    except (OSError, KeyError, TypeError, RuntimeError, sqlite3.Error):
        print("操作失败；请检查配置、权限与运行状态（未输出敏感响应）", file=sys.stderr)
        return 1
    finally:
        if store:
            store.close()


def chatgpt_command(args, config):
    from dataclasses import replace
    from .chatgpt import ChatGPT
    from .chatgpt_auth import ChatGPTAuth
    from .chatgpt_transport import ChatGPTTransport
    auth = ChatGPTAuth(config.chatgpt_auth_file, ChatGPTTransport(config.timeout_seconds))
    if args.command == "chatgpt-login":
        result = auth.login(args.port, args.timeout, args.new_account)
    elif args.command == "chatgpt-status":
        result = auth.status()
    elif args.command == "chatgpt-logout":
        result = auth.logout()
    elif args.command == "chatgpt-use":
        auth.select(args.client_id)
        result = auth.status()
    elif args.command == "chatgpt-import":
        auth.import_file(args.file)
        result = auth.status()
    elif args.command == "chatgpt-models":
        result = {"models": ChatGPT(config, http=auth.http, auth=auth).models()}
    else:
        # Explicit small inference only; no Notion, database or life simulation.
        model_config = replace(config, model_provider="chatgpt")
        output = Model(model_config).complete('只返回 JSON 对象 {"ok":true}。', {"purpose": "connection_test"})
        if output != {"ok": True}:
            raise InvalidOutput("连接测试没有返回预期 JSON")
        result = {"model_live_test": True, "completed": True, "notion_writes": False}
    print_json(result)
    if args.command == "chatgpt-logout" and not result["revocation_confirmed"]:
        print("本地已退出，但未确认远端撤销；请在 ChatGPT 设置中断开 Orbit。", file=sys.stderr)
        return 1
    return 0


def run_daemon(engine):
    stopping = False

    def stop(signum, frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    with engine.store.lock():
        engine.store.recover(stamp(engine.clock()))
    while not stopping:
        from .scheduler import wake_seconds
        try:
            with engine.store.lock():
                result = engine.tick()
                yesterday = day(engine.clock() - timedelta(days=1))
                if not engine.store.get("paused", False) and not engine.store.pending() and engine.generation_ready(stamp(engine.clock())):
                    if engine.store.get("daily_checked") != yesterday:
                        try:
                            diary_result = engine.diary(yesterday)
                            if diary_result in {"saved", "unchanged", "no_recorded_events"}:
                                with engine.store.db:
                                    engine.store.put("daily_checked", yesterday)
                        except (RemoteError, Conflict, InvalidOutput) as exc:
                            engine.failure(stamp(engine.clock()), exc)
                print_json({"at": stamp(engine.clock()), "result": result})
        except Busy:
            pass
        except (RemoteError, Conflict, InvalidOutput) as exc:
            engine.failure(stamp(engine.clock()), exc)
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
            print("运行错误；本轮未确认成功，请查看状态", file=sys.stderr)
            return 1
        deadline = time.monotonic() + wake_seconds(engine)
        while not stopping and time.monotonic() < deadline:
            time.sleep(min(1, max(0, deadline - time.monotonic())))
    return 0
