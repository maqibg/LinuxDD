"""浏览器会话管理。"""

import logging
import os
import time
from typing import Optional

from DrissionPage import Chromium, ChromiumOptions
from curl_cffi import requests

from src.browser_helpers import (
    apply_proxy_to_browser,
    best_effort_logout,
    build_proxy_url,
    build_requests_session_from_tab,
    get_tab_url,
    has_login_ready,
    has_reusable_login,
    log_login_wait_status,
    maybe_auto_fill_login,
)

logger = logging.getLogger(__name__)


class BrowserSessionManager:
    """管理 Chromium noVNC 登录和当前 Discourse 会话。"""

    def __init__(
        self,
        base_url: str,
        login_url: str,
        auth_config: dict,
        browser_config: dict,
        proxy_config: Optional[dict] = None,
        remote_browser_config: Optional[dict] = None,
        novnc_config: Optional[dict] = None,
        manual_review_notifier: Optional[object] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.login_url = login_url.rstrip("/")
        self.auth_config = auth_config or {}
        self.browser_config = browser_config or {}
        self.remote_browser_config = remote_browser_config or {}
        self.novnc_config = novnc_config or {}
        self.proxy_url = build_proxy_url(proxy_config)
        self.manual_review_notifier = manual_review_notifier
        self.manual_login = self._is_manual_login()
        self.username = self._get_credential("username", "username_env", "LINUX_DO_USERNAME")
        self.password = self._get_credential("password", "password_env", "LINUX_DO_PASSWORD")
        self.browser = None
        self.tab = None

    def _is_manual_login(self) -> bool:
        auth_mode = str(self.auth_config.get("mode", "browser")).strip().lower()
        remote_enabled = bool(self.remote_browser_config.get("enabled", False))
        return remote_enabled or auth_mode in {"manual", "manual_browser"}

    def _get_credential(self, value_key: str, env_key: str, default_env: str) -> str:
        value = str(self.auth_config.get(value_key, "")).strip()
        if value:
            return value
        env_name = str(self.auth_config.get(env_key, default_env)).strip() or default_env
        return str(os.environ.get(env_name, "")).strip()

    def start(self) -> requests.Session:
        try:
            self.browser, self.tab = self._build_browser()
            self.ensure_login()
            return self.build_session()
        except BaseException:
            self.close()
            raise

    def _build_browser(self):
        user_data_dir = str(self.browser_config.get("user_data_dir", "./chrome_profile"))
        headless = bool(self.browser_config.get("headless", True))
        debug_port = int(self.browser_config.get("debug_port", 9222))
        if self.manual_login and headless:
            raise ValueError("远程/手动登录模式必须设置 browser.headless=false")

        options = ChromiumOptions()
        options.set_user_data_path(user_data_dir)
        options.set_local_port(debug_port)
        apply_proxy_to_browser(options, self.proxy_url)
        self._apply_headless_options(options, headless)
        browser = None
        try:
            browser = Chromium(options)
            return browser, browser.latest_tab
        except BaseException:
            self._quit_browser(browser)
            raise

    def _apply_headless_options(self, options: ChromiumOptions, headless: bool) -> None:
        if headless:
            options.headless(True)
            options.set_argument("--headless=new")
        else:
            options.set_argument("--window-size", "1280,900")
        if self.browser_config.get("disable_infobars", True):
            options.set_argument("--disable-infobars")
        if self.browser_config.get("disable_automation_controlled", True):
            options.set_argument("--disable-blink-features", "AutomationControlled")
        if os.name == "posix" and self.browser_config.get("no_sandbox", True):
            options.set_argument("--no-sandbox")
        if os.name == "posix" and self.browser_config.get("disable_dev_shm_usage", True):
            options.set_argument("--disable-dev-shm-usage")

    def ensure_login(self) -> None:
        if self.tab is None:
            raise RuntimeError("浏览器尚未启动")
        self.tab.get(self.login_url)
        if self._has_reusable_login():
            print("[INFO] 访问 /login 后已自动跳转到首页，复用现有登录态。")
            self._notify_login_completed()
            return
        self._log_login_mode()
        self._notify_login_required()
        self._wait_for_login()
        self._notify_login_completed()

    def _has_reusable_login(self) -> bool:
        return has_reusable_login(
            self.tab,
            base_url=self.base_url,
            login_url=self.login_url,
            proxy_url=self.proxy_url,
        )

    def _log_login_mode(self) -> None:
        if self.manual_login:
            print("[WARN] 当前未复用到登录态，请通过 noVNC 远程浏览器手动完成登录。")
        else:
            print("[WARN] 当前未复用到登录态，开始自动填写账号密码登录...")

    def _notify_login_required(self) -> None:
        if self.manual_login and self.manual_review_notifier:
            if not self.manual_review_notifier.notify_login_required():
                raise RuntimeError("发送 noVNC 登录通知失败")

    def _notify_login_completed(self) -> None:
        if self.manual_review_notifier:
            self.manual_review_notifier.notify_login_completed()

    def _wait_for_login(self) -> None:
        timeout_seconds = int(self.novnc_config.get("login_wait_timeout", 600))
        start = time.time()
        last_status_log = 0.0
        last_fill_try = 0.0
        has_tried_fill = False

        while True:
            time.sleep(1)
            ready, has_cf, has_auth = has_login_ready(
                self.tab,
                base_url=self.base_url,
                login_url=self.login_url,
                proxy_url=self.proxy_url,
            )
            if ready:
                print("[INFO] 登录流程完成，检测到页面已跳转且 cookie 已就绪。")
                return
            last_fill_try, filled = self._try_auto_fill(last_fill_try)
            has_tried_fill = has_tried_fill or filled
            last_status_log = self._maybe_log_wait_status(
                last_status_log,
                has_auth,
                has_cf,
                has_tried_fill,
            )
            if time.time() - start > timeout_seconds:
                raise TimeoutError("等待登录超时：请检查 noVNC 远程浏览器中的 Cloudflare 挑战或登录状态。")

    def _try_auto_fill(self, last_fill_try: float) -> tuple[float, bool]:
        return maybe_auto_fill_login(
            self.tab,
            self.login_url,
            self.username,
            self.password,
            get_tab_url(self.tab),
            last_fill_try,
            self.manual_login,
        )

    def _maybe_log_wait_status(
        self,
        last_status_log: float,
        has_auth: bool,
        has_cf: bool,
        has_tried_fill: bool,
    ) -> float:
        now = time.time()
        if now - last_status_log < 10:
            return last_status_log
        log_login_wait_status(
            get_tab_url(self.tab),
            has_auth,
            has_cf,
            has_tried_fill,
            self.manual_login,
        )
        return now

    def build_session(self) -> requests.Session:
        if self.tab is None:
            raise RuntimeError("浏览器尚未启动")
        return build_requests_session_from_tab(
            self.tab,
            base_url=self.base_url,
            proxy_url=self.proxy_url,
        )

    def relogin(self) -> requests.Session:
        if self.tab is None:
            raise RuntimeError("浏览器尚未启动")
        print("[WARN] 检测到 403，尝试执行 logout 并重新登录...")
        best_effort_logout(self.tab, base_url=self.base_url)
        self.ensure_login()
        return self.build_session()

    def recover_after_fetch_failure(self) -> requests.Session:
        if self.tab is None:
            raise RuntimeError("浏览器尚未启动")
        if not self.manual_login:
            return self.relogin()
        self.open_site_home()
        self._wait_for_login()
        self._notify_login_completed()
        return self.build_session()

    def open_site_home(self) -> bool:
        if self.tab is None:
            return False
        try:
            self.tab.get(self.base_url)
            return True
        except Exception as exc:
            logger.warning("打开站点首页失败: %s", exc)
            return False

    def close(self) -> None:
        if self.browser is None:
            return
        self._quit_browser(self.browser)
        self.browser = None
        self.tab = None

    @staticmethod
    def _quit_browser(browser) -> None:
        if browser is None:
            return
        try:
            browser.quit()
        except Exception as exc:
            logger.warning("关闭浏览器失败: %s", exc)
