"""Tests for realtime quote provider integration."""
import json
import pytest
from types import SimpleNamespace
from unittest.mock import patch
import pandas as pd


def _reset_fund_flow_state(provider_cls):
    provider_cls._fund_flow_cache.clear()
    provider_cls._fund_flow_failures.clear()
    provider_cls._fund_flow_last_request_monotonic = 0.0


def test_akshare_get_realtime_quotes_returns_structured_json():
    """CnAkshareProvider.get_realtime_quotes returns JSON with expected fields."""
    from tradingagents.dataflows.providers.cn_akshare_provider import CnAkshareProvider

    mock_df = pd.DataFrame({
        "代码": ["600519", "000001"],
        "名称": ["贵州茅台", "平安银行"],
        "最新价": [1800.0, 12.5],
        "今开": [1790.0, 12.3],
        "最高": [1810.0, 12.6],
        "最低": [1785.0, 12.2],
        "昨收": [1795.0, 12.4],
        "成交量": [50000, 800000],
        "成交额": [90000000, 10000000],
    })

    provider = CnAkshareProvider()
    # Mock Sina to fail so it falls back to Eastmoney mock
    with patch.object(provider, "_fetch_quotes_sina", return_value="{}"), \
         patch.object(provider, "_ak") as mock_ak:
        mock_ak.return_value.stock_zh_a_spot_em.return_value = mock_df
        result = provider.get_realtime_quotes(["600519.SH", "000001.SZ"])

    data = json.loads(result)
    assert "600519.SH" in data
    q = data["600519.SH"]
    assert q["price"] == 1800.0
    assert q["previous_close"] == 1795.0
    assert q["change"] == 5.0
    assert q["change_pct"] == pytest.approx(0.2786, abs=0.001)
    assert q["open"] == 1790.0
    assert q["volume"] == 50000
    assert "000001.SZ" in data


def test_akshare_get_realtime_quotes_empty_symbols():
    from tradingagents.dataflows.providers.cn_akshare_provider import CnAkshareProvider

    provider = CnAkshareProvider()
    result = provider.get_realtime_quotes([])
    assert json.loads(result) == {}


def test_route_to_vendor_resolves_realtime_quotes():
    """route_to_vendor can route get_realtime_quotes to the correct category."""
    from tradingagents.dataflows.interface import get_category_for_method
    category = get_category_for_method("get_realtime_quotes")
    assert category == "realtime_data"


def test_akshare_lhb_detail_uses_date_range_signature_and_filters_symbol():
    from tradingagents.dataflows.providers.cn_akshare_provider import CnAkshareProvider

    calls = {}

    class FakeAk:
        def stock_lhb_detail_em(self, start_date: str, end_date: str):
            calls["start_date"] = start_date
            calls["end_date"] = end_date
            return pd.DataFrame({
                "代码": ["600498", "000001"],
                "名称": ["烽火通信", "平安银行"],
                "龙虎榜净买额": [123.0, -456.0],
            })

    provider = CnAkshareProvider()
    with patch.object(provider, "_ak", return_value=FakeAk()):
        result = provider.get_lhb_detail("600498.SH", "2026-06-04")

    assert calls == {"start_date": "20260604", "end_date": "20260604"}
    assert "600498.SH 龙虎榜明细" in result
    assert "烽火通信" in result
    assert "平安银行" not in result


def test_akshare_lhb_detail_treats_empty_eastmoney_result_as_no_data():
    from tradingagents.dataflows.providers.cn_akshare_provider import CnAkshareProvider

    class FakeAk:
        def stock_lhb_detail_em(self, start_date: str, end_date: str):
            raise TypeError("'NoneType' object is not subscriptable")

    provider = CnAkshareProvider()
    with patch.object(provider, "_ak", return_value=FakeAk()):
        result = provider.get_lhb_detail("600498.SH", "2026-06-04")

    assert "无龙虎榜数据" in result


