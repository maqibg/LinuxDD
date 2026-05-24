"""Discourse API 客户端，支持 User-Api-Key 或浏览器 cookie 会话。"""

import logging
import time
from typing import Dict, List, Optional

from curl_cffi import requests

logger = logging.getLogger(__name__)


class FetchPostsError(RuntimeError):
    """新帖抓取失败。"""


class AuthenticationRequired(RuntimeError):
    """站点会话无效或被拒绝。"""


class DiscourseAPI:
    """拉取 Discourse 新帖并统一字段格式。"""

    def __init__(
        self,
        base_url: str,
        api_key: Optional[str] = None,
        proxy_config: Optional[dict] = None,
        session: Optional[requests.Session] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = (api_key or "").strip()
        self.session = session or requests.Session()
        self.headers = self._build_headers()
        self.proxies = self._build_proxies(proxy_config)

    def set_session(self, session: requests.Session) -> None:
        self.session = session

    def fetch_new_posts(self, max_retries: int = 3) -> List[Dict]:
        url = f"{self.base_url}/new.json"
        for attempt in range(1, max_retries + 1):
            try:
                if attempt > 1:
                    time.sleep(min(2**attempt, 10))
                response = self.session.get(
                    url,
                    headers=self.headers,
                    impersonate="chrome",
                    timeout=30,
                    proxies=self.proxies,
                )
                return self._parse_new_posts_response(response)
            except AuthenticationRequired:
                raise
            except Exception as exc:
                logger.warning("请求 %s 失败 attempt=%s error=%s", url, attempt, exc)
                if attempt == max_retries:
                    logger.error("所有重试均失败: %s", url)
                    raise FetchPostsError(f"请求 {url} 失败: {exc}") from exc
        return []

    def check_login_status(self) -> Dict:
        try:
            response = self.session.get(
                f"{self.base_url}/session/current.json",
                headers=self.headers,
                impersonate="chrome",
                timeout=10,
                proxies=self.proxies,
            )
            if not response.ok:
                return {"is_logged_in": False, "error": response.status_code}
            data = response.json()
            return {
                "is_logged_in": data.get("current_user") is not None,
                "user": data.get("current_user"),
                "response": data,
            }
        except Exception as exc:
            logger.error("检查登录状态时出错: %s", exc)
            return {"is_logged_in": False, "error": str(exc)}

    def _build_headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "X-Requested-With": "XMLHttpRequest",
        }
        if self.api_key:
            headers["User-Api-Key"] = self.api_key
        return headers

    @staticmethod
    def _build_proxies(proxy_config: Optional[dict]) -> Optional[dict[str, str]]:
        if not proxy_config or not proxy_config.get("enabled", False):
            return None
        proxy_type = proxy_config.get("type", "http")
        proxy_host = proxy_config.get("host", "127.0.0.1")
        proxy_port = proxy_config.get("port", 7890)
        proxy_url = f"{proxy_type}://{proxy_host}:{proxy_port}"
        logger.info("站点代理已启用: %s", proxy_url)
        return {"http": proxy_url, "https": proxy_url}

    def _parse_new_posts_response(self, response) -> List[Dict]:
        if response.status_code == 403:
            raise AuthenticationRequired("Discourse API 返回 403")
        response.raise_for_status()
        data = response.json()
        topics = self._extract_topics(data)
        logger.info("API响应成功，获取到 %s 个帖子", len(topics))
        return self._process_topics(topics)

    @staticmethod
    def _extract_topics(data: dict) -> List[Dict]:
        if not isinstance(data, dict):
            raise FetchPostsError(f"API响应格式不符合预期: {type(data).__name__}")

        if "topic_list" in data:
            topic_list = data["topic_list"]
            if isinstance(topic_list, dict) and isinstance(topic_list.get("topics"), list):
                return topic_list["topics"]
            raise FetchPostsError("API响应 topic_list.topics 缺失或不是列表")

        if "topics" in data:
            topics = data["topics"]
            if isinstance(topics, list):
                return topics
            raise FetchPostsError("API响应 topics 不是列表")

        keys = ", ".join(str(key) for key in data.keys())
        raise FetchPostsError(f"API响应格式不符合预期，缺少 topics: {keys}")

    @staticmethod
    def _normalize_tags(tags: list) -> List[str]:
        if not tags:
            return []
        return [tag["name"] if isinstance(tag, dict) else str(tag) for tag in tags]

    def _process_topics(self, topics: List[Dict]) -> List[Dict]:
        processed = []
        for topic in topics:
            post = self._build_post(topic)
            post["url"] = f"{self.base_url}/t/{post['slug']}/{post['id']}"
            processed.append(post)
        return processed

    def _build_post(self, topic: Dict) -> Dict:
        topic_id = self._topic_id(topic)
        return {
            "id": topic_id,
            "title": topic.get("title") or topic.get("fancy_title") or "无标题",
            "created_at": topic.get("created_at"),
            "last_posted_at": topic.get("last_posted_at"),
            "bumped_at": topic.get("bumped_at"),
            "visible": topic.get("visible", True),
            "closed": topic.get("closed", False),
            "archived": topic.get("archived", False),
            "pinned": topic.get("pinned", False),
            "tags": self._normalize_tags(topic.get("tags", [])),
            "category_id": topic.get("category_id"),
            "posts_count": topic.get("posts_count", 1),
            "reply_count": topic.get("reply_count", 0),
            "views": topic.get("views", 0),
            "like_count": topic.get("like_count", 0),
            "last_poster_username": topic.get("last_poster_username"),
            "slug": topic.get("slug", "topic"),
            "archetype": topic.get("archetype", "regular"),
            "image_url": topic.get("image_url"),
            "excerpt": topic.get("excerpt"),
        }

    @staticmethod
    def _topic_id(topic: Dict) -> int:
        if not isinstance(topic, dict):
            raise FetchPostsError(f"API响应 topic 不是对象: {type(topic).__name__}")
        raw_id = topic.get("id")
        if isinstance(raw_id, bool):
            raise FetchPostsError("API响应 topic.id 不是有效整数")
        try:
            topic_id = int(raw_id)
        except (TypeError, ValueError) as exc:
            raise FetchPostsError(f"API响应 topic.id 不是有效整数: {raw_id}") from exc
        if topic_id <= 0:
            raise FetchPostsError(f"API响应 topic.id 必须大于 0: {raw_id}")
        return topic_id
