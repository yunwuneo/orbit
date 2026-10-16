# 事件后续链实现与验证记录

记录日期：2026-10-16，Asia/Shanghai。结论：已完成后续链本地实现；98 项本地测试通过；受控虚构隔离区的真实模型、Notion 新接口、事项关系/投影、计划复评和丢失写入响应恢复已通过一次有限验收。生产初始化、持续运行和云部署仍未验收。真实证据见[隔离验收报告](event-continuation-live-acceptance-2026-10-16.md)。

实现阶段按顺序读取 AGENTS、REQUIREMENTS、交接和规格，并只读检查 Notion 核心设定、生活规则、记忆维护规则和正式数据源 schema。随后按用户要求在独立标记的虚构隔离区调用已配置模型与 Notion API；没有修改正式故事资料、启动常驻服务或创建工作树。复用标准库，未安装依赖。原有未提交工作保留。

## 实现对应

| 任务 | 已实现内容 | 验证与限制 |
| --- | --- | --- |
| C01 | continuation 配置、可选 threads 源、事务内 user_version=1 迁移 | 旧表和消费保留；非法参数、重复升级、迁移失败回滚通过 |
| C02 | threads/nodes/edges/tasks/dependencies/plan_links；稳定触发、修订及来源版本 | 人工来源版本唯一；任务原子提交、失败恢复、防重通过 |
| C03 | 显式 schema plan/apply；事项页、根关系回填、节点关系及进度管理块 | REST 替身验证加法初始化、中断续跑、关系目标、人工文字保护；真实新接口未测 |
| C04 | 普通生活 v1 与 world/decision/plan v2 分离；严格结构、主体、时间和引用校验 | 按事项重读全链，旧根越过近期上限仍可取得；长链压缩保留来源版本，必要资料超限则阻塞 |
| C05 | 依赖 outbox、跨 run 资源顺序、稳定回执及来源复核 | 本地生成不同于远端核对；根/事项/关系/判断/投影/计划回执恢复与提交回滚有测试 |
| C06 | 独立轮询/生活游标、到期预算、无回复复评、人工优先、停机当前复评 | 默认轮询不提高日常频率；技术错误不计无回复；过期任务不重放剧情 |
| C07 | 计划时间复读、当前决定与独立实际结果、结果核对后才更新完成 | 测试精确微秒边界、取消/改期、结果失败阻塞完成；补写与当前事项隔离 |
| C08 | CLI、配置样例、技术摘要、README/部署/交接 | threads/thread/recheck/adopt/schema 命令与 --help 同步；本地摘要不输出正文或凭据 |
| C09 | 新增虚构跨模块回归、opt-in 真实联调及远端核对 | 98 项本地通过；一次隔离真实链路通过；全部分支/长期/生产验收未完成 |

新增主要文件：`orbit/threads.py`（索引与任务仓储）、`orbit/continuation.py`（编排）、`orbit/thread_schema.py`（结构初始化）、`orbit/scheduler.py`（唤醒）、`tests/test_continuation.py`（虚构验收）与 `tests/live_continuation.py`（显式真实探针）。

## 本地证据

当前统一验证命令：`PYTHONDONTWRITEBYTECODE=1 TZ=Asia/Shanghai python3 -m unittest discover -q`；98 项通过。故事时区固定为 `Asia/Shanghai`。新增用例以 A01—A18 对应主要场景，并扩展回执、事务、真实 Notion 日期格式和模型耗时边界：

