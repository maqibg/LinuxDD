"""多站点 TG 监控主流程。"""

import logging
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from src.config_loader import ConfigLoader
from src.database import Database
from src.discourse_api import AuthenticationRequired
from src.manual_review import mark_manual_review_ok, retry_fetch_after_manual_review
from src.post_pusher import PostPusher
from src.site_schedule import (
    due_site_keys,
    initial_due_times,
    reschedule_sites,
    seconds_until_next_due,
)
from src.site_runtime import build_site_runtime

logger = logging.getLogger(__name__)


class Monitor:
    """多站点 Discourse 监控与 Telegram 推送。"""

    def __init__(self, config_path: str = "config.yaml"):
        self.config = ConfigLoader.load(config_path)
        self.setup_logging()
        self.db = Database(self.config["global"]["database"]["path"])
        self.sites: dict[str, dict] = {}
        self.running = False
        self._closed = False
        self.check_count: dict[str, int] = {}
        self.consecutive_failures = 0
        self.last_success_time: Optional[datetime] = None
        self.max_consecutive_failures = 5
        try:
            self._cleanup_old_data()
            self._init_sites()
            self.pusher = PostPusher(self.db, self.sites)
        except BaseException:
            self._cleanup_started_resources()
            raise
        if not self.sites:
            logger.warning("没有启用任何站点")

    def setup_logging(self) -> None:
        log_config = self.config["global"]["logging"]
        log_level = getattr(logging, log_config["level"].upper())
        log_file = Path(log_config["file"])
        log_file.parent.mkdir(parents=True, exist_ok=True)

        formatter = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        from logging.handlers import RotatingFileHandler

        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=log_config.get("max_bytes", 10485760),
            backupCount=log_config.get("backup_count", 3),
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)

        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)

        root_logger = logging.getLogger()
        root_logger.handlers.clear()
        root_logger.setLevel(log_level)
        root_logger.addHandler(file_handler)
        root_logger.addHandler(console_handler)

    def _cleanup_old_data(self) -> None:
        try:
            deleted = self.db.clear_old_records(days=30)
            if deleted > 0:
                logger.info("启动清理：删除 %s 条 30 天前的旧记录", deleted)
        except Exception as exc:
            logger.warning("清理旧数据失败: %s", exc)

    def _init_sites(self) -> None:
        for site_key, site_config in self.config["sites"].items():
            if not site_config.get("enabled", False):
                logger.info("站点 %s 未启用", site_config.get("name", site_key))
                continue
            runtime = build_site_runtime(site_key, site_config, self.config["global"])
            self.sites[site_key] = runtime
            self.check_count[site_key] = 0
            sub_count = len([sub for sub in runtime["subscribers"] if sub["chat_id"]])
            logger.info(
                "站点 %s 已启用 (关键词订阅者: %s)",
                site_config.get("name", site_key),
                sub_count,
            )

    def check_new_posts(self, site_keys: Optional[list[str]] = None) -> int:
        if not self.sites:
            logger.warning("没有可检查的站点")
            return 0
        selected_site_keys = self._select_site_keys(site_keys)
        if not selected_site_keys:
            logger.warning("没有匹配到可检查的站点: %s", site_keys)
            return 0
        try:
            site_results = self._collect_site_results(selected_site_keys)
            errors = self._collect_fetch_errors(site_results, selected_site_keys)
            all_posts = self._merge_posts(site_results)
            if not all_posts:
                if errors:
                    raise RuntimeError("站点抓取失败: " + "; ".join(errors))
                logger.info("所有站点均无新帖子")
                self._record_success()
                return 0
            pushed, push_errors = self._push_new_posts(all_posts)
            errors.extend(push_errors)
            if push_errors and pushed == 0:
                raise RuntimeError("本轮推送全部失败: " + "; ".join(push_errors))
            self._record_check_result(errors)
            return pushed
        except Exception as exc:
            self._record_failure(exc)
            raise

    def _select_site_keys(self, site_keys: Optional[list[str]]) -> list[str]:
        if site_keys is None:
            return list(self.sites)
        return [site_key for site_key in site_keys if site_key in self.sites]

    def _collect_site_results(self, site_keys: list[str]) -> dict[str, dict]:
        site_results = {}
        with ThreadPoolExecutor(max_workers=max(1, len(site_keys))) as executor:
            futures = {
                executor.submit(self._fetch_site_posts, site_key, site): site_key
                for site_key, site in self.sites.items()
                if site_key in site_keys
            }
            for future in as_completed(futures):
                site_key = futures[future]
                self.check_count[site_key] = self.check_count.get(site_key, 0) + 1
                try:
                    result = future.result()
                    site_results[site_key] = result
                    logger.info(
                        "=== [%s] 第 %s 次检查 ===",
                        result["name"],
                        self.check_count[site_key],
                    )
                except Exception as exc:
                    logger.error("[%s] 获取数据失败: %s", site_key, exc)
        return site_results

    @staticmethod
    def _collect_fetch_errors(site_results: dict[str, dict], site_keys: list[str]) -> list[str]:
        errors = []
        for site_key in site_keys:
            result = site_results.get(site_key)
            if not result:
                errors.append(f"{site_key}: no_result")
            elif result.get("error"):
                errors.append(f"{site_key}: {result['error']}")
        return errors

    def _fetch_site_posts(self, site_key: str, site: dict) -> Dict:
        result = {"site_key": site_key, "name": site["name"], "posts": [], "error": None}
        try:
            all_posts = site["api"].fetch_new_posts()
            mark_manual_review_ok(site)
        except AuthenticationRequired as exc:
            logger.error("[%s] 会话失效或被拒绝: %s", site["name"], exc, exc_info=True)
            all_posts = self._retry_with_manual_review(site, exc, result)
        except Exception as exc:
            logger.error("[%s] 获取帖子时出错: %s", site["name"], exc, exc_info=True)
            all_posts = self._retry_with_manual_review(site, exc, result)

        if not all_posts:
            logger.info("[%s] 未获取到新帖子", site["name"])
            return result
        logger.info("[%s] 获取到 %s 个帖子", site["name"], len(all_posts))
        filtered_posts = site["filter"].filter_posts(all_posts)
        logger.info("[%s] 过滤后剩余 %s 个帖子", site["name"], len(filtered_posts))
        result["posts"] = filtered_posts
        return result

    def _retry_with_manual_review(self, site: dict, error: Exception, result: dict) -> list[dict]:
        try:
            posts = retry_fetch_after_manual_review(site, error)
            mark_manual_review_ok(site)
            return posts
        except Exception as review_exc:
            logger.error("[%s] 人工处理后重试仍失败: %s", site["name"], review_exc, exc_info=True)
            result["error"] = str(review_exc)
            return []

    def _merge_posts(self, site_results: dict[str, dict]) -> list[dict]:
        all_posts = []
        for site_key, result in site_results.items():
            for post in result["posts"]:
                post["_site_key"] = site_key
                all_posts.append(post)
        return all_posts

    def _push_new_posts(self, all_posts: list[dict]) -> tuple[int, list[str]]:
        total_success, errors = self.pusher.push_new_posts(all_posts)
        logger.info("本轮检查完成，成功推送 %s 条消息", total_success)
        return total_success, errors

    def _record_check_result(self, errors: list[str]) -> None:
        if errors:
            self._record_failure(RuntimeError("; ".join(errors)))
        else:
            self._record_success()

    def _record_success(self) -> None:
        self.consecutive_failures = 0
        self.last_success_time = datetime.now()

    def _record_failure(self, error: Exception) -> None:
        self.consecutive_failures += 1
        logger.error("检查失败 (连续第 %s 次): %s", self.consecutive_failures, error)
        if self.consecutive_failures >= self.max_consecutive_failures:
            logger.critical(
                "系统健康告警：连续失败 %s 次！上次成功时间: %s",
                self.consecutive_failures,
                self.last_success_time or "从未成功",
            )

    def start(self) -> None:
        logger.info("=" * 60)
        logger.info("多站点 TG 监控系统启动")
        logger.info("=" * 60)
        signal.signal(signal.SIGINT, self._signal_handler)
        try:
            signal.signal(signal.SIGTERM, self._signal_handler)
        except Exception:
            pass
        self.running = True
        due_times = initial_due_times(self.sites)
        due_times = self._run_due_check(due_times)
        try:
            while self.running:
                time.sleep(seconds_until_next_due(due_times, time.monotonic()))
                due_times = self._run_due_check(due_times)
        except KeyboardInterrupt:
            logger.info("收到键盘中断信号")
        finally:
            self.stop()

    def _run_due_check(self, due_times: dict[str, float]) -> dict[str, float]:
        now = time.monotonic()
        site_keys = due_site_keys(due_times, now)
        if not site_keys:
            return due_times
        try:
            self.check_new_posts(site_keys)
        except Exception as exc:
            logger.error("本轮调度检查失败，等待下次检查: %s", exc)
        return reschedule_sites(due_times, self.sites, site_keys, time.monotonic())

    def stop(self) -> None:
        if self._closed:
            return
        logger.info("正在停止监控...")
        self.running = False
        self._cleanup_started_resources()
        logger.info("监控已停止")

    def _cleanup_started_resources(self) -> None:
        for site in self.sites.values():
            auth_manager = site.get("auth_manager")
            if auth_manager:
                auth_manager.close()
        self.db.close_all()
        self._closed = True

    def _signal_handler(self, signum, frame) -> None:
        logger.info("收到信号 %s", signum)
        self.stop()
        sys.exit(0)
