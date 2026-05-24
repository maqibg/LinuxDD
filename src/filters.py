"""帖子过滤模块。"""

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

from src.utils import get_post_sort_key

logger = logging.getLogger(__name__)


class PostFilter:
    """基础过滤、分类过滤、时间过滤和关键词匹配。"""

    def __init__(self, filter_config: dict):
        self.categories = filter_config.get("categories", [])
        self.keywords = self._parse_keywords(filter_config.get("keywords", ""))
        self.exclude_keywords = self._parse_keywords(
            filter_config.get("exclude_keywords", "")
        )
        self.sort_by_time = filter_config.get("sort_by_time", True)
        self.max_post_age = filter_config.get("max_post_age", 0)

    @staticmethod
    def _parse_keywords(keywords) -> List[str]:
        if not keywords:
            return []
        if isinstance(keywords, str):
            return [item.strip().lower() for item in keywords.split("|") if item.strip()]
        if isinstance(keywords, list):
            return [str(item).strip().lower() for item in keywords if str(item).strip()]
        return []

    def filter_posts(self, posts: List[Dict]) -> List[Dict]:
        filtered = []
        for post in posts:
            if not self._check_basic_filters(post):
                continue
            if self.categories and post.get("category_id") not in self.categories:
                logger.debug("帖子 %s 不在监控分类中，跳过", post.get("id"))
                continue
            if not self._check_post_age(post):
                logger.debug("帖子 %s 超过时间限制，跳过", post.get("id"))
                continue
            filtered.append(post)

        if self.sort_by_time:
            filtered.sort(key=get_post_sort_key)
        return filtered

    def match_keyword(self, post: Dict) -> Optional[str]:
        if not self.keywords:
            return None
        title = (post.get("title") or "").lower()
        for keyword in self.keywords:
            if keyword in title:
                return keyword
        return None

    def is_blocked(self, post: Dict) -> bool:
        if not self.exclude_keywords:
            return False
        title = (post.get("title") or "").lower()
        for keyword in self.exclude_keywords:
            if keyword in title:
                logger.debug("帖子命中屏蔽词 keyword=%s title=%s", keyword, post.get("title"))
                return True
        return False

    def _check_basic_filters(self, post: Dict) -> bool:
        if not post.get("visible", True):
            return False
        if post.get("closed", False):
            return False
        if post.get("archived", False):
            return False
        if post.get("pinned", False):
            return False
        return post.get("archetype", "regular") == "regular"

    def _check_post_age(self, post: Dict) -> bool:
        if self.max_post_age <= 0:
            return True

        created_at = post.get("created_at")
        if not created_at:
            return True

        try:
            post_time = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            age_minutes = (datetime.now(timezone.utc) - post_time).total_seconds() / 60
            return age_minutes <= self.max_post_age
        except (ValueError, TypeError) as exc:
            logger.debug("时间解析失败 post_id=%s error=%s", post.get("id"), exc)
            return True
