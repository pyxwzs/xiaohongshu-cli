"""Tests for multi-account cookie storage."""

import json

import yaml
from click.testing import CliRunner

from xhs_cli.cli import cli
from xhs_cli.cookies import (
    get_active_account,
    get_current_account_name,
    get_note_by_index,
    list_account_records,
    load_saved_cookies,
    save_account_profile,
    save_cookies,
    save_note_index,
    set_active_account,
)

runner = CliRunner()


def _isolate_config(monkeypatch, tmp_path):
    monkeypatch.setattr("xhs_cli.cookies.get_config_dir", lambda: tmp_path)
    monkeypatch.setattr("xhs_cli.cookies._TOKEN_CACHE_MEMORY", None)
    monkeypatch.setattr("xhs_cli.cookies._TOKEN_CACHE_PATH", None)
    set_active_account(None)
    return tmp_path


class TestAccountStorage:
    def test_accounts_keep_separate_cookies_and_indexes(self, monkeypatch, tmp_path):
        _isolate_config(monkeypatch, tmp_path)

        set_active_account("work")
        save_cookies({"a1": "work-cookie"})
        save_note_index([{"note_id": "work-note", "xsec_token": "t", "xsec_source": "pc_search"}])
        save_account_profile({"nickname": "工作号", "red_id": "work001", "user_id": "u-work"})

        set_active_account("personal")
        save_cookies({"a1": "personal-cookie"})
        save_note_index([{"note_id": "personal-note", "xsec_token": "p", "xsec_source": "pc_feed"}])

        set_active_account("work")
        assert load_saved_cookies()["a1"] == "work-cookie"
        assert get_note_by_index(1)["note_id"] == "work-note"

        set_active_account("personal")
        assert load_saved_cookies()["a1"] == "personal-cookie"
        assert get_note_by_index(1)["note_id"] == "personal-note"

    def test_legacy_cookies_move_into_default_account(self, monkeypatch, tmp_path):
        _isolate_config(monkeypatch, tmp_path)
        (tmp_path / "cookies.json").write_text(json.dumps({"a1": "legacy"}))
        (tmp_path / "token_cache.json").write_text("{}")

        loaded = load_saved_cookies()

        assert loaded["a1"] == "legacy"
        assert not (tmp_path / "cookies.json").exists()
        assert (tmp_path / "accounts" / "default" / "cookies.json").is_file()
        assert (tmp_path / "accounts" / "default" / "token_cache.json").is_file()

    def test_current_account_is_used_when_flag_is_omitted(self, monkeypatch, tmp_path):
        _isolate_config(monkeypatch, tmp_path)
        set_active_account("work")
        save_cookies({"a1": "work-cookie"})
        set_active_account("personal")
        save_cookies({"a1": "personal-cookie"})

        result = runner.invoke(cli, ["account", "use", "work"])
        assert result.exit_code == 0, result.output

        set_active_account(None)
        assert get_current_account_name() == "work"
        assert get_active_account() == "work"
        assert load_saved_cookies()["a1"] == "work-cookie"

    def test_logout_clears_only_the_selected_account(self, monkeypatch, tmp_path):
        _isolate_config(monkeypatch, tmp_path)
        set_active_account("work")
        save_cookies({"a1": "work-cookie"})
        set_active_account("personal")
        save_cookies({"a1": "personal-cookie"})
        set_active_account(None)

        result = runner.invoke(cli, ["--account", "personal", "logout"])
        assert result.exit_code == 0, result.output

        set_active_account("work")
        assert load_saved_cookies()["a1"] == "work-cookie"
        set_active_account("personal")
        assert load_saved_cookies() is None

    def test_accounts_json_lists_saved_profiles(self, monkeypatch, tmp_path):
        _isolate_config(monkeypatch, tmp_path)
        set_active_account("work")
        save_cookies({"a1": "work-cookie"})
        save_account_profile({"nickname": "工作号", "red_id": "work001", "user_id": "u-work"})
        set_active_account(None)

        result = runner.invoke(cli, ["account", "use", "work", "--json"])
        assert result.exit_code == 0, result.output
        set_active_account(None)

        result = runner.invoke(cli, ["accounts", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["data"]["current"] == "work"
        work = next(item for item in payload["data"]["accounts"] if item["name"] == "work")
        assert work["authenticated"] is True
        assert work["nickname"] == "工作号"
        assert work["current"] is True

    def test_remove_account_switches_current_pointer(self, monkeypatch, tmp_path):
        _isolate_config(monkeypatch, tmp_path)
        set_active_account("work")
        save_cookies({"a1": "work-cookie"})
        set_active_account("personal")
        save_cookies({"a1": "personal-cookie"})
        set_active_account(None)
        runner.invoke(cli, ["account", "use", "work"])

        result = runner.invoke(cli, ["account", "remove", "work", "-y", "--yaml"])
        assert result.exit_code == 0, result.output
        payload = yaml.safe_load(result.output)
        assert payload["data"]["removed"] == "work"
        assert get_current_account_name() == "personal"
        assert not (tmp_path / "accounts" / "work").exists()

    def test_invalid_account_name_is_rejected(self):
        result = runner.invoke(cli, ["--account", "../x", "accounts"])
        assert result.exit_code != 0
        assert "Account name" in result.output

    def test_use_missing_account_fails(self, monkeypatch, tmp_path):
        _isolate_config(monkeypatch, tmp_path)
        result = runner.invoke(cli, ["account", "use", "missing", "--json"])
        assert result.exit_code != 0
        payload = json.loads(result.output)
        assert payload["ok"] is False
        assert list_account_records()[0]["name"] == "default"
