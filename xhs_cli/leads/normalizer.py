"""Normalize unstable Xiaohongshu payloads without inventing missing values."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from .models import NormalizedPost


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _timestamp(value: Any) -> datetime | None:
    parsed = _optional_int(value)
    if parsed is None:
        return None
    if parsed > 10_000_000_000:
        parsed //= 1000
    try:
        return datetime.fromtimestamp(parsed, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def normalize_search_item(item: dict[str, Any], keyword: str) -> NormalizedPost | None:
    card = item.get("note_card")
    if not isinstance(card, dict):
        return None
    post_id = str(item.get("id") or card.get("note_id") or "")
    if not post_id:
        return None
    token = str(item.get("xsec_token") or card.get("xsec_token") or "")
    interact_value = card.get("interact_info")
    interact: dict[str, Any] = interact_value if isinstance(interact_value, dict) else {}
    raw_count = interact.get("comment_count") if "comment_count" in interact else None
    count = _optional_int(raw_count)
    user_value = card.get("user")
    user: dict[str, Any] = user_value if isinstance(user_value, dict) else {}
    url = f"https://www.xiaohongshu.com/explore/{post_id}"
    if token:
        url += f"?xsec_token={quote(token)}&xsec_source=pc_search"
    return NormalizedPost(
        post_id=post_id,
        url=url,
        title=str(card.get("title") or card.get("display_title") or ""),
        content=str(card.get("desc") or ""),
        author_name=str(user.get("nickname") or user.get("nick_name") or ""),
        author_id=str(user.get("user_id") or ""),
        published_at=_timestamp(card.get("time")),
        comment_count=count,
        comment_count_source="search" if count is not None else "unknown",
        comment_count_reliable=count is not None,
        xsec_token=token,
        keyword=keyword,
    )


def merge_detail(post: NormalizedPost, payload: dict[str, Any]) -> NormalizedPost:
    items = payload.get("items")
    if not isinstance(items, list) or not items or not isinstance(items[0], dict):
        return post
    card = items[0].get("note_card")
    if not isinstance(card, dict):
        return post
    interact_value = card.get("interact_info")
    interact: dict[str, Any] = interact_value if isinstance(interact_value, dict) else {}
    raw_count = interact.get("comment_count") if "comment_count" in interact else None
    count = _optional_int(raw_count)
    user_value = card.get("user")
    user: dict[str, Any] = user_value if isinstance(user_value, dict) else {}
    updates: dict[str, Any] = {
        "title": str(card.get("title") or card.get("display_title") or post.title),
        "content": str(card.get("desc") or post.content),
        "author_name": str(user.get("nickname") or user.get("nick_name") or post.author_name),
        "author_id": str(user.get("user_id") or post.author_id),
        "published_at": _timestamp(card.get("time")) or post.published_at,
    }
    if count is not None:
        updates.update(comment_count=count, comment_count_source="detail", comment_count_reliable=True)
    return post.model_copy(update=updates)
