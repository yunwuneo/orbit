# 事件后续链：新会话交接

更新：2026-10-16，Asia/Shanghai。事件后续链已实现，98 项本地测试通过；一次虚构隔离区真实模型与 Notion 链路已验收。详细范围、发现和限制见[实现记录](event-continuation-implementation.md)与[真实验收报告](event-continuation-live-acceptance-2026-10-16.md)。正式功能仍默认关闭，尚未初始化正式 schema、启用服务或部署。

## 后续工作目标

若继续开发，先按 [AGENTS.md](/Users/neo/Developer/orbit/AGENTS.md)、[REQUIREMENTS.md](/Users/neo/Developer/orbit/REQUIREMENTS.md)、[事件后续链规格](event-continuation-spec.md)读取约束，再检查当前代码和 Git 状态。优先补强尚未覆盖的边界：所有计划结果、连续无回复、更多网络/写入回执故障组合、长停机与多事项持续负载；需要真实服务器才能完成的 Linux/systemd、日本云服务器和长期运行验收应明确区分于本地证据。

## 已验证能力

- 稳定事项 ID、因果节点与来源版本；Echo 可自行追问、接受、拒绝、暂缓或不行动。
- 人工补充优先、按事项重读、持久调度、失败恢复和已核对后写回。
- 未来计划到期时由 Echo 当前判断；完成需要独立执行结果回执。
- Notion 事项进度、完整时间、来源/角色、父子关系及稳定写入标记通过隔离远端审计。
- 用户要求的统一故事时区为 `Asia/Shanghai`；运行库采用 UTC 时间戳，部署服务显式设置 `TZ=Asia/Shanghai`。
- 真实隔离区已归档，验收本地数据库已清理；报告只保留虚构样本和脱敏摘要。

## 重要边界

- 正式 `[continuation]` 仍为关闭；本次未修改正式故事事件、状态或记忆，没有启动常驻进程、部署或推送。
- 有限真实样本不证明长期角色语义稳定，也不覆盖全部恢复边界与所有计划分支。
- Notion REST 不提供原子 CAS，读取后写入之间仍存在并发竞争窗口。
- 凭据位于忽略的 `.env` / `orbit.toml`，不得复制、打印或提交。真实模型调用只使用虚构隔离数据。
- 保持节省存储，复用依赖，不创建不必要的工作树或环境；清理前核对文件用途。

## 复核命令

```sh
cd /Users/neo/Developer/orbit
git status --short
PYTHONDONTWRITEBYTECODE=1 TZ=Asia/Shanghai python3 -m unittest discover -q
```

隔离真实探针位于 `tests/live_continuation.py`，含远端建树、真实模型调用、写入和归档副作用；只在用户明确要求进行真实验收时运行，并使用独立 `--state`。不要自动运行正式 `once`、`run`、`backfill` 或 schema `--apply`。
