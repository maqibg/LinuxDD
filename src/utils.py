"""公共工具函数。"""


def get_post_sort_key(post: dict) -> str:
    """返回帖子排序时间键，优先使用发帖时间。"""
    return (
        post.get("created_at")
        or post.get("last_posted_at")
        or post.get("bumped_at")
        or ""
    )
