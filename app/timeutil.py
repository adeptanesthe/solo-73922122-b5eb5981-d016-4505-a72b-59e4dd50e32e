"""披露时刻的解析与规范化。

所有时刻统一规范化为 UTC 的 ISO-8601 字符串（固定微秒精度），
这样字典序比较即等于时间先后比较，可直接用于 SQL 的 <= 过滤。
"""
from __future__ import annotations

from datetime import datetime, timezone


def parse_moment(raw: str) -> datetime:
    """解析 ISO-8601 时刻；不带时区时按 UTC 处理。非法输入抛 ValueError。"""
    s = raw.strip()
    if not s:
        raise ValueError("空时刻")
    if s[-1] in ("Z", "z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def fmt_moment(dt: datetime) -> str:
    """规范化为固定微秒精度的 UTC ISO 字符串，保证字典序 == 时间序。"""
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


def normalize_moment(raw: str) -> str:
    return fmt_moment(parse_moment(raw))


def utc_now() -> str:
    return fmt_moment(datetime.now(timezone.utc))
