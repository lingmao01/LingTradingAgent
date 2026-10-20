"""A-share trading calendar (``src.market_calendar``): days, NAV timing, cache.

The provider is injected at ``_fetch_calendar_rows``, the disk cache is
redirected per test through ``VIBE_TRADING_CALENDAR_CACHE_DIR``, and the
process memo is reset before and after every test, so no test reaches the
network or the developer's own ``~/.vibe-trading``.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import src.market_calendar as mc

pytestmark = pytest.mark.unit


def _rows() -> tuple[dict[str, dict[str, object]], tuple[date, date]]:
    """A late-June-2026 window: two sessions, a holiday, and a weekend."""
    return (
        {
            "2026-06-22": {"is_open": 1, "pretrade_date": "2026-06-19"},
            "2026-06-23": {"is_open": 1, "pretrade_date": "2026-06-22"},
            "2026-06-24": {"is_open": 1, "pretrade_date": "2026-06-23"},
            "2026-06-25": {"is_open": 0, "pretrade_date": "2026-06-24"},
            "2026-06-26": {"is_open": 1, "pretrade_date": "2026-06-24"},
            "2026-06-27": {"is_open": 0, "pretrade_date": "2026-06-26"},
            "2026-06-28": {"is_open": 0, "pretrade_date": "2026-06-26"},
        },
        (date(2026, 1, 1), date(2027, 1, 1)),
    )


@pytest.fixture(autouse=True)
def _isolated_calendar(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Redirect the disk cache and give every test a cold process memo."""
    monkeypatch.setenv(mc._CACHE_DIR_ENV, str(tmp_path / "calendar-cache"))
    mc._reset_cache()
    yield
    mc._reset_cache()


class TestIsTradingDay:
    def test_open_and_closed_days(self):
        with patch.object(mc, "_fetch_calendar_rows", return_value=_rows()):
            assert mc.is_trading_day("2026-06-24") is True
            assert mc.is_trading_day(date(2026, 6, 25)) is False
            assert mc.is_trading_day(datetime(2026, 6, 26, 9, 30)) is True
            assert mc.is_trading_day("2026-06-27") is False

    def test_unavailable_calendar_answers_none(self):
        with patch.object(mc, "_fetch_calendar_rows", return_value=None):
            assert mc.is_trading_day("2026-06-24") is None

    def test_unrecognizable_day_raises(self):
        with pytest.raises(ValueError):
            mc.is_trading_day("24/06/2026")


class TestPreviousTradingDay:
    def test_uses_the_sources_pretrade_cell(self):
        with patch.object(mc, "_fetch_calendar_rows", return_value=_rows()):
            assert mc.previous_trading_day("2026-06-24") == date(2026, 6, 23)

    def test_a_pretrade_cell_equal_to_the_day_falls_back_to_the_scan(self):
        """The source's own cell is not trusted when it names the same day."""
        rows, window = _rows()
        rows["2026-06-24"] = {"is_open": 1, "pretrade_date": "2026-06-24"}
        with patch.object(mc, "_fetch_calendar_rows", return_value=(rows, window)):
            assert mc.previous_trading_day("2026-06-24") == date(2026, 6, 23)

    def test_a_missing_pretrade_cell_falls_back_to_the_scan(self):
        rows, window = _rows()
        rows["2026-06-24"] = {"is_open": 1, "pretrade_date": None}
        with patch.object(mc, "_fetch_calendar_rows", return_value=(rows, window)):
            assert mc.previous_trading_day("2026-06-24") == date(2026, 6, 23)

    def test_the_scan_skips_closed_days(self):
        rows = {
            "2026-06-24": {"is_open": 1, "pretrade_date": "2026-06-24"},
            "2026-06-23": {"is_open": 0, "pretrade_date": "2026-06-22"},
            "2026-06-22": {"is_open": 1, "pretrade_date": "2026-06-19"},
        }
        with patch.object(
            mc, "_fetch_calendar_rows", return_value=(rows, (date(2026, 1, 1), date(2027, 1, 1)))
        ):
            assert mc.previous_trading_day("2026-06-24") == date(2026, 6, 22)

    def test_no_earlier_open_day_answers_none(self):
        rows = {"2026-06-24": {"is_open": 1, "pretrade_date": "2026-06-24"}}
        with patch.object(
            mc, "_fetch_calendar_rows", return_value=(rows, (date(2026, 1, 1), date(2027, 1, 1)))
        ):
            assert mc.previous_trading_day("2026-06-24") is None

    def test_unavailable_calendar_answers_none(self):
        with patch.object(mc, "_fetch_calendar_rows", return_value=None):
            assert mc.previous_trading_day("2026-06-24") is None


