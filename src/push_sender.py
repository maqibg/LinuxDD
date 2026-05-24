"""Telegram 推送执行函数。"""

import logging

from src.message_formatter import format_message
from src.push_logs import log_subscriber_failure, log_subscriber_success

logger = logging.getLogger(__name__)


def send_post_to_channel(site: dict, post: dict) -> dict:
    try:
        if not site["telegram_channel"]:
            return _failure("channel_not_configured")
        result = site["telegram_channel"].send_message(format_message(post, site["name"]))
        if result["success"]:
            logger.info("[%s] ✅ 全量推送成功: %s", site["name"], post["title"])
            return _success()
        logger.error(
            "[%s] ❌ 全量推送失败: %s (错误: %s)",
            site["name"],
            post["title"],
            result["error"],
        )
        return _failure(result["error"])
    except Exception as exc:
        logger.error("全量推送时出错: %s", exc, exc_info=True)
        return _failure(str(exc))


def send_post_to_subscriber(site: dict, post: dict, keyword: str, subscriber: dict) -> dict:
    try:
        message = f"关键词匹配: {keyword}\n\n{format_message(post, site['name'])}"
        result = subscriber["bot"].send_message(message)
        if result["success"]:
            log_subscriber_success(site, subscriber, post, keyword)
            return _success()
        log_subscriber_failure(site, subscriber, post, result["error"])
        return _failure(result["error"])
    except Exception as exc:
        logger.error("关键词推送时出错: %s", exc, exc_info=True)
        return _failure(str(exc))


def _success() -> dict:
    return {"success": True, "error": None}


def _failure(error: str | None) -> dict:
    return {"success": False, "error": error or "send_failed"}
