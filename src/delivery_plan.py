"""推送目标计算。"""

from src.delivery_keys import delivery_target_key


def build_delivery_targets(post: dict, site_key: str, site: dict) -> list[dict]:
    targets = []
    if site["channel_id"]:
        targets.append(
            {
                "kind": "channel",
                "key": delivery_target_key(site_key, "channel", site["channel_id"]),
            }
        )
    targets.extend(_build_subscriber_targets(post, site_key, site["subscribers"]))
    return targets


def _build_subscriber_targets(post: dict, site_key: str, subscribers: list[dict]) -> list[dict]:
    targets = []
    for subscriber in subscribers:
        if not subscriber["chat_id"] or not subscriber["bot"]:
            continue
        matched_keyword = subscriber["filter"].match_keyword(post)
        if not matched_keyword or subscriber["filter"].is_blocked(post):
            continue
        targets.append(
            {
                "kind": "subscriber",
                "key": delivery_target_key(site_key, "subscriber", subscriber["rule_id"]),
                "subscriber": subscriber,
                "keyword": matched_keyword,
            }
        )
    return targets
