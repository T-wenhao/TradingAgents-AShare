import asyncio

from tradingagents.agents.analysts.smart_money_analyst import create_smart_money_analyst
from tradingagents.graph.data_collector import DataCollector


class _FailIfCalledLLM:
    called = False

    async def astream(self, _messages):
        self.called = True
        raise AssertionError("LLM should not be called when core smart-money data is missing")
        yield


def _make_state():
    return {
        "trade_date": "2026-06-04",
        "company_of_interest": "600498.SH",
        "horizon": "short",
        "user_intent": {
            "raw_query": "分析 600498.SH",
            "ticker": "600498.SH",
            "horizons": ["short"],
            "focus_areas": ["主力资金"],
            "specific_questions": [],
        },
    }


def test_smart_money_degrades_when_core_money_data_missing():
    collector = DataCollector()
    collector._cache["600498.SH_2026-06-04"] = {
        "fund_flow_individual": "东方财富个股资金流向数据获取失败：ProxyError",
        "lhb": "600498.SH 在 2026-06-04 无龙虎榜数据（非异动日属正常）。",
        "indicators": {
            "vwma": 53.58,
            "rsi": 48.8,
            "close_50_sma": 51.57,
            "close_10_ema": 51.76,
        },
        "price_volume_recent": "最近量价反馈数据：\n日期 收盘 成交量 换手率",
        "vpa_indicators": "**OBV 趋势（10日）**: 上升",
    }
    llm = _FailIfCalledLLM()
    node = create_smart_money_analyst(llm, collector)

    result = asyncio.run(node(_make_state()))

    assert llm.called is False
    report = result["smart_money_report"]
    assert "核心资金数据不足" in report
    assert "无法形成可信的主力资金方向判断" in report
    assert "弱反馈验证" not in report
    assert '<!-- VERDICT: {"direction": "中性"' in report
    assert result["analyst_traces"][0]["verdict"] == "中性"