- 邀请→追问→配角补充→再判断；直接接受、拒绝、沉默和暂缓；外部节点不带主观字段。
- 人工补充与到期同轮只走一次新判断；同名事项不混并；显式关系歧义不接纳；已发生的新关联补充也可发现。
- 无回复不生成生活事件；计数持久化并触发 Echo 当前复评，普通生活继续。
- 精确发生/计划时间不被分钟索引替代；未来关联补充不进入当前判断，人工计划时间更正生效。
- 一事项失败而另一事项继续；执行结果未核对时，完成写回及投影等待。
- 模拟根/事项页、关系、决定、投影与计划回执丢失，重开/续跑不会重复创建或重复生成已有输出。
- 模型期间来源修改丢弃输出；取消、归档和权限错误阻塞相关事项；人工备注保留，受管理块或检查时间被改写时不覆盖。
- 超过近期事件上限的根仍被读取；较早节点按当前权威属性压缩并保留 ID、版本、时间和性质，根及最新依据保留原文。
- 暂停不作新判断；停机的多个过期检查合并为一次当前复评；时钟回退拒绝倒写。
- 旧库、消费、初始化中断、补写隔离、日记筛选、清理后的因果回执、复评幂等与轮询频率均有断言。

虚构数据与 REST 替身覆盖上表本地断言。真实模型选择、远端资源计数、唯一标记、节点因果关系、投影与回执结果单列于[隔离验收报告](event-continuation-live-acceptance-2026-10-16.md)；有限样本不能证明所有语义分支或生产运行可靠性。

## 受控真实验收入口

```sh
python3 -m tests.live_continuation plan
python3 -m tests.live_continuation setup
python3 -m tests.live_continuation plan
python3 -m tests.live_continuation run --lost-ack
python3 -m tests.live_continuation run
python3 -m tests.live_continuation scenario-plan
python3 -m tests.live_continuation continue
python3 -m tests.live_continuation audit
python3 -m tests.live_continuation cleanup
```

`plan` 只读远端预检；`setup` 在配置运行页下创建一个有稳定标识的隔离根、七个纯虚构页面、三个初版数据源，再通过新初始化器建立事项源。`run` 仅向配置模型发送隔离区虚构正文，运行有限一轮；后续调用可以补入人工虚构补充。它不强制 Echo 追问或接受，真实走出的分支会影响可验收范围。`--lost-ack` 在一次真实创建提交后模拟丢失响应；下一次运行先恢复。

本地状态默认 `work/continuation-live.sqlite3`，不能指向正式运行数据库；可用 `--state` 为独立验收指定专用路径。`cleanup` 仅归档登记的隔离根，核对后保留脱敏摘要；清理本任务 state 和锁之前先保存所需报告，不能删正式运行库。已归档的 state 不可继续生成，新测试须使用新的专用 state。脚本不直接写正式事件/计划/记忆或生活快照。

本次执行验证了邀请追问、人工补充、再次判断、未来计划、到期执行结果、`no_event`、真实创建后丢失响应恢复和远端回读。Echo 自主选择了追问、接受及执行；脚本没有预定这些结果。真实模型拒绝写入的协议无效输出在修复提示后安全重试。所有分支的语义稳定性、其余回执边界、多轮无回复和长时间运行仍未覆盖。远端隔离树已归档；本地验收数据库与锁已清理。

## 明确限制

- Notion REST 没有原子 CAS；采用读前、写后及再次来源核对，仍有最后读取与 PATCH 之间的竞争窗口。
- 事项视图需人工设置；初始化 API 的真实可用性（含 self-relation）须隔离验证。
- 旧版已消费但无法恢复角色决定的事件会报告冲突，当前不提供自动历史合并。管理员补写不自动建立/合并活动事项。
- 长链压缩使用远端权威概要/决定属性和节点元数据，来源每次重读；必要原文仍超预算时阻塞，不用截断结果作判断。
- 事项管理字段被人工修改后会保持冲突，需要核对处理；不通过强制覆盖或删库恢复。
- 新功能真实模型语义、全部真实恢复边界、正式持续运行、Linux/日本服务器、长期规模和成本尚未验收。

接口依据查阅了 Notion 官方 [创建数据库](https://developers.notion.com/reference/create-database)、[更新数据源](https://developers.notion.com/reference/update-a-data-source)及 [关系属性](https://developers.notion.com/reference/property-object)。程序继续固定 `Notion-Version: 2025-09-03`，使用 data_source_id 和 single_property；文档核对不等于远端实测。
