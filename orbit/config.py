"""Public configuration and credentials from the environment or a local .env file."""

import math
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID


PAGES = {
    "center": "3f922e543e5880998b63eca74ea0198c",
    "identity": "68222989091b44028faf077e7b76f53a",
    "rules": "3fb22e543e58810e8a35f8422cb29aa1",
    "memory_rules": "3fa22e543e58819bb03bdc483aac4b83",
    "state": "6797da8062884bbbb0ed373ebb50fe0c",
    "journal": "3b03a15996aa4a14b552394e980a966e",
    "operations": "81d891723d4c49578af222310f613539",
}
SOURCES = {
    "memories": "e045a421-17a6-4c56-b17c-46f046fb64b4",
    "events": "676f2a53-0349-4afe-b173-7a7cc096430c",
    "plans": "f8cd3ee7-528d-4232-b413-799ffd39436c",
}


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Continuation:
    enabled: bool = False
    input_poll_seconds: int = 60
    default_check_seconds: int = 600
    min_check_seconds: int = 60
    no_reply_review_after: int = 3
    max_tasks_per_tick: int = 8


@dataclass(frozen=True)
class Config:
    db_path: str = "var/orbit.sqlite3"
    interval_seconds: int = 600
    window_seconds: int = 600
    backfill_seconds: int = 600
    context_days: int = 7
    max_context_chars: int = 180000
    max_source_items: int = 2000
    recent_events: int = 12
    timeout_seconds: float = 45
    max_attempts: int = 5
    retention_days: int = 30
    model_base_url: str = ""
    model_name: str = ""
    model_provider: str = "openai_compatible"
    chatgpt_auth_file: str = "var/chatgpt/credentials.json"
    model_key: str = field(default="", repr=False)
    notion_token: str = field(default="", repr=False)
    pages: dict = field(default_factory=lambda: dict(PAGES))
    sources: dict = field(default_factory=lambda: dict(SOURCES))
    continuation: Continuation = field(default_factory=Continuation)

    def require_connections(self, model=True):
        if not isinstance(self.notion_token, str) or not self.notion_token.strip():
            raise ConfigError("需要配置 Notion Token")
        if model:
            self.require_model()

    def require_model(self):
        if self.model_provider not in {"openai_compatible", "chatgpt"}:
            raise ConfigError("model.provider 必须是 openai_compatible 或 chatgpt")
        if not isinstance(self.model_name, str) or not self.model_name.strip():
            raise ConfigError("需要配置 Model Name")
        if self.model_provider == "chatgpt":
            return
        if not all(isinstance(v, str) and v.strip() for v in [self.model_base_url, self.model_key]):
            raise ConfigError("需要配置模型 Base URL、API Key、Model Name")
        url = urlsplit(self.model_base_url)
        if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ConfigError("模型 Base URL 必须是不含凭据、查询串的 HTTPS 地址")


def read_dotenv(path, required=False):
    """Read simple dotenv assignments as data, without expanding or executing values."""
    try:
        with Path(path).open(encoding="utf-8-sig") as stream:
            content = stream.read(131073)
    except FileNotFoundError:
        if required:
            raise ConfigError("指定的环境变量文件不存在") from None
        return {}
    except (OSError, UnicodeError):
        raise ConfigError("无法读取环境变量文件") from None
    if len(content) > 131072:
        raise ConfigError("环境变量文件超过大小上限")
    result = {}
    for number, line in enumerate(content.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)", line)
        if not match:
            raise ConfigError(f"环境变量文件第 {number} 行格式无效")
        key, value = match.groups()
        if value.startswith(("'", '"')):
            if value[0] == "'":
                quoted = re.fullmatch(r"'([^']*)'\s*(?:#.*)?", value)
            else:
                quoted = re.fullmatch(r'"((?:\\.|[^"\\])*)"\s*(?:#.*)?', value)
            if not quoted:
                raise ConfigError(f"环境变量文件第 {number} 行引号格式无效")
            inner = quoted.group(1)
            if value[0] == '"':
                escapes = {"n": "\n", "r": "\r", "t": "\t", "\\": "\\", '"': '"'}
                inner = re.sub(r"\\([nrt\\\"])", lambda m: escapes[m[1]], inner)
            value = inner
        else:
            value = re.sub(r"\s+#.*", "", value).rstrip()
        result[key] = value
    return result


