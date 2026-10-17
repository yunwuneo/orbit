# ChatGPT 套餐接入

Orbit 支持两种互不自动切换的模型接入方式：

| provider | 协议 | 凭据 |
| --- | --- | --- |
| `openai_compatible`（默认） | 自定义 Base URL + `/chat/completions`，原有 `messages`/`choices` 格式 | `ORBIT_MODEL_API_KEY` |
| `chatgpt` | 固定官方 `https://api.openai.com/v1/responses`，HTTP/SSE | Orbit 自己注册和保存的 ChatGPT OAuth 会话 |

没有 `model.provider` 的旧配置继续使用原有格式。切换 provider 不修改故事状态、来源、消费记录或待写回内容；不会因 OAuth 失败自动使用另一条计费路径。本次仅新增接入能力，没有启用真实账号或修改已有部署配置。

## 安装与配置

兼容 API 路线仍无需第三方依赖。ChatGPT 登录、导入与 ID Token 校验需要维护中的 PyJWT 和 cryptography，不使用未验证的 JWT 内容作为身份。仅启用此路线时安装：

```sh
python3 -m pip install '.[chatgpt]'
```

在原 TOML 的对应段中设置，其他 Notion 与运行配置保留：

```toml
[model]
provider = "chatgpt"
name = "登录后查到的可用模型 slug"

[chatgpt]
auth_file = "var/chatgpt/credentials.json"
```

`auth_file` 相对 TOML 所在目录解析，支持绝对路径。可通过 `ORBIT_MODEL_PROVIDER`、`ORBIT_MODEL_NAME`、`ORBIT_CHATGPT_AUTH_FILE` 覆盖；进程环境 > 配置同目录 `.env`（或显式 `--env-file`）> TOML。注意已有环境变量会覆盖 TOML 的 provider/name。

ChatGPT 模式忽略 `base_url` 和模型 API Key，只向官方服务发送 OAuth 凭据。Notion Token 仍独立配置。凭据目录建议 `0700`、文件必须为当前用户所有且没有组/其他用户权限（写入为 `0600`）；拒绝凭据或锁文件的符号链接，以及其他用户可写的直接父目录。默认 `var/` 已被 Git 忽略；自定义路径也必须放在 Git 之外或加入忽略规则。

## 登录与验证

```sh
python3 -m orbit chatgpt-login
python3 -m orbit chatgpt-status
python3 -m orbit chatgpt-models
```

登录先监听本机 `127.0.0.1:1455/auth/callback`，然后在终端显示 Continue with ChatGPT 授权链接。自行在本机浏览器打开并授权，程序不自动启动或控制浏览器。端口被占用时可用 `chatgpt-login --port 0` 自动分配；等待默认 180 秒，可设置 `--timeout 1..600`。回调地址不能替换成 `localhost`，每次登录使用相同路径及本次实际端口。

首次登录使用动态公开客户端注册；保存本次回调返回的正式 client ID。后续重登复用注册和本机 Host ID。即使一次性授权码交换失败，已颁发的注册 ID 也保留供重试。PKCE、随机 state/nonce、回调路径/Host、重复参数和 OIDC 签名、issuer、audience、过期时间及身份均做校验。签名公钥与撤销地址只允许来自官方认证域名。

`chatgpt-status` 完全离线，不创建运行数据库、不刷新 Token，也不显示 Token 或个人资料。`chatgpt-models` 会联网并可能刷新凭据，只显示账号目录中允许展示的 slug/name；列表不等于实际模型请求已成功。

将返回的模型 `slug` 填入 `model.name`，再显式执行小型连通性测试：

```sh
python3 -m orbit chatgpt-test
```

此命令使用 ChatGPT 路线做一次最小 JSON 推理，不读取或写入 Notion、不推进生活、不创建运行数据库。它会消耗相应套餐额度。通过后仍需在隔离 Notion 中验收角色协议与写回，再启用正式 `run`。

## 多账号与退出

```sh
python3 -m orbit chatgpt-login --new-account
python3 -m orbit chatgpt-use oaiapp_返回的注册ID
python3 -m orbit chatgpt-logout
```

每个注册独立保存身份、client ID、Token、权限与有效期；添加账号不会覆盖其他注册。`chatgpt-use` 选择当前账号，后续请求重新读取该选择。普通登录是对当前账号重新授权，身份不匹配时拒绝替换。缺少套餐权限时保留已验证登录记录，但不发起推理。

退出会尝试向 OIDC discovery 指定的撤销端点撤销当前 renewable session，然后删除本地该账号的 access/refresh/ID Token，保留注册和 Host ID。其他账号保留。无法确认远端撤销时，命令返回失败状态并提示去 ChatGPT 设置断开 Orbit；本地仍停止使用该会话。

