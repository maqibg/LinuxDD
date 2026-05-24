"""Telegram 消息格式化。"""

import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)


def format_message(post: dict, site_name: str) -> str:
    title = post.get("title", "无标题")
    tags = post.get("tags", [])
    tags_str = " ".join([f"#{tag}" for tag in tags]) if tags else "无"
    created_at = format_time(post.get("created_at", "未知"))
    author = post.get("last_poster_username", "未知")
    url = post.get("url", "")
    return (
        f"标题：{title}\n"
        f"标签：{tags_str}\n"
        f"发帖时间：{created_at}\n"
        f"作者：{author}\n"
        f"站点：[{site_name}]\n"
        f"{url}"
    )


def format_time(created_at: str) -> str:
    if not created_at or not isinstance(created_at, str):
        return "未知"
    if created_at == "未知":
        return created_at
    try:
        dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        dt = dt.astimezone(timezone(timedelta(hours=8)))
        return dt.strftime("%Y/%m/%d %H:%M:%S")
    except (ValueError, TypeError) as exc:
        logger.debug("时间解析失败: %s error=%s", created_at, exc)
        return "未知"
