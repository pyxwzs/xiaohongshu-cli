"""Validated models for the lead discovery pipeline."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class ScoreBreakdown(BaseModel):
    need: int = Field(ge=0, le=30)
    skill: int = Field(ge=0, le=25)
    payment: int = Field(ge=0, le=25)
    recency: int = Field(ge=0, le=10)
    clarity: int = Field(ge=0, le=10)

    @property
    def total(self) -> int:
        return self.need + self.skill + self.payment + self.recency + self.clarity


class LeadAnalysis(BaseModel):
    is_help_request: bool
    service_category: str
    skill_match: bool
    score: int = Field(ge=0, le=100)
    score_breakdown: ScoreBreakdown
    reason: str = Field(min_length=1, max_length=1000)
    uncertainties: list[str] = Field(default_factory=list)
    suggested_comment: str = Field(default="", max_length=1000)
    requires_human_review: bool = True

    @model_validator(mode="after")
    def validate_score_total(self):
        if self.score != self.score_breakdown.total:
            raise ValueError("score must equal the sum of score_breakdown")
        return self


class NormalizedPost(BaseModel):
    post_id: str
    url: str
    title: str = ""
    content: str = ""
    author_name: str = ""
    author_id: str = ""
    published_at: datetime | None = None
    comment_count: int | None = None
    comment_count_source: Literal["search", "detail", "comments", "unknown"] = "unknown"
    comment_count_reliable: bool = False
    xsec_token: str = ""
    xsec_source: str = "pc_search"
    keyword: str = ""


class LeadConfig(BaseModel):
    enabled: bool = False
    schedule_minutes: int = Field(default=60, ge=5)
    max_posts_per_run: int = Field(default=50, ge=1, le=500)
    max_ai_calls_per_run: int = Field(default=20, ge=0, le=200)
    min_score: int = Field(default=75, ge=0, le=100)
    recent_hours: int = Field(default=72, ge=1, le=24 * 30)
    require_zero_comments: bool = True
    require_human_approval: bool = True
    auto_publish_comment: bool = False
    auto_send_dm: bool = False
    keywords: list[str] = Field(default_factory=list)
    ai_base_url_env: str = "LEAD_AI_BASE_URL"
    ai_api_key_env: str = "LEAD_AI_API_KEY"
    ai_model_env: str = "LEAD_AI_MODEL"

    @model_validator(mode="after")
    def enforce_manual_contact(self):
        if self.auto_publish_comment or self.auto_send_dm:
            raise ValueError("automatic comments and DMs are not supported")
        return self
