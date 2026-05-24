"""命令行入口。"""

import atexit
import logging

from src.lock import SingleInstanceLock
from src.monitor import Monitor

logger = logging.getLogger(__name__)


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="多站点 Discourse 论坛监控系统")
    parser.add_argument("-c", "--config", default="config.yaml", help="配置文件路径 (默认: config.yaml)")
    parser.add_argument("--check-once", action="store_true", help="仅检查一次后退出")
    args = parser.parse_args()

    lock = SingleInstanceLock()
    if not lock.acquire():
        print("错误：已有监控实例在运行，请先停止后再启动")
        return 1

    atexit.register(lock.release)
    try:
        monitor = Monitor(args.config)
        if args.check_once:
            logger.info("执行单次检查...")
            count = monitor.check_new_posts()
            logger.info("检查完成，推送了 %s 条消息", count)
            monitor.stop()
        else:
            monitor.start()
        return 0
    except KeyboardInterrupt:
        logger.info("用户中断")
        return 0
    except Exception as exc:
        logger.error("程序异常退出: %s", exc, exc_info=True)
        return 1
    finally:
        lock.release()
