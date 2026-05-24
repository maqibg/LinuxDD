"""浏览器登录辅助函数。"""

import logging
import time
from typing import Optional

from DrissionPage import ChromiumOptions
from curl_cffi import requests

logger = logging.getLogger(__name__)


def build_proxy_url(proxy_cfg: Optional[dict]) -> str | None:
    if not proxy_cfg or not proxy_cfg.get("enabled", False):
        return None
    proxy_type = proxy_cfg.get("type", "http")
    proxy_host = str(proxy_cfg.get("host", "127.0.0.1")).strip() or "127.0.0.1"
    proxy_port = str(proxy_cfg.get("port", 7890)).strip() or "7890"
    return f"{proxy_type}://{proxy_host}:{proxy_port}"


def apply_proxy_to_browser(browser_options: ChromiumOptions, proxy_url: str | None) -> None:
    if proxy_url:
        browser_options.set_proxy(proxy_url)


def apply_proxy_to_session(session: requests.Session, proxy_url: str | None) -> requests.Session:
    if proxy_url:
        session.trust_env = False
        session.proxies.update({"http": proxy_url, "https": proxy_url})
    return session


def get_cookie_names(tab) -> set[str]:
    return {str(cookie.get("name", "")) for cookie in tab.cookies()}


def has_auth_cookies(tab) -> bool:
    names = get_cookie_names(tab)
    return "_t" in names or "_forum_session" in names


def has_cf_cookies(tab) -> bool:
    names = get_cookie_names(tab)
    return any(name.startswith("cf") or name.startswith("__cf") for name in names)


def get_tab_url(tab) -> str:
    try:
        return str(getattr(tab, "url", "") or "")
    except Exception as exc:
        logger.debug("读取 tab.url 失败: %s", exc)
    try:
        return str(tab.run_js("return location.href") or "")
    except Exception as exc:
        logger.debug("通过 JS 读取当前 URL 失败: %s", exc)
        return ""


def is_redirected_from_login(tab, base_url: str, login_url: str) -> bool:
    current_url = get_tab_url(tab)
    if not current_url:
        return False
    if not current_url.startswith(base_url.rstrip("/")):
        return False
    return not current_url.startswith(login_url.rstrip("/"))


def get_tab_user_agent(tab) -> str:
    try:
        return str(tab.run_js("return navigator.userAgent") or "")
    except Exception as exc:
        logger.debug("读取浏览器 User-Agent 失败，使用默认值: %s", exc)
        return "Mozilla/5.0"


def make_base_headers(base_url: str, user_agent: str) -> dict[str, str]:
    return {
        "User-Agent": user_agent,
        "Accept": "application/json, text/plain, */*",
        "Referer": base_url.rstrip("/") + "/",
    }


def build_requests_session_from_tab(
    tab,
    base_url: str,
    proxy_url: str | None = None,
) -> requests.Session:
    session = requests.Session()
    session.headers.update(make_base_headers(base_url, get_tab_user_agent(tab)))
    apply_proxy_to_session(session, proxy_url)
    for cookie in tab.cookies():
        name = cookie.get("name")
        value = cookie.get("value")
        domain = cookie.get("domain")
        path = cookie.get("path", "/")
        if name and value:
            session.cookies.set(name, value, domain=domain, path=path)
    return session


def is_session_authenticated(
    tab,
    base_url: str,
    proxy_url: str | None = None,
) -> bool:
    if not has_auth_cookies(tab):
        return False
    try:
        session = build_requests_session_from_tab(tab, base_url=base_url, proxy_url=proxy_url)
        response = session.get(f"{base_url.rstrip('/')}/session/current.json", timeout=10)
        if response.status_code != 200:
            logger.debug("会话校验失败，HTTP 状态码: %s", response.status_code)
            return False
        payload = response.json()
        current_user = payload.get("current_user")
        return isinstance(current_user, dict) and bool(str(current_user.get("username", "")).strip())
    except Exception as exc:
        logger.debug("会话校验请求失败: %s", exc)
        return False


def has_login_ready(
    tab,
    base_url: str,
    login_url: str,
    proxy_url: str | None = None,
) -> tuple[bool, bool, bool]:
    redirected = is_redirected_from_login(tab, base_url=base_url, login_url=login_url)
    has_cf = has_cf_cookies(tab)
    has_auth = has_auth_cookies(tab)
    ready = redirected and has_auth and is_session_authenticated(tab, base_url, proxy_url)
    return ready, has_cf, has_auth


def has_reusable_login(
    tab,
    base_url: str,
    login_url: str,
    proxy_url: str | None = None,
) -> bool:
    redirected = is_redirected_from_login(tab, base_url=base_url, login_url=login_url)
    if not redirected or not has_auth_cookies(tab):
        return False
    return is_session_authenticated(tab, base_url=base_url, proxy_url=proxy_url)


def try_auto_fill_login(tab, username: str, password: str) -> bool:
    username_selectors = [
        "#login-account-name",
        "#login-account",
        "input[name='login']",
        "input[name='username']",
        "input[type='email']",
    ]
    password_selectors = [
        "#login-account-password",
        "input[name='password']",
        "input[type='password']",
    ]
    button_selectors = [
        "#login-button",
        "button[type='submit']",
        "button.btn-primary",
    ]

    for user_sel in username_selectors:
        for pass_sel in password_selectors:
            for btn_sel in button_selectors:
                try:
                    account_input = tab.ele(user_sel, timeout=2)
                    password_input = tab.ele(pass_sel, timeout=2)
                    login_button = tab.ele(btn_sel, timeout=2)
                    account_input.input(username, clear=True)
                    password_input.input(password, clear=True)
                    login_button.click()
                    return True
                except Exception:
                    continue
    return False


def best_effort_logout(tab, base_url: str) -> None:
    try:
        tab.get(f"{base_url.rstrip('/')}/logout")
    except Exception as exc:
        logger.debug("执行 logout 失败，继续重新登录流程: %s", exc)


def maybe_auto_fill_login(
    tab,
    login_url: str,
    username: str,
    password: str,
    current_url: str,
    last_fill_try: float,
    manual_login: bool,
) -> tuple[float, bool]:
    if manual_login:
        return last_fill_try, False
    now = time.time()
    if not username or not password:
        return last_fill_try, False
    if not current_url.startswith(login_url.rstrip("/")):
        return last_fill_try, False
    if now - last_fill_try < 8:
        return last_fill_try, False
    filled = try_auto_fill_login(tab, username=username, password=password)
    if filled:
        print("[INFO] 已执行自动填充并提交登录表单，等待跳转与 cookie 生效...")
    else:
        print("[WARN] 暂未定位到登录输入框，继续等待页面加载/挑战完成后重试。")
    return now, filled


def log_login_wait_status(
    current_url: str,
    has_auth: bool,
    has_cf: bool,
    has_tried_fill: bool,
    manual_login: bool,
) -> None:
    mode = "manual" if manual_login else "auto"
    print(
        f"[INFO] 等待登录完成... mode={mode} url={current_url or 'unknown'} "
        f"auth_cookie={has_auth} cf_cookie={has_cf} tried_fill={has_tried_fill}"
    )
