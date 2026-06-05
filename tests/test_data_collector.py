from unittest.mock import patch
import pandas as pd

from tradingagents.graph.data_collector import DataCollector, make_cache_key, _format_recent_price_volume


def test_make_cache_key():
    assert make_cache_key("600519", "2026-03-12") == "600519_2026-03-12"


def test_collect_populates_required_keys():
    collector = DataCollector()
    stub_pool = {
        "stock_data": "data", "indicators": {}, "news": "n", "global_news": "gn",
        "fundamentals": "f", "balance_sheet": "bs", "cashflow": "cf",
        "income_statement": "is", "fund_flow_board": "ffb",
        "fund_flow_individual": "ffi", "lhb": "lhb",
        "insider_transactions": "it", "zt_pool": "zt", "hot_stocks": "hs",
    }
    with patch("tradingagents.graph.data_collector._fetch_all", return_value=stub_pool):
        result = collector.collect("600519", "2026-03-12")
    assert "stock_data" in result
    assert "lhb" in result
    assert "zt_pool" in result


def test_collect_uses_cache_on_second_call():
    collector = DataCollector()
    stub_pool = {"stock_data": "x", "indicators": {}}
    with patch("tradingagents.graph.data_collector._fetch_all", return_value=stub_pool) as mock_fetch:
        collector.collect("600519", "2026-03-12")
        collector.collect("600519", "2026-03-12")
    assert mock_fetch.call_count == 1


def test_evict_removes_from_cache():
    collector = DataCollector()
    collector._cache["600519_2026-03-12"] = {"stock_data": "x"}
    collector.evict("600519", "2026-03-12")
    assert "600519_2026-03-12" not in collector._cache


def test_get_window_short_returns_14_day_window():
    collector = DataCollector()
    pool = {"stock_data": "x", "indicators": {}}
    sliced = collector.get_window(pool, horizon="short", trade_date="2026-03-12")
    assert sliced["_data_window"] == "14天"
    assert sliced["_horizon"] == "short"


def test_get_window_medium_returns_90_day_window():
    collector = DataCollector()
    pool = {"stock_data": "x", "indicators": {}}
    sliced = collector.get_window(pool, horizon="medium", trade_date="2026-03-12")
    assert sliced["_data_window"] == "90天"
    assert sliced["_horizon"] == "medium"


def test_format_recent_price_volume_includes_feedback_dimensions():
    df = pd.DataFrame({
        "date": pd.date_range("2026-05-01", periods=25, freq="D"),
        "open": range(25),
        "high": range(1, 26),
        "low": range(25),
        "close": range(2, 27),
        "volume": range(100, 125),
        "turnover_rate": [1.2] * 25,
    })

    result = _format_recent_price_volume(df, days=5)

    assert "最近量价反馈数据" in result
    assert "收盘" in result
    assert "涨跌幅%" in result
    assert "量比(20日)" in result
    assert "换手率" in result