## 自托管 VM

官方支持开源应用在自托管 VM 上运行。本机完成 Orbit 登录后，通过 SSH/SCP 等安全通道传入该 Orbit 凭据文件，并在 VM 上运行：

```sh
python3 -m orbit --config /etc/orbit/orbit.toml chatgpt-import /受保护路径/本机凭据.json
python3 -m orbit --config /etc/orbit/orbit.toml chatgpt-models
python3 -m orbit --config /etc/orbit/orbit.toml chatgpt-test
```

源文件同样要求当前用户所有、`0600` 权限。导入仅取源文件当前选中的注册，校验其 ID Token 的签名和身份（允许已过期的身份提示，不把它当有效会话），保留或创建 VM 自己的 Host ID。不要直接覆盖 VM 正在使用的凭据文件。后续刷新交给 VM；停止本机使用这份相同会话，避免跨主机同时刷新同一个轮换 Token。确认导入成功后，由管理员清理传输中间副本，保护唯一有效凭据。

systemd 配置的 `ProtectSystem=strict` / `ProtectHome=true` 下，应将 `chatgpt.auth_file` 设置为 `/var/lib/orbit/chatgpt/credentials.json`。它位于已有 `ReadWritePaths=/var/lib/orbit` 内，目录和文件由 `orbit` 用户所有。安装依赖的 Python 必须与 `ExecStart` 相同；如果用了虚拟环境，需要同步调整解释器路径。登录/导入命令也应以服务用户和同一配置路径执行。

## 刷新、流与恢复

- Access Token 在接近到期时刷新。保存服务端 `expires_in` 和 `earliest_refresh_at`；官方目前说明 Access Token 为 1 小时、轮换 Refresh Token 为 30 天。刷新使用颁发的 client ID，不使用动态注册入口 ID。
- 刷新受独立文件锁保护；其他进程遇锁会得到可重试的安全错误。刷新成功后 access/refresh/权限/有效期一起原子写入，并 fsync；凭据只在受保护文件中，绝不进入 SQLite、提示词或 Notion。
- 刷新不做盲目自动重放，避免轮换会话争用。暂时网络故障保留凭据；明确无效、过期或被复用的 Refresh Token 会清除失效 Token，保留注册以供重登。
- Responses 请求使用 `instructions`、完整 `input` 数组、`store=false`、`stream=true`。不发送此路线不支持的 `system` 消息、temperature、max_output_tokens 或 previous_response_id；上下文仍由 Orbit 从 Notion 组装。
- 只在 `response.completed` 且最终 assistant 文本完整、与已接收增量一致时解析业务 JSON。中断、拒绝、incomplete、工具调用或额度失败都不会当作 Echo 的完成决定。流大小、输出长度、超时与重试次数有上限。
- 套餐权限、资格或额度错误不自动切换 API Key，也不推断额度重置时间；接入现有 generation 阻塞/退避恢复。修复权限、额度或登录后，执行原有 `retry` 重新开放生成。已生成内容的技术写回仍可通过 `sync` 恢复。
- 模型输出若包含当前请求的 OAuth 凭据、保留的 ID Token 或原有受保护凭据，会在模型边界拒绝。错误只记录安全分类，不记录原始响应正文、Token、回调码或授权回调 URL。

## 验证范围与官方资料

2026-10-17：完整 135 项离线测试通过（原有 98 项、新增 37 项），覆盖兼容 API 回归、RSA 签名/身份校验、PKCE/回调、防伪状态、刷新轮换与互斥、账号选择、退出与 VM 导入、原子文件写入、SSE 中断/额度错误，以及生成失败不提交生活内容。真实 ChatGPT 登录、账号套餐资格、可用模型、真实推理与云端长期运行尚未验收。测试用随机生成的 RSA 密钥与虚构凭据，不访问真实账号。

```sh
PYTHONDONTWRITEBYTECODE=1 TZ=Asia/Shanghai python3 -m unittest discover -v
```

未安装可选依赖时，真实 JWT 签名与 OAuth 登录测试标记为 skipped，其余测试仍执行；完整验证需安装 `.[chatgpt]`。

- [官方注册与登录](https://developers.openai.com/siwc/token-sharing-open-source/sign-in)
- [模型与 Responses 调用](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference)
- [刷新、账号与会话](https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions)
- [自托管 VM](https://developers.openai.com/siwc/token-sharing-open-source/self-hosted-vms)
- [预览限制](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations)
