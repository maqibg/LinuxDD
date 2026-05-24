"""帖子推送与送达状态记录。"""

import time

from src.delivery_keys import topic_storage_id
from src.delivery_plan import build_delivery_targets
from src.push_sender import send_post_to_channel, send_post_to_subscriber
from src.utils import get_post_sort_key


class PostPusher:
    """按目标推送帖子，并维护每个目标的送达状态。"""

    def __init__(self, db, sites: dict[str, dict]):
        self.db = db
        self.sites = sites

    def push_new_posts(self, all_posts: list[dict]) -> tuple[int, list[str]]:
        all_posts.sort(key=lambda post: (get_post_sort_key(post), post.get("id", 0)))
        for post in all_posts:
            post["_db_id"] = topic_storage_id(post["_site_key"], post["id"])
        notified_ids = self.db.batch_is_notified([post["_db_id"] for post in all_posts])
        pending_posts = [post for post in all_posts if post["_db_id"] not in notified_ids]

        total_success = 0
        errors = []
        for post in pending_posts:
            success, post_errors = self.push_post(post)
            total_success += success
            errors.extend(post_errors)
        return total_success, errors

    def push_post(self, post: dict) -> tuple[int, list[str]]:
        site_key = post["_site_key"]
        site = self.sites[site_key]
        topic_id = post["_db_id"]
        errors = []
        if not self.record_topic(post, topic_id):
            return 0, [f"{site_key}:{topic_id}: topic_record_failed"]
        targets = build_delivery_targets(post, site_key, site)
        if not targets:
            return 0, self.mark_topic_notified(topic_id, site_key)

        target_keys = [target["key"] for target in targets]
        if not self.db.ensure_delivery_targets(topic_id, target_keys):
            return 0, [f"{site_key}:{topic_id}: delivery_targets_init_failed"]
        delivered = self.db.get_delivered_targets(topic_id)
        pending_targets = [target for target in targets if target["key"] not in delivered]
        if not pending_targets:
            return 0, self.mark_topic_notified(topic_id, site_key)

        success, target_errors = self.push_targets(post, pending_targets)
        errors.extend(target_errors)
        if self.db.are_all_targets_delivered(topic_id, target_keys):
            errors.extend(self.mark_topic_notified(topic_id, site_key))
        return success, errors

    def push_targets(self, post: dict, targets: list[dict]) -> tuple[int, list[str]]:
        site = self.sites[post["_site_key"]]
        topic_id = post["_db_id"]
        success = 0
        errors = []
        for target in targets:
            result = self.send_to_target(site, post, target)
            if result["success"] and self.db.mark_target_delivered(topic_id, target["key"]):
                success += 1
            elif result["success"]:
                errors.append(self.format_target_error(post, target, "delivery_state_failed"))
            else:
                errors.append(self.record_target_failure(post, target, result))
            time.sleep(2)
        return success, errors

    def record_target_failure(
        self,
        post: dict,
        target: dict,
        result: dict,
    ) -> str:
        error = result.get("error") or "send_failed"
        if not self.db.mark_target_failed(post["_db_id"], target["key"], error):
            error = f"{error}; delivery_state_failed"
        return self.format_target_error(post, target, error)

    @staticmethod
    def send_to_target(site: dict, post: dict, target: dict) -> dict:
        if target["kind"] == "channel":
            return send_post_to_channel(site, post)
        return send_post_to_subscriber(
            site,
            post,
            target["keyword"],
            target["subscriber"],
        )

    def record_topic(self, post: dict, topic_id: int) -> bool:
        return self.db.add_topic(
            topic_id=topic_id, title=post["title"], category_id=post.get("category_id"),
            created_at=post.get("created_at"), author=post.get("last_poster_username"),
            url=post["url"], excerpt=post.get("excerpt"), notified=0,
        )

    def mark_topic_notified(self, topic_id: int, site_key: str) -> list[str]:
        if self.db.mark_as_notified(topic_id):
            return []
        return [f"{site_key}:{topic_id}: notified_state_failed"]

    @staticmethod
    def format_target_error(post: dict, target: dict, error: str) -> str:
        return f"{post['_site_key']}:{post['_db_id']}:{target['key']}: {error}"
