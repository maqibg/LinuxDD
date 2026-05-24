"""YAML 配置加载与校验。"""

import logging
import os
from pathlib import Path
from typing import Any, Dict

import yaml

logger = logging.getLogger(__name__)


class ConfigLoader:
    """配置加载器。"""

    @staticmethod
    def load(config_path: str) -> Dict[str, Any]:
        path = Path(config_path)
        if not path.exists():
            raise FileNotFoundError(f"配置文件不存在: {config_path}")
        try:
            with path.open("r", encoding="utf-8") as file_obj:
                config = yaml.safe_load(file_obj)
            ConfigLoader._validate_config(config)
            logger.info("配置文件加载成功: %s", config_path)
            return config
        except yaml.YAMLError as exc:
            logger.error("配置文件格式错误: %s", exc)
            raise

    @staticmethod
    def _validate_config(config: Dict[str, Any]) -> None:
        if not isinstance(config, dict):
            raise ValueError("配置顶层必须是 YAML 对象")
        for key in ("sites", "global"):
            if key not in config:
                raise ValueError(f"配置缺少必需项: {key}")
        if not config.get("sites"):
            raise ValueError("配置中没有定义任何站点")
        for site_key, site_config in config["sites"].items():
            ConfigLoader._validate_site(site_key, site_config)
        ConfigLoader._validate_global(config["global"])

    @staticmethod
    def _validate_site(site_key: str, site_config: dict) -> None:
        if not site_config.get("enabled", False):
            return
        ConfigLoader._require(site_config, "base_url", f"站点 {site_key} 缺少 base_url")
        ConfigLoader._validate_auth(site_key, site_config)
        ConfigLoader._validate_interval(site_key, site_config)
        ConfigLoader._validate_filter(site_key, site_config.get("filter", {}))
        ConfigLoader._validate_telegram(
            site_key,
            site_config.get("telegram", {}),
            site_config.get("filter", {}),
        )

    @staticmethod
    def _validate_auth(site_key: str, site_config: dict) -> None:
        if ConfigLoader.is_browser_auth_site(site_config):
            ConfigLoader._validate_browser_auth(site_key, site_config)
            return
        if not str(site_config.get("user_api_key", "")).strip():
            raise ValueError(f"站点 {site_key} 缺少 user_api_key 或浏览器登录配置")

    @staticmethod
    def _validate_browser_auth(site_key: str, site_config: dict) -> None:
        auth = site_config.get("auth", {}) or {}
        browser = site_config.get("browser", {})
        ConfigLoader._require(browser, "user_data_dir", f"站点 {site_key} 缺少 browser.user_data_dir")
        ConfigLoader._require(browser, "debug_port", f"站点 {site_key} 缺少 browser.debug_port")
        debug_port = browser.get("debug_port")
        if isinstance(debug_port, bool) or not ConfigLoader._is_positive_int(debug_port):
            raise ValueError(f"站点 {site_key} 的 browser.debug_port 必须是正整数")
        for key in ("no_sandbox", "disable_dev_shm_usage", "disable_infobars", "disable_automation_controlled"):
            if key in browser and not isinstance(browser[key], bool):
                raise ValueError(f"站点 {site_key} 的 browser.{key} 必须是布尔值")
        if ConfigLoader.is_manual_browser_site(site_config) and browser.get("headless") is not False:
            raise ValueError(f"站点 {site_key} 手动/远程浏览器登录必须设置 browser.headless=false")
        if not ConfigLoader.is_manual_browser_site(site_config):
            ConfigLoader._validate_auto_login_credentials(site_key, auth)

    @staticmethod
    def _validate_auto_login_credentials(site_key: str, auth_config: dict) -> None:
        has_username = ConfigLoader._has_credential(
            auth_config,
            "username",
            "username_env",
            "LINUX_DO_USERNAME",
        )
        has_password = ConfigLoader._has_credential(
            auth_config,
            "password",
            "password_env",
            "LINUX_DO_PASSWORD",
        )
        if not has_username or not has_password:
            raise ValueError(f"站点 {site_key} 自动浏览器登录必须配置用户名和密码或对应环境变量")

    @staticmethod
    def _validate_interval(site_key: str, site_config: dict) -> None:
        check_interval = site_config.get("check_interval", 60)
        if not isinstance(check_interval, (int, float)) or check_interval < 10:
            raise ValueError(f"站点 {site_key} 的 check_interval 必须 >= 10 秒")

    @staticmethod
    def _validate_filter(site_key: str, filter_config: dict) -> None:
        max_post_age = filter_config.get("max_post_age", 0)
        if not isinstance(max_post_age, (int, float)) or max_post_age < 0:
            raise ValueError(f"站点 {site_key} 的 max_post_age 必须 >= 0")

    @staticmethod
    def _validate_telegram(site_key: str, telegram: dict, site_filter: dict) -> None:
        if not telegram.get("bot_token"):
            raise ValueError(f"站点 {site_key} 的 Telegram 配置缺少 bot_token")
        has_channel = bool(telegram.get("channel_id"))
        subscribers = telegram.get("keyword_subscribers") or []
        has_active_subscribers = ConfigLoader._has_active_subscriber(subscribers)
        has_legacy = bool(telegram.get("keyword_chat_id") or telegram.get("user_id"))
        if has_active_subscribers and has_legacy:
            raise ValueError(f"站点 {site_key} 不能同时配置新版和旧版关键词订阅目标")
        if not has_channel and not has_active_subscribers and not has_legacy:
            raise ValueError(f"站点 {site_key} 至少需要 channel_id 或 keyword_subscribers")
        if subscribers:
            ConfigLoader._validate_subscribers(site_key, subscribers)
        if has_legacy and not str(site_filter.get("keywords", "")).strip():
            raise ValueError(f"站点 {site_key} 使用旧版关键词订阅时必须配置 filter.keywords")
        ConfigLoader._validate_manual_review(site_key, telegram.get("manual_review"))

    @staticmethod
    def _validate_manual_review(site_key: str, review_config: Any) -> None:
        if review_config is None:
            return
        if not isinstance(review_config, dict):
            raise ValueError(f"站点 {site_key} 的 manual_review 必须是字典")
        if not review_config.get("enabled", False):
            return
        if not ConfigLoader._has_manual_review_target(review_config):
            raise ValueError(
                f"站点 {site_key} 启用 manual_review 时必须配置 chat_id、chat_id_env 或 chat_id_from"
            )
        target = str(review_config.get("chat_id_from", "")).strip()
        if target and target not in {"channel_id", "first_keyword_subscriber"} and not ConfigLoader._is_chat_id(target):
            raise ValueError(f"站点 {site_key} 的 manual_review.chat_id_from 不支持: {target}")
        port = review_config.get("port")
        if port is not None and not ConfigLoader._is_positive_int(port):
            raise ValueError(f"站点 {site_key} 的 manual_review.port 必须是正整数")
        for key in ("on_login_required", "on_fetch_error"):
            if key in review_config and not isinstance(review_config[key], bool):
                raise ValueError(f"站点 {site_key} 的 manual_review.{key} 必须是布尔值")

    @staticmethod
    def _has_manual_review_target(review_config: dict) -> bool:
        if ConfigLoader.config_or_env(review_config, "chat_id", "chat_id_env"):
            return True
        return bool(str(review_config.get("chat_id_from", "")).strip())

    @staticmethod
    def _is_chat_id(value: str) -> bool:
        return value.lstrip("-").isdigit()

    @staticmethod
    def _has_active_subscriber(subscribers: Any) -> bool:
        if not isinstance(subscribers, list):
            return False
        return any(
            isinstance(subscriber, dict) and bool(subscriber.get("chat_id"))
            for subscriber in subscribers
        )

    @staticmethod
    def _validate_subscribers(site_key: str, subscribers: list) -> None:
        if not isinstance(subscribers, list):
            raise ValueError(f"站点 {site_key} 的 keyword_subscribers 必须是列表")
        for index, subscriber in enumerate(subscribers):
            if not isinstance(subscriber, dict):
                raise ValueError(f"站点 {site_key} 的 keyword_subscribers[{index}] 必须是字典")
            if not subscriber.get("keywords"):
                raise ValueError(f"站点 {site_key} 的 keyword_subscribers[{index}] 缺少 keywords")

    @staticmethod
    def _validate_global(global_config: dict) -> None:
        database = global_config.get("database", {})
        logging_config = global_config.get("logging", {})
        ConfigLoader._require(database, "path", "global.database 缺少 path")
        ConfigLoader._require(logging_config, "file", "global.logging 缺少 file")
        ConfigLoader._require(logging_config, "level", "global.logging 缺少 level")
        ConfigLoader._validate_novnc(global_config.get("novnc", {}))

    @staticmethod
    def _validate_novnc(novnc_config: Any) -> None:
        if novnc_config is None:
            return
        if not isinstance(novnc_config, dict):
            raise ValueError("global.novnc 必须是字典")
        for key in ("enabled",):
            if key in novnc_config and not isinstance(novnc_config[key], bool):
                raise ValueError(f"global.novnc.{key} 必须是布尔值")
        for key in ("port", "vnc_port", "login_wait_timeout", "display_number"):
            if key in novnc_config and not ConfigLoader._is_positive_int(novnc_config[key]):
                raise ValueError(f"global.novnc.{key} 必须是正整数")
        listen_host = str(novnc_config.get("listen_host", "")).strip()
        if listen_host and any(char.isspace() for char in listen_host):
            raise ValueError("global.novnc.listen_host 不能包含空白字符")

    @staticmethod
    def is_browser_auth_site(site_config: dict) -> bool:
        auth_mode = str(site_config.get("auth", {}).get("mode", "")).lower()
        remote_enabled = bool(site_config.get("remote_browser", {}).get("enabled", False))
        return auth_mode in {"browser", "manual", "manual_browser"} or remote_enabled

    @staticmethod
    def is_manual_browser_site(site_config: dict) -> bool:
        auth_mode = str(site_config.get("auth", {}).get("mode", "")).lower()
        remote_enabled = bool(site_config.get("remote_browser", {}).get("enabled", False))
        return auth_mode in {"manual", "manual_browser"} or remote_enabled

    @staticmethod
    def _require(config: dict, key: str, message: str) -> None:
        if not config.get(key):
            raise ValueError(message)

    @staticmethod
    def _has_credential(
        config: dict,
        value_key: str,
        env_key: str,
        default_env: str,
    ) -> bool:
        return bool(ConfigLoader.config_or_env(config, value_key, env_key, default_env))

    @staticmethod
    def config_or_env(
        config: dict,
        value_key: str,
        env_key: str,
        default_env: str = "",
    ) -> str:
        value = str(config.get(value_key, "")).strip()
        if value:
            return value
        env_name = str(config.get(env_key, default_env)).strip() or default_env
        if not env_name:
            return ""
        return str(os.environ.get(env_name, "")).strip()

    @staticmethod
    def _is_positive_int(value: Any) -> bool:
        try:
            return int(value) > 0
        except (TypeError, ValueError):
            return False
