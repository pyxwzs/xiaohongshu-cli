"""Configuration loading for lead discovery."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from ..cookies import get_account_dir
from .models import LeadConfig


def default_config_path() -> Path:
    return get_account_dir() / "lead_agent.yaml"


def load_config(path: str | Path | None = None) -> LeadConfig:
    config_path = Path(path) if path else default_config_path()
    if not config_path.is_file():
        return LeadConfig()
    payload: Any = yaml.safe_load(config_path.read_text()) or {}
    if not isinstance(payload, dict):
        raise ValueError("lead configuration must be a YAML object")
    raw = payload.get("lead_agent", payload)
    if not isinstance(raw, dict):
        raise ValueError("lead_agent must be a YAML object")
    ai = raw.pop("ai", {}) if isinstance(raw.get("ai", {}), dict) else {}
    mapped = dict(raw)
    if ai:
        mapped["ai_base_url_env"] = ai.get("base_url_env", "LEAD_AI_BASE_URL")
        mapped["ai_api_key_env"] = ai.get("api_key_env", "LEAD_AI_API_KEY")
        mapped["ai_model_env"] = ai.get("model_env", "LEAD_AI_MODEL")
    return LeadConfig.model_validate(mapped)


def create_default_config(path: str | Path | None = None) -> Path:
    config_path = Path(path) if path else default_config_path()
    if config_path.exists():
        raise FileExistsError(f"configuration already exists: {config_path}")
    config_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "lead_agent": {
            "enabled": False,
            "schedule_minutes": 60,
            "max_posts_per_run": 50,
            "max_ai_calls_per_run": 20,
            "min_score": 75,
            "recent_hours": 72,
            "require_zero_comments": True,
            "require_human_approval": True,
            "auto_publish_comment": False,
            "auto_send_dm": False,
            "keywords": [
                "有偿求助 Python",
                "计算机作业求助",
                "找人开发小程序",
                "Docker 部署失败",
            ],
            "ai": {
                "provider": "openai_compatible",
                "base_url_env": "LEAD_AI_BASE_URL",
                "api_key_env": "LEAD_AI_API_KEY",
                "model_env": "LEAD_AI_MODEL",
            },
        }
    }
    config_path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False))
    config_path.chmod(0o600)
    return config_path
