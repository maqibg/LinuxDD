"""推送日志辅助函数。"""

import logging

logger = logging.getLogger(__name__)


def log_subscriber_success(site: dict, subscriber: dict, post: dict, keyword: str) -> None:
    logger.info(
        "[%s] ✅ 关键词推送成功 -> [%s]: %s (关键词: %s)",
        site["name"],
        subscriber["name"],
        post["title"],
        keyword,
    )


def log_subscriber_failure(site: dict, subscriber: dict, post: dict, error: str) -> None:
    logger.error(
        "[%s] ❌ 关键词推送失败 -> [%s]: %s (错误: %s)",
        site["name"],
        subscriber["name"],
        post["title"],
        error,
    )
