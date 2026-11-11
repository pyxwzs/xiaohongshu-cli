from datetime import datetime, timezone

from xhs_cli.leads.config import create_default_config, load_config
from xhs_cli.leads.filter import prefilter_reason
from xhs_cli.leads.models import LeadAnalysis, LeadConfig, NormalizedPost, ScoreBreakdown
from xhs_cli.leads.normalizer import merge_detail, normalize_search_item
from xhs_cli.leads.pipeline import LeadPipeline
from xhs_cli.leads.storage import LeadRepository


def _item(post_id="p1", comments="0"):
    interact = {} if comments is None else {"comment_count": comments}
    return {
        "id": post_id,
        "xsec_token": "token",
        "note_card": {
            "display_title": "Python 作业求助",
            "desc": "代码报错，想找人帮忙调试，可以付费",
            "time": int(datetime.now(timezone.utc).timestamp() * 1000),
            "user": {"nickname": "student", "user_id": "u1"},
            "interact_info": interact,
        },
    }


class FakeClient:
    def __init__(self, items, detail=None, comments=None):
        self.items = items
        self.detail = detail or {"items": []}
        self.comments = comments or {"comments": []}

    def search_notes(self, *_args, **_kwargs):
        return {"items": self.items, "has_more": False}

    def get_note_detail(self, *_args, **_kwargs):
        return self.detail

    def get_comments(self, *_args, **_kwargs):
        return self.comments


class FakeAnalyzer:
    model = "fake-model"

    def __init__(self):
        self.calls = 0

    def analyze(self, _post):
        self.calls += 1
        return LeadAnalysis(
            is_help_request=True,
            service_category="python_debugging",
            skill_match=True,
            score=90,
            score_breakdown=ScoreBreakdown(need=30, skill=25, payment=20, recency=10, clarity=5),
            reason="明确的付费代码调试需求",
            uncertainties=[],
            suggested_comment="可以先提供报错和最小复现代码。",
        )


def test_normalizer_preserves_unknown_comment_count():
    post = normalize_search_item(_item(comments=None), "代码求助")
    assert post is not None
    assert post.comment_count is None
    assert post.comment_count_reliable is False


def test_merge_detail_promotes_explicit_zero_to_reliable():
    post = normalize_search_item(_item(comments=None), "代码求助")
    detail = {"items": [{"note_card": {"interact_info": {"comment_count": "0"}}}]}
    merged = merge_detail(post, detail)
    assert merged.comment_count == 0
    assert merged.comment_count_source == "detail"
    assert merged.comment_count_reliable is True


def test_analysis_rejects_mismatched_total():
    try:
        LeadAnalysis(
            is_help_request=True,
            service_category="web",
            skill_match=True,
            score=99,
            score_breakdown=ScoreBreakdown(need=1, skill=1, payment=1, recency=1, clarity=1),
            reason="x",
        )
    except ValueError:
        pass
    else:
        raise AssertionError("mismatched score must fail validation")


def test_pipeline_recommends_zero_comment_help_request(tmp_path):
    config = LeadConfig(enabled=True, keywords=["代码求助"], min_score=75)
    with LeadRepository(tmp_path / "leads.db") as repository:
        summary = LeadPipeline(FakeClient([_item()]), repository, config, FakeAnalyzer()).run()
        rows = repository.list_leads(status="pending_review")
    assert summary["recommended"] == 1
    assert len(rows) == 1
    assert rows[0]["score"] == 90


def test_pipeline_notifies_same_post_only_once(tmp_path):
    config = LeadConfig(enabled=True, keywords=["代码求助"], min_score=75)
    analyzer = FakeAnalyzer()
    with LeadRepository(tmp_path / "leads.db") as repository:
        pipeline = LeadPipeline(FakeClient([_item()]), repository, config, analyzer)
        first = pipeline.run()
        second = pipeline.run()
    assert len(first["notifications"]) == 1
    assert second["notifications"] == []
    assert analyzer.calls == 1


def test_pipeline_filters_nonzero_comments_before_ai(tmp_path):
    config = LeadConfig(enabled=True, keywords=["代码求助"])
    with LeadRepository(tmp_path / "leads.db") as repository:
        summary = LeadPipeline(FakeClient([_item(comments="2")]), repository, config, FakeAnalyzer()).run()
        rows = repository.list_leads(status="filtered")
    assert summary["recommended"] == 0
    assert len(rows) == 1


def test_repository_tracks_multiple_keyword_hits_without_duplicate_posts(tmp_path):
    post = NormalizedPost(post_id="p1", url="https://example/p1", keyword="first")
    with LeadRepository(tmp_path / "leads.db") as repository:
        run1 = repository.start_run("first")
        assert repository.upsert_post(post, run1) is True
        run2 = repository.start_run("second")
        assert repository.upsert_post(post.model_copy(update={"keyword": "second"}), run2) is False
        count = repository.connection.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
        hits = repository.connection.execute("SELECT COUNT(*) FROM post_hits").fetchone()[0]
    assert count == 1
    assert hits == 2


def test_prefilter_rejects_obvious_service_ad_but_keeps_help_request():
    ad = NormalizedPost(
        post_id="ad",
        url="https://example/ad",
        title="专业承接 Python",
        content="各种代码都能接，有需要直接私信，价格便宜",
    )
    request = NormalizedPost(
        post_id="request",
        url="https://example/request",
        title="Python 报错求助",
        content="课程代码运行失败，有人可以帮忙看看吗？可以付费。",
    )
    assert prefilter_reason(ad) == "obvious_service_ad"
    assert prefilter_reason(request) is None


def test_create_and_load_safe_default_config(tmp_path):
    path = create_default_config(tmp_path / "lead_agent.yaml")
    config = load_config(path)
    assert config.enabled is False
    assert config.auto_publish_comment is False
    assert config.auto_send_dm is False
    assert "计算机作业求助" in config.keywords