def test_akshare_individual_fund_flow_falls_back_to_eastmoney_curl():
    from tradingagents.dataflows.providers.cn_akshare_provider import CnAkshareProvider
    _reset_fund_flow_state(CnAkshareProvider)

    class FakeAk:
        def stock_individual_fund_flow(self, stock: str, market: str):
            raise RuntimeError("proxy failed")

    payload = {
        "data": {
            "klines": [
                "2026-06-01,-278712336.0,216997440.0,61714880.0,-195355936.0,-83356400.0,-8.23,6.41,1.82,-5.77,-2.46,47.55,-8.36,0.00,0.00",
                "2026-06-02,298703920.0,-167398928.0,-131304976.0,84019408.0,214684512.0,7.39,-4.14,-3.25,2.08,5.31,49.50,4.10,0.00,0.00",
                "2026-06-03,320271632.0,-80236736.0,-240034864.0,-9726016.0,329997648.0,5.31,-1.33,-3.98,-0.16,5.47,52.02,5.09,0.00,0.00",
                "2026-06-04,28200048.0,-85337760.0,57137696.0,43538384.0,-15338336.0,0.54,-1.63,1.09,0.83,-0.29,52.75,1.40,0.00,0.00",
            ]
        }
    }
    seen_cmds = []

    def fake_run(cmd, check, capture_output, text):
        seen_cmds.append(cmd)
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    provider = CnAkshareProvider()
    with patch.object(provider, "_ak", return_value=FakeAk()), \
         patch.object(CnAkshareProvider, "_wait_fund_flow_rate_limit", return_value=None), \
         patch("tradingagents.dataflows.providers.cn_akshare_provider.shutil.which", return_value="/usr/bin/curl"), \
         patch("tradingagents.dataflows.providers.cn_akshare_provider.subprocess.run", side_effect=fake_run):
        result = provider.get_individual_fund_flow("600498.SH")

    assert "600498.SH 近5日主力资金净流向（Eastmoney 直连备用接口）" in result
    assert "2026-06-04" in result
    assert "主力净流入-净额" in result
    assert "28200048.0" in result
    assert "fields2=f51,f52" in seen_cmds[0][-1]
    assert "%2C" not in seen_cmds[0][-1]
    assert "--noproxy" in seen_cmds[0]


def test_akshare_individual_fund_flow_success_cache_avoids_repeated_fetch():
    from tradingagents.dataflows.providers.cn_akshare_provider import CnAkshareProvider
    _reset_fund_flow_state(CnAkshareProvider)

    class FakeAk:
        call_count = 0

        def stock_individual_fund_flow(self, stock: str, market: str):
            self.call_count += 1
            return pd.DataFrame({
                "日期": [pd.to_datetime("2026-06-04").date()],
                "收盘价": [52.75],
                "涨跌幅": [1.40],
                "主力净流入-净额": [28200048.0],
                "主力净流入-净占比": [0.54],
            })

    fake_ak = FakeAk()
    provider = CnAkshareProvider()
    with patch.object(provider, "_ak", return_value=fake_ak), \
         patch.object(CnAkshareProvider, "_wait_fund_flow_rate_limit", return_value=None):
        first = provider.get_individual_fund_flow("600498.SH")
        second = provider.get_individual_fund_flow("600498.SH")

    assert fake_ak.call_count == 1
    assert first == second
    assert "近5日主力资金净流向" in second


def test_akshare_individual_fund_flow_failure_cooldown_avoids_repeated_requests():
    from tradingagents.dataflows.providers.cn_akshare_provider import CnAkshareProvider
    _reset_fund_flow_state(CnAkshareProvider)

    class FakeAk:
        call_count = 0

        def stock_individual_fund_flow(self, stock: str, market: str):
            self.call_count += 1
            raise RuntimeError("proxy failed")

    fake_ak = FakeAk()
    curl_calls = []

    def fake_run(cmd, check, capture_output, text):
        curl_calls.append(cmd)
        return SimpleNamespace(returncode=52, stdout="", stderr="curl: (52) Empty reply from server")

    provider = CnAkshareProvider()
    with patch.object(provider, "_ak", return_value=fake_ak), \
         patch.object(CnAkshareProvider, "_wait_fund_flow_rate_limit", return_value=None), \
         patch("tradingagents.dataflows.providers.cn_akshare_provider.shutil.which", return_value="/usr/bin/curl"), \
         patch("tradingagents.dataflows.providers.cn_akshare_provider.subprocess.run", side_effect=fake_run):
        first = provider.get_individual_fund_flow("600498.SH")
        second = provider.get_individual_fund_flow("600498.SH")

    assert fake_ak.call_count == 1
    assert len(curl_calls) == 1
    assert first == second
    assert "东方财富个股资金流向数据获取失败" in second