class TestLatestNavDate:
    def test_after_the_disclosure_hour_the_day_itself_counts(self):
        with patch.object(mc, "_fetch_calendar_rows", return_value=_rows()):
            assert mc.latest_nav_date(datetime(2026, 6, 24, 20, 0)) == date(2026, 6, 24)
            assert mc.latest_nav_date(datetime(2026, 6, 24, 21, 30)) == date(2026, 6, 24)

    def test_before_the_disclosure_hour_the_previous_session_counts(self):
        with patch.object(mc, "_fetch_calendar_rows", return_value=_rows()):
            assert mc.latest_nav_date(datetime(2026, 6, 24, 9, 30)) == date(2026, 6, 23)

    def test_a_weekend_falls_back_to_the_last_session(self):
        with patch.object(mc, "_fetch_calendar_rows", return_value=_rows()):
            assert mc.latest_nav_date(datetime(2026, 6, 27, 10, 0)) == date(2026, 6, 26)

    def test_unavailable_calendar_answers_none(self):
        with patch.object(mc, "_fetch_calendar_rows", return_value=None):
            assert mc.latest_nav_date(datetime(2026, 6, 24, 21, 0)) is None


class TestSessionContext:
    def test_an_answerable_calendar_reports_facts(self):
        with patch.object(mc, "_fetch_calendar_rows", return_value=_rows()):
            assert mc.session_context(datetime(2026, 6, 24, 21, 0)) == {
                "is_trading_day": True,
                "latest_nav_date": "2026-06-24",
                "source": "tushare",
            }

    def test_an_unavailable_calendar_reports_explicit_unknowns(self):
        with patch.object(mc, "_fetch_calendar_rows", return_value=None):
            assert mc.session_context(datetime(2026, 6, 24, 21, 0)) == {
                "is_trading_day": None,
                "latest_nav_date": None,
                "source": None,
            }


class TestDiskCache:
    def test_a_fetched_window_survives_a_restart(self):
        with patch.object(mc, "_fetch_calendar_rows", return_value=_rows()) as fetch:
            assert mc.is_trading_day("2026-06-24") is True
            assert fetch.call_count == 1
            assert mc._cache_file().exists()

            # A new process: the memo is gone and the endpoint is rate-limited,
            # so this answer has to come from the disk copy.
            mc._reset_cache()
            assert mc.is_trading_day("2026-06-24") is True
            assert fetch.call_count == 1

    def test_a_corrupt_cache_file_is_ignored_and_rewritten(self):
        path = mc._cache_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        mc._reset_cache()

        with patch.object(mc, "_fetch_calendar_rows", return_value=_rows()):
            assert mc.is_trading_day("2026-06-24") is True

        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["window"] == ["2026-01-01", "2027-01-01"]
        assert payload["rows"]["2026-06-24"]["is_open"] == 1

    def test_a_failed_write_is_not_fatal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        monkeypatch.setenv(mc._CACHE_DIR_ENV, str(blocker / "sub"))

        with patch.object(mc, "_fetch_calendar_rows", return_value=_rows()):
            assert mc.is_trading_day("2026-06-24") is True


class TestTushareToken:
    @staticmethod
    def _config(token: str):
        return SimpleNamespace(data=SimpleNamespace(tushare_token=token))

    def test_a_configured_token_is_returned(self):
        with patch(
            "src.config.accessor.get_env_config", return_value=self._config("ts-secret")
        ):
            assert mc._tushare_token() == "ts-secret"

    def test_the_placeholder_token_counts_as_unconfigured(self):
        with patch(
            "src.config.accessor.get_env_config",
            return_value=self._config("your-tushare-token"),
        ):
            assert mc._tushare_token() == ""

    def test_an_unreadable_config_answers_no_token(self):
        with patch("src.config.accessor.get_env_config", side_effect=RuntimeError("boom")):
            assert mc._tushare_token() == ""

    def test_no_token_means_no_fetch(self):
        with patch.object(mc, "_tushare_token", return_value=""):
            assert mc._fetch_calendar_rows(date(2026, 6, 24)) is None