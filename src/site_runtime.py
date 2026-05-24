"""站点运行时构建。"""

from typing import Optional

from src.browser_auth import BrowserSessionManager
from src.config_loader import ConfigLoader
from src.discourse_api import DiscourseAPI
from src.filters import PostFilter
from src.manual_review import ManualReviewNotifier
from src.telegram_bot import TelegramBot


def build_site_runtime(site_key: str, site_config: dict, global_config: dict) -> dict:
    tg_config = site_config["telegram"]
    site_filter = site_config.get("filter", {})
    site_proxy_config = select_site_proxy(site_config, global_config)
    telegram_proxy_config = global_config.get("proxy")
    novnc_config = global_config.get("novnc", {}) or {}
    notifier = build_manual_review_notifier(site_key, site_config, telegram_proxy_config, novnc_config)
    auth_manager = build_auth_manager(site_config, site_proxy_config, novnc_config, notifier)
    api = build_api(site_config, site_proxy_config, auth_manager)
    return {
        "name": site_config.get("name", site_key),
        "config": site_config,
        "api": api,
        "auth_manager": auth_manager,
        "manual_review_notifier": notifier,
        "telegram_channel": build_channel_bot(tg_config, telegram_proxy_config),
        "filter": PostFilter(site_filter),
        "channel_id": tg_config.get("channel_id", ""),
        "subscribers": parse_subscribers(tg_config, telegram_proxy_config, site_filter),
    }


def select_site_proxy(site_config: dict, global_config: dict) -> Optional[dict]:
    site_proxy = site_config.get("proxy")
    if is_enabled_proxy(site_proxy):
        return site_proxy
    global_site_proxy = global_config.get("site_proxy")
    if is_enabled_proxy(global_site_proxy):
        return global_site_proxy
    return None


def is_enabled_proxy(proxy_config: Optional[dict]) -> bool:
    return isinstance(proxy_config, dict) and bool(proxy_config.get("enabled", False))


def build_manual_review_notifier(
    site_key: str,
    site_config: dict,
    proxy_config: Optional[dict],
    novnc_config: Optional[dict],
) -> Optional[ManualReviewNotifier]:
    tg_config = site_config["telegram"]
    review_config = tg_config.get("manual_review") or {}
    if not review_config.get("enabled", False):
        return None
    chat_id = resolve_manual_review_chat_id(tg_config, review_config)
    if not chat_id:
        raise ValueError(f"站点 {site_key} 的 manual_review 未解析到有效 chat_id")
    token = ConfigLoader.config_or_env(review_config, "bot_token", "bot_token_env")
    bot = TelegramBot(
        bot_token=token or tg_config["bot_token"],
        chat_id=chat_id,
        proxy_config=proxy_config,
    )
    return ManualReviewNotifier(
        site_key=site_key,
        site_name=site_config.get("name", site_key),
        base_url=site_config["base_url"],
        login_url=site_config.get("login_url", f"{site_config['base_url'].rstrip('/')}/login"),
        config=review_config,
        novnc_config=novnc_config,
        bot=bot,
    )


def resolve_manual_review_chat_id(tg_config: dict, review_config: dict) -> str:
    chat_id = ConfigLoader.config_or_env(review_config, "chat_id", "chat_id_env")
    if chat_id:
        return chat_id
    source = str(review_config.get("chat_id_from", "")).strip()
    if source == "channel_id":
        return str(tg_config.get("channel_id", "")).strip()
    if source == "first_keyword_subscriber":
        return first_keyword_subscriber_chat_id(tg_config)
    if source.lstrip("-").isdigit():
        return source
    return ""


def first_keyword_subscriber_chat_id(tg_config: dict) -> str:
    subscribers = tg_config.get("keyword_subscribers") or []
    for subscriber in subscribers:
        if isinstance(subscriber, dict) and subscriber.get("chat_id"):
            return str(subscriber["chat_id"]).strip()
    return ""


