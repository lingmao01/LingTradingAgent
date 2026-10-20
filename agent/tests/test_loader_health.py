"""Offline contracts for the separately scheduled public-source canary."""

from datetime import date, timedelta
import json
import subprocess

import pandas as pd
import pytest
import requests

from backtest import loader_health as health

TODAY = date(2026, 9, 28)


def frame(age=0):
    return pd.DataFrame(
        {"open": [10.0], "high": [12.0], "low": [9.0], "close": [11.0], "volume": [100.0]},
        index=pd.DatetimeIndex([TODAY - timedelta(days=age)]),
    )


def test_catalog_covers_every_public_network_loader():
    assert health.coverage_errors() == []


def test_new_loader_cannot_silently_disappear(monkeypatch):
    from backtest.loaders.registry import LOADER_REGISTRY

    monkeypatch.setitem(LOADER_REGISTRY, "new_public_source", type("Loader", (), {"requires_auth": False}))
    assert "unmapped:new_public_source" in health.coverage_errors()


@pytest.mark.parametrize("age,status", [(0, "healthy"), (14, "healthy"), (15, "stale"), (-1, "invalid")])
def test_freshness_bounds(age, status):
    assert health.check_frame(frame(age), TODAY)["status"] == status


@pytest.mark.parametrize(
    "change,reason",
    [
        (lambda f: f.iloc[:0], "empty_frame"),
        (lambda f: f.drop(columns="close"), "schema"),
        (lambda f: f.assign(close="11"), "numeric_columns"),
        (lambda f: f.assign(close=float("nan")), "nonfinite_values"),
        (lambda f: f.assign(high=5), "ohlc_order"),
        (lambda f: f.assign(volume=-1), "invalid_prices_or_volume"),
        (lambda f: f.reset_index(drop=True), "datetime_index"),
        (lambda f: pd.concat([f, f]), "index_order"),
    ],
)
def test_normalized_contract(change, reason):
    assert health.check_frame(change(frame()), TODAY)["reason"] == reason


def test_no_keys_operator_home_or_cache_in_child(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_TOKEN", "private-test-value")
    monkeypatch.setenv("OPENAI_API_KEY", "private-test-value")
    monkeypatch.setenv("VIBE_TRADING_DATA_CACHE", "1")
    monkeypatch.setenv("PYTHONPATH", "/operator/overrides")
    env = health.child_environment(str(tmp_path))
    assert "GITHUB_TOKEN" not in env and "OPENAI_API_KEY" not in env
    assert env["HOME"] == env["VIBE_TRADING_HOME"] == str(tmp_path)
    assert env["VIBE_TRADING_DATA_CACHE"] == "0"
    assert env["PYTHONPATH"] != "/operator/overrides"


def test_unavailable_endpoint_is_not_skipped(monkeypatch):
    from backtest.loaders.registry import LOADER_REGISTRY

    monkeypatch.setitem(
        LOADER_REGISTRY,
        "tencent",
        type(
            "Loader",
            (),
            {
                "requires_auth": False,
                "is_available": lambda self: False,
            },
        ),
    )
    assert health.probe("tencent", TODAY) == {
        "status": "unavailable",
        "reason": "availability_probe_failed",
        "attempts": 2,
    }


def test_network_error_is_sanitized_and_retried(monkeypatch):
    from backtest.loaders.registry import LOADER_REGISTRY

    calls = []

    def fetch(self, *args, **kwargs):
        calls.append(args)
        raise requests.exceptions.ConnectionError("https://secret:token@internal.example")

    monkeypatch.setitem(
        LOADER_REGISTRY,
        "tencent",
        type(
            "Loader",
            (),
            {
                "requires_auth": False,
                "is_available": lambda self: True,
                "fetch": fetch,
            },
        ),
    )
    result = health.probe("tencent", TODAY)
    assert result["status"] == "unreachable" and len(calls) == 2
    assert "secret" not in json.dumps(result)


def test_child_deadline_is_failure(monkeypatch):
    def timeout(*args, **kwargs):
        assert kwargs["timeout"] == 0.1
        raise subprocess.TimeoutExpired(args[0], 0.1)

    monkeypatch.setattr(health.subprocess, "run", timeout)
    assert health.run_source("tencent", TODAY, 0.1)["status"] == "timeout"


def test_real_child_is_killed_at_deadline():
    assert health.run_source("tencent", TODAY, 0.001)["status"] == "timeout"


@pytest.mark.parametrize(
    "status,coverage,exit_code",
    [
        ("healthy", [], 0),
        ("unavailable", [], 1),
        ("missing_dependency", [], 1),
        ("healthy", ["unmapped:new"], 1),
    ],
)
def test_lane_exit_and_report(monkeypatch, tmp_path, status, coverage, exit_code):
    output = tmp_path / "report.json"
    monkeypatch.setattr(health.sys, "argv", ["health", "--output", str(output)])
    monkeypatch.setattr(health, "CANARY_SYMBOLS", {"test": "TEST"})
    monkeypatch.setattr(health, "coverage_errors", lambda: coverage)
    monkeypatch.setattr(health, "run_source", lambda *args: {"source": "test", "status": status})
    assert health.main() == exit_code
    report = json.loads(output.read_text())
    assert report["coverage_errors"] == coverage
    assert report["sources"] == [{"source": "test", "status": status}]


@pytest.mark.parametrize(
    "payload,status",
    [
        ('{"status":"healthy","rows":2}', "healthy"),
        ("not json", "error"),
        ("[]", "error"),
        ('{"status":"skip"}', "error"),
        (None, "error"),
    ],
)
def test_child_report_handling(monkeypatch, payload, status):
    from pathlib import Path

    def child(command, **kwargs):
        if payload is not None:
            Path(command[-1]).write_text(payload)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(health.subprocess, "run", child)
    assert health.run_source("tencent", TODAY, 1)["status"] == status


def test_second_attempt_recovers_without_fallback(monkeypatch):
    from backtest.loaders.registry import LOADER_REGISTRY

    calls = []

    def fetch(self, codes, start, end, *, interval):
        calls.append(codes)
        if len(calls) == 1:
            return {}
        return {"601398.SH": frame()}

    monkeypatch.setitem(
        LOADER_REGISTRY,
        "tencent",
        type(
            "Loader",
            (),
            {
                "requires_auth": False,
                "is_available": lambda self: True,
                "fetch": fetch,
            },
        ),
    )
    result = health.probe("tencent", TODAY)
    assert result["status"] == "healthy" and result["attempts"] == 2
    assert calls == [["601398.SH"], ["601398.SH"]]
