"""Cheap deterministic filters applied before paid AI analysis."""

from __future__ import annotations

import re

from .models import NormalizedPost

_AD_PATTERNS = (
    r"(?:专业|长期)?承接",
    r"在线接单",
    r"接(?:各种|各类|软件|小程序|网站|代码|python|java)",
    r"有需要(?:可以|直接)?(?:私信|联系|找我)",
    r"欢迎(?:私信|咨询|来聊)",
    r"价格(?:便宜|实惠|可谈)",
    r"按需报价",
    r"源码交付",
)

_RECRUITMENT_PATTERNS = (r"招聘", r"招募.*(?:开发|程序员)", r"寻找合伙人")


def prefilter_reason(post: NormalizedPost) -> str | None:
    """Return a deterministic rejection reason, or None when AI should decide."""
    text = f"{post.title}\n{post.content}".lower()
    ad_hits = sum(bool(re.search(pattern, text, re.IGNORECASE)) for pattern in _AD_PATTERNS)
    if ad_hits >= 2:
        return "obvious_service_ad"
    if any(re.search(pattern, text, re.IGNORECASE) for pattern in _RECRUITMENT_PATTERNS):
        return "recruitment_or_partnership"
    return None
