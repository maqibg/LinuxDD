"""进程单实例锁。"""

import logging
import os
from pathlib import Path

if os.name == "nt":
    import ctypes

    _KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)

try:
    import fcntl
except ImportError:
    fcntl = None

logger = logging.getLogger(__name__)


class SingleInstanceLock:
    """避免重复启动多个监控实例。"""

    def __init__(self, lock_file: str = "data/monitor.lock"):
        self.lock_file = Path(lock_file)
        self.lock_file.parent.mkdir(parents=True, exist_ok=True)
        self.lock_fd = None

    def acquire(self) -> bool:
        if fcntl is None:
            return self._acquire_by_exclusive_create()
        try:
            self.lock_fd = open(self.lock_file, "w")
            fcntl.flock(self.lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.lock_fd.write(str(os.getpid()))
            self.lock_fd.flush()
            logger.info("进程锁已获取 (PID: %s)", os.getpid())
            return True
        except (IOError, OSError) as exc:
            if self.lock_fd:
                self.lock_fd.close()
                self.lock_fd = None
            logger.error("无法获取进程锁: %s", exc)
            return False

    def _acquire_by_exclusive_create(self) -> bool:
        return self._try_exclusive_create(remove_stale=True)

    def _try_exclusive_create(self, remove_stale: bool) -> bool:
        try:
            fd = os.open(str(self.lock_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            self.lock_fd = os.fdopen(fd, "w")
            self.lock_fd.write(str(os.getpid()))
            self.lock_fd.flush()
            logger.info("进程锁已获取 (PID: %s)", os.getpid())
            return True
        except FileExistsError:
            if remove_stale and self._remove_stale_lock():
                return self._try_exclusive_create(remove_stale=False)
            logger.error("无法获取进程锁，可能已有实例在运行")
            return False

    def _remove_stale_lock(self) -> bool:
        lock_pid = self._read_lock_pid()
        if lock_pid is not None and self._is_pid_running(lock_pid):
            return False
        try:
            self.lock_file.unlink()
            logger.warning("已清理过期进程锁: %s", self.lock_file)
            return True
        except OSError as exc:
            logger.error("清理过期进程锁失败: %s", exc)
            return False

    def _read_lock_pid(self) -> int | None:
        try:
            raw_pid = self.lock_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            logger.warning("读取进程锁失败: %s", exc)
            return None
        if not raw_pid.isdigit():
            logger.warning("进程锁内容无效: %s", raw_pid)
            return None
        return int(raw_pid)

    @staticmethod
    def _is_pid_running(pid: int) -> bool:
        if pid <= 0:
            return False
        if os.name == "nt":
            return _is_windows_pid_running(pid)
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def release(self) -> None:
        if not self.lock_fd:
            return
        try:
            if fcntl is not None:
                fcntl.flock(self.lock_fd.fileno(), fcntl.LOCK_UN)
            self.lock_fd.close()
            self.lock_file.unlink(missing_ok=True)
            logger.info("进程锁已释放")
        except Exception as exc:
            logger.warning("释放进程锁失败: %s", exc)
        finally:
            self.lock_fd = None


def _is_windows_pid_running(pid: int) -> bool:
    process_query_limited_information = 0x1000
    handle = _KERNEL32.OpenProcess(
        process_query_limited_information,
        False,
        pid,
    )
    if not handle:
        return ctypes.get_last_error() == 5
    _KERNEL32.CloseHandle(handle)
    return True