def build_auth_manager(
    site_config: dict,
    site_proxy_config: Optional[dict],
    novnc_config: Optional[dict],
    manual_review_notifier: Optional[ManualReviewNotifier] = None,
) -> Optional[BrowserSessionManager]:
    if not ConfigLoader.is_browser_auth_site(site_config):
        return None
    auth_cfg = site_config.get("auth", {}) or {}
    browser_cfg = site_config.get("browser", {}) or {}
    remote_cfg = site_config.get("remote_browser", {}) or {}
    login_url = site_config.get("login_url", f"{site_config['base_url'].rstrip('/')}/login")
    return BrowserSessionManager(
        base_url=site_config["base_url"],
        login_url=login_url,
        auth_config=auth_cfg,
        browser_config=browser_cfg,
        proxy_config=site_proxy_config,
        remote_browser_config=remote_cfg,
        novnc_config=novnc_config,
        manual_review_notifier=manual_review_notifier,
    )


def build_api(site_config: dict, site_proxy_config: Optional[dict], auth_manager) -> DiscourseAPI:
    api_key = None if auth_manager else str(site_config.get("user_api_key", "")).strip() or None
    session = auth_manager.start() if auth_manager else None
    return DiscourseAPI(
        base_url=site_config["base_url"],
        api_key=api_key,
        proxy_config=site_proxy_config,
        session=session,
    )


def build_channel_bot(tg_config: dict, proxy_config: Optional[dict]) -> Optional[TelegramBot]:
    channel_id = tg_config.get("channel_id", "")
    if not channel_id:
        return None
    return TelegramBot(
        bot_token=tg_config["bot_token"],
        chat_id=channel_id,
        proxy_config=proxy_config,
    )


def parse_subscribers(
    tg_config: dict,
    proxy_config: Optional[dict],
    site_filter: Optional[dict] = None,
) -> list[dict]:
    subscribers = []
    bot_token = tg_config["bot_token"]
    legacy_filter = site_filter or {}
    sub_configs = tg_config.get("keyword_subscribers") or []
    if has_active_subscriber_config(sub_configs):
        for sub_config in tg_config["keyword_subscribers"]:
            subscribers.append(build_subscriber(sub_config, bot_token, proxy_config))
    elif tg_config.get("keyword_chat_id") or tg_config.get("user_id"):
        chat_id = tg_config.get("keyword_chat_id", "") or tg_config.get("user_id", "")
        filter_config = {
            "keywords": legacy_filter.get("keywords", ""),
            "exclude_keywords": legacy_filter.get("exclude_keywords", ""),
        }
        subscribers.append(
            {
                "chat_id": chat_id,
                "name": "默认订阅者",
                "rule_id": build_subscriber_rule_id(chat_id, filter_config),
                "filter": PostFilter(filter_config),
                "bot": TelegramBot(
                    bot_token=bot_token,
                    chat_id=chat_id,
                    proxy_config=proxy_config,
                ),
            }
        )
    return subscribers


def build_subscriber(sub_config: dict, bot_token: str, proxy_config: Optional[dict]) -> dict:
    chat_id = sub_config.get("chat_id", "")
    name = sub_config.get("name", chat_id or "未命名")
    filter_config = {
        "keywords": sub_config.get("keywords", ""),
        "exclude_keywords": sub_config.get("exclude_keywords", ""),
    }
    return {
        "chat_id": chat_id,
        "name": name,
        "rule_id": build_subscriber_rule_id(chat_id, filter_config),
        "filter": PostFilter(filter_config),
        "bot": TelegramBot(
            bot_token=bot_token,
            chat_id=chat_id,
            proxy_config=proxy_config,
        )
        if chat_id
        else None,
    }


def build_subscriber_rule_id(chat_id: str, filter_config: dict) -> str:
    keywords = str(filter_config.get("keywords", "")).strip()
    exclude_keywords = str(filter_config.get("exclude_keywords", "")).strip()
    return f"{chat_id}:{keywords}:{exclude_keywords}"


def has_active_subscriber_config(subscribers: object) -> bool:
    if not isinstance(subscribers, list):
        return False
    return any(isinstance(subscriber, dict) and subscriber.get("chat_id") for subscriber in subscribers)
