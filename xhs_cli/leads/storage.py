"""SQLite persistence for discovered leads."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..cookies import get_account_dir
from .models import LeadAnalysis, NormalizedPost

SCHEMA_VERSION = 1


def default_database_path() -> Path:
    return get_account_dir() / "leads.sqlite3"


class LeadRepository:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else default_database_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self._migrate()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def _migrate(self) -> None:
        self.connection.executescript("""
        CREATE TABLE IF NOT EXISTS schema_meta(version INTEGER NOT NULL);
        INSERT INTO schema_meta(version) SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM schema_meta);
        CREATE TABLE IF NOT EXISTS posts(
          post_id TEXT PRIMARY KEY, url TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',
          content_excerpt TEXT NOT NULL DEFAULT '', author_name TEXT NOT NULL DEFAULT '',
          author_id TEXT NOT NULL DEFAULT '', published_at TEXT, first_seen_at TEXT NOT NULL,
          last_seen_at TEXT NOT NULL, comment_count INTEGER, comment_count_source TEXT NOT NULL,
          comment_count_reliable INTEGER NOT NULL DEFAULT 0, comment_verified_at TEXT,
          status TEXT NOT NULL DEFAULT 'new'
        );
        CREATE TABLE IF NOT EXISTS search_runs(
          id INTEGER PRIMARY KEY AUTOINCREMENT, keyword TEXT NOT NULL, started_at TEXT NOT NULL,
          finished_at TEXT, fetched_count INTEGER NOT NULL DEFAULT 0,
          new_count INTEGER NOT NULL DEFAULT 0, qualified_count INTEGER NOT NULL DEFAULT 0,
          status TEXT NOT NULL DEFAULT 'running', error_summary TEXT
        );
        CREATE TABLE IF NOT EXISTS post_hits(
          post_id TEXT NOT NULL REFERENCES posts(post_id), keyword TEXT NOT NULL,
          search_run_id INTEGER REFERENCES search_runs(id), seen_at TEXT NOT NULL,
          UNIQUE(post_id, keyword, search_run_id)
        );
        CREATE TABLE IF NOT EXISTS lead_analysis(
          id INTEGER PRIMARY KEY AUTOINCREMENT, post_id TEXT NOT NULL REFERENCES posts(post_id),
          model_name TEXT NOT NULL, prompt_version TEXT NOT NULL,
          is_help_request INTEGER NOT NULL, skill_match INTEGER NOT NULL,
          score INTEGER NOT NULL, category TEXT NOT NULL, reason TEXT NOT NULL,
          uncertainties_json TEXT NOT NULL, draft_comment TEXT NOT NULL, analyzed_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS lead_events(
          id INTEGER PRIMARY KEY AUTOINCREMENT, post_id TEXT NOT NULL REFERENCES posts(post_id),
          event_type TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS uq_notification_event
          ON lead_events(post_id, event_type) WHERE event_type = 'notified';
        CREATE INDEX IF NOT EXISTS idx_posts_status ON posts(status);
        CREATE INDEX IF NOT EXISTS idx_posts_first_seen ON posts(first_seen_at);
        CREATE INDEX IF NOT EXISTS idx_analysis_score ON lead_analysis(score);
        """)
        self.connection.commit()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def start_run(self, keyword: str) -> int:
        cursor = self.connection.execute(
            "INSERT INTO search_runs(keyword, started_at) VALUES (?, ?)", (keyword, self._now())
        )
        self.connection.commit()
        if cursor.lastrowid is None:
            raise RuntimeError("failed to create search run")
        return int(cursor.lastrowid)

    def finish_run(self, run_id: int, *, fetched: int, new: int, qualified: int,
                   status: str = "completed", error: str | None = None) -> None:
        self.connection.execute(
            "UPDATE search_runs SET finished_at=?, fetched_count=?, new_count=?, "
            "qualified_count=?, status=?, error_summary=? WHERE id=?",
            (self._now(), fetched, new, qualified, status, error, run_id),
        )
        self.connection.commit()

    def upsert_post(self, post: NormalizedPost, run_id: int) -> bool:
        now = self._now()
        existed = self.connection.execute("SELECT 1 FROM posts WHERE post_id=?", (post.post_id,)).fetchone() is not None
        self.connection.execute("""
        INSERT INTO posts(post_id,url,title,content_excerpt,author_name,author_id,published_at,
          first_seen_at,last_seen_at,comment_count,comment_count_source,comment_count_reliable,status)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(post_id) DO UPDATE SET url=excluded.url,title=excluded.title,
          content_excerpt=excluded.content_excerpt,author_name=excluded.author_name,
          author_id=excluded.author_id,published_at=COALESCE(excluded.published_at,posts.published_at),
          last_seen_at=excluded.last_seen_at,comment_count=excluded.comment_count,
          comment_count_source=excluded.comment_count_source,
          comment_count_reliable=excluded.comment_count_reliable
        """, (post.post_id, post.url, post.title, post.content[:4000], post.author_name,
              post.author_id, post.published_at.isoformat() if post.published_at else None,
              now, now, post.comment_count, post.comment_count_source,
              int(post.comment_count_reliable), "new"))
        self.connection.execute(
            "INSERT OR IGNORE INTO post_hits(post_id,keyword,search_run_id,seen_at) VALUES(?,?,?,?)",
            (post.post_id, post.keyword, run_id, now),
        )
        self.connection.commit()
        return not existed

    def set_status(self, post_id: str, status: str, note: str = "") -> None:
        self.connection.execute("UPDATE posts SET status=? WHERE post_id=?", (status, post_id))
        self.connection.execute(
            "INSERT INTO lead_events(post_id,event_type,note,created_at) VALUES(?,?,?,?)",
            (post_id, status, note, self._now()),
        )
        self.connection.commit()

    def get_status(self, post_id: str) -> str | None:
        row = self.connection.execute("SELECT status FROM posts WHERE post_id=?", (post_id,)).fetchone()
        return str(row["status"]) if row else None

    def load_post(self, post_id: str) -> NormalizedPost | None:
        row = self.connection.execute("SELECT * FROM posts WHERE post_id=?", (post_id,)).fetchone()
        if row is None:
            return None
        data = dict(row)
        query = parse_qs(urlparse(data["url"]).query)
        return NormalizedPost(
            post_id=data["post_id"],
            url=data["url"],
            title=data["title"],
            content=data["content_excerpt"],
            author_name=data["author_name"],
            author_id=data["author_id"],
            published_at=data["published_at"],
            comment_count=data["comment_count"],
            comment_count_source=data["comment_count_source"],
            comment_count_reliable=bool(data["comment_count_reliable"]),
            xsec_token=query.get("xsec_token", [""])[0],
            xsec_source=query.get("xsec_source", ["pc_search"])[0],
            keyword="manual_recheck",
        )

    def update_verification(self, post: NormalizedPost) -> None:
        verified_at = self._now() if post.comment_count_reliable else None
        self.connection.execute(
            "UPDATE posts SET comment_count=?, comment_count_source=?, comment_count_reliable=?, "
            "comment_verified_at=?, last_seen_at=? WHERE post_id=?",
            (post.comment_count, post.comment_count_source, int(post.comment_count_reliable),
             verified_at, self._now(), post.post_id),
        )
        self.connection.commit()

    def add_event_once(self, post_id: str, event_type: str, note: str = "") -> bool:
        try:
            self.connection.execute(
                "INSERT INTO lead_events(post_id,event_type,note,created_at) VALUES(?,?,?,?)",
                (post_id, event_type, note, self._now()),
            )
            self.connection.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def save_analysis(self, post_id: str, analysis: LeadAnalysis, model_name: str,
                      prompt_version: str = "v1") -> None:
        self.connection.execute("""
        INSERT INTO lead_analysis(post_id,model_name,prompt_version,is_help_request,skill_match,
          score,category,reason,uncertainties_json,draft_comment,analyzed_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """, (post_id, model_name, prompt_version, int(analysis.is_help_request),
              int(analysis.skill_match), analysis.score, analysis.service_category,
              analysis.reason, json.dumps(analysis.uncertainties, ensure_ascii=False),
              analysis.suggested_comment, self._now()))
        self.connection.commit()

    def has_analysis(self, post_id: str, model_name: str, prompt_version: str) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM lead_analysis WHERE post_id=? AND model_name=? AND prompt_version=? LIMIT 1",
            (post_id, model_name, prompt_version),
        ).fetchone() is not None

    def list_leads(self, status: str | None = None, min_score: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        params: list[Any] = [min_score]
        where = "WHERE COALESCE(a.score,0) >= ?"
        if status:
            where += " AND p.status = ?"
            params.append(status)
        params.append(limit)
        rows = self.connection.execute(f"""
        SELECT p.*, a.score, a.category, a.reason, a.draft_comment
        FROM posts p LEFT JOIN lead_analysis a ON a.id=(
          SELECT id FROM lead_analysis WHERE post_id=p.post_id ORDER BY id DESC LIMIT 1)
        {where} ORDER BY COALESCE(a.score,0) DESC, p.first_seen_at DESC LIMIT ?
        """, params).fetchall()
        return [dict(row) for row in rows]

    def get_lead(self, post_id: str) -> dict[str, Any] | None:
        rows = self.list_leads(limit=10000)
        return next((row for row in rows if row["post_id"] == post_id), None)

    def stats(self, days: int) -> dict[str, Any]:
        modifier = f"-{int(days)} days"
        statuses = self.connection.execute(
            "SELECT status, COUNT(*) count FROM posts "
            "WHERE datetime(first_seen_at) >= datetime('now', ?) GROUP BY status",
            (modifier,),
        ).fetchall()
        searched = self.connection.execute(
            "SELECT COALESCE(SUM(fetched_count),0) FROM search_runs "
            "WHERE datetime(started_at) >= datetime('now', ?)",
            (modifier,),
        ).fetchone()[0]
        keyword_rows = self.connection.execute("""
        SELECT h.keyword,
          COUNT(DISTINCT h.post_id) AS discovered,
          COUNT(DISTINCT CASE WHEN p.status='pending_review' THEN h.post_id END) AS recommended,
          COUNT(DISTINCT CASE WHEN p.status IN ('contacted','quoted','won','lost') THEN h.post_id END) AS contacted,
          COUNT(DISTINCT CASE WHEN p.status='won' THEN h.post_id END) AS won
        FROM post_hits h JOIN posts p ON p.post_id=h.post_id
        WHERE datetime(h.seen_at) >= datetime('now', ?)
        GROUP BY h.keyword ORDER BY discovered DESC
        """, (modifier,)).fetchall()
        keyword_stats = []
        for row in keyword_rows:
            item = dict(row)
            item["effective_rate"] = (
                item["recommended"] / item["discovered"] if item["discovered"] else None
            )
            item["win_rate"] = item["won"] / item["contacted"] if item["contacted"] else None
            keyword_stats.append(item)
        return {
            "days": days,
            "searched": searched,
            "by_status": {row["status"]: row["count"] for row in statuses},
            "by_keyword": keyword_stats,
        }
