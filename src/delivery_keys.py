"""推送去重键生成。"""

import hashlib
import zlib


def topic_storage_id(site_key: str, topic_id: int) -> int:
    """为多站点生成 SQLite INTEGER 主键，兼容既有 linuxdo 记录。"""
    real_topic_id = int(topic_id)
    if site_key == "linuxdo":
        return real_topic_id
    site_hash = zlib.crc32(site_key.encode("utf-8")) & 0x7FFFFFFF
    if site_hash == 0:
        site_hash = 1
    return (site_hash << 32) | (real_topic_id & 0xFFFFFFFF)


def delivery_target_key(
    site_key: str,
    target_type: str,
    identifier: str,
) -> str:
    """生成不暴露 chat_id/channel_id 的目标键。"""
    digest = hashlib.sha256(f"{site_key}:{target_type}:{identifier}".encode("utf-8"))
    suffix = digest.hexdigest()[:16]
    return f"{site_key}:{target_type}:{suffix}"
