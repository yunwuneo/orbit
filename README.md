# Orbit

以 Notion 为权威资料中心、按上海现实时间推进的 Echo 生活模拟服务。面向日本云服务器独立部署，仅使用 Python 3.11+ 标准库与 SQLite，不需要安装第三方依赖。

已有初版实现，55 项本地测试及真实模型、隔离 Notion 写回、单轮常驻进程联调已通过。**正式持续运行、云服务器部署和长期稳定性尚未验收**。测试范围、发现的兼容问题与修复见 [联调报告](docs/live-acceptance-2026-10-16.md)，需求覆盖见 [实现与验收记录](docs/implementation.md)。

## 配置与启动

复制 `orbit.toml.example` 为 `orbit.toml`，填写模型 Base URL 和 Model Name。Base URL 包含供应商要求的 `/v1` 前缀，程序追加 `/chat/completions`。凭据从环境变量读取：

- `ORBIT_MODEL_API_KEY`：所选 OpenAI 兼容服务的密钥。
- `ORBIT_NOTION_TOKEN`：独立 Notion 集成的 Token。需为其开放配置中的页面和三个数据源；Codex 连接器授权不会自动转交给服务器进程。
- `ORBIT_MODEL_BASE_URL`、`ORBIT_MODEL_NAME` 可覆盖 TOML 中的公开模型配置。

程序自动读取配置文件同目录的 `.env`（默认是项目根目录），可将 `.env.example` 复制为 `.env` 后填写。优先级为：**进程环境变量 > `.env` > TOML 配置**；不会修改进程环境变量。`.env` 已被 Git 忽略，建议权限设为 `0600`。

支持 `KEY=value`、可选 `export`、单/双引号、空行和注释；双引号内支持 `\n`、`\r`、`\t`、`\\`、`\"` 转义。未加引号的值在空白后的 `#` 开始注释，密钥内的 `#` 可用引号保留。每个赋值占一行，不执行 shell 命令或展开 `${VAR}`。

可用 `python3 -m orbit --env-file /path/to/orbit.env check` 指定其他文件；显式指定的文件不存在或格式错误会报错，错误消息不包含变量值。生产环境也可继续由服务管理器加载受保护的环境文件，见 [部署与操作](docs/deployment.md)。不绑定 OpenAI 官方服务，不创建新密钥，不在仓库保存真实凭据。

```sh
python3 -m orbit check    # 只读取 Notion，不调用模型或写入；只需 Notion Token
python3 -m orbit status   # 本地状态，不需要网络或凭据
python3 -m orbit once     # 在当前时刻判断一次；每次执行都作为独立启动，记录此前缺口
python3 -m orbit run      # 常驻、定时唤醒；Ctrl-C / SIGTERM 正常停止
```

首次启动只定位当前时刻，不从历史日记自动补写。默认每 10 分钟唤醒，实时窗口 10 分钟；这是可调整的实现默认值。宿主机设为日本时区也不改变故事日期。

## 管理命令

```sh
python3 -m orbit pause
python3 -m orbit resume
python3 -m orbit sync
python3 -m orbit retry
python3 -m orbit diary 2026-10-16
python3 -m orbit backfill --start '2026-10-16T08:00:00+08:00' --end '2026-10-16T09:00:00+08:00' --max-segments 6
python3 -m orbit backfill --resume TASK_ID --max-segments 6
python3 -m orbit cancel-backfill TASK_ID
```

补写区间须位于 `status` 中已记录的缺口内。补写按顺序分段并可续跑，期间实时推进等待；发现历史冲突保留冲突，管理员可取消后续补写。它不会回退当前生活快照，也不会修改现在的计划状态；当年的意图和计划判断保存在对应历史事件正文。

暂停停止新生活生成，已经生成内容的技术写回仍可恢复。恢复只从当前时刻继续；到期计划保持未完成，由 Echo 现在判断。日记只整理已结束的上海日期及该日已记录事件；常驻服务自动整理前一天，补写后可手动重整相应日期，原文保留。

写入回执不明确时先核对稳定标识，找不到也不会盲目创建第二份。达到重试上限后阻塞，`retry` 重新开放核对，**不会清除“可能已发送”标志**。只有管理员已在 Notion 核实确实不存在时，才使用：

```sh
python3 -m orbit confirm-absent WRITE_ID --verified-in-notion
python3 -m orbit sync
```

## 验证

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -v
```

测试使用内存中的 Notion REST 实现和可控 Echo 输出，不联网、不创建真实剧情或记忆。无需下载依赖、创建虚拟环境或工作树。运行数据库只保留待写内容和少量恢复索引，成功读回后清除任务正文；测试临时目录自动清理。
