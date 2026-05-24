"""站点检查调度。"""

import time


def initial_due_times(sites: dict[str, dict]) -> dict[str, float]:
    now = time.monotonic()
    return {site_key: now for site_key in sites}


def due_site_keys(due_times: dict[str, float], now: float) -> list[str]:
    return [site_key for site_key, due_at in due_times.items() if due_at <= now]


def reschedule_sites(
    due_times: dict[str, float],
    sites: dict[str, dict],
    checked_keys: list[str],
    now: float,
) -> dict[str, float]:
    next_due = dict(due_times)
    for site_key in checked_keys:
        interval = float(sites[site_key]["config"].get("check_interval", 60))
        next_due[site_key] = now + interval
    return next_due


def seconds_until_next_due(due_times: dict[str, float], now: float) -> float:
    if not due_times:
        return 1.0
    wait_seconds = min(due_times.values()) - now
    return max(0.1, min(wait_seconds, 5.0))