def load_config(path="orbit.toml", env_file=None):
    p = Path(path)
    file_env = read_dotenv(env_file if env_file is not None else p.parent / ".env",
                           required=env_file is not None)
    environment = {**file_env, **os.environ}
    raw = tomllib.loads(p.read_text()) if p.exists() else {}
    allowed = {"runtime", "model", "chatgpt", "pages", "sources", "continuation"}
    if raw.keys() - allowed:
        raise ConfigError("未知配置段")
    runtime = raw.get("runtime", {})
    keys = {"db_path", "interval_seconds", "window_seconds", "backfill_seconds", "context_days",
            "max_context_chars", "max_source_items", "recent_events", "timeout_seconds", "max_attempts", "retention_days"}
    if (runtime.keys() - keys or raw.get("model", {}).keys() - {"base_url", "name", "provider"}
            or raw.get("chatgpt", {}).keys() - {"auth_file"}):
        raise ConfigError("未知配置项；凭据请通过环境变量或 .env 提供")
    pages, sources = dict(PAGES), dict(SOURCES)
    for target, name in [(pages, "pages"), (sources, "sources")]:
        if raw.get(name, {}).keys() - (target.keys() | ({"threads"} if name == "sources" else set())):
            raise ConfigError("未知 Notion 资源用途")
        target.update(raw.get(name, {}))
        try:
            for value in target.values():
                UUID(value)
        except (ValueError, TypeError, AttributeError) as exc:
            raise ConfigError("Notion 资源 ID 必须是 UUID") from exc
    continuation_raw = raw.get("continuation", {})
    if continuation_raw.keys() - Continuation.__dataclass_fields__.keys():
        raise ConfigError("未知后续链配置项")
    continuation = Continuation(**continuation_raw)
    if type(continuation.enabled) is not bool:
        raise ConfigError("continuation.enabled 必须是布尔值")
    for name in Continuation.__dataclass_fields__.keys() - {"enabled"}:
        value = getattr(continuation, name)
        if type(value) is not int or value <= 0:
            raise ConfigError("后续链间隔和计数必须是正整数")
    if continuation.default_check_seconds < continuation.min_check_seconds:
        raise ConfigError("默认检查间隔不能小于最小间隔")
    config = Config(**runtime, pages=pages, sources=sources, continuation=continuation,
                    model_provider=environment.get("ORBIT_MODEL_PROVIDER", raw.get("model", {}).get("provider", "openai_compatible")),
                    chatgpt_auth_file=environment.get("ORBIT_CHATGPT_AUTH_FILE", raw.get("chatgpt", {}).get("auth_file", "var/chatgpt/credentials.json")),
                    model_base_url=environment.get("ORBIT_MODEL_BASE_URL", raw.get("model", {}).get("base_url", "")),
                    model_name=environment.get("ORBIT_MODEL_NAME", raw.get("model", {}).get("name", "")),
                    model_key=environment.get("ORBIT_MODEL_API_KEY", ""),
                    notion_token=environment.get("ORBIT_NOTION_TOKEN", ""))
    for name in keys - {"db_path"}:
        value = getattr(config, name)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value <= 0:
            raise ConfigError("运行参数必须是正数")
        if name != "timeout_seconds" and not isinstance(value, int):
            raise ConfigError("计数、间隔与天数必须是整数")
    if config.interval_seconds > config.window_seconds:
        raise ConfigError("唤醒间隔不能大于实时窗口")
    if not isinstance(config.db_path, str) or not config.db_path:
        raise ConfigError("db_path 必须是文件路径")
    if config.model_provider not in {"openai_compatible", "chatgpt"}:
        raise ConfigError("model.provider 必须是 openai_compatible 或 chatgpt")
    if not isinstance(config.chatgpt_auth_file, str) or not config.chatgpt_auth_file.strip():
        raise ConfigError("chatgpt.auth_file 必须是文件路径")
    # Unlike the historic database path, the new credential path is relative to the TOML.
    from dataclasses import replace
    config = replace(config, chatgpt_auth_file=str(p.parent / Path(config.chatgpt_auth_file).expanduser()))
    return config
