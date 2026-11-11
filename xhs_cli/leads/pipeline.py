"""Single-run lead discovery pipeline."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from ..client import XhsClient
from ..exceptions import IpBlockedError, NeedVerifyError, SessionExpiredError, XhsApiError
from .ai_client import PROMPT_VERSION, AnalysisUnavailable, LeadAnalyzer
from .filter import prefilter_reason
from .models import LeadConfig, NormalizedPost
from .normalizer import merge_detail, normalize_search_item
from .storage import LeadRepository


class LeadPipeline:
    def __init__(self, client: XhsClient, repository: LeadRepository, config: LeadConfig,
                 analyzer: LeadAnalyzer | None = None):
        self.client = client
        self.repository = repository
        self.config = config
        self.analyzer = analyzer or LeadAnalyzer(config)

    def verify_post(self, post: NormalizedPost) -> NormalizedPost:
        if post.comment_count is not None:
            return post
        try:
            detail = self.client.get_note_detail(
                post.post_id, xsec_token=post.xsec_token, xsec_source=post.xsec_source
            )
            post = merge_detail(post, detail)
        except XhsApiError:
            return post
        if post.comment_count is not None or not post.xsec_token:
            return post
        try:
            comments = self.client.get_comments(
                post.post_id, xsec_token=post.xsec_token, xsec_source=post.xsec_source
            )
        except XhsApiError:
            return post
        values = comments.get("comments") if isinstance(comments, dict) else None
        if isinstance(values, list):
            if not values and bool(comments.get("has_more")):
                return post
            return post.model_copy(update={
                "comment_count": len(values),
                "comment_count_source": "comments",
                "comment_count_reliable": True,
            })
        return post

    def run(self) -> dict[str, Any]:
        summary: dict[str, Any] = {"keywords": 0, "fetched": 0, "new": 0, "qualified": 0,
                                   "recommended": 0, "needs_verification": 0,
                                   "notifications": [], "errors": []}
        remaining_posts = self.config.max_posts_per_run
        remaining_ai = self.config.max_ai_calls_per_run
        cutoff = datetime.now(timezone.utc) - timedelta(hours=self.config.recent_hours)
        seen: set[str] = set()
        for keyword in self.config.keywords:
            if remaining_posts <= 0:
                break
            summary["keywords"] += 1
            run_id = self.repository.start_run(keyword)
            fetched = new = qualified = 0
            try:
                payload = self.client.search_notes(keyword, page=1, page_size=min(20, remaining_posts),
                                                   sort="time_descending", note_type=0)
                items = payload.get("items", []) if isinstance(payload, dict) else []
                fetched = len(items)
                summary["fetched"] += fetched
                for item in items:
                    if remaining_posts <= 0:
                        break
                    post = normalize_search_item(item, keyword)
                    if post is None:
                        continue
                    remaining_posts -= 1
                    if post.post_id in seen:
                        self.repository.upsert_post(post, run_id)
                        continue
                    seen.add(post.post_id)
                    post = self.verify_post(post)
                    is_new = self.repository.upsert_post(post, run_id)
                    new += int(is_new)
                    summary["new"] += int(is_new)
                    if self.repository.get_status(post.post_id) in {
                        "approved", "skipped", "contacted", "quoted", "won", "lost"
                    }:
                        continue
                    if post.comment_count is None or not post.comment_count_reliable:
                        self.repository.set_status(post.post_id, "needs_verification")
                        summary["needs_verification"] += 1
                        continue
                    if self.config.require_zero_comments and post.comment_count > 0:
                        self.repository.set_status(post.post_id, "filtered", "comment_count > 0")
                        continue
                    if post.published_at and post.published_at < cutoff:
                        self.repository.set_status(post.post_id, "filtered", "outside recent window")
                        continue
                    rule_reason = prefilter_reason(post)
                    if rule_reason:
                        self.repository.set_status(post.post_id, "filtered", rule_reason)
                        continue
                    qualified += 1
                    summary["qualified"] += 1
                    if self.repository.has_analysis(post.post_id, self.analyzer.model, PROMPT_VERSION):
                        continue
                    if remaining_ai <= 0:
                        self.repository.set_status(post.post_id, "pending_ai")
                        continue
                    remaining_ai -= 1
                    try:
                        analysis = self.analyzer.analyze(post)
                    except AnalysisUnavailable as exc:
                        self.repository.set_status(post.post_id, "pending_ai", str(exc))
                        continue
                    self.repository.save_analysis(post.post_id, analysis, self.analyzer.model, PROMPT_VERSION)
                    if analysis.is_help_request and analysis.skill_match and analysis.score >= self.config.min_score:
                        self.repository.set_status(post.post_id, "pending_review")
                        summary["recommended"] += 1
                        if self.repository.add_event_once(post.post_id, "notified", analysis.reason):
                            summary["notifications"].append({
                                "post_id": post.post_id,
                                "title": post.title,
                                "url": post.url,
                                "score": analysis.score,
                                "reason": analysis.reason,
                                "draft_comment": analysis.suggested_comment,
                            })
                    else:
                        self.repository.set_status(post.post_id, "filtered", analysis.reason)
                self.repository.finish_run(run_id, fetched=fetched, new=new, qualified=qualified)
            except Exception as exc:
                self.repository.finish_run(run_id, fetched=fetched, new=new, qualified=qualified,
                                           status="failed", error=str(exc))
                summary["errors"].append({"keyword": keyword, "error": str(exc)})
                if isinstance(exc, (NeedVerifyError, IpBlockedError, SessionExpiredError)):
                    break
        return summary
