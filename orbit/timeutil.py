from datetime import datetime, timezone
from zoneinfo import ZoneInfo

TIMEZONE = "Asia/Shanghai"
SHANGHAI = ZoneInfo(TIMEZONE)


def now():
    return datetime.now(timezone.utc)


def parse(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("时间必须包含时区")
    return dt.astimezone(timezone.utc)


def stamp(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


def day(dt):
    return dt.astimezone(SHANGHAI).date().isoformat()
