"""SQLite 去重数据库。"""

import logging
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional, Set

logger = logging.getLogger(__name__)


class Database:
    """线程安全的 SQLite 数据库管理器。"""

    def __init__(self, db_path: str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._lock = threading.Lock()
        self._connections: Set[sqlite3.Connection] = set()
        self._init_schema()
        logger.info("数据库已初始化: %s", self.db_path)

    @property
    def conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(str(self.db_path))
            self._local.conn.row_factory = sqlite3.Row
            self._local.conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._connections.add(self._local.conn)
        return self._local.conn

    def _init_schema(self) -> None:
        cursor = self.conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS topics (
                id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                category_id INTEGER,
                created_at TEXT,
                author TEXT,
                url TEXT,
                excerpt TEXT,
                first_seen_at TEXT DEFAULT CURRENT_TIMESTAMP,
                notified INTEGER DEFAULT 0
            )
            """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_notified_created
            ON topics(notified, created_at)
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS deliveries (
                topic_id INTEGER NOT NULL,
                target_key TEXT NOT NULL,
                delivered INTEGER DEFAULT 0,
                last_error TEXT,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (topic_id, target_key),
                FOREIGN KEY (topic_id) REFERENCES topics(id) ON DELETE CASCADE
            )
            """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_deliveries_topic_delivered
            ON deliveries(topic_id, delivered)
            """
        )
        self.conn.commit()

    def add_topic(
        self,
        topic_id: int,
        title: str,
        category_id: Optional[int] = None,
        created_at: Optional[str] = None,
        author: Optional[str] = None,
        url: Optional[str] = None,
        excerpt: Optional[str] = None,
        notified: int = 0,
    ) -> bool:
        try:
            cursor = self.conn.cursor()
            cursor.execute(
                """
                INSERT INTO topics
                (id, title, category_id, created_at, author, url, excerpt, notified)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title = excluded.title,
                    category_id = excluded.category_id,
                    created_at = excluded.created_at,
                    author = excluded.author,
                    url = excluded.url,
                    excerpt = excluded.excerpt,
                    notified = CASE
                        WHEN topics.notified = 1 THEN 1
                        ELSE excluded.notified
                    END
                """,
                (topic_id, title, category_id, created_at, author, url, excerpt, notified),
            )
            with self._lock:
                self.conn.commit()
            return True
        except sqlite3.Error as exc:
            logger.error("记录帖子失败 topic_id=%s error=%s", topic_id, exc)
            self.conn.rollback()
            return False

    def ensure_delivery_targets(self, topic_id: int, target_keys: list[str]) -> bool:
        if not target_keys:
            return True
        try:
            cursor = self.conn.cursor()
            cursor.executemany(
                """
                INSERT OR IGNORE INTO deliveries (topic_id, target_key, delivered)
                VALUES (?, ?, 0)
                """,
                [(topic_id, target_key) for target_key in target_keys],
            )
            with self._lock:
                self.conn.commit()
            return True
        except sqlite3.Error as exc:
            logger.error("初始化推送目标失败 topic_id=%s error=%s", topic_id, exc)
            self.conn.rollback()
            return False

    def get_delivered_targets(self, topic_id: int) -> set[str]:
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT target_key FROM deliveries
            WHERE topic_id = ? AND delivered = 1
            """,
            (topic_id,),
        )
        return {row["target_key"] for row in cursor.fetchall()}

    def mark_target_delivered(self, topic_id: int, target_key: str) -> bool:
        return self._upsert_delivery(topic_id, target_key, 1, None)

    def mark_target_failed(self, topic_id: int, target_key: str, error: str) -> bool:
        return self._upsert_delivery(topic_id, target_key, 0, error)

    def _upsert_delivery(
        self, topic_id: int, target_key: str, delivered: int, error: Optional[str]
    ) -> bool:
        try:
            cursor = self.conn.cursor()
            cursor.execute(
                """
                INSERT INTO deliveries (topic_id, target_key, delivered, last_error, updated_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(topic_id, target_key) DO UPDATE SET
                    delivered = excluded.delivered,
                    last_error = excluded.last_error,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (topic_id, target_key, delivered, error),
            )
            with self._lock:
                self.conn.commit()
            return True
        except sqlite3.Error as exc:
            logger.error("记录推送目标状态失败 topic_id=%s error=%s", topic_id, exc)
            self.conn.rollback()
            return False

    def are_all_targets_delivered(self, topic_id: int, target_keys: list[str]) -> bool:
        if not target_keys:
            return True
        return set(target_keys).issubset(self.get_delivered_targets(topic_id))

    def mark_as_notified(self, topic_id: int) -> bool:
        try:
            cursor = self.conn.cursor()
            cursor.execute("UPDATE topics SET notified = 1 WHERE id = ?", (topic_id,))
            with self._lock:
                self.conn.commit()
            return cursor.rowcount > 0
        except sqlite3.Error as exc:
            logger.error("标记帖子已推送失败 topic_id=%s error=%s", topic_id, exc)
            self.conn.rollback()
            return False

    def is_notified(self, topic_id: int) -> bool:
        cursor = self.conn.cursor()
        cursor.execute("SELECT id FROM topics WHERE id = ? AND notified = 1", (topic_id,))
        return cursor.fetchone() is not None

    def batch_is_notified(self, topic_ids: list[int]) -> set[int]:
        if not topic_ids:
            return set()
        placeholders = ",".join("?" * len(topic_ids))
        cursor = self.conn.cursor()
        cursor.execute(
            f"SELECT id FROM topics WHERE notified = 1 AND id IN ({placeholders})",
            topic_ids,
        )
        return {row["id"] for row in cursor.fetchall()}

    def get_statistics(self) -> dict:
        cursor = self.conn.cursor()
        cursor.execute("SELECT COUNT(*) as count FROM topics")
        total_records = cursor.fetchone()["count"]
        cursor.execute("SELECT COUNT(*) as count FROM topics WHERE notified = 1")
        notified_count = cursor.fetchone()["count"]
        cursor.execute("SELECT COUNT(*) as count FROM deliveries WHERE delivered = 1")
        delivered_count = cursor.fetchone()["count"]
        today = datetime.now().strftime("%Y-%m-%d")
        cursor.execute(
            """
            SELECT COUNT(*) as count FROM topics
            WHERE notified = 1 AND DATE(first_seen_at) = ?
            """,
            (today,),
        )
        today_count = cursor.fetchone()["count"]
        return {
            "total_records": total_records,
            "notified_count": notified_count,
            "delivered_count": delivered_count,
            "today_count": today_count,
        }

    def clear_old_records(self, days: int = 30) -> int:
        try:
            cursor = self.conn.cursor()
            cursor.execute(
                """
                DELETE FROM deliveries
                WHERE topic_id IN (
                    SELECT id FROM topics
                    WHERE DATE(first_seen_at) < DATE('now', '-' || ? || ' days')
                )
                """,
                (days,),
            )
            cursor.execute(
                """
                DELETE FROM topics
                WHERE DATE(first_seen_at) < DATE('now', '-' || ? || ' days')
                """,
                (days,),
            )
            deleted = cursor.rowcount
            with self._lock:
                self.conn.commit()
            return deleted
        except sqlite3.Error as exc:
            logger.error("清理旧记录失败: %s", exc)
            self.conn.rollback()
            return 0

    def clear_all_records(self) -> bool:
        try:
            self.conn.execute("DELETE FROM deliveries")
            self.conn.execute("DELETE FROM topics")
            with self._lock:
                self.conn.commit()
            return True
        except sqlite3.Error as exc:
            logger.error("清空记录失败: %s", exc)
            self.conn.rollback()
            return False

    def close(self) -> None:
        if hasattr(self._local, "conn") and self._local.conn:
            conn = self._local.conn
            try:
                conn.close()
                self._connections.discard(conn)
            finally:
                self._local.conn = None

    def close_all(self) -> None:
        closed_count = 0
        for conn in list(self._connections):
            try:
                conn.close()
                closed_count += 1
            except Exception as exc:
                logger.warning("关闭数据库连接失败: %s", exc)
        self._connections.clear()
        self._local = threading.local()
        logger.info("已关闭 %s 个数据库连接", closed_count)
