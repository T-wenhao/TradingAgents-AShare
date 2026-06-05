import asyncio

from langchain_core.messages import HumanMessage, SystemMessage
from tradingagents.dataflows.config import get_config
from tradingagents.prompts import get_prompt
from tradingagents.graph.intent_parser import build_horizon_context
from tradingagents.agents.utils.agent_states import current_tracker_var, extract_verdict


def create_smart_money_analyst(llm, data_collector=None):
    async def _safe(tool, payload):
        try:
            return await asyncio.to_thread(tool.invoke, payload)
        except Exception as exc:
            return f"调用失败：{exc}"

    def _looks_unavailable(value) -> bool:
        text = str(value or "")
        markers = ("失败", "暂不可用", "无数据", "接口返回空结果", "No available vendor", "调用失败")
        return any(marker in text for marker in markers)

    def _has_fund_flow_detail(value) -> bool:
        text = str(value or "")
        return "近5日主力资金净流向" in text and not _looks_unavailable(text)

    def _has_lhb_detail(value) -> bool:
        text = str(value or "")
        if _looks_unavailable(text):
            return False
        no_detail_markers = ("无龙虎榜数据", "无龙虎榜", "非异动日属正常", "无公开席位")
        if any(marker in text for marker in no_detail_markers):
            return False
        return "龙虎榜明细" in text

    def _build_feedback_context(pool, fund_flow, lhb) -> str:
        if pool is None:
            return ""

        missing = []
        if not _has_fund_flow_detail(fund_flow):
            missing.append("近5日主力资金净流向")
        if not _has_lhb_detail(lhb):
            missing.append("龙虎榜明细")
        if not missing:
            return ""

        indicators = pool.get("indicators", {}) or {}
        indicator_lines = "\n".join(
            f"- {key}: {value}"
            for key, value in indicators.items()
            if key in ("close_50_sma", "close_10_ema", "rsi", "macd", "atr", "vwma")
        ) or "无可用指标"

        return (
            "【资金数据缺失处理】\n"
            f"缺失项：{', '.join(missing)}。以下量价数据不能替代资金净买卖额或席位数据，"
            "只能作为盘面辅助观察，禁止据此断言主力净流入、建仓、派发或洗盘结束：\n\n"
            f"{pool.get('price_volume_recent', '最近量价数据不可用')}\n\n"
            f"关键指标：\n{indicator_lines}\n\n"
            f"{pool.get('vpa_indicators', 'VPA 数据不足')}"
        )

    def _build_core_data_missing_report(ticker, current_date, fund_flow, lhb, feedback_context) -> str:
        return (
            f"### {ticker} 主力资金行为分析报告（{current_date}）\n\n"
            "#### 一、数据基础与缺失说明\n"
            "- **近5日主力资金净流向**：未取得可用净买卖额数据，无法判断主力资金净流入或净流出。\n"
            "- **龙虎榜/席位明细**：未取得可用机构席位明细；若为非异动日无披露，属于正常市场状态，但不能提供席位验证。\n"
            "- **降级规则**：VPA、OBV、换手率、VWMA 只能描述盘面量价变化，不能替代资金净买卖额或席位数据，"
            "不得据此推断主力建仓、派发、洗盘结束或资金回流。\n\n"
            "#### 二、原始接口返回\n"
            f"**资金流向接口**\n{fund_flow}\n\n"
            f"**龙虎榜接口**\n{lhb}\n\n"
            "#### 三、辅助盘面观察\n"
            f"{feedback_context or '辅助量价数据不可用。'}\n\n"
            "#### 四、结论\n"
            "核心资金数据不足，当前无法形成可信的主力资金方向判断。建议等待东方财富资金流向恢复、龙虎榜出现有效席位明细，"
            "或后续报告取得明确资金净买卖额后再评估主力意图。\n\n"
            '<!-- VERDICT: {"direction": "中性", "reason": "核心资金数据缺失，无法判断主力"} -->'
        )

    async def smart_money_analyst_node(state):
        current_date = state["trade_date"]
        ticker = state["company_of_interest"]
        print(f"[Smart Money Analyst] START {ticker} {current_date}")
        horizon = "short"  # 资金面固定短期视角
        user_intent = state.get("user_intent") or {}
        focus_areas = user_intent.get("focus_areas", [])
        specific_questions = user_intent.get("specific_questions", [])

        config = get_config()
        system_message = get_prompt("smart_money_system_message", config=config) or ""
        horizon_ctx = build_horizon_context(horizon, focus_areas, specific_questions, agent_type="smart_money")

        pool = data_collector.get(ticker, current_date) if data_collector else None

        if pool is not None:
            fund_flow = pool.get("fund_flow_individual", "无数据")
            lhb = pool.get("lhb", "无数据")
            volume = pool.get("indicators", {}).get("vwma", "无数据")
            feedback_context = _build_feedback_context(pool, fund_flow, lhb)
        else:
            from tradingagents.agents.utils.agent_utils import (
                get_individual_fund_flow, get_lhb_detail, get_indicators,
            )
            
            # Parallelize fallback fetches
            results = await asyncio.gather(
                _safe(get_individual_fund_flow, {"symbol": ticker}),
                _safe(get_lhb_detail, {"symbol": ticker, "date": current_date}),
                _safe(get_indicators, {
                    "symbol": ticker, "indicator": "volume",
                    "curr_date": current_date, "look_back_days": 20,
                })
            )
            fund_flow, lhb, volume = results
            feedback_context = ""

        has_fund_flow = _has_fund_flow_detail(fund_flow)
        has_lhb = _has_lhb_detail(lhb)
        if not has_fund_flow and not has_lhb:
            full_content = _build_core_data_missing_report(ticker, current_date, fund_flow, lhb, feedback_context)
            tracker = current_tracker_var.get()
            if tracker:
                tracker._emit_token("Smart Money Analyst", "smart_money_report", full_content)
            print(f"[Smart Money Analyst] DEGRADED {ticker}, report length={len(full_content)}")
            return {
                "smart_money_report": full_content,
                "analyst_traces": [{
                    "agent": "smart_money_analyst",
                    "horizon": horizon,
                    "data_window": "近期可用",
                    "key_finding": "核心资金数据缺失，仅输出数据不足结论",
                    "verdict": "中性",
                    "confidence": "低",
                }],
            }

        messages = [
            SystemMessage(content=(
                system_message
                + "\n\n请严格基于提供的量化数据输出分析，全程使用中文。"
                + "\n若主力净流向不可用且龙虎榜无明细，必须判定为数据不足/中性；"
                + "不得用 VPA、OBV、换手率或 VWMA 反推主力建仓、派发、洗盘结束或资金回流。"
            )),
            HumanMessage(content=(
                horizon_ctx + "\n"
                f"请分析 {ticker} 在 {current_date} 的主力资金行为。\n\n"
                f"【近5日主力资金净流向】\n{fund_flow}\n\n"
                f"【龙虎榜数据】\n{lhb}\n\n"
                f"【成交量指标(vwma)】\n{volume}\n\n"
                f"{feedback_context}"
            )),
        ]

        # ── 实现 Token 级流式输出 ──────────────────
        tracker = current_tracker_var.get()
        full_content = ""
        async for chunk in llm.astream(messages):
            content = chunk.content if hasattr(chunk, "content") else str(chunk)
            full_content += content
            if tracker:
                tracker._emit_token("Smart Money Analyst", "smart_money_report", content)

        print(f"[Smart Money Analyst] DONE {ticker}, report length={len(full_content)}")
        verdict, confidence = extract_verdict(full_content)
        return {
            "smart_money_report": full_content,
            "analyst_traces": [{
                "agent": "smart_money_analyst",
                "horizon": horizon,
                "data_window": "近期可用",
                "key_finding": f"主力资金分析结论：{verdict}",
                "verdict": verdict,
                "confidence": confidence,
            }],
        }

    return smart_money_analyst_node
