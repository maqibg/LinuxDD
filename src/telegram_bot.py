"""Telegram Bot 推送模块。"""

import logging
import re
import time
from datetime import datetime
from typing import Optional

from curl_cffi import requests

logger = logging.getLogger(__name__)

RETRYABLE_HTTP_STATUS = {500, 502, 503, 504}
TELEGRAM_BOT_URL_PATTERN = re.compile(r"(https://api\.telegram\.org/bot)[^/\s]+")


def sanitize_telegram_error(error: object, bot_token: str = "") -> str:
    text = str(error or "send_failed")
    if bot_token:
        text = text.replace(bot_token, "***")
    return TELEGRAM_BOT_URL_PATTERN.sub(r"\1***", text)


class TelegramBot:
    """封装 Telegram sendMessage，包含代理和重试处理。"""

    def __init__(self, bot_token: str, chat_id: str, proxy_config: Optional[dict] = None):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.api_url = f"https://api.telegram.org/bot{bot_token}"
        self.proxies = self._build_proxies(proxy_config)

    @staticmethod
    def _build_proxies(proxy_config: Optional[dict]) -> Optional[dict[str, str]]:
        if not proxy_config or not proxy_config.get("enabled", False):
            return None
        proxy_type = proxy_config.get("type", "http")
        proxy_host = proxy_config.get("host", "127.0.0.1")
        proxy_port = proxy_config.get("port", 7890)
        proxy_url = f"{proxy_type}://{proxy_host}:{proxy_port}"
        logger.debug("Telegram 代理已配置: %s", proxy_url)
        return {"http": proxy_url, "https": proxy_url}

    def send_message(
        self,
        message: str,
        parse_mode: str = None,
        disable_web_page_preview: bool = False,
        max_retries: int = 5,
    ) -> dict:
        if not self.chat_id:
            logger.warning("未配置推送目标 chat_id")
            return self._failure("no_chat_id")

        payload = self._build_payload(message, parse_mode, disable_web_page_preview)
        for attempt in range(1, max_retries + 1):
            result = self._try_send(payload, attempt, max_retries)
            if result.get("retry"):
                continue
            return result
        return self._failure("max_retries_exceeded")

    def _build_payload(
        self,
        message: str,
        parse_mode: str,
        disable_web_page_preview: bool,
    ) -> dict:
        payload = {
            "chat_id": self.chat_id,
            "text": message,
            "disable_web_page_preview": disable_web_page_preview,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        return payload

    def _try_send(self, payload: dict, attempt: int, max_retries: int) -> dict:
        try:
            response = requests.post(
                f"{self.api_url}/sendMessage",
                json=payload,
                impersonate="chrome",
                timeout=30,
                proxies=self.proxies,
            )
            return self._parse_http_response(response, attempt, max_retries)
        except requests.exceptions.Timeout:
            return self._handle_network_error("timeout", attempt, max_retries)
        except requests.exceptions.RequestException as exc:
            return self._handle_network_error(exc, attempt, max_retries)
        except Exception as exc:
            safe_error = self._safe_error(exc)
            logger.error("发送 Telegram 消息失败: %s", safe_error)
            return self._failure(safe_error)

    def _parse_http_response(self, response, attempt: int, max_retries: int) -> dict:
        status_code = int(getattr(response, "status_code", 0) or 0)
        result = self._read_json_response(response, status_code)
        if isinstance(result, dict) and result.get("ok"):
            return self._success_response(result)

        retry_result = self._retry_for_http_status(
            status_code,
            result,
            attempt,
            max_retries,
        )
        if retry_result is not None:
            return retry_result
        if isinstance(result, dict):
            return self._parse_error_response(result, attempt, max_retries)
        return self._failure(self._non_json_error(status_code))

    @staticmethod
    def _read_json_response(response, status_code: int) -> Optional[dict]:
        try:
            result = response.json()
            return result if isinstance(result, dict) else None
        except Exception as exc:
            logger.warning("Telegram 返回非 JSON 响应 status=%s error=%s", status_code, exc)
            return None

    @staticmethod
    def _success_response(result: dict) -> dict:
        if result.get("ok"):
            payload = result.get("result", {})
            return {
                "success": True,
                "message_id": payload.get("message_id"),
                "date": payload.get("date"),
                "error": None,
            }
        return TelegramBot._failure("telegram_not_ok")

    def _retry_for_http_status(
        self,
        status_code: int,
        result: Optional[dict],
        attempt: int,
        max_retries: int,
    ) -> Optional[dict]:
        if status_code == 429:
            retry_after = self._retry_after(result)
            return self._retry_or_fail("http_429", retry_after + 1, attempt, max_retries)
        if status_code in RETRYABLE_HTTP_STATUS:
            error = self._status_error(status_code, result)
            wait_time = min(3 * attempt, 15)
            return self._retry_or_fail(error, wait_time, attempt, max_retries)
        return None

    def _parse_error_response(self, result: dict, attempt: int, max_retries: int) -> dict:
        error_code = result.get("error_code")
        error_desc = self._safe_error(result.get("description", "未知错误"))
        if error_code == 429:
            retry_after = self._retry_after(result)
            return self._retry_or_fail(error_desc, retry_after + 1, attempt, max_retries)
        if error_code in RETRYABLE_HTTP_STATUS:
            wait_time = min(3 * attempt, 15)
            return self._retry_or_fail(error_desc, wait_time, attempt, max_retries)
        logger.error("Telegram 消息发送失败: %s", error_desc)
        return self._failure(error_desc)

    def _retry_or_fail(
        self,
        error: str,
        wait_time: int,
        attempt: int,
        max_retries: int,
    ) -> dict:
        safe_error = self._safe_error(error)
        if attempt >= max_retries:
            logger.error("Telegram 可重试错误达到最大次数: %s", safe_error)
            return self._failure(safe_error)
        logger.warning("Telegram 可重试错误: %s，等待 %s 秒后重试", safe_error, wait_time)
        time.sleep(wait_time)
        return {"retry": True}

    @staticmethod
    def _retry_after(result: Optional[dict]) -> int:
        if not isinstance(result, dict):
            return 5
        value = result.get("parameters", {}).get("retry_after", 5)
        try:
            return int(value)
        except (TypeError, ValueError):
            return 5

    @staticmethod
    def _status_error(status_code: int, result: Optional[dict]) -> str:
        if isinstance(result, dict) and result.get("description"):
            return str(result["description"])
        return f"http_{status_code}"

    @staticmethod
    def _non_json_error(status_code: int) -> str:
        if status_code:
            return f"http_{status_code}_non_json_response"
        return "invalid_json_response"

    def _handle_network_error(
        self,
        error: object,
        attempt: int,
        max_retries: int,
    ) -> dict:
        safe_error = self._safe_error(error)
        wait_time = min(3 * attempt, 15)
        logger.warning("Telegram 网络错误: %s，第 %s/%s 次", safe_error, attempt, max_retries)
        if attempt < max_retries:
            time.sleep(wait_time)
            return {"retry": True}
        return self._failure(safe_error)

    def _safe_error(self, error: object) -> str:
        return sanitize_telegram_error(error, self.bot_token)

    @staticmethod
    def _failure(error: str) -> dict:
        return {
            "success": False,
            "message_id": None,
            "date": None,
            "error": error,
        }

    def test_connection(self) -> bool:
        result = self.send_message(
            f"测试推送\n\nLinux.do 监控系统测试\n时间: {self._get_current_time()}"
        )
        return bool(result["success"])

    @staticmethod
    def _get_current_time() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def escape_html(text: str) -> str:
        escape_map = {
            "&": "&amp;",
            "<": "&lt;",
            ">": "&gt;",
            '"': "&quot;",
            "'": "&#39;",
        }
        result = text
        for char, escaped in escape_map.items():
            result = result.replace(char, escaped)
        return result
