"""Validate HTTP Retry-After without trusting arbitrary response header text."""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math
import re

MAX_RETRY_SECONDS = 7 * 24 * 60 * 60


def retry_after_seconds(value: str | None, now: datetime | None = None) -> int | None:
    if not isinstance(value, str):
        return None
    supplied = value.strip()
    if re.fullmatch(r"[0-9]{1,10}", supplied):
        seconds = int(supplied)
    else:
        try:
            when = parsedate_to_datetime(supplied)
            if when.tzinfo is None:
                return None
            current = now or datetime.now(timezone.utc)
            seconds = max(0, math.ceil((when - current).total_seconds()))
        except (ValueError, TypeError, IndexError, OverflowError):
            return None
    return seconds if 0 <= seconds <= MAX_RETRY_SECONDS else None


def rate_limit_message(retry_after: str | None, now: datetime | None = None) -> str:
    seconds = retry_after_seconds(retry_after, now)
    prefix = "ChatGPT web rate limit reached"
    if seconds is None:
        return prefix
    return f"{prefix}; retry after {seconds} seconds"
