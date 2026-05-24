"""noVNC 人工处理通知。"""

import logging
from datetime import datetime

from src.telegram_bot import TelegramBot

logger = logging.getLogger(__name__)

DEFAULT_NOVNC_PORT = 6080
MAX_ERROR_TEXT_LENGTH = 300


class ManualReviewNotifier:
    """向指定 Telegram 用户发送 noVNC 人工处理入口。"""

    def __init__(
        self,
        site_key: str,
        site_name: str,
        base_url: str,
        login_url: str,
        config: dict,
        novnc_config: dict | None,
        bot: TelegramBot,
    ):
        self.site_key = site_key
        self.site_name = site_name
        self.base_url = base_url.rstrip("/")
        self.login_url = login_url.rstrip("/")
        self.config = config or {}
        self.novnc_config = novnc_config or {}
        self.bot = bot
        self.sent_events: set[str] = set()

    def notify_login_required(self) -> bool:
        if not self._event_enabled("on_login_required", True):
            return True
        return self._send_once("login_required", self._login_required_message())

    def notify_login_completed(self) -> None:
        self.sent_events.discard("login_required")

    def notify_fetch_error(self, error: object) -> bool:
        if not self._event_enabled("on_fetch_error", True):
            return True
        return self._send_once("fetch_error", self._fetch_error_message(error))

    def mark_fetch_ok(self) -> None:
        self.sent_events.discard("fetch_error")

    def _event_enabled(self, key: str, default: bool) -> bool:
        return bool(self.config.get(key, default))

    def _send_once(self, event_key: str, message: str) -> bool:
        if event_key in self.sent_events:
            return True
        result = self.bot.send_message(
            message,
            disable_web_page_preview=True,
            max_retries=3,
        )
        if not result.get("success"):
            logger.error("[%s] noVNC 人工处理通知发送失败: %s", self.site_key, result.get("error"))
            return False
        self.sent_events.add(event_key)
        logger.info("[%s] 已发送 noVNC 人工处理通知: %s", self.site_key, event_key)
        return True

    def _login_required_message(self) -> str:
        return "\n".join(
            [
                "[LinuxDD] 需要手动登录",
                "",
                f"站点: {self.site_name}",
                f"页面: {self.login_url}",
                f"noVNC: {self.vnc_url()}",
                "",
                "原因: 启动时未检测到可复用登录态，请打开 noVNC 完成登录和人机验证。",
                f"时间: {self._now()}",
            ]
        )

    def _fetch_error_message(self, error: object) -> str:
        return "\n".join(
            [
                "[LinuxDD] 获取帖子失败，需要人工处理",
                "",
                f"站点: {self.site_name}",
                f"页面: {self.base_url}",
                f"noVNC: {self.vnc_url()}",
                f"错误: {self._short_error(error)}",
                "",
                "处理: 打开 noVNC，在浏览器中完成 Cloudflare 验证或重新登录；程序会在后续检查自动恢复。",
                f"时间: {self._now()}",
            ]
        )

    def vnc_url(self) -> str:
        configured = self._config_value("vnc_url") or self._novnc_value("public_url")
        if configured:
            return configured
        host = self._public_host()
        port = self._public_port()
        scheme = self._config_value("scheme") or self._novnc_value("scheme") or "http"
        return f"{scheme}://{host}:{port}/vnc.html"

    def _public_host(self) -> str:
        host = self._config_value("public_host") or self._novnc_value("public_host")
        return self._strip_url_prefix(host) or "127.0.0.1"

    def _public_port(self) -> int:
        value = self._config_value("port") or self._novnc_value("port") or DEFAULT_NOVNC_PORT
        try:
            return int(value)
        except (TypeError, ValueError):
            return DEFAULT_NOVNC_PORT

    def _config_value(self, value_key: str) -> str:
        value = str(self.config.get(value_key, "")).strip()
        return value

    def _novnc_value(self, value_key: str) -> str:
        value = str(self.novnc_config.get(value_key, "")).strip()
        return value

    @staticmethod
    def _strip_url_prefix(value: str) -> str:
        return value.removeprefix("http://").removeprefix("https://").rstrip("/")

    @staticmethod
    def _short_error(error: object) -> str:
        text = str(error or "unknown_error").replace("\n", " ").strip()
        if len(text) <= MAX_ERROR_TEXT_LENGTH:
            return text
        return text[:MAX_ERROR_TEXT_LENGTH] + "..."

    @staticmethod
    def _now() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def retry_fetch_after_manual_review(site: dict, error: Exception) -> list[dict]:
    """通知人工处理，等待浏览器会话恢复后重试一次取帖。"""
    auth_manager = site.get("auth_manager")
    notifier = site.get("manual_review_notifier")
    if auth_manager and not notifier:
        raise RuntimeError("手动浏览器恢复需要启用 manual_review 通知") from error
    _open_review_page(auth_manager)
    _notify_fetch_error(site, error)
    if not auth_manager:
        raise RuntimeError(str(error)) from error
    session = auth_manager.recover_after_fetch_failure()
    site["api"].set_session(session)
    return site["api"].fetch_new_posts()


def mark_manual_review_ok(site: dict) -> None:
    notifier = site.get("manual_review_notifier")
    if notifier:
        notifier.mark_fetch_ok()


def _open_review_page(auth_manager: object) -> None:
    if auth_manager and hasattr(auth_manager, "open_site_home"):
        auth_manager.open_site_home()


def _notify_fetch_error(site: dict, error: Exception) -> None:
    notifier = site.get("manual_review_notifier")
    if notifier:
        if not notifier.notify_fetch_error(error):
            raise RuntimeError("发送 noVNC 人工处理通知失败") from error
